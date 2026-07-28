from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from decimal import Decimal

import pytest

import glodex.domain.eligibility as eligibility
from glodex.domain.catalog import (
    CanonicalProduct,
    CostComponents,
    EntityKind,
    ExchangeRate,
    ExchangeRateTable,
    Offer,
    ProductSource,
    StockStatus,
)
from glodex.domain.eligibility import (
    EligibilityContext,
    EligibilityOutput,
    EligibleProduct,
    FilterSummary,
    OfferGateOutput,
    OfferPricingCandidate,
    assemble_eligibility,
    run_offer_gates,
    run_product_gates,
    scorer_input,
)
from glodex.domain.evidence import EvidenceEntityType, EvidenceRef, FieldEvidence
from glodex.domain.intent import SourceSpan, TargetCategory
from glodex.domain.pricing import KnownCost, calculate_landed_cost

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec("GLO-P0-006", "GLO-P0-009"),
]

_SNAPSHOT = "m0-v1"
_CAPTURED_AT = datetime(2026, 1, 1, tzinfo=UTC)
_PRODUCT_PATHS = (
    "product.title",
    "product.category",
    "product.entity_kind",
)
_OFFER_CORE_PATHS = (
    "offer.inventory",
    "offer.market",
    "offer.cost_components.currency",
)
_COST_NAMES = ("item_price", "shipping", "tax", "duty")


def _product(product_id: str, *, ordinal: int) -> CanonicalProduct:
    source = ProductSource(
        provider_id="catalog-provider",
        source_uri=f"fixture://catalog/products/{product_id}",
        snapshot_ordinal=ordinal,
    )
    return CanonicalProduct(
        snapshot_version=_SNAPSHOT,
        product_id=product_id,
        title=f"TravelBook {product_id}",
        category="laptop",
        entity_kind=EntityKind.PRIMARY_PRODUCT,
        snapshot_ordinal=ordinal,
        sources=(source,),
        attributes=(),
        field_evidence=tuple(
            FieldEvidence(
                field_path=path,
                evidence_id=f"ev-{product_id}-{path.rsplit('.', 1)[-1]}",
            )
            for path in _PRODUCT_PATHS
        ),
    )


def _product_evidence(product: CanonicalProduct) -> tuple[EvidenceRef, ...]:
    source = product.sources[0]
    return tuple(
        EvidenceRef(
            evidence_id=binding.evidence_id,
            snapshot_version=product.snapshot_version,
            entity_type=EvidenceEntityType.PRODUCT,
            product_id=product.product_id,
            offer_id=None,
            currency=None,
            field_path=binding.field_path,
            provider_id=source.provider_id,
            source_uri=source.source_uri,
            captured_at=_CAPTURED_AT,
        )
        for binding in product.field_evidence
    )


def _offer(
    *,
    product_id: str,
    provider_id: str,
    offer_id: str,
    item_price: str,
    ordinal: int,
) -> Offer:
    prefix = f"{provider_id}-{offer_id}"
    costs = {
        "item_price": KnownCost(
            amount=Decimal(item_price),
            evidence_id=f"ev-{prefix}-item_price",
        ),
        "shipping": KnownCost(
            amount=Decimal("0.00"),
            evidence_id=f"ev-{prefix}-shipping",
        ),
        "tax": KnownCost(
            amount=Decimal("0.00"),
            evidence_id=f"ev-{prefix}-tax",
        ),
        "duty": KnownCost(
            amount=Decimal("0.00"),
            evidence_id=f"ev-{prefix}-duty",
        ),
    }
    return Offer(
        snapshot_version=_SNAPSHOT,
        offer_id=offer_id,
        product_id=product_id,
        provider_id=provider_id,
        source_uri=f"fixture://{provider_id}/offers/{offer_id}",
        market="US",
        stock_status=StockStatus.IN_STOCK,
        cost_components=CostComponents(
            currency="USD",
            item_price=costs["item_price"],
            shipping=costs["shipping"],
            tax=costs["tax"],
            duty=costs["duty"],
        ),
        captured_at=_CAPTURED_AT,
        snapshot_ordinal=ordinal,
        field_evidence=(
            *(
                FieldEvidence(
                    field_path=path,
                    evidence_id=f"ev-{prefix}-{path.rsplit('.', 1)[-1]}",
                )
                for path in _OFFER_CORE_PATHS
            ),
            *(
                FieldEvidence(
                    field_path=f"offer.cost_components.{name}",
                    evidence_id=costs[name].evidence_id,
                )
                for name in _COST_NAMES
            ),
        ),
    )


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


