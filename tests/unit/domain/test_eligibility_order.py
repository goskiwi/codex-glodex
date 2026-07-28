from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass
from decimal import Decimal
from inspect import Parameter, signature

import pytest

import glodex.domain.eligibility as eligibility
from glodex.domain.eligibility import (
    OFFER_GATE_ORDER,
    PRODUCT_GATE_ORDER,
    EligibilityContext,
    EligibilityOutput,
    EligibleCandidates,
    GateResult,
    OfferGateId,
    ProductGateId,
    ProductGateOutput,
    _run_offer_gates,
    _run_product_gates,
    scorer_input,
)
from glodex.domain.intent import (
    BudgetMax,
    InterpretedRequest,
    PreferredCriterion,
    SourceSpan,
    TargetCategory,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec("GLO-P0-006", "AC-002"),
]


@dataclass(frozen=True, slots=True)
class _PrepricedCandidate:
    product_id: str
    category: str
    exact_cost: Decimal


@dataclass(frozen=True, slots=True)
class _MutableGraphCandidate:
    product_id: str
    metadata: object


def _build_prepriced_candidate(
    product_id: str,
    *,
    category: str = "laptop",
    exact_cost: str = "800.00",
) -> _PrepricedCandidate:
    return _PrepricedCandidate(
        product_id=product_id,
        category=category,
        exact_cost=Decimal(exact_cost),
    )


def _request_with_preference(value: str) -> InterpretedRequest:
    preference_text = {
        "lightweight": "轻薄",
        "portable": "便携",
    }[value]
    return InterpretedRequest(
        required=(
            TargetCategory(
                category="laptop",
                source_span=SourceSpan(start=0, end=3, text="笔记本"),
            ),
            BudgetMax(
                amount=Decimal("1000"),
                currency="CNY",
                source_span=SourceSpan(start=4, end=12, text="预算1000元"),
            ),
        ),
        preferred=(
            PreferredCriterion(
                value=value,
                source_span=SourceSpan(start=13, end=15, text=preference_text),
            ),
        ),
        parser_version="test-v1",
    )


def _product_reasons(
    gate: ProductGateId,
    candidate: _PrepricedCandidate,
    context: EligibilityContext,
) -> tuple[str, ...]:
    target = next(
        constraint for constraint in context.required if isinstance(constraint, TargetCategory)
    )
    if gate is ProductGateId.CATEGORY and candidate.category != target.category:
        return ("category_mismatch",)
    return ()


def _offer_reasons(
    gate: OfferGateId,
    candidate: _PrepricedCandidate,
    context: EligibilityContext,
) -> tuple[str, ...]:
    budget = next(
        constraint for constraint in context.required if isinstance(constraint, BudgetMax)
    )
    if gate is OfferGateId.BUDGET and candidate.exact_cost > budget.amount:
        return ("over_budget",)
    return ()


def _eligible_output(
    candidates: tuple[_PrepricedCandidate, ...],
    context: EligibilityContext,
) -> tuple[
    ProductGateOutput[_PrepricedCandidate],
    EligibilityOutput[_PrepricedCandidate],
]:
    products = _run_product_gates(candidates, context, _product_reasons)
    return products, _run_offer_gates(products, _offer_reasons)


def test_gate_order_is_fixed_and_gate_results_are_immutable() -> None:
    candidate = _build_prepriced_candidate("eligible")
    context = EligibilityContext.from_interpreted_request(_request_with_preference("lightweight"))

    product_output, output = _eligible_output((candidate,), context)

    assert PRODUCT_GATE_ORDER == (
        ProductGateId.CATEGORY,
        ProductGateId.ENTITY_KIND,
        ProductGateId.EXCLUSION,
        ProductGateId.REQUIRED_EVIDENCE,
    )
    assert OFFER_GATE_ORDER == (
        OfferGateId.SOURCE,
        OfferGateId.STOCK,
        OfferGateId.COST_COMPLETENESS,
        OfferGateId.EXCHANGE_RATE,
        OfferGateId.BUDGET,
    )
    assert tuple(result.gate for result in product_output.gate_results) == PRODUCT_GATE_ORDER
    assert tuple(result.gate for result in output.offer_gate_results) == OFFER_GATE_ORDER
    assert all(result.before_count == 1 for result in product_output.gate_results)
    assert all(result.after_count == 1 for result in output.offer_gate_results)
    with pytest.raises(FrozenInstanceError):
        product_output.gate_results[0].kept = ()  # type: ignore[misc]


