from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from decimal import Decimal
from inspect import Parameter, signature

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
    EligibleOffer,
    OfferGateId,
    OfferGateOutput,
    OfferPricingCandidate,
    OfferRejectionCode,
    diagnose_offer_rejections,
    run_offer_gates,
    run_product_gates,
)
from glodex.domain.evidence import EvidenceEntityType, EvidenceRef, FieldEvidence
from glodex.domain.intent import BudgetMax, SourceSpan, StockRequired, TargetCategory
from glodex.domain.pricing import (
    KnownCost,
    LandedCost,
    PricingFailure,
    PricingFailureCode,
    UnknownCost,
    UnknownCostDetail,
    calculate_landed_cost,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec("GLO-P0-005", "GLO-P0-006", "AC-003", "AC-005"),
]

_SNAPSHOT = "m0-v1"
_CAPTURED_AT = datetime(2026, 1, 1, tzinfo=UTC)
_CORE_OFFER_PATHS = (
    "offer.inventory",
    "offer.market",
    "offer.cost_components.currency",
)
_COST_NAMES = ("item_price", "shipping", "tax", "duty")


def _product(
    product_id: str = "product-1",
    *,
    category: str = "laptop",
) -> CanonicalProduct:
    return CanonicalProduct(
        snapshot_version=_SNAPSHOT,
        product_id=product_id,
        title=f"TravelBook {product_id}",
        category=category,
        entity_kind=EntityKind.PRIMARY_PRODUCT,
        snapshot_ordinal=0,
        sources=(
            ProductSource(
                provider_id="catalog-provider",
                source_uri=f"fixture://catalog/products/{product_id}",
                snapshot_ordinal=0,
            ),
        ),
        attributes=(),
        field_evidence=tuple(
            FieldEvidence(
                field_path=field_path,
                evidence_id=f"ev-{product_id}-{field_path.rsplit('.', 1)[-1]}",
            )
            for field_path in (
                "product.title",
                "product.category",
                "product.entity_kind",
            )
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


def _context(
    *,
    budget: str | None = "800.00",
    budget_currency: str | None = "USD",
    stock_required: bool = False,
) -> EligibilityContext:
    required: list[TargetCategory | BudgetMax | StockRequired] = [
        TargetCategory(
            category="laptop",
            source_span=SourceSpan(start=0, end=3, text="笔记本"),
        )
    ]
    if budget is not None:
        required.append(
            BudgetMax(
                amount=Decimal(budget),
                currency=budget_currency,
                source_span=SourceSpan(start=4, end=12, text="预算800元"),
            )
        )
    if stock_required:
        required.append(
            StockRequired(
                source_span=SourceSpan(start=13, end=16, text="有库存"),
            )
        )
    return EligibilityContext(required=tuple(required))


def _product_output(
    products: tuple[CanonicalProduct, ...],
    context: EligibilityContext,
):
    return run_product_gates(
        products,
        context,
        tuple(item for product in products for item in _product_evidence(product)),
    )


def _offer(
    offer_id: str = "offer-1",
    *,
    product_id: str = "product-1",
    provider_id: str = "provider-a",
    source_currency: str = "USD",
    item_price: str = "700.00",
    stock_status: StockStatus = StockStatus.IN_STOCK,
    unknown: tuple[str, str] | None = None,
) -> Offer:
    costs: dict[str, KnownCost | UnknownCost] = {
        "item_price": KnownCost(
            amount=Decimal(item_price),
            evidence_id=f"ev-{provider_id}-{offer_id}-item-price",
        ),
        "shipping": KnownCost(
            amount=Decimal("0.00"),
            evidence_id=f"ev-{provider_id}-{offer_id}-shipping",
        ),
        "tax": KnownCost(
            amount=Decimal("0.00"),
            evidence_id=f"ev-{provider_id}-{offer_id}-tax",
        ),
        "duty": KnownCost(
            amount=Decimal("0.00"),
            evidence_id=f"ev-{provider_id}-{offer_id}-duty",
        ),
    }
    if unknown is not None:
        component, reason = unknown
        costs[component] = UnknownCost(reason=reason)
    cost_components = CostComponents(
        currency=source_currency,
        item_price=costs["item_price"],
        shipping=costs["shipping"],
        tax=costs["tax"],
        duty=costs["duty"],
    )
    cost_bindings = tuple(
        FieldEvidence(
            field_path=f"offer.cost_components.{component}",
            evidence_id=cost.evidence_id,
        )
        for component, cost in costs.items()
        if type(cost) is KnownCost
    )
    return Offer(
        snapshot_version=_SNAPSHOT,
        offer_id=offer_id,
        product_id=product_id,
        provider_id=provider_id,
        source_uri=f"fixture://{provider_id}/offers/{offer_id}",
        market="US",
        stock_status=stock_status,
        cost_components=cost_components,
        captured_at=_CAPTURED_AT,
        snapshot_ordinal=0,
        field_evidence=(
            *(
                FieldEvidence(
                    field_path=field_path,
                    evidence_id=f"ev-{provider_id}-{offer_id}-{field_path.rsplit('.', 1)[-1]}",
                )
                for field_path in _CORE_OFFER_PATHS
            ),
            *cost_bindings,
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


def _rates(
    *,
    currencies: tuple[str, ...] = ("USD", "EUR", "CNY"),
) -> ExchangeRateTable:
    base_per_unit = {
        "USD": Decimal("1"),
        "EUR": Decimal("1.2"),
        "CNY": Decimal("0.14"),
        "GBP": Decimal("1.3"),
    }
    return ExchangeRateTable(
        snapshot_version=_SNAPSHOT,
        base_currency="USD",
        rates=tuple(
            ExchangeRate(
                snapshot_version=_SNAPSHOT,
                currency=currency,
                base_per_unit=base_per_unit[currency],
                minor_units=2,
                evidence_id=f"ev-rate-{currency.casefold()}",
                snapshot_ordinal=index,
            )
            for index, currency in enumerate(currencies)
        ),
    )


def _pricing_candidate(
    offer: Offer,
    context: EligibilityContext,
    *,
    display_currency: str = "USD",
    rates: ExchangeRateTable | None = None,
) -> OfferPricingCandidate:
    budget = next(
        (constraint for constraint in context.required if type(constraint) is BudgetMax),
        None,
    )
    result = calculate_landed_cost(
        offer.cost_components,
        _rates() if rates is None else rates,
        display_currency=display_currency,
        budget_max=None if budget is None else budget.amount,
        budget_currency=None if budget is None else budget.currency,
    )
    return OfferPricingCandidate(offer=offer, pricing=result)


def _run(
    offers: tuple[Offer, ...],
    context: EligibilityContext,
    *,
    products: tuple[CanonicalProduct, ...] | None = None,
    pricing: tuple[OfferPricingCandidate, ...] | None = None,
    evidence: tuple[EvidenceRef, ...] | None = None,
    display_currency: str = "USD",
) -> OfferGateOutput:
    if products is None:
        product_ids = tuple(dict.fromkeys(offer.product_id for offer in offers))
        products = tuple(_product(product_id) for product_id in product_ids)
    if pricing is None:
        pricing = tuple(
            _pricing_candidate(offer, context, display_currency=display_currency)
            for offer in offers
        )
    if evidence is None:
        evidence = tuple(item for offer in offers for item in _offer_evidence(offer))
    return run_offer_gates(
        _product_output(products, context),
        offers,
        pricing,
        evidence,
        display_currency=display_currency,
    )


def _gate_result(output: OfferGateOutput, gate: OfferGateId):
    return next(result for result in output.gate_results if result.gate is gate)


def test_public_offer_runner_has_only_the_fixed_concrete_surface() -> None:
    parameters = signature(run_offer_gates).parameters

    assert "run_offer_gates" in eligibility.__all__
    assert tuple(parameters) == (
        "product_output",
        "offers",
        "pricing",
        "evidence",
        "display_currency",
    )
    assert parameters["display_currency"].kind is Parameter.KEYWORD_ONLY
    assert "predicate" not in parameters
    assert "relax" not in parameters
    assert all(
        parameter.kind not in (Parameter.VAR_POSITIONAL, Parameter.VAR_KEYWORD)
        for parameter in parameters.values()
    )
    with pytest.raises(TypeError):
        run_offer_gates(None, (), (), (), "USD")  # type: ignore[call-arg, arg-type]
    with pytest.raises(TypeError):
        run_offer_gates(  # type: ignore[call-arg, arg-type]
            None,
            (),
            (),
            (),
            display_currency="USD",
            relax=True,
        )


@pytest.mark.parametrize(
    ("budget", "expected_gates"),
    [
        (
            "800.00",
            (
                OfferGateId.SOURCE,
                OfferGateId.STOCK,
                OfferGateId.COST_COMPLETENESS,
                OfferGateId.EXCHANGE_RATE,
                OfferGateId.BUDGET,
            ),
        ),
        (
            None,
            (
                OfferGateId.SOURCE,
                OfferGateId.STOCK,
                OfferGateId.COST_COMPLETENESS,
                OfferGateId.EXCHANGE_RATE,
            ),
        ),
    ],
)
def test_offer_gate_order_and_passed_gates_are_exactly_the_active_order(
    budget: str | None,
    expected_gates: tuple[OfferGateId, ...],
) -> None:
    context = _context(budget=budget)
    offer = _offer()

    output = _run((offer,), context)

    assert tuple(result.gate for result in output.gate_results) == expected_gates
    assert output.input_candidates[0].offer is offer
    assert output.funnel_candidates == output.input_candidates
    assert output.eligible_offers == (
        EligibleOffer(
            offer=offer,
            landed_cost=output.input_candidates[0].pricing,
            passed_gates=expected_gates,
        ),
    )
    assert isinstance(output.eligible_offers[0].landed_cost, LandedCost)
    with pytest.raises(FrozenInstanceError):
        output.eligible_offers[0].passed_gates = ()  # type: ignore[misc]


def test_three_provider_offers_are_conserved_in_catalog_order() -> None:
    context = _context()
    offers = (
        _offer("shared-offer", provider_id="provider-z"),
        _offer("shared-offer", provider_id="provider-a"),
        _offer("shared-offer", provider_id="provider-m"),
    )

    output = _run(offers, context)

    assert tuple(item.offer for item in output.input_candidates) == offers
    assert tuple(item.offer for item in output.funnel_candidates) == offers
    assert tuple(item.offer for item in output.eligible_offers) == offers
    assert tuple(item.offer.provider_id for item in output.eligible_offers) == (
        "provider-z",
        "provider-a",
        "provider-m",
    )


def test_offer_for_product_rejected_upstream_is_preserved_but_never_enters_offer_gates() -> None:
    context = _context()
    kept_product = _product("product-kept")
    rejected_product = _product("product-rejected", category="camera")
    kept_offer = _offer("offer-kept", product_id=kept_product.product_id)
    ignored_offer = _offer(
        "offer-ignored",
        product_id=rejected_product.product_id,
        stock_status=StockStatus.OUT_OF_STOCK,
    )
    offers = (ignored_offer, kept_offer)

    output = _run(
        offers,
        context,
        products=(rejected_product, kept_product),
    )

    assert tuple(item.offer for item in output.input_candidates) == offers
    assert tuple(item.offer for item in output.funnel_candidates) == (kept_offer,)
    assert output.gate_results[0].before == output.funnel_candidates
    assert all(
        rejection.candidate.offer is not ignored_offer
        for result in output.gate_results
        for rejection in result.rejected
    )
    assert tuple(item.offer for item in output.eligible_offers) == (kept_offer,)


@pytest.mark.parametrize(
    ("stock_status", "reason"),
    [
        (StockStatus.OUT_OF_STOCK, OfferRejectionCode.OUT_OF_STOCK.value),
        (StockStatus.UNKNOWN, OfferRejectionCode.STOCK_UNKNOWN.value),
    ],
)
@pytest.mark.parametrize("stock_required", [False, True])
def test_non_in_stock_offer_is_always_rejected_with_a_specific_reason(
    stock_status: StockStatus,
    reason: str,
    stock_required: bool,
) -> None:
    context = _context(stock_required=stock_required)
    offer = _offer(stock_status=stock_status)

    output = _run((offer,), context)

    stock = _gate_result(output, OfferGateId.STOCK)
    assert stock.rejected[0].reasons == (reason,)
    assert output.eligible_offers == ()
    assert all(
        result.before == ()
        for result in output.gate_results
        if result.gate not in (OfferGateId.SOURCE, OfferGateId.STOCK)
    )


def test_stock_required_does_not_change_in_stock_membership() -> None:
    offer = _offer()

    implicit = _run((offer,), _context(stock_required=False))
    explicit = _run((offer,), _context(stock_required=True))

    assert tuple(item.offer for item in implicit.eligible_offers) == (offer,)
    assert tuple(item.offer for item in explicit.eligible_offers) == (offer,)


def test_unknown_cost_is_rejected_without_a_budget_and_budget_gate_never_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(budget=None)
    offer = _offer(unknown=("shipping", "CARRIER_NOT_DISCLOSED"))
    seen_gates: list[OfferGateId] = []
    original = eligibility._business_offer_reasons

    def tracked_reasons(
        gate: OfferGateId,
        candidate: OfferPricingCandidate,
        evidence_index: dict[str, EvidenceRef],
        budget: BudgetMax | None,
    ) -> tuple[str, ...]:
        seen_gates.append(gate)
        return original(gate, candidate, evidence_index, budget)

    monkeypatch.setattr(eligibility, "_business_offer_reasons", tracked_reasons)

    output = _run((offer,), context)

    cost = _gate_result(output, OfferGateId.COST_COMPLETENESS)
    assert cost.rejected[0].reasons == (OfferRejectionCode.COST_UNKNOWN.value,)
    assert OfferGateId.BUDGET not in seen_gates
    assert tuple(result.gate for result in output.gate_results) == (
        OfferGateId.SOURCE,
        OfferGateId.STOCK,
        OfferGateId.COST_COMPLETENESS,
        OfferGateId.EXCHANGE_RATE,
    )


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (
            PricingFailure(
                code=PricingFailureCode.MISSING_EXCHANGE_RATE,
                missing_currencies=("GBP",),
            ),
            OfferRejectionCode.EXCHANGE_RATE_MISSING.value,
        ),
        (
            PricingFailure(code=PricingFailureCode.DECIMAL_ARITHMETIC),
            OfferRejectionCode.PRICING_ARITHMETIC.value,
        ),
    ],
)
def test_fx_failures_are_rejected_at_the_exchange_rate_gate(
    failure: PricingFailure,
    reason: str,
) -> None:
    context = _context()
    offer = _offer(source_currency="GBP")
    candidate = OfferPricingCandidate(offer=offer, pricing=failure)

    output = _run(
        (offer,),
        context,
        pricing=(candidate,),
    )

    exchange_rate = _gate_result(output, OfferGateId.EXCHANGE_RATE)
    assert exchange_rate.rejected[0].reasons == (reason,)
    assert output.eligible_offers == ()


def test_budget_uses_exact_inclusive_value_not_equal_quantized_display() -> None:
    context = _context(budget="800.00")
    equal = _offer("offer-equal", item_price="800.00")
    epsilon_over = _offer("offer-over", item_price="800.004")

    output = _run((equal, epsilon_over), context)

    equal_cost = output.input_candidates[0].pricing
    over_cost = output.input_candidates[1].pricing
    assert type(equal_cost) is LandedCost
    assert type(over_cost) is LandedCost
    assert equal_cost.display_quantized == over_cost.display_quantized == Decimal("800.00")
    assert equal_cost.budget_exact == Decimal("800.00")
    assert over_cost.budget_exact == Decimal("800.004")
    assert tuple(item.offer for item in output.eligible_offers) == (equal,)
    budget = _gate_result(output, OfferGateId.BUDGET)
    assert budget.rejected[0].candidate.offer is epsilon_over
    assert budget.rejected[0].reasons == (OfferRejectionCode.OVER_BUDGET.value,)


@pytest.mark.parametrize(
    ("budget_currency", "display_currency", "item_price", "expected_currency"),
    [
        ("EUR", "USD", "120.00", "EUR"),
        (None, "EUR", "120.00", "EUR"),
    ],
)
def test_explicit_and_inherited_budget_currency_contracts(
    budget_currency: str | None,
    display_currency: str,
    item_price: str,
    expected_currency: str,
) -> None:
    context = _context(
        budget="100.00",
        budget_currency=budget_currency,
    )
    offer = _offer(item_price=item_price)

    output = _run(
        (offer,),
        context,
        display_currency=display_currency,
    )

    landed = output.eligible_offers[0].landed_cost
    assert landed.display_currency == display_currency
    assert landed.budget_currency == expected_currency
    assert landed.budget_exact == Decimal("100.00")
    assert landed.budget_max == Decimal("100.00")


def test_diagnostics_reports_all_determinable_reasons_without_recovery() -> None:
    context = _context(budget="800.00")
    offer = _offer(
        stock_status=StockStatus.OUT_OF_STOCK,
        item_price="800.01",
    )
    product_output = _product_output((_product(),), context)
    candidate = _pricing_candidate(offer, context)
    evidence = _offer_evidence(offer)

    output = run_offer_gates(
        product_output,
        (offer,),
        (candidate,),
        evidence,
        display_currency="USD",
    )
    diagnostics = diagnose_offer_rejections(
        product_output,
        (offer,),
        (candidate,),
        evidence,
        display_currency="USD",
    )

    assert tuple(item.gate for item in diagnostics) == (
        OfferGateId.STOCK,
        OfferGateId.BUDGET,
    )
    assert diagnostics[0].reasons == (OfferRejectionCode.OUT_OF_STOCK.value,)
    assert diagnostics[1].reasons == (OfferRejectionCode.OVER_BUDGET.value,)
    assert sum(len(result.rejected) for result in output.gate_results) == 1
    assert output.eligible_offers == ()


@pytest.mark.parametrize(
    "failure_code",
    [
        PricingFailureCode.UNKNOWN_COST,
        PricingFailureCode.MISSING_EXCHANGE_RATE,
        PricingFailureCode.DECIMAL_ARITHMETIC,
    ],
)
def test_pricing_failure_diagnostics_never_invents_a_budget_reason(
    failure_code: PricingFailureCode,
) -> None:
    context = _context(budget="800.00")
    if failure_code is PricingFailureCode.UNKNOWN_COST:
        offer = _offer(unknown=("tax", "TAX_NOT_DISCLOSED"))
        failure = PricingFailure(
            code=failure_code,
            unknown_costs=(UnknownCostDetail(component="tax", reason="TAX_NOT_DISCLOSED"),),
        )
    elif failure_code is PricingFailureCode.MISSING_EXCHANGE_RATE:
        offer = _offer(source_currency="GBP")
        failure = PricingFailure(
            code=failure_code,
            missing_currencies=("GBP",),
        )
    else:
        offer = _offer()
        failure = PricingFailure(code=failure_code)
    candidate = OfferPricingCandidate(offer=offer, pricing=failure)
    product_output = _product_output((_product(),), context)

    diagnostics = diagnose_offer_rejections(
        product_output,
        (offer,),
        (candidate,),
        _offer_evidence(offer),
        display_currency="USD",
    )

    assert OfferGateId.BUDGET not in {item.gate for item in diagnostics}


def test_offer_output_is_nominally_constructed_and_seal_detects_tampering() -> None:
    with pytest.raises(TypeError, match="concrete offer gates"):
        OfferGateOutput()  # type: ignore[call-arg]

    output = _run((_offer(),), _context())

    assert eligibility._is_valid_offer_output(output)
    object.__setattr__(output, "eligible_offers", ())
    assert not eligibility._is_valid_offer_output(output)


def test_forged_or_tampered_product_output_cannot_enter_offer_gates() -> None:
    context = _context()
    product_output = _product_output((_product(),), context)
    offer = _offer()
    candidate = _pricing_candidate(offer, context)
    forged = object.__new__(type(product_output))
    object.__setattr__(forged, "context", product_output.context)
    object.__setattr__(forged, "candidates", product_output.candidates)
    object.__setattr__(forged, "gate_results", product_output.gate_results)

    with pytest.raises(TypeError, match="sealed ProductGateOutput"):
        run_offer_gates(
            forged,
            (offer,),
            (candidate,),
            _offer_evidence(offer),
            display_currency="USD",
        )

    object.__setattr__(product_output, "candidates", ())
    with pytest.raises(TypeError, match="sealed ProductGateOutput"):
        run_offer_gates(
            product_output,
            (offer,),
            (candidate,),
            _offer_evidence(offer),
            display_currency="USD",
        )


@pytest.mark.parametrize("case", ["missing", "extra", "duplicate", "reordered", "replacement"])
def test_offer_and_pricing_alignment_failures_are_boundary_errors(case: str) -> None:
    context = _context()
    first = _offer("offer-1", provider_id="provider-a")
    second = _offer("offer-2", provider_id="provider-b")
    offers = (first, second)
    first_candidate = _pricing_candidate(first, context)
    second_candidate = _pricing_candidate(second, context)
    pricing: tuple[OfferPricingCandidate, ...]
    if case == "missing":
        pricing = (first_candidate,)
    elif case == "extra":
        pricing = (first_candidate, second_candidate, second_candidate)
    elif case == "duplicate":
        pricing = (first_candidate, first_candidate)
    elif case == "reordered":
        pricing = (second_candidate, first_candidate)
    else:
        replacement = replace(first)
        pricing = (
            _pricing_candidate(replacement, context),
            second_candidate,
        )

    with pytest.raises(ValueError, match=r"one-to-one|identity and order"):
        run_offer_gates(
            _product_output((_product(),), context),
            offers,
            pricing,
            (*_offer_evidence(first), *_offer_evidence(second)),
            display_currency="USD",
        )


def test_rejected_product_offer_still_requires_aligned_pricing_before_filtering() -> None:
    context = _context()
    rejected_product = _product("product-rejected", category="camera")
    kept_product = _product("product-kept")
    ignored = _offer("offer-ignored", product_id=rejected_product.product_id)
    kept = _offer("offer-kept", product_id=kept_product.product_id)

    with pytest.raises(ValueError, match="one-to-one"):
        run_offer_gates(
            _product_output((rejected_product, kept_product), context),
            (ignored, kept),
            (_pricing_candidate(kept, context),),
            (*_offer_evidence(ignored), *_offer_evidence(kept)),
            display_currency="USD",
        )


def test_duplicate_provider_scoped_offer_identity_is_rejected() -> None:
    context = _context()
    first = _offer()
    duplicate = replace(first, source_uri="fixture://provider-a/offers/duplicate-copy")

    with pytest.raises(ValueError, match="offer identities must be unique"):
        run_offer_gates(
            _product_output((_product(),), context),
            (first, duplicate),
            (
                _pricing_candidate(first, context),
                _pricing_candidate(duplicate, context),
            ),
            (*_offer_evidence(first), *_offer_evidence(duplicate)),
            display_currency="USD",
        )


def test_offer_must_reference_the_original_product_universe_and_snapshot() -> None:
    context = _context()
    unknown_product = _offer(product_id="unknown-product")
    wrong_snapshot = replace(_offer(), snapshot_version="m0-v2")

    with pytest.raises(ValueError, match="outside the product gate universe"):
        run_offer_gates(
            _product_output((_product(),), context),
            (unknown_product,),
            (
                OfferPricingCandidate(
                    offer=unknown_product,
                    pricing=PricingFailure(code=PricingFailureCode.DECIMAL_ARITHMETIC),
                ),
            ),
            _offer_evidence(unknown_product),
            display_currency="USD",
        )
    with pytest.raises(ValueError, match="snapshot"):
        run_offer_gates(
            _product_output((_product(),), context),
            (wrong_snapshot,),
            (
                OfferPricingCandidate(
                    offer=wrong_snapshot,
                    pricing=PricingFailure(code=PricingFailureCode.DECIMAL_ARITHMETIC),
                ),
            ),
            _offer_evidence(wrong_snapshot),
            display_currency="USD",
        )


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "cross_snapshot",
        "cross_product",
        "cross_offer",
        "wrong_entity",
        "wrong_provider",
        "wrong_source",
        "wrong_field",
    ],
)
def test_offer_evidence_closure_failure_is_a_source_rejection(case: str) -> None:
    context = _context()
    offer = _offer()
    evidence = _offer_evidence(offer)
    target_id = offer.field_evidence[0].evidence_id
    if case == "missing":
        evidence = tuple(item for item in evidence if item.evidence_id != target_id)
    elif case == "wrong_entity":
        evidence = tuple(
            EvidenceRef(
                evidence_id=item.evidence_id,
                snapshot_version=item.snapshot_version,
                entity_type=EvidenceEntityType.PRODUCT,
                product_id=item.product_id,
                offer_id=None,
                currency=None,
                field_path="product.title",
                provider_id=item.provider_id,
                source_uri=item.source_uri,
                captured_at=item.captured_at,
            )
            if item.evidence_id == target_id
            else item
            for item in evidence
        )
    else:
        changes: dict[str, object]
        if case == "cross_snapshot":
            changes = {"snapshot_version": "m0-v2"}
        elif case == "cross_product":
            changes = {"product_id": "product-2"}
        elif case == "cross_offer":
            changes = {"offer_id": "offer-2"}
        elif case == "wrong_provider":
            changes = {"provider_id": "provider-b"}
        elif case == "wrong_source":
            changes = {"source_uri": "fixture://provider-a/offers/other"}
        else:
            changes = {"field_path": "offer.market"}
        evidence = tuple(
            replace(item, **changes) if item.evidence_id == target_id else item for item in evidence
        )

    output = _run(
        (offer,),
        context,
        evidence=evidence,
    )

    source = _gate_result(output, OfferGateId.SOURCE)
    assert source.rejected[0].reasons == (OfferRejectionCode.SOURCE_INVALID.value,)
    assert output.eligible_offers == ()