def _context() -> EligibilityContext:
    return EligibilityContext(
        required=(
            TargetCategory(
                category="laptop",
                source_span=SourceSpan(start=0, end=3, text="笔记本"),
            ),
        )
    )


def _rates() -> ExchangeRateTable:
    return ExchangeRateTable(
        snapshot_version=_SNAPSHOT,
        base_currency="USD",
        rates=(
            ExchangeRate(
                snapshot_version=_SNAPSHOT,
                currency="USD",
                base_per_unit=Decimal("1"),
                minor_units=2,
                evidence_id="ev-rate-usd",
                snapshot_ordinal=0,
            ),
        ),
    )


def _assemble(
    products: tuple[CanonicalProduct, ...],
    offers: tuple[Offer, ...],
):
    return _pipeline_outputs(products, offers)[2]


def _pipeline_outputs(
    products: tuple[CanonicalProduct, ...],
    offers: tuple[Offer, ...],
):
    context = _context()
    product_output = run_product_gates(
        products,
        context,
        tuple(item for product in products for item in _product_evidence(product)),
    )
    pricing = tuple(
        OfferPricingCandidate(
            offer=offer,
            pricing=calculate_landed_cost(
                offer.cost_components,
                _rates(),
                display_currency="USD",
            ),
        )
        for offer in offers
    )
    offer_output = run_offer_gates(
        product_output,
        offers,
        pricing,
        tuple(item for offer in offers for item in _offer_evidence(offer)),
        display_currency="USD",
    )
    output = assemble_eligibility(product_output, offer_output)
    return product_output, offer_output, output


def test_business_product_validation_computes_its_signature_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    product = _product("product-1", ordinal=0)
    product_output = run_product_gates(
        (product,),
        _context(),
        _product_evidence(product),
    )
    original_signature = eligibility._product_output_signature
    signature_calls = 0

    def tracked_signature(value: object) -> bytes:
        nonlocal signature_calls
        signature_calls += 1
        return original_signature(value)  # type: ignore[arg-type]

    monkeypatch.setattr(eligibility, "_product_output_signature", tracked_signature)

    assert eligibility._is_valid_business_product_output(product_output)
    assert signature_calls == 1


