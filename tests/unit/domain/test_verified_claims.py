from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from glodex.domain.assembly import (
    VerifiedClaims,
    build_verified_claims,
    require_verified_claims,
)
from glodex.domain.catalog import (
    CanonicalProduct,
    CostComponents,
    ExchangeRate,
    ExchangeRateTable,
    Offer,
    Product,
    ProductSource,
    StockStatus,
    aggregate_catalog,
)
from glodex.domain.eligibility import (
    EligibilityContext,
    EligibleProduct,
    OfferPricingCandidate,
    assemble_eligibility,
    run_offer_gates,
    run_product_gates,
)
from glodex.domain.evidence import (
    EvidenceEntityType,
    EvidenceRef,
    FieldEvidence,
    VerifiedClaimType,
)
from glodex.domain.intent import (
    BudgetMax,
    InterpretedRequest,
    SourceSpan,
    TargetCategory,
)
from glodex.domain.pricing import (
    PRICING_ALGORITHM_VERSION,
    KnownCost,
    LandedCost,
    UnknownCost,
    calculate_landed_cost,
    canonical_exact_amount,
)
from tests.builders import build_product

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec(
        "GLO-P0-008",
        "GLO-P0-009",
        "GLO-NFR-002",
        "AC-007",
        "AC-008",
    ),
]

_SNAPSHOT = "m0-v1"
_CAPTURED_AT = datetime(2026, 1, 1, tzinfo=UTC)
_COST_NAMES = ("item_price", "shipping", "tax", "duty")
_RATE_VALUES = {
    "USD": (Decimal("1"), 2),
    "EUR": (Decimal("1.2"), 2),
    "GBP": (Decimal("1.5"), 2),
    "CNY": (Decimal("0.14"), 2),
}


@dataclass(frozen=True, slots=True)
class _Fixture:
    candidate: EligibleProduct
    interpreted: InterpretedRequest
    evidence: tuple[EvidenceRef, ...]
    rates: ExchangeRateTable


def _costs(prefix: str, *, item_price: str) -> CostComponents:
    return CostComponents(
        currency="EUR",
        item_price=KnownCost(
            amount=Decimal(item_price),
            evidence_id=f"ev-{prefix}-item_price",
        ),
        shipping=KnownCost(
            amount=Decimal("10"),
            evidence_id=f"ev-{prefix}-shipping",
        ),
        tax=KnownCost(
            amount=Decimal("5"),
            evidence_id=f"ev-{prefix}-tax",
        ),
        duty=KnownCost(
            amount=Decimal("0.00"),
            evidence_id=f"ev-{prefix}-duty",
        ),
    )


def _offer(
    *,
    provider_id: str = "provider-a",
    offer_id: str = "offer-1",
    item_price: str = "100",
    ordinal: int = 0,
) -> Offer:
    prefix = f"{provider_id}-{offer_id}"
    costs = _costs(prefix, item_price=item_price)
    return Offer(
        snapshot_version=_SNAPSHOT,
        offer_id=offer_id,
        product_id="product-1",
        provider_id=provider_id,
        source_uri=f"fixture://{provider_id}/offers/{offer_id}",
        market="US",
        stock_status=StockStatus.IN_STOCK,
        cost_components=costs,
        captured_at=_CAPTURED_AT,
        snapshot_ordinal=ordinal,
        field_evidence=(
            FieldEvidence(
                field_path="offer.inventory",
                evidence_id=f"ev-{prefix}-inventory",
            ),
            FieldEvidence(
                field_path="offer.market",
                evidence_id=f"ev-{prefix}-market",
            ),
            FieldEvidence(
                field_path="offer.cost_components.currency",
                evidence_id=f"ev-{prefix}-currency",
            ),
            *tuple(
                FieldEvidence(
                    field_path=f"offer.cost_components.{name}",
                    evidence_id=getattr(costs, name).evidence_id,
                )
                for name in _COST_NAMES
            ),
        ),
    )


def _rates() -> ExchangeRateTable:
    return ExchangeRateTable(
        snapshot_version=_SNAPSHOT,
        base_currency="USD",
        rates=tuple(
            ExchangeRate(
                snapshot_version=_SNAPSHOT,
                currency=currency,
                base_per_unit=values[0],
                minor_units=values[1],
                evidence_id=f"ev-rate-{currency.casefold()}",
                snapshot_ordinal=ordinal,
            )
            for ordinal, (currency, values) in enumerate(_RATE_VALUES.items())
        ),
    )