def test_duplicate_or_mutable_offer_evidence_input_fails_closed() -> None:
    context = _context()
    offer = _offer()
    evidence = _offer_evidence(offer)
    product_output = _product_output((_product(),), context)
    pricing = (_pricing_candidate(offer, context),)

    with pytest.raises(ValueError, match="duplicate evidence ID"):
        run_offer_gates(
            product_output,
            (offer,),
            pricing,
            (*evidence, evidence[0]),
            display_currency="USD",
        )
    with pytest.raises(TypeError, match="tuple"):
        run_offer_gates(
            product_output,
            (offer,),
            pricing,
            list(evidence),  # type: ignore[arg-type]
            display_currency="USD",
        )


@pytest.mark.parametrize(
    ("nested_model", "field_name", "bad_value"),
    [
        ("offer", "market", ""),
        ("binding", "evidence_id", ""),
        ("cost", "evidence_id", ""),
    ],
)
def test_mutated_offer_nested_models_fail_at_the_public_boundary(
    nested_model: str,
    field_name: str,
    bad_value: object,
) -> None:
    context = _context()
    offer = _offer()
    candidate = _pricing_candidate(offer, context)
    evidence = _offer_evidence(offer)
    if nested_model == "offer":
        target: object = offer
    elif nested_model == "binding":
        target = offer.field_evidence[0]
    else:
        target = offer.cost_components.item_price
    object.__setattr__(target, field_name, bad_value)

    with pytest.raises(TypeError, match=r"offer invariants|pricing"):
        run_offer_gates(
            _product_output((_product(),), context),
            (offer,),
            (candidate,),
            evidence,
            display_currency="USD",
        )


