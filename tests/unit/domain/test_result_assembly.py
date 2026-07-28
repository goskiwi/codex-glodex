# ruff: noqa: RUF001

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from glodex.domain.assembly import (
    ResultDraft,
    assemble_result_drafts,
    require_verified_claims,
    result_evidence_closure,
)
from glodex.domain.catalog import (
    CanonicalProduct,
    CostComponents,
    ExchangeRateTable,
    Offer,
    Product,
    ProductAttribute,
    aggregate_catalog,
)
from glodex.domain.eligibility import (
    EligibilityContext,
    EligibilityOutput,
    EligibleProduct,
    OfferPricingCandidate,
    assemble_eligibility,
    run_offer_gates,
    run_product_gates,
)
from glodex.domain.evidence import EvidenceEntityType, EvidenceRef, FieldEvidence
from glodex.domain.intent import (
    BudgetMax,
    InterpretedRequest,
    PreferredCriterion,
    SourceSpan,
    TargetCategory,
)
from glodex.domain.pricing import KnownCost, LandedCost, calculate_landed_cost
from tests.builders import (
    SNAPSHOT_VERSION,
    build_evidence_ref,
    build_exchange_rates,
    build_offer,
    build_product,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec("GLO-P0-009", "GLO-NFR-001", "GLO-NFR-002", "GLO-NFR-003"),
]


@dataclass(frozen=True, slots=True)
class _Pipeline:
    query: str
    interpreted: InterpretedRequest
    evidence: tuple[EvidenceRef, ...]
    rates: ExchangeRateTable
    eligibility: EligibilityOutput[EligibleProduct]
    ranked: tuple[EligibleProduct, ...]
    display_currency: str = "USD"
    snapshot_version: str = SNAPSHOT_VERSION


def _source_span(query: str, text: str) -> SourceSpan:
    start = query.index(text)
    return SourceSpan(start=start, end=start + len(text), text=text)


def _raw_product(index: int) -> Product:
    product_id = f"product-{index}"
    provider_id = f"provider-{index}"
    prefix = f"ev-{product_id}"
    return build_product(
        product_id=product_id,
        provider_id=provider_id,
        source_uri=f"fixture://{provider_id}/products/{product_id}",
        title=f"TravelBook {index}",
        snapshot_ordinal=index,
        attributes=(
            ProductAttribute(
                name="portable",
                value="便携",
                evidence_id=f"{prefix}-portable",
            ),
        ),
        field_evidence=(
            FieldEvidence(field_path="product.title", evidence_id=f"{prefix}-title"),
            FieldEvidence(field_path="product.category", evidence_id=f"{prefix}-category"),
            FieldEvidence(field_path="product.entity_kind", evidence_id=f"{prefix}-kind"),
        ),
    )


def _raw_offer(index: int) -> Offer:
    product_id = f"product-{index}"
    provider_id = f"provider-{index}"
    offer_id = f"offer-{index}"
    prefix = f"ev-{offer_id}"
    costs = CostComponents(
        currency="USD",
        item_price=KnownCost(
            amount=Decimal(500 + index),
            evidence_id=f"{prefix}-item_price",
        ),
        shipping=KnownCost(amount=Decimal("10"), evidence_id=f"{prefix}-shipping"),
        tax=KnownCost(amount=Decimal("5"), evidence_id=f"{prefix}-tax"),
        duty=KnownCost(amount=Decimal("0.00"), evidence_id=f"{prefix}-duty"),
    )
    return build_offer(
        offer_id=offer_id,
        product_id=product_id,
        provider_id=provider_id,
        source_uri=f"fixture://{provider_id}/offers/{offer_id}",
        market="US",
        cost_components=costs,
        snapshot_ordinal=index,
        field_evidence=(
            FieldEvidence(field_path="offer.inventory", evidence_id=f"{prefix}-inventory"),
            FieldEvidence(field_path="offer.market", evidence_id=f"{prefix}-market"),
            FieldEvidence(
                field_path="offer.cost_components.currency",
                evidence_id=f"{prefix}-currency",
            ),
            *tuple(
                FieldEvidence(
                    field_path=f"offer.cost_components.{name}",
                    evidence_id=getattr(costs, name).evidence_id,
                )
                for name in ("item_price", "shipping", "tax", "duty")
            ),
        ),
    )


def _product_evidence(product: Product) -> tuple[EvidenceRef, ...]:
    return (
        *tuple(
            build_evidence_ref(
                evidence_id=binding.evidence_id,
                product_id=product.product_id,
                field_path=binding.field_path,
                provider_id=product.provider_id,
                source_uri=product.source_uri,
            )
            for binding in product.field_evidence
        ),
        *tuple(
            build_evidence_ref(
                evidence_id=attribute.evidence_id,
                product_id=product.product_id,
                field_path=f"product.attributes.{attribute.name}",
                provider_id=product.provider_id,
                source_uri=product.source_uri,
            )
            for attribute in product.attributes
        ),
    )