def test_search_style_pipeline_computes_product_signature_at_most_three_times(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    product = _product("product-1", ordinal=0)
    offer = _offer(
        product_id=product.product_id,
        provider_id="provider-a",
        offer_id="offer-a",
        item_price="10.00",
        ordinal=0,
    )
    context = _context()
    pricing = (
        OfferPricingCandidate(
            offer=offer,
            pricing=calculate_landed_cost(
                offer.cost_components,
                _rates(),
                display_currency="USD",
            ),
        ),
    )
    original_signature = eligibility._product_output_signature
    signature_calls = 0

    def tracked_signature(value: object) -> bytes:
        nonlocal signature_calls
        signature_calls += 1
        return original_signature(value)  # type: ignore[arg-type]

    monkeypatch.setattr(eligibility, "_product_output_signature", tracked_signature)

    product_output = run_product_gates(
        (product,),
        context,
        _product_evidence(product),
    )
    offer_output = run_offer_gates(
        product_output,
        (offer,),
        pricing,
        _offer_evidence(offer),
        display_currency="USD",
    )
    output = assemble_eligibility(product_output, offer_output)
    scorer_input(output)
    scorer_input(output)

    assert signature_calls == 3


def test_assembly_rejects_tampered_exact_product_parent() -> None:
    product = _product("product-1", ordinal=0)
    offer = _offer(
        product_id=product.product_id,
        provider_id="provider-a",
        offer_id="offer-a",
        item_price="10.00",
        ordinal=0,
    )
    product_output, offer_output, _ = _pipeline_outputs((product,), (offer,))
    object.__setattr__(product_output, "diagnostics", ("tampered",))

    with pytest.raises(TypeError, match="concrete product business gates"):
        assemble_eligibility(product_output, offer_output)


def test_assembly_retains_every_eligible_offer_and_selects_by_the_exact_stable_key() -> None:
    product = _product("product-1", ordinal=3)
    offers = (
        _offer(
            product_id=product.product_id,
            provider_id="provider-0",
            offer_id="offer-0",
            item_price="10.004",
            ordinal=0,
        ),
        _offer(
            product_id=product.product_id,
            provider_id="provider-b",
            offer_id="offer-a",
            item_price="10.00",
            ordinal=1,
        ),
        _offer(
            product_id=product.product_id,
            provider_id="provider-a",
            offer_id="offer-z",
            item_price="10.00",
            ordinal=2,
        ),
        _offer(
            product_id=product.product_id,
            provider_id="provider-a",
            offer_id="offer-a",
            item_price="10.00",
            ordinal=3,
        ),
    )

    output = _assemble((product,), offers)

    assert len(output.candidates) == 1
    eligible = output.candidates[0]
    assert type(eligible) is EligibleProduct
    assert eligible.product is product
    assert tuple(item.offer for item in eligible.eligible_offers) == offers
    assert eligible.selected_offer is eligible.eligible_offers[3]
    assert eligible.selected_offer.offer.provider_id == "provider-a"
    assert eligible.selected_offer.offer.offer_id == "offer-a"
    assert eligible.selected_offer.landed_cost.display_exact == Decimal("10.00")
    assert scorer_input(output).candidates is output.candidates


def test_product_without_an_eligible_offer_never_enters_the_assembled_candidates() -> None:
    without_offer = _product("product-without-offer", ordinal=1)
    with_offer = _product("product-with-offer", ordinal=2)
    offer = _offer(
        product_id=with_offer.product_id,
        provider_id="provider-a",
        offer_id="offer-a",
        item_price="20.00",
        ordinal=0,
    )

    output = _assemble((without_offer, with_offer), (offer,))

    assert tuple(item.product for item in output.candidates) == (with_offer,)
    assert output.candidates[0].eligible_offers[0].offer is offer


def test_selected_offer_is_independent_of_offer_input_order() -> None:
    product = _product("product-1", ordinal=0)
    offers = (
        _offer(
            product_id=product.product_id,
            provider_id="provider-b",
            offer_id="offer-a",
            item_price="10.00",
            ordinal=0,
        ),
        _offer(
            product_id=product.product_id,
            provider_id="provider-a",
            offer_id="offer-b",
            item_price="10.00",
            ordinal=1,
        ),
    )

    forward = _assemble((product,), offers)
    reverse = _assemble((product,), tuple(reversed(offers)))

    assert forward.candidates[0].selected_offer.offer.provider_id == "provider-a"
    assert reverse.candidates[0].selected_offer.offer.provider_id == "provider-a"


def test_scorer_rejects_forged_tampered_or_cross_bound_assembly_outputs() -> None:
    product = _product("product-1", ordinal=0)
    offer = _offer(
        product_id=product.product_id,
        provider_id="provider-a",
        offer_id="offer-a",
        item_price="10.00",
        ordinal=0,
    )
    product_output, offer_output, output = _pipeline_outputs((product,), (offer,))

    forged = object.__new__(EligibilityOutput)
    for name in EligibilityOutput.__slots__:
        if name == "__weakref__":
            continue
        object.__setattr__(forged, name, getattr(output, name))
    with pytest.raises(TypeError, match="assembled EligibilityOutput"):
        scorer_input(forged)

    object.__setattr__(output, "candidates", ())
    with pytest.raises(TypeError, match="assembled EligibilityOutput"):
        scorer_input(output)

    second_product = _product("product-2", ordinal=1)
    second_offer = _offer(
        product_id=second_product.product_id,
        provider_id="provider-b",
        offer_id="offer-b",
        item_price="11.00",
        ordinal=1,
    )
    _, second_offer_output, _ = _pipeline_outputs((second_product,), (second_offer,))
    with pytest.raises(ValueError, match="exact product output"):
        assemble_eligibility(product_output, second_offer_output)

    with pytest.raises(TypeError, match="concrete offer gates"):
        OfferGateOutput()  # type: ignore[call-arg]
    with pytest.raises(FrozenInstanceError):
        offer_output.eligible_offers = ()  # type: ignore[misc]


@pytest.mark.parametrize(
    "tamper_target",
    (
        "product_candidates",
        "product_candidate",
        "product_diagnostics",
        "offer_diagnostics",
        "filter_summary",
        "selected_offer",
    ),
)
def test_scorer_rejects_every_tampered_assembled_dependency(
    tamper_target: str,
) -> None:
    product = _product("product-1", ordinal=0)
    eligible_offers = (
        _offer(
            product_id=product.product_id,
            provider_id="provider-a",
            offer_id="offer-a",
            item_price="10.00",
            ordinal=0,
        ),
        _offer(
            product_id=product.product_id,
            provider_id="provider-b",
            offer_id="offer-b",
            item_price="11.00",
            ordinal=1,
        ),
    )
    rejected_offer = _offer(
        product_id=product.product_id,
        provider_id="provider-c",
        offer_id="offer-c",
        item_price="12.00",
        ordinal=2,
    )
    object.__setattr__(rejected_offer, "stock_status", StockStatus.OUT_OF_STOCK)
    product_output, offer_output, output = _pipeline_outputs(
        (product,),
        (*eligible_offers, rejected_offer),
    )

    if tamper_target == "product_candidates":
        replacement = tuple(candidate for candidate in product_output.candidates)
        assert replacement is not product_output.candidates
        object.__setattr__(product_output, "candidates", replacement)
    elif tamper_target == "product_candidate":
        object.__setattr__(product_output.candidates[0], "title", "tampered")
    elif tamper_target == "product_diagnostics":
        object.__setattr__(product_output, "diagnostics", ("tampered",))
    elif tamper_target == "offer_diagnostics":
        object.__setattr__(offer_output, "diagnostics", ())
    elif tamper_target == "filter_summary":
        object.__setattr__(
            output,
            "filter_summary",
            FilterSummary(stages=(), reason_counts=()),
        )
    else:
        object.__setattr__(
            output.candidates[0],
            "selected_offer",
            output.candidates[0].eligible_offers[1],
        )

    with pytest.raises(TypeError, match="assembled EligibilityOutput"):
        scorer_input(output)


def test_eligible_product_rejects_duplicate_or_non_minimum_selected_offer() -> None:
    product = _product("product-1", ordinal=0)
    offers = (
        _offer(
            product_id=product.product_id,
            provider_id="provider-a",
            offer_id="offer-a",
            item_price="10.00",
            ordinal=0,
        ),
        _offer(
            product_id=product.product_id,
            provider_id="provider-b",
            offer_id="offer-b",
            item_price="11.00",
            ordinal=1,
        ),
    )
    output = _assemble((product,), offers)
    first, second = output.candidates[0].eligible_offers

    with pytest.raises(ValueError, match="stable offer key"):
        EligibleProduct(
            product=product,
            eligible_offers=(first, second),
            selected_offer=second,
        )
    with pytest.raises(ValueError, match="identities must be unique"):
        EligibleProduct(
            product=product,
            eligible_offers=(first, first),
            selected_offer=first,
        )