def test_success_and_unknown_failure_must_exactly_correspond_to_the_offer() -> None:
    context = _context()
    first = _offer("offer-1")
    second = _offer("offer-2")
    first_pricing = _pricing_candidate(first, context).pricing

    with pytest.raises(ValueError, match="components must exactly match"):
        OfferPricingCandidate(offer=second, pricing=first_pricing)

    unknown_offer = _offer(unknown=("shipping", "CARRIER_NOT_DISCLOSED"))
    wrong_detail = PricingFailure(
        code=PricingFailureCode.UNKNOWN_COST,
        unknown_costs=(UnknownCostDetail(component="shipping", reason="DIFFERENT_REASON"),),
    )
    with pytest.raises(ValueError, match="details must exactly match"):
        OfferPricingCandidate(offer=unknown_offer, pricing=wrong_detail)


@pytest.mark.parametrize(
    "failure_code",
    [
        PricingFailureCode.MISSING_EXCHANGE_RATE,
        PricingFailureCode.DECIMAL_ARITHMETIC,
    ],
)
def test_fx_failure_is_invalid_for_an_offer_with_unknown_cost(
    failure_code: PricingFailureCode,
) -> None:
    offer = _offer(unknown=("duty", "DUTY_NOT_DISCLOSED"))
    failure = (
        PricingFailure(
            code=failure_code,
            missing_currencies=("USD",),
        )
        if failure_code is PricingFailureCode.MISSING_EXCHANGE_RATE
        else PricingFailure(code=failure_code)
    )

    with pytest.raises(ValueError, match="UNKNOWN_COST"):
        OfferPricingCandidate(offer=offer, pricing=failure)