def test_rejected_candidates_never_reach_later_gates_or_scorer() -> None:
    wrong_category = _build_prepriced_candidate("wrong-category", category="camera")
    over_budget = _build_prepriced_candidate("over-budget", exact_cost="1000.01")
    eligible = _build_prepriced_candidate("eligible", exact_cost="1000.00")
    candidates = (wrong_category, over_budget, eligible)
    context = EligibilityContext.from_interpreted_request(_request_with_preference("lightweight"))
    product_calls: list[tuple[ProductGateId, str]] = []
    offer_calls: list[tuple[OfferGateId, str]] = []

    def product_reasons(
        gate: ProductGateId,
        candidate: _PrepricedCandidate,
        gate_context: EligibilityContext,
    ) -> tuple[str, ...]:
        product_calls.append((gate, candidate.product_id))
        return _product_reasons(gate, candidate, gate_context)

    def offer_reasons(
        gate: OfferGateId,
        candidate: _PrepricedCandidate,
        gate_context: EligibilityContext,
    ) -> tuple[str, ...]:
        offer_calls.append((gate, candidate.product_id))
        return _offer_reasons(gate, candidate, gate_context)

    product_output = _run_product_gates(candidates, context, product_reasons)
    output = _run_offer_gates(product_output, offer_reasons)

    assert tuple(
        gate for gate, product_id in product_calls if product_id == wrong_category.product_id
    ) == (ProductGateId.CATEGORY,)
    assert all(product_id != wrong_category.product_id for _, product_id in offer_calls)
    assert (
        tuple(gate for gate, product_id in offer_calls if product_id == over_budget.product_id)
        == OFFER_GATE_ORDER
    )
    assert output.candidates == (eligible,)
    assert {
        rejected.candidate.product_id
        for result in (*output.product_gate_results, *output.offer_gate_results)
        for rejected in result.rejected
    } == {"wrong-category", "over-budget"}


def test_preferred_is_absent_from_eligibility_context_and_cannot_change_membership() -> None:
    candidates = (
        _build_prepriced_candidate("eligible"),
        _build_prepriced_candidate("over-budget", exact_cost="1000.01"),
    )
    lightweight_context = EligibilityContext.from_interpreted_request(
        _request_with_preference("lightweight")
    )
    portable_context = EligibilityContext.from_interpreted_request(
        _request_with_preference("portable")
    )

    _, lightweight_output = _eligible_output(candidates, lightweight_context)
    _, portable_output = _eligible_output(candidates, portable_context)

    assert lightweight_context == portable_context
    assert not hasattr(lightweight_context, "preferred")
    assert lightweight_output.candidates == portable_output.candidates


def test_offer_and_scorer_boundaries_require_nominal_pipeline_outputs() -> None:
    candidate = _build_prepriced_candidate("eligible")
    context = EligibilityContext.from_interpreted_request(_request_with_preference("lightweight"))
    product_output = _run_product_gates((candidate,), context, _product_reasons)
    eligibility_output = _run_offer_gates(product_output, _offer_reasons)

    with pytest.raises(TypeError, match="internal product gates"):
        ProductGateOutput()  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="internal offer gates"):
        EligibilityOutput()  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="scorer_input"):
        EligibleCandidates()  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="ProductGateOutput"):
        _run_offer_gates((candidate,), _offer_reasons)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="EligibilityOutput"):
        scorer_input(product_output)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="assembled EligibilityOutput"):
        scorer_input(eligibility_output)


class _StringReason(str):
    pass


@pytest.mark.parametrize(
    ("reasons", "error"),
    [
        ([], TypeError),
        (("",), ValueError),
        (("duplicate", "duplicate"), ValueError),
        ((_StringReason("spoofed"),), ValueError),
    ],
)
def test_gate_predicate_reasons_fail_closed(
    reasons: object,
    error: type[Exception],
) -> None:
    candidate = _build_prepriced_candidate("eligible")
    context = EligibilityContext.from_interpreted_request(_request_with_preference("lightweight"))

    def hostile_predicate(
        gate: ProductGateId,
        value: _PrepricedCandidate,
        gate_context: EligibilityContext,
    ) -> tuple[str, ...]:
        del gate, value, gate_context
        return reasons  # type: ignore[return-value]

    with pytest.raises(error):
        _run_product_gates((candidate,), context, hostile_predicate)