def _offer_evidence(offer: Offer) -> tuple[EvidenceRef, ...]:
    return tuple(
        build_evidence_ref(
            evidence_id=binding.evidence_id,
            entity_type=EvidenceEntityType.OFFER,
            product_id=offer.product_id,
            offer_id=offer.offer_id,
            field_path=binding.field_path,
            provider_id=offer.provider_id,
            source_uri=offer.source_uri,
        )
        for binding in offer.field_evidence
    )


def _pipeline(*, count: int = 2, budget: bool = True) -> _Pipeline:
    products = tuple(_raw_product(index) for index in range(1, count + 1))
    offers = tuple(_raw_offer(index) for index in range(1, count + 1))
    aggregation = aggregate_catalog(products, offers)
    assert all(type(product) is CanonicalProduct for product in aggregation.products)
    rates = build_exchange_rates()
    rate_evidence = build_evidence_ref(
        evidence_id="ev-rate-usd",
        entity_type=EvidenceEntityType.EXCHANGE_RATE,
        product_id=None,
        currency="USD",
        field_path="exchange_rate.base_per_unit",
        provider_id="fixture-fx",
        source_uri="fixture://fx/USD",
    )
    evidence = (
        *(item for product in products for item in _product_evidence(product)),
        *(item for offer in offers for item in _offer_evidence(offer)),
        rate_evidence,
    )

    query = "笔记本，预算800美元，便携" if budget else "笔记本，便携"
    required = (
        TargetCategory(
            category="laptop",
            source_span=_source_span(query, "笔记本"),
        ),
        *(
            (
                BudgetMax(
                    amount=Decimal("800"),
                    currency="USD",
                    source_span=_source_span(query, "预算800美元"),
                ),
            )
            if budget
            else ()
        ),
    )
    interpreted = InterpretedRequest(
        required=required,
        preferred=(
            PreferredCriterion(
                value="portable",
                source_span=_source_span(query, "便携"),
            ),
        ),
        parser_version="fixture-v1",
    )
    context = EligibilityContext.from_interpreted_request(interpreted)
    product_output = run_product_gates(aggregation.products, context, evidence)
    pricing = tuple(
        OfferPricingCandidate(
            offer=offer,
            pricing=calculate_landed_cost(
                offer.cost_components,
                rates,
                display_currency="USD",
                budget_max=Decimal("800") if budget else None,
                budget_currency="USD" if budget else None,
            ),
        )
        for offer in aggregation.offers
    )
    assert all(type(item.pricing) is LandedCost for item in pricing)
    offer_output = run_offer_gates(
        product_output,
        aggregation.offers,
        pricing,
        evidence,
        display_currency="USD",
    )
    eligibility = assemble_eligibility(product_output, offer_output)
    ranked = tuple(reversed(eligibility.candidates))
    return _Pipeline(
        query=query,
        interpreted=interpreted,
        evidence=evidence,
        rates=rates,
        eligibility=eligibility,
        ranked=ranked,
    )


@pytest.mark.parametrize("top_k, expected_count", [(1, 1), (2, 2), (3, 2)])
def test_assembly_takes_the_exact_ranked_prefix(
    top_k: int,
    expected_count: int,
) -> None:
    pipeline = _pipeline()

    drafts = assemble_result_drafts(
        pipeline.ranked,
        pipeline.query,
        pipeline.interpreted,
        pipeline.evidence,
        pipeline.rates,
        top_k=top_k,
    )

    assert len(drafts) == expected_count
    assert all(type(draft) is ResultDraft for draft in drafts)
    assert all(draft.candidate is pipeline.ranked[index] for index, draft in enumerate(drafts))
    first = drafts[0]
    assert require_verified_claims(first.verified_claims) is first.verified_claims
    assert first.matched_requirements == ("target_category", "budget_max")
    assert first.projection.unknowns == ()
    assert "满足预算：" in first.projection.reason
    assert "有库存：" in first.projection.reason
    assert "匹配偏好：portable=便携" in first.projection.reason
    assert set(first.projection.evidence_ids).issubset(first.evidence_ids)
    assert f"ev-{first.candidate.product.product_id}-title" in first.evidence_ids
    assert f"ev-{first.candidate.product.product_id}-kind" in first.evidence_ids