def test_mutated_pricing_trace_fails_closed_before_offer_gates() -> None:
    context = _context()
    offer = _offer()
    candidate = _pricing_candidate(offer, context)
    assert type(candidate.pricing) is LandedCost
    object.__setattr__(candidate.pricing.trace, "snapshot_version", "m0-v2")

    with pytest.raises(ValueError, match="snapshot and currency"):
        _run(
            (offer,),
            context,
            pricing=(candidate,),
        )


@pytest.mark.parametrize("case", ["display_currency", "budget_currency", "budget_max"])
def test_pricing_request_contract_mismatch_is_a_boundary_error(case: str) -> None:
    context = _context(budget="800.00", budget_currency="EUR")
    offer = _offer(item_price="100.00")
    if case == "display_currency":
        submitted = _pricing_candidate(
            offer,
            context,
            display_currency="EUR",
        )
    elif case == "budget_currency":
        submitted = _pricing_candidate(
            offer,
            _context(budget="800.00", budget_currency="USD"),
        )
    else:
        submitted = _pricing_candidate(
            offer,
            _context(budget="801.00", budget_currency="EUR"),
        )

    with pytest.raises(ValueError, match=r"currency|maximum"):
        run_offer_gates(
            _product_output((_product(),), context),
            (offer,),
            (submitted,),
            _offer_evidence(offer),
            display_currency="USD",
        )