def test_gate_result_rejects_an_identity_substitution_with_matching_counts() -> None:
    candidate = _build_prepriced_candidate("candidate")
    equal_but_injected = _build_prepriced_candidate("candidate")

    with pytest.raises(ValueError, match="partition"):
        GateResult(
            gate=ProductGateId.CATEGORY,
            before=(candidate,),
            kept=(equal_but_injected,),
            rejected=(),
        )


def test_forged_nominal_outputs_cannot_cross_pipeline_boundaries() -> None:
    candidate = _build_prepriced_candidate("eligible")
    context = EligibilityContext.from_interpreted_request(_request_with_preference("lightweight"))
    forged_product_output = object.__new__(ProductGateOutput)
    object.__setattr__(forged_product_output, "context", context)
    object.__setattr__(forged_product_output, "candidates", (candidate,))
    object.__setattr__(forged_product_output, "gate_results", ())
    forged_eligibility_output = object.__new__(EligibilityOutput)
    object.__setattr__(forged_eligibility_output, "context", context)
    object.__setattr__(forged_eligibility_output, "candidates", (candidate,))
    object.__setattr__(forged_eligibility_output, "product_gate_results", ())
    object.__setattr__(forged_eligibility_output, "offer_gate_results", ())

    assert not hasattr(ProductGateOutput, "_create")
    assert not hasattr(EligibilityOutput, "_create")
    assert not hasattr(EligibleCandidates, "_create")
    with pytest.raises(TypeError, match="ProductGateOutput"):
        _run_offer_gates(forged_product_output, _offer_reasons)
    with pytest.raises(TypeError, match="EligibilityOutput"):
        scorer_input(forged_eligibility_output)


def test_public_concrete_runners_have_no_arbitrary_predicate_or_relaxation_entry() -> None:
    assert "run_product_gates" in eligibility.__all__
    assert "run_offer_gates" in eligibility.__all__
    assert "_run_offer_gates" not in eligibility.__all__
    parameters = signature(eligibility.run_product_gates).parameters
    offer_parameters = signature(eligibility.run_offer_gates).parameters

    assert tuple(parameters) == ("products", "context", "evidence")
    assert tuple(offer_parameters) == (
        "product_output",
        "offers",
        "pricing",
        "evidence",
        "display_currency",
    )
    assert "predicate" not in parameters
    assert "predicate" not in offer_parameters
    assert "relax" not in parameters
    assert "relax" not in offer_parameters
    assert all(
        parameter.kind not in (Parameter.VAR_POSITIONAL, Parameter.VAR_KEYWORD)
        for parameter in (*parameters.values(), *offer_parameters.values())
    )
    context = EligibilityContext(required=())
    with pytest.raises(TypeError):
        eligibility.run_product_gates(
            (),
            context,
            (),
            lambda *_: (),  # type: ignore[call-arg]
        )
    with pytest.raises(TypeError):
        eligibility.run_product_gates(
            (),
            context,
            (),
            relax=True,  # type: ignore[call-arg]
        )


@pytest.mark.parametrize("mutable_value", [{}, [], set()])
def test_mutable_candidate_graph_is_rejected_before_a_predicate_runs(
    mutable_value: object,
) -> None:
    candidate = _MutableGraphCandidate(
        product_id="mutable",
        metadata=mutable_value,
    )
    context = EligibilityContext.from_interpreted_request(_request_with_preference("lightweight"))
    calls = 0

    def predicate(
        gate: ProductGateId,
        value: _MutableGraphCandidate,
        gate_context: EligibilityContext,
    ) -> tuple[str, ...]:
        nonlocal calls
        del gate, value, gate_context
        calls += 1
        return ()

    with pytest.raises(TypeError, match="deeply immutable"):
        _run_product_gates((candidate,), context, predicate)

    assert calls == 0


def test_non_dataclass_candidate_is_rejected_before_a_predicate_runs() -> None:
    context = EligibilityContext.from_interpreted_request(_request_with_preference("lightweight"))
    calls = 0

    def predicate(
        gate: ProductGateId,
        value: object,
        gate_context: EligibilityContext,
    ) -> tuple[str, ...]:
        nonlocal calls
        del gate, value, gate_context
        calls += 1
        return ()

    with pytest.raises(TypeError, match="frozen slots dataclass"):
        _run_product_gates(({"product_id": "mutable"},), context, predicate)

    assert calls == 0