def _product_evidence(product: Product) -> tuple[EvidenceRef, ...]:
    field_refs = tuple(
        EvidenceRef(
            evidence_id=binding.evidence_id,
            snapshot_version=product.snapshot_version,
            entity_type=EvidenceEntityType.PRODUCT,
            product_id=product.product_id,
            offer_id=None,
            currency=None,
            field_path=binding.field_path,
            provider_id=product.provider_id,
            source_uri=product.source_uri,
            captured_at=_CAPTURED_AT,
        )
        for binding in product.field_evidence
    )
    attribute_refs = tuple(
        EvidenceRef(
            evidence_id=attribute.evidence_id,
            snapshot_version=product.snapshot_version,
            entity_type=EvidenceEntityType.PRODUCT,
            product_id=product.product_id,
            offer_id=None,
            currency=None,
            field_path=f"product.attributes.{attribute.name}",
            provider_id=product.provider_id,
            source_uri=product.source_uri,
            captured_at=_CAPTURED_AT,
        )
        for attribute in product.attributes
    )
    return (*field_refs, *attribute_refs)


def _offer_evidence(offer: Offer) -> tuple[EvidenceRef, ...]:
    return tuple(
        EvidenceRef(
            evidence_id=binding.evidence_id,
            snapshot_version=offer.snapshot_version,
            entity_type=EvidenceEntityType.OFFER,
            product_id=offer.product_id,
            offer_id=offer.offer_id,
            currency=None,
            field_path=binding.field_path,
            provider_id=offer.provider_id,
            source_uri=offer.source_uri,
            captured_at=_CAPTURED_AT,
        )
        for binding in offer.field_evidence
    )


def _rate_evidence(rates: ExchangeRateTable) -> tuple[EvidenceRef, ...]:
    return tuple(
        EvidenceRef(
            evidence_id=rate.evidence_id,
            snapshot_version=rate.snapshot_version,
            entity_type=EvidenceEntityType.EXCHANGE_RATE,
            product_id=None,
            offer_id=None,
            currency=rate.currency,
            field_path="exchange_rate.base_per_unit",
            provider_id="fixture-fx",
            source_uri=f"fixture://fx/{rate.currency}",
            captured_at=_CAPTURED_AT,
        )
        for rate in rates.rates
    )


