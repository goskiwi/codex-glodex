# ruff: noqa: RUF001

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pytest

from glodex.domain.assembly import (
    ReasonProjection,
    _new_verified_claims,
    build_verified_claims,
    render_reason,
)
from glodex.domain.catalog import (
    CatalogBatch,
    CostComponents,
    ProductAttribute,
    aggregate_catalog_batch,
)
from glodex.domain.eligibility import (
    EligibilityContext,
    OfferPricingCandidate,
    assemble_eligibility,
    run_offer_gates,
    run_product_gates,
)
from glodex.domain.evidence import (
    EvidenceEntityType,
    EvidenceRef,
    FieldEvidence,
    VerifiedClaim,
    VerifiedClaimType,
)
from glodex.domain.intent import (
    BudgetMax,
    InterpretedRequest,
    PreferredCriterion,
    SourceSpan,
    TargetCategory,
)
from glodex.domain.pricing import KnownCost, LandedCost, calculate_landed_cost
from tests.builders import build_catalog_batch, build_offer

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec("GLO-P0-008", "GLO-P0-009", "AC-007"),
]


def _preferred(text: str, *, canonical_value: str = "must-not-be-read") -> PreferredCriterion:
    return PreferredCriterion(
        value=canonical_value,
        source_span=SourceSpan(start=0, end=len(text), text=text),
    )