@pytest.mark.parametrize("target_gate", PRODUCT_GATE_ORDER)
def test_product_predicate_cannot_persistently_mutate_at_any_gate(
    target_gate: ProductGateId,
) -> None:
    candidate = _build_prepriced_candidate("candidate")
    context = EligibilityContext.from_interpreted_request(_request_with_preference("lightweight"))

    def mutating_predicate(
        gate: ProductGateId,
        value: _PrepricedCandidate,
        gate_context: EligibilityContext,
    ) -> tuple[str, ...]:
        del gate_context
        if gate is target_gate:
            object.__setattr__(value, "category", "camera")
        return ()

    with pytest.raises(TypeError, match="must not mutate"):
        _run_product_gates((candidate,), context, mutating_predicate)


@pytest.mark.parametrize("target_gate", OFFER_GATE_ORDER)
def test_offer_predicate_cannot_persistently_mutate_at_any_gate(
    target_gate: OfferGateId,
) -> None:
    candidate = _build_prepriced_candidate("candidate")
    context = EligibilityContext.from_interpreted_request(_request_with_preference("lightweight"))
    product_output = _run_product_gates((candidate,), context, _product_reasons)

    def mutating_predicate(
        gate: OfferGateId,
        value: _PrepricedCandidate,
        gate_context: EligibilityContext,
    ) -> tuple[str, ...]:
        del gate_context
        if gate is target_gate:
            object.__setattr__(value, "exact_cost", Decimal("9999.00"))
        return ()

    with pytest.raises(TypeError, match="must not mutate"):
        _run_offer_gates(product_output, mutating_predicate)


def test_structure_rechecks_are_per_gate_not_per_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = EligibilityContext.from_interpreted_request(_request_with_preference("lightweight"))
    original_signature = eligibility._structure_signature
    signature_calls = 0

    def tracked_signature(*values: object) -> bytes:
        nonlocal signature_calls
        signature_calls += 1
        return original_signature(*values)

    monkeypatch.setattr(eligibility, "_structure_signature", tracked_signature)

    def calls_for(candidate_count: int) -> int:
        before = signature_calls
        candidates = tuple(
            _build_prepriced_candidate(f"candidate-{index}") for index in range(candidate_count)
        )
        _run_product_gates(candidates, context, _product_reasons)
        return signature_calls - before

    assert calls_for(1) == calls_for(8)


def test_product_boundary_rejects_forgery_and_post_mint_tampering() -> None:
    candidate = _build_prepriced_candidate("eligible")
    injected = _build_prepriced_candidate("injected")
    context = EligibilityContext.from_interpreted_request(_request_with_preference("lightweight"))
    legal = _run_product_gates((candidate,), context, _product_reasons)
    forged = object.__new__(ProductGateOutput)
    object.__setattr__(forged, "context", legal.context)
    object.__setattr__(forged, "candidates", legal.candidates)
    object.__setattr__(forged, "gate_results", legal.gate_results)

    assert "_provenance" not in ProductGateOutput.__slots__
    assert "_provenance" not in EligibilityOutput.__slots__
    with pytest.raises(TypeError, match="ProductGateOutput"):
        _run_offer_gates(forged, _offer_reasons)

    object.__setattr__(legal, "candidates", (injected,))
    with pytest.raises(TypeError, match="ProductGateOutput"):
        _run_offer_gates(legal, _offer_reasons)


def test_boundaries_reject_candidate_and_eligibility_output_tampering() -> None:
    product_candidate = _build_prepriced_candidate("product-candidate")
    context = EligibilityContext.from_interpreted_request(_request_with_preference("lightweight"))
    product_output = _run_product_gates((product_candidate,), context, _product_reasons)
    object.__setattr__(product_candidate, "exact_cost", Decimal("9999.00"))

    with pytest.raises(TypeError, match="ProductGateOutput"):
        _run_offer_gates(product_output, _offer_reasons)

    eligible_candidate = _build_prepriced_candidate("eligible-candidate")
    clean_product_output = _run_product_gates(
        (eligible_candidate,),
        context,
        _product_reasons,
    )
    output = _run_offer_gates(clean_product_output, _offer_reasons)
    object.__setattr__(output, "candidates", (_build_prepriced_candidate("injected"),))

    with pytest.raises(TypeError, match="EligibilityOutput"):
        scorer_input(output)

    scorer_candidate = _build_prepriced_candidate("scorer-candidate")
    scorer_product_output = _run_product_gates(
        (scorer_candidate,),
        context,
        _product_reasons,
    )
    scorer_output = _run_offer_gates(scorer_product_output, _offer_reasons)
    object.__setattr__(scorer_candidate, "category", "camera")

    with pytest.raises(TypeError, match="EligibilityOutput"):
        scorer_input(scorer_output)