@pytest.mark.parametrize("top_k", [True, 0, 4])
def test_assembly_rejects_invalid_top_k(top_k: object) -> None:
    pipeline = _pipeline()

    with pytest.raises(ValueError, match="top_k"):
        assemble_result_drafts(
            pipeline.ranked,
            pipeline.query,
            pipeline.interpreted,
            pipeline.evidence,
            pipeline.rates,
            top_k=top_k,  # type: ignore[arg-type]
        )


def test_assembly_rejects_duplicate_products_and_invalid_source_spans() -> None:
    pipeline = _pipeline()

    with pytest.raises(ValueError, match="unique product"):
        assemble_result_drafts(
            (pipeline.ranked[0], pipeline.ranked[0]),
            pipeline.query,
            pipeline.interpreted,
            pipeline.evidence,
            pipeline.rates,
            top_k=2,
        )

    preferred = pipeline.interpreted.preferred[0]
    object.__setattr__(
        preferred,
        "source_span",
        SourceSpan(start=0, end=2, text="便携"),
    )
    with pytest.raises(ValueError, match="source-span"):
        assemble_result_drafts(
            pipeline.ranked,
            pipeline.query,
            pipeline.interpreted,
            pipeline.evidence,
            pipeline.rates,
            top_k=2,
        )


def test_no_budget_still_assembles_landed_cost_evidence_without_budget_copy() -> None:
    pipeline = _pipeline(budget=False)

    draft = assemble_result_drafts(
        pipeline.ranked,
        pipeline.query,
        pipeline.interpreted,
        pipeline.evidence,
        pipeline.rates,
        top_k=1,
    )[0]

    assert draft.matched_requirements == ("target_category",)
    assert "满足预算：" not in draft.projection.reason
    assert "ev-rate-usd" in draft.evidence_ids
    assert any(evidence_id.endswith("-item_price") for evidence_id in draft.evidence_ids)


def test_result_evidence_closure_is_complete_ordered_and_excludes_unrelated_rows() -> None:
    pipeline = _pipeline(count=3)
    unrelated = tuple(
        build_evidence_ref(
            evidence_id=f"ev-unrelated-{index}",
            product_id=f"unrelated-{index}",
            field_path="product.title",
            provider_id="unrelated-provider",
            source_uri=f"fixture://unrelated/{index}",
        )
        for index in range(100)
    )
    full_evidence = (*unrelated[:50], *pipeline.evidence, *unrelated[50:])

    closure = result_evidence_closure(
        pipeline.ranked,
        full_evidence,
        pipeline.rates,
    )

    required_ids = {
        *(
            binding.evidence_id
            for candidate in pipeline.ranked
            for binding in candidate.product.field_evidence
        ),
        *(
            evidence_id
            for candidate in pipeline.ranked
            for attribute in candidate.product.attributes
            for evidence_id in attribute.evidence_ids
        ),
        *(
            binding.evidence_id
            for candidate in pipeline.ranked
            for eligible in candidate.eligible_offers
            for binding in eligible.offer.field_evidence
        ),
        *(rate.evidence_id for rate in pipeline.rates.rates),
    }
    assert tuple(item.evidence_id for item in closure) == tuple(
        item.evidence_id for item in full_evidence if item.evidence_id in required_ids
    )
    assert all(not item.evidence_id.startswith("ev-unrelated-") for item in closure)

    drafts = assemble_result_drafts(
        pipeline.ranked,
        pipeline.query,
        pipeline.interpreted,
        closure,
        pipeline.rates,
        top_k=1,
    )
    assert drafts[0].verified_claims.evidence is closure


@pytest.mark.parametrize("failure", ["missing", "duplicate", "snapshot"])
def test_result_evidence_closure_fails_closed_on_invalid_full_evidence(
    failure: str,
) -> None:
    pipeline = _pipeline()
    if failure == "missing":
        evidence = pipeline.evidence[1:]
        match = "incomplete"
    elif failure == "duplicate":
        evidence = (*pipeline.evidence, pipeline.evidence[0])
        match = "duplicate"
    else:
        wrong_snapshot = build_evidence_ref(
            evidence_id="ev-unrelated-wrong-snapshot",
            snapshot_version="other-v1",
            product_id="unrelated",
            field_path="product.title",
        )
        evidence = (*pipeline.evidence, wrong_snapshot)
        match = "snapshot"

    with pytest.raises(ValueError, match=match):
        result_evidence_closure(
            pipeline.ranked,
            evidence,
            pipeline.rates,
        )


def test_result_evidence_closure_rejects_an_empty_ranked_batch() -> None:
    pipeline = _pipeline()

    with pytest.raises(ValueError, match="requires ranked candidates"):
        result_evidence_closure(
            (),
            pipeline.evidence,
            pipeline.rates,
        )