def _bundle(
    *preferred: PreferredCriterion,
    budget: bool = True,
    second_offer: bool = False,
    attribute: tuple[str, str] | None = None,
):
    batch = _catalog_batch(second_offer=second_offer, attribute=attribute)
    aggregation = aggregate_catalog_batch(batch)
    assert batch.exchange_rates is not None
    required = (
        TargetCategory(
            category="laptop",
            source_span=SourceSpan(start=0, end=3, text="笔记本"),
        ),
        *(
            (
                BudgetMax(
                    amount=Decimal("800"),
                    currency="USD",
                    source_span=SourceSpan(start=4, end=12, text="预算800元"),
                ),
            )
            if budget
            else ()
        ),
    )
    interpreted = InterpretedRequest(
        required=required,
        preferred=preferred,
        parser_version="fixture-v1",
    )
    context = EligibilityContext.from_interpreted_request(interpreted)
    product_output = run_product_gates(
        aggregation.products,
        context,
        batch.evidence,
    )
    pricing = tuple(
        OfferPricingCandidate(
            offer=offer,
            pricing=calculate_landed_cost(
                offer.cost_components,
                batch.exchange_rates,
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
        batch.evidence,
        display_currency="USD",
    )
    candidate = assemble_eligibility(product_output, offer_output).candidates[0]
    bundle = build_verified_claims(
        candidate,
        interpreted,
        batch.evidence,
        batch.exchange_rates,
    )
    return bundle, interpreted


def _catalog_batch(
    *,
    second_offer: bool,
    attribute: tuple[str, str] | None,
) -> CatalogBatch:
    batch = build_catalog_batch()
    if attribute is not None:
        name, value = attribute
        evidence_id = f"ev-product-{name}"
        original_attribute_evidence = next(
            item for item in batch.evidence if item.evidence_id == "ev-product-weight"
        )
        batch = CatalogBatch(
            snapshot_version=batch.snapshot_version,
            products=(
                replace(
                    batch.products[0],
                    attributes=(
                        ProductAttribute(
                            name=name,
                            value=value,
                            evidence_id=evidence_id,
                        ),
                    ),
                ),
            ),
            offers=batch.offers,
            evidence=(
                *tuple(item for item in batch.evidence if item.evidence_id != "ev-product-weight"),
                replace(
                    original_attribute_evidence,
                    evidence_id=evidence_id,
                    field_path=f"product.attributes.{name}",
                ),
            ),
            exchange_rates=batch.exchange_rates,
        )
    if not second_offer:
        return batch
    first = batch.offers[0]
    prefix = "ev-provider-b-offer-2"
    costs = CostComponents(
        currency="USD",
        item_price=KnownCost(
            amount=Decimal("699.00"),
            evidence_id=f"{prefix}-item_price",
        ),
        shipping=KnownCost(
            amount=Decimal("0.00"),
            evidence_id=f"{prefix}-shipping",
        ),
        tax=KnownCost(
            amount=Decimal("55.92"),
            evidence_id=f"{prefix}-tax",
        ),
        duty=KnownCost(
            amount=Decimal("0.00"),
            evidence_id=f"{prefix}-duty",
        ),
    )
    second = build_offer(
        offer_id="offer-2",
        provider_id="provider-b",
        source_uri="fixture://provider-b/offers/offer-2",
        cost_components=costs,
        captured_at=first.captured_at,
        snapshot_ordinal=1,
        field_evidence=(
            FieldEvidence(
                field_path="offer.inventory",
                evidence_id=f"{prefix}-inventory",
            ),
            FieldEvidence(
                field_path="offer.market",
                evidence_id=f"{prefix}-market",
            ),
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
    second_evidence = tuple(
        EvidenceRef(
            evidence_id=binding.evidence_id,
            snapshot_version=second.snapshot_version,
            entity_type=EvidenceEntityType.OFFER,
            product_id=second.product_id,
            offer_id=second.offer_id,
            currency=None,
            field_path=binding.field_path,
            provider_id=second.provider_id,
            source_uri=second.source_uri,
            captured_at=second.captured_at,
        )
        for binding in second.field_evidence
    )
    return CatalogBatch(
        snapshot_version=batch.snapshot_version,
        products=batch.products,
        offers=(*batch.offers, second),
        evidence=(*batch.evidence, *second_evidence),
        exchange_rates=batch.exchange_rates,
    )


def _claim(
    bundle,
    claim_type: VerifiedClaimType,
) -> VerifiedClaim:
    return next(claim for claim in bundle.claims if claim.claim_type is claim_type)


def test_reason_uses_fixed_budget_inventory_preference_order_and_used_evidence() -> None:
    bundle, interpreted = _bundle(
        _preferred("weight", canonical_value="unrelated"),
    )
    budget = _claim(bundle, VerifiedClaimType.WITHIN_BUDGET)
    inventory = _claim(bundle, VerifiedClaimType.INVENTORY)
    attribute = _claim(bundle, VerifiedClaimType.ATTRIBUTE)

    projection = render_reason(bundle, interpreted)

    assert projection.reason == (
        f"满足预算：到手价 {budget.value}；有库存：{inventory.value}；匹配偏好：{attribute.value}"
    )
    assert projection.unknowns == ()
    assert projection.evidence_ids == tuple(
        dict.fromkeys(
            (
                *budget.evidence_ids,
                *inventory.evidence_ids,
                *attribute.evidence_ids,
            )
        )
    )
    assert "ev-product-category" not in projection.evidence_ids


def test_no_budget_renders_inventory_first_and_excludes_unused_price_evidence() -> None:
    bundle, interpreted = _bundle(
        _preferred("weight"),
        budget=False,
    )
    inventory = _claim(bundle, VerifiedClaimType.INVENTORY)
    attribute = _claim(bundle, VerifiedClaimType.ATTRIBUTE)
    landed = _claim(bundle, VerifiedClaimType.LANDED_COST)

    projection = render_reason(bundle, interpreted)

    assert projection.reason == (f"有库存：{inventory.value}；匹配偏好：{attribute.value}")
    assert projection.evidence_ids == (
        *inventory.evidence_ids,
        *attribute.evidence_ids,
    )
    assert not set(landed.evidence_ids).issubset(projection.evidence_ids)


def test_preference_matching_reads_only_source_span_text_and_attribute_claims() -> None:
    bundle, interpreted = _bundle(
        _preferred("durable", canonical_value="weight"),
        _preferred("TravelBook", canonical_value="weight"),
        _preferred("provider-a", canonical_value="weight"),
    )

    projection = render_reason(bundle, interpreted)

    assert "匹配偏好" not in projection.reason
    assert projection.unknowns == (
        "未证实偏好：durable",
        "未证实偏好：TravelBook",
        "未证实偏好：provider-a",
    )
    assert projection.reason.startswith("满足预算：")
    assert projection.reason.endswith("有库存：provider-a/US")


def test_two_preferences_matching_one_claim_render_it_once() -> None:
    bundle, interpreted = _bundle(
        _preferred("weight"),
        _preferred("1.2kg"),
    )
    attribute = _claim(bundle, VerifiedClaimType.ATTRIBUTE)

    projection = render_reason(bundle, interpreted)

    assert projection.reason.count(f"匹配偏好：{attribute.value}") == 1
    assert projection.unknowns == ()
    assert projection.evidence_ids.count(attribute.evidence_ids[0]) == 1


def test_unverified_preferences_are_stable_and_never_become_positive_copy() -> None:
    bundle, interpreted = _bundle(
        _preferred("耐用"),
        _preferred("高性能"),
        budget=False,
    )

    projection = render_reason(bundle, interpreted)

    assert projection.reason == "有库存：provider-a/US"
    assert projection.unknowns == (
        "未证实偏好：耐用",
        "未证实偏好：高性能",
    )
    assert "耐用" not in projection.reason
    assert "高性能" not in projection.reason


@pytest.mark.parametrize("value", ["false", "unknown", "Ｎ／Ａ"])
def test_negative_or_unknown_attribute_values_cannot_prove_a_preference(
    value: str,
) -> None:
    bundle, interpreted = _bundle(
        _preferred("travel"),
        budget=False,
        attribute=("travel_ready", value),
    )

    projection = render_reason(bundle, interpreted)

    assert projection.reason == "有库存：provider-a/US"
    assert projection.unknowns == ("未证实偏好：travel",)


def test_affirmative_attribute_value_can_prove_a_source_span_match() -> None:
    bundle, interpreted = _bundle(
        _preferred("travel"),
        budget=False,
        attribute=("travel_ready", "true"),
    )

    projection = render_reason(bundle, interpreted)

    assert projection.reason.endswith("匹配偏好：travel_ready=true")
    assert projection.unknowns == ()


def test_claim_input_order_cannot_change_rendered_projection() -> None:
    bundle, interpreted = _bundle(_preferred("weight"))
    reordered = _new_verified_claims(
        bundle.candidate,
        tuple(reversed(bundle.claims)),
        bundle.evidence,
        bundle.exchange_rates,
    )

    assert render_reason(reordered, interpreted) == render_reason(bundle, interpreted)


def test_removing_an_attribute_claim_turns_its_preference_into_an_unknown() -> None:
    bundle, interpreted = _bundle(_preferred("weight"), budget=False)
    without_attribute = _new_verified_claims(
        bundle.candidate,
        tuple(
            claim for claim in bundle.claims if claim.claim_type is not VerifiedClaimType.ATTRIBUTE
        ),
        bundle.evidence,
        bundle.exchange_rates,
    )

    projection = render_reason(without_attribute, interpreted)

    assert projection.reason == "有库存：provider-a/US"
    assert projection.unknowns == ("未证实偏好：weight",)
    assert "ev-product-weight" not in projection.evidence_ids


def test_forged_attribute_or_budget_claim_cannot_be_rendered() -> None:
    bundle, interpreted = _bundle(_preferred("travel"))
    original_attribute = _claim(bundle, VerifiedClaimType.ATTRIBUTE)
    original_budget = _claim(bundle, VerifiedClaimType.WITHIN_BUDGET)
    forged_attribute = replace(
        original_attribute,
        value="travel_ready=true",
    )
    forged_budget = replace(
        original_budget,
        value="0 USD <= 800 USD",
        evidence_ids=("ev-product-category",),
    )

    for original, forged, message in (
        (original_attribute, forged_attribute, "attribute claim"),
        (original_budget, forged_budget, "budget claim"),
    ):
        forged_bundle = _new_verified_claims(
            bundle.candidate,
            tuple(forged if claim is original else claim for claim in bundle.claims),
            bundle.evidence,
            bundle.exchange_rates,
        )
        with pytest.raises(ValueError, match=message):
            render_reason(forged_bundle, interpreted)


def test_non_selected_offer_claims_are_ignored() -> None:
    bundle, interpreted = _bundle(_preferred("weight"), second_offer=True)

    projection = render_reason(bundle, interpreted)

    assert "provider-b" not in projection.reason
    assert not any(
        evidence_id.startswith("ev-provider-b-offer-2") for evidence_id in projection.evidence_ids
    )


def test_budget_request_without_selected_budget_claim_fails_closed() -> None:
    bundle, interpreted = _bundle()
    without_budget = _new_verified_claims(
        bundle.candidate,
        tuple(
            claim
            for claim in bundle.claims
            if claim.claim_type is not VerifiedClaimType.WITHIN_BUDGET
        ),
        bundle.evidence,
        bundle.exchange_rates,
    )

    with pytest.raises(ValueError, match="budget claim"):
        render_reason(without_budget, interpreted)


def test_missing_or_ambiguous_selected_inventory_claim_fails_closed() -> None:
    bundle, interpreted = _bundle()
    without_inventory = _new_verified_claims(
        bundle.candidate,
        tuple(
            claim for claim in bundle.claims if claim.claim_type is not VerifiedClaimType.INVENTORY
        ),
        bundle.evidence,
        bundle.exchange_rates,
    )
    inventory = _claim(bundle, VerifiedClaimType.INVENTORY)
    duplicate_inventory = _new_verified_claims(
        bundle.candidate,
        (*bundle.claims, inventory),
        bundle.evidence,
        bundle.exchange_rates,
    )

    with pytest.raises(ValueError, match="inventory claim"):
        render_reason(without_inventory, interpreted)
    with pytest.raises(ValueError, match="inventory claim"):
        render_reason(duplicate_inventory, interpreted)


def test_projection_is_frozen_and_rejects_mutable_or_duplicate_values() -> None:
    bundle, interpreted = _bundle()
    projection = render_reason(bundle, interpreted)

    with pytest.raises(FrozenInstanceError):
        projection.reason = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        ReasonProjection(  # type: ignore[arg-type]
            reason="有库存：provider-a/US",
            unknowns=["耐用"],
            evidence_ids=("ev-1",),
        )
    with pytest.raises(ValueError, match="unique"):
        ReasonProjection(
            reason="有库存：provider-a/US",
            unknowns=(),
            evidence_ids=("ev-1", "ev-1"),
        )