def _fixture(*, budget: bool = True, second_offer: bool = False) -> _Fixture:
    raw_product = build_product()
    offers = (
        _offer(),
        *(
            (
                _offer(
                    provider_id="provider-b",
                    offer_id="offer-2",
                    item_price="105",
                    ordinal=1,
                ),
            )
            if second_offer
            else ()
        ),
    )
    aggregation = aggregate_catalog((raw_product,), offers)
    canonical = aggregation.products[0]
    assert type(canonical) is CanonicalProduct
    rates = _rates()
    evidence = (
        *_product_evidence(raw_product),
        *(ref for offer in aggregation.offers for ref in _offer_evidence(offer)),
        *_rate_evidence(rates),
    )
    required: tuple[TargetCategory | BudgetMax, ...] = (
        TargetCategory(
            category="laptop",
            source_span=SourceSpan(start=0, end=3, text="笔记本"),
        ),
        *(
            (
                BudgetMax(
                    mode="maximum",
                    target_amount=Decimal("2000"),
                    lower_bound=None,
                    upper_bound=Decimal("2000"),
                    currency="CNY",
                    source_span=SourceSpan(start=4, end=13, text="预算2000元"),
                ),
            )
            if budget
            else ()
        ),
    )
    interpreted = InterpretedRequest(
        required=required,
        preferred=(),
        parser_version="fixture-v1",
    )
    context = EligibilityContext.from_interpreted_request(interpreted)
    product_output = run_product_gates((canonical,), context, evidence)
    pricing = tuple(
        OfferPricingCandidate(
            offer=offer,
            pricing=calculate_landed_cost(
                offer.cost_components,
                rates,
                display_currency="GBP",
                budget_mode="maximum" if budget else None,
                budget_target_amount=Decimal("2000") if budget else None,
                budget_lower_bound=None,
                budget_upper_bound=Decimal("2000") if budget else None,
                budget_currency="CNY" if budget else None,
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
        display_currency="GBP",
    )
    assembled = assemble_eligibility(product_output, offer_output)
    return _Fixture(
        candidate=assembled.candidates[0],
        interpreted=interpreted,
        evidence=evidence,
        rates=rates,
    )


def _build(fixture: _Fixture, *, evidence: tuple[EvidenceRef, ...] | None = None):
    return build_verified_claims(
        fixture.candidate,
        fixture.interpreted,
        fixture.evidence if evidence is None else evidence,
        fixture.rates,
    )


def _replace_evidence(
    evidence: tuple[EvidenceRef, ...],
    evidence_id: str,
    replacement: EvidenceRef | None,
) -> tuple[EvidenceRef, ...]:
    return tuple(
        replacement if ref.evidence_id == evidence_id else ref
        for ref in evidence
        if replacement is not None or ref.evidence_id != evidence_id
    )


def test_builder_mints_sealed_evidence_complete_claims_in_fixed_order() -> None:
    fixture = _fixture()

    bundle = _build(fixture)
    checked = require_verified_claims(bundle)

    assert checked is bundle
    assert checked.candidate is fixture.candidate
    assert tuple(claim.claim_type for claim in checked.claims) == (
        VerifiedClaimType.CATEGORY,
        VerifiedClaimType.ATTRIBUTE,
        VerifiedClaimType.INVENTORY,
        VerifiedClaimType.LANDED_COST,
        VerifiedClaimType.WITHIN_BUDGET,
    )
    category, attribute, inventory, landed, budget = checked.claims
    assert category.value == "laptop"
    assert category.evidence_ids == ("ev-product-category",)
    assert attribute.value == "weight=1.2kg"
    assert attribute.evidence_ids == ("ev-product-weight",)
    assert inventory.value == "US"
    assert inventory.provider_id == "provider-a"
    assert inventory.offer_id == "offer-1"
    assert inventory.evidence_ids == (
        "ev-provider-a-offer-1-inventory",
        "ev-provider-a-offer-1-market",
    )
    assert landed.value == "92 GBP"
    assert landed.algorithm_version == PRICING_ALGORITHM_VERSION
    expected_derived_evidence = (
        "ev-provider-a-offer-1-item_price",
        "ev-provider-a-offer-1-shipping",
        "ev-provider-a-offer-1-tax",
        "ev-provider-a-offer-1-duty",
        "ev-provider-a-offer-1-currency",
        "ev-rate-eur",
        "ev-rate-gbp",
        "ev-rate-cny",
    )
    assert landed.evidence_ids == expected_derived_evidence
    assert budget.value == "到手价 985.71 CNY ≤ 2000 CNY"
    assert budget.evidence_ids == expected_derived_evidence
    assert budget.algorithm_version == PRICING_ALGORITHM_VERSION
    assert category.algorithm_version is None
    assert attribute.algorithm_version is None
    assert inventory.algorithm_version is None

    reordered = build_verified_claims(
        fixture.candidate,
        fixture.interpreted,
        tuple(reversed(fixture.evidence)),
        fixture.rates,
    )
    assert reordered.claims == bundle.claims


def test_no_budget_mints_landed_cost_but_no_budget_claim_and_deduplicates_fx() -> None:
    fixture = _fixture(budget=False)

    bundle = _build(fixture)

    assert tuple(claim.claim_type for claim in bundle.claims) == (
        VerifiedClaimType.CATEGORY,
        VerifiedClaimType.ATTRIBUTE,
        VerifiedClaimType.INVENTORY,
        VerifiedClaimType.LANDED_COST,
    )
    landed = bundle.claims[-1]
    assert landed.evidence_ids[-2:] == ("ev-rate-eur", "ev-rate-gbp")
    assert landed.evidence_ids.count("ev-rate-gbp") == 1


def test_every_eligible_offer_receives_inventory_landed_and_budget_claims() -> None:
    fixture = _fixture(second_offer=True)

    bundle = _build(fixture)

    offer_claims = tuple(
        claim
        for claim in bundle.claims
        if claim.claim_type
        in (
            VerifiedClaimType.INVENTORY,
            VerifiedClaimType.LANDED_COST,
            VerifiedClaimType.WITHIN_BUDGET,
        )
    )
    assert tuple((claim.provider_id, claim.offer_id) for claim in offer_claims) == (
        ("provider-a", "offer-1"),
        ("provider-a", "offer-1"),
        ("provider-a", "offer-1"),
        ("provider-b", "offer-2"),
        ("provider-b", "offer-2"),
        ("provider-b", "offer-2"),
    )


def test_bundle_is_frozen_and_forged_or_tampered_values_are_rejected() -> None:
    fixture = _fixture()
    bundle = _build(fixture)

    with pytest.raises(TypeError, match="internal"):
        VerifiedClaims()  # type: ignore[call-arg]
    with pytest.raises(FrozenInstanceError):
        bundle.claims = ()  # type: ignore[misc]

    forged = object.__new__(VerifiedClaims)
    for name in VerifiedClaims.__slots__:
        if name != "__weakref__":
            object.__setattr__(forged, name, getattr(bundle, name))
    with pytest.raises(TypeError, match="sealed"):
        require_verified_claims(forged)

    object.__setattr__(bundle, "claims", ())
    with pytest.raises(TypeError, match="sealed"):
        require_verified_claims(bundle)


@pytest.mark.parametrize("source_input", ["evidence", "exchange_rate"])
def test_bundle_seals_the_source_evidence_and_rate_table(
    source_input: str,
) -> None:
    fixture = _fixture()
    bundle = _build(fixture)

    if source_input == "evidence":
        object.__setattr__(
            fixture.evidence[0],
            "source_uri",
            "fixture://tampered/source",
        )
    else:
        eur = next(rate for rate in fixture.rates.rates if rate.currency == "EUR")
        object.__setattr__(eur, "base_per_unit", Decimal("1.21"))

    with pytest.raises(TypeError, match="sealed"):
        require_verified_claims(bundle)


@pytest.mark.parametrize("evidence_id", ["ev-product-category", "ev-product-weight"])
def test_missing_product_fact_evidence_fails_closed(evidence_id: str) -> None:
    fixture = _fixture()

    with pytest.raises(ValueError, match="evidence"):
        _build(
            fixture,
            evidence=_replace_evidence(fixture.evidence, evidence_id, None),
        )


@pytest.mark.parametrize(
    "corruption",
    ("snapshot", "product", "provider", "source", "field", "entity"),
)
def test_existing_but_wrong_product_evidence_fails_closed(corruption: str) -> None:
    fixture = _fixture()
    original = next(ref for ref in fixture.evidence if ref.evidence_id == "ev-product-category")
    if corruption == "entity":
        replacement = EvidenceRef(
            evidence_id=original.evidence_id,
            snapshot_version=original.snapshot_version,
            entity_type=EvidenceEntityType.OFFER,
            product_id=original.product_id,
            offer_id="other-offer",
            currency=None,
            field_path="offer.inventory",
            provider_id=original.provider_id,
            source_uri=original.source_uri,
            captured_at=original.captured_at,
        )
    else:
        changes: dict[str, object] = {
            "snapshot": {"snapshot_version": "m0-v2"},
            "product": {"product_id": "other-product"},
            "provider": {"provider_id": "other-provider"},
            "source": {"source_uri": "fixture://other/products/product-1"},
            "field": {"field_path": "product.title"},
        }[corruption]
        replacement = replace(original, **changes)

    with pytest.raises(ValueError, match="evidence"):
        _build(
            fixture,
            evidence=_replace_evidence(
                fixture.evidence,
                original.evidence_id,
                replacement,
            ),
        )


def test_multi_source_product_evidence_must_match_its_original_source_binding() -> None:
    fixture = _fixture()
    product = fixture.candidate.product
    second_provider = "provider-b"
    second_source_uri = "fixture://provider-b/products/product-1"
    second_ids_by_path = {
        "product.title": "ev-provider-b-title",
        "product.category": "ev-provider-b-category",
        "product.entity_kind": "ev-provider-b-kind",
    }
    second_attribute_id = "ev-provider-b-weight"
    second_source = ProductSource(
        provider_id=second_provider,
        source_uri=second_source_uri,
        snapshot_ordinal=1,
        evidence_ids=(
            *second_ids_by_path.values(),
            second_attribute_id,
        ),
    )
    multi_source_product = replace(
        product,
        sources=(*product.sources, second_source),
        attributes=(
            replace(
                product.attributes[0],
                evidence_ids=(
                    *product.attributes[0].evidence_ids,
                    second_attribute_id,
                ),
            ),
        ),
        field_evidence=(
            *product.field_evidence,
            *(
                FieldEvidence(field_path=path, evidence_id=evidence_id)
                for path, evidence_id in second_ids_by_path.items()
            ),
        ),
    )
    multi_source_candidate = replace(
        fixture.candidate,
        product=multi_source_product,
    )
    original_by_path = {
        ref.field_path: ref
        for ref in fixture.evidence
        if ref.entity_type is EvidenceEntityType.PRODUCT
    }
    second_evidence = (
        *(
            replace(
                original_by_path[path],
                evidence_id=evidence_id,
                provider_id=second_provider,
                source_uri=second_source_uri,
            )
            for path, evidence_id in second_ids_by_path.items()
        ),
        replace(
            original_by_path["product.attributes.weight"],
            evidence_id=second_attribute_id,
            provider_id=second_provider,
            source_uri=second_source_uri,
        ),
    )
    complete_evidence = (*fixture.evidence, *second_evidence)

    build_verified_claims(
        multi_source_candidate,
        fixture.interpreted,
        complete_evidence,
        fixture.rates,
    )

    first_category = original_by_path["product.category"]
    wrong_source_binding = replace(
        first_category,
        provider_id=second_provider,
        source_uri=second_source_uri,
    )
    corrupted = _replace_evidence(
        complete_evidence,
        first_category.evidence_id,
        wrong_source_binding,
    )
    with pytest.raises(ValueError, match="product evidence"):
        build_verified_claims(
            multi_source_candidate,
            fixture.interpreted,
            corrupted,
            fixture.rates,
        )


@pytest.mark.parametrize(
    "suffix",
    (
        "inventory",
        "market",
        "currency",
        "item_price",
        "shipping",
        "tax",
        "duty",
    ),
)
def test_missing_each_offer_input_evidence_fails_closed(suffix: str) -> None:
    fixture = _fixture()
    evidence_id = f"ev-provider-a-offer-1-{suffix}"

    with pytest.raises(ValueError, match="evidence"):
        _build(
            fixture,
            evidence=_replace_evidence(fixture.evidence, evidence_id, None),
        )


@pytest.mark.parametrize(
    "corruption",
    ("snapshot", "product", "offer", "provider", "source", "field", "entity"),
)
def test_existing_but_wrong_offer_evidence_fails_closed(corruption: str) -> None:
    fixture = _fixture()
    original = next(
        ref for ref in fixture.evidence if ref.evidence_id == "ev-provider-a-offer-1-inventory"
    )
    if corruption == "entity":
        replacement = EvidenceRef(
            evidence_id=original.evidence_id,
            snapshot_version=original.snapshot_version,
            entity_type=EvidenceEntityType.PRODUCT,
            product_id=original.product_id,
            offer_id=None,
            currency=None,
            field_path="product.title",
            provider_id=original.provider_id,
            source_uri=original.source_uri,
            captured_at=original.captured_at,
        )
    else:
        changes = {
            "snapshot": {"snapshot_version": "m0-v2"},
            "product": {"product_id": "other-product"},
            "offer": {"offer_id": "other-offer"},
            "provider": {"provider_id": "other-provider"},
            "source": {"source_uri": "fixture://other/offers/offer-1"},
            "field": {"field_path": "offer.market"},
        }[corruption]
        replacement = replace(original, **changes)

    with pytest.raises(ValueError, match="evidence"):
        _build(
            fixture,
            evidence=_replace_evidence(
                fixture.evidence,
                original.evidence_id,
                replacement,
            ),
        )


@pytest.mark.parametrize("currency", ("EUR", "GBP", "CNY"))
def test_missing_each_used_fx_evidence_fails_closed(currency: str) -> None:
    fixture = _fixture()

    with pytest.raises(ValueError, match="exchange-rate evidence"):
        _build(
            fixture,
            evidence=_replace_evidence(
                fixture.evidence,
                f"ev-rate-{currency.casefold()}",
                None,
            ),
        )


@pytest.mark.parametrize("corruption", ("snapshot", "currency", "field", "entity"))
def test_existing_but_wrong_fx_evidence_fails_closed(corruption: str) -> None:
    fixture = _fixture()
    original = next(ref for ref in fixture.evidence if ref.evidence_id == "ev-rate-eur")
    if corruption == "entity":
        replacement = EvidenceRef(
            evidence_id=original.evidence_id,
            snapshot_version=original.snapshot_version,
            entity_type=EvidenceEntityType.PRODUCT,
            product_id="product-1",
            offer_id=None,
            currency=None,
            field_path="product.title",
            provider_id=original.provider_id,
            source_uri=original.source_uri,
            captured_at=original.captured_at,
        )
    else:
        changes = {
            "snapshot": {"snapshot_version": "m0-v2"},
            "currency": {"currency": "USD"},
            "field": {"field_path": "exchange_rate.minor_units"},
        }[corruption]
        replacement = replace(original, **changes)

    with pytest.raises(ValueError, match="exchange-rate evidence"):
        _build(
            fixture,
            evidence=_replace_evidence(
                fixture.evidence,
                original.evidence_id,
                replacement,
            ),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("base_per_unit", Decimal("1.21")),
        ("minor_units", 3),
        ("evidence_id", "ev-rate-eur-other"),
        ("snapshot_ordinal", 99),
    ),
)
def test_fx_trace_must_match_the_actual_rate_table(field: str, value: object) -> None:
    fixture = _fixture()
    rates = tuple(
        replace(rate, **{field: value}) if rate.currency == "EUR" else rate
        for rate in fixture.rates.rates
    )
    mismatched_table = ExchangeRateTable(
        snapshot_version=fixture.rates.snapshot_version,
        base_currency=fixture.rates.base_currency,
        rates=rates,
    )

    with pytest.raises(ValueError, match="exchange-rate trace"):
        build_verified_claims(
            fixture.candidate,
            fixture.interpreted,
            fixture.evidence,
            mismatched_table,
        )


def test_duplicate_evidence_ids_fail_closed_before_claim_assembly() -> None:
    fixture = _fixture()

    with pytest.raises(ValueError, match="duplicate evidence"):
        _build(fixture, evidence=(*fixture.evidence, fixture.evidence[0]))


def test_unknown_cost_or_unknown_stock_cannot_produce_positive_claims() -> None:
    cost_fixture = _fixture()
    cost_components = cost_fixture.candidate.eligible_offers[0].offer.cost_components
    object.__setattr__(
        cost_components,
        "shipping",
        UnknownCost(reason="carrier did not disclose shipping"),
    )
    with pytest.raises((TypeError, ValueError)):
        _build(cost_fixture)

    stock_fixture = _fixture()
    offer = stock_fixture.candidate.eligible_offers[0].offer
    object.__setattr__(offer, "stock_status", StockStatus.UNKNOWN)
    with pytest.raises((TypeError, ValueError)):
        _build(stock_fixture)


def test_missing_or_wrong_pricing_algorithm_fails_closed() -> None:
    fixture = _fixture()
    trace = fixture.candidate.eligible_offers[0].landed_cost.trace
    object.__setattr__(trace, "algorithm_version", "pricing-v2")

    with pytest.raises((TypeError, ValueError), match="pricing"):
        _build(fixture)


def test_request_budget_must_match_exact_landed_cost_budget_contract() -> None:
    fixture = _fixture()
    mismatched = InterpretedRequest(
        required=(
            fixture.interpreted.required[0],
            BudgetMax(
                mode="maximum",
                target_amount=Decimal("1999"),
                lower_bound=None,
                upper_bound=Decimal("1999"),
                currency="CNY",
                source_span=SourceSpan(start=4, end=13, text="预算1999元"),
            ),
        ),
        preferred=(),
        parser_version="fixture-v1",
    )

    with pytest.raises(ValueError, match="budget"):
        build_verified_claims(
            fixture.candidate,
            mismatched,
            fixture.evidence,
            fixture.rates,
        )


def test_budget_claim_uses_currency_minor_units_for_visible_price() -> None:
    fixture = _fixture()
    bundle = _build(fixture)
    budget = bundle.claims[-1]
    landed = fixture.candidate.eligible_offers[0].landed_cost

    assert budget.claim_type is VerifiedClaimType.WITHIN_BUDGET
    assert budget.value.startswith("到手价 985.71 CNY")
    assert canonical_exact_amount(landed.budget_exact) not in budget.value
    assert canonical_exact_amount(landed.budget_exact) != landed.display_json
