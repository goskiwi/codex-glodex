from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import ROUND_UP, Decimal, localcontext

import pytest

from glodex.domain.catalog import CostComponents, ExchangeRate, ExchangeRateTable
from glodex.domain.pricing import (
    PRICING_ALGORITHM_VERSION,
    CostBreakdown,
    CostComponentTrace,
    ExchangeRateTrace,
    KnownCost,
    LandedCost,
    PricingFailure,
    PricingFailureCode,
    PricingTrace,
    UnknownCost,
    calculate_landed_cost,
    pricing_context,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec("GLO-P0-005", "AC-005", "AC-008"),
]

SNAPSHOT_VERSION = "m0-v1"


def _known(component: str, amount: str) -> KnownCost:
    return KnownCost(
        amount=Decimal(amount),
        evidence_id=f"ev-cost-{component}",
    )


def _complete_costs(
    *,
    currency: str = "EUR",
    item_price: str = "100",
    shipping: str = "10",
    tax: str = "5",
    duty: str = "0.00",
) -> CostComponents:
    return CostComponents(
        currency=currency,
        item_price=_known("item_price", item_price),
        shipping=_known("shipping", shipping),
        tax=_known("tax", tax),
        duty=_known("duty", duty),
    )


def _rate(
    currency: str,
    base_per_unit: str,
    minor_units: int,
    ordinal: int,
) -> ExchangeRate:
    return ExchangeRate(
        snapshot_version=SNAPSHOT_VERSION,
        currency=currency,
        base_per_unit=Decimal(base_per_unit),
        minor_units=minor_units,
        evidence_id=f"ev-rate-{currency.lower()}",
        snapshot_ordinal=ordinal,
    )


def _rates(*currencies: str) -> ExchangeRateTable:
    definitions = {
        "USD": ("1", 2),
        "EUR": ("1.2", 2),
        "GBP": ("1.5", 2),
        "JPY": ("0.01", 0),
        "CAD": ("0.75", 2),
    }
    ordered = ("USD", *currencies)
    unique = tuple(dict.fromkeys(ordered))
    return ExchangeRateTable(
        snapshot_version=SNAPSHOT_VERSION,
        base_currency="USD",
        rates=tuple(
            _rate(currency, *definitions[currency], ordinal)
            for ordinal, currency in enumerate(unique)
        ),
    )


def _require_success(result: LandedCost | PricingFailure) -> LandedCost:
    assert type(result) is LandedCost
    return result


def _require_failure(result: LandedCost | PricingFailure) -> PricingFailure:
    assert type(result) is PricingFailure
    return result


def test_base_per_unit_formula_produces_display_budget_and_quantized_totals() -> None:
    result = _require_success(
        calculate_landed_cost(
            _complete_costs(),
            _rates("EUR", "GBP", "JPY"),
            display_currency="GBP",
            budget_max=Decimal("13800"),
            budget_currency="JPY",
        )
    )

    assert result.kind == "SUCCESS"
    assert result.display_currency == "GBP"
    assert result.budget_currency == "JPY"
    assert result.display_exact == Decimal("92")
    assert result.budget_exact == Decimal("13800")
    assert result.display_quantized == Decimal("92.00")
    assert result.budget_max == Decimal("13800")
    assert result.within_budget is True
    assert result.display_exact_json == "92"
    assert result.budget_exact_json == "13800"
    assert result.display_json == "92.00"


def test_each_component_is_converted_without_quantization_before_sum() -> None:
    costs = _complete_costs(
        currency="USD",
        item_price="0.005",
        shipping="0.005",
        tax="0.005",
        duty="0.005",
    )

    result = _require_success(
        calculate_landed_cost(
            costs,
            _rates(),
            display_currency="USD",
        )
    )

    assert tuple(item.display_exact for item in result.trace.components) == (
        Decimal("0.005"),
        Decimal("0.005"),
        Decimal("0.005"),
        Decimal("0.005"),
    )
    assert result.display_exact == Decimal("0.020")
    assert result.display_quantized == Decimal("0.02")


@pytest.mark.parametrize(
    ("budget_max", "expected"),
    [
        (Decimal("1.005"), True),
        (Decimal("1.004"), False),
    ],
)
def test_budget_comparison_is_exact_inclusive_and_ignores_display_rounding(
    budget_max: Decimal,
    expected: bool,
) -> None:
    result = _require_success(
        calculate_landed_cost(
            _complete_costs(
                currency="USD",
                item_price="1.005",
                shipping="0.00",
                tax="0.00",
                duty="0.00",
            ),
            _rates(),
            display_currency="USD",
            budget_max=budget_max,
        )
    )

    assert result.display_quantized == Decimal("1.00")
    assert result.budget_exact == Decimal("1.005")
    assert result.within_budget is expected


def test_undeclared_budget_currency_inherits_display_currency() -> None:
    result = _require_success(
        calculate_landed_cost(
            _complete_costs(),
            _rates("EUR", "GBP"),
            display_currency="GBP",
            budget_max=Decimal("92"),
        )
    )

    assert result.budget_currency == "GBP"
    assert result.budget_exact == result.display_exact == Decimal("92")
    assert result.trace.budget_rate == result.trace.display_rate
    assert result.within_budget is True


def test_no_budget_still_produces_budget_currency_exact_value_without_a_verdict() -> None:
    result = _require_success(
        calculate_landed_cost(
            _complete_costs(),
            _rates("EUR", "GBP"),
            display_currency="GBP",
        )
    )

    assert result.budget_currency == "GBP"
    assert result.budget_exact == Decimal("92")
    assert result.budget_max is None
    assert result.within_budget is None


def test_unknown_costs_return_an_explicit_failure_with_verbatim_reasons() -> None:
    costs = CostComponents(
        currency="CAD",
        item_price=_known("item_price", "100"),
        shipping=UnknownCost(reason="CARRIER:NOT_DISCLOSED"),
        tax=UnknownCost(reason="UPSTREAM/TAX_PENDING"),
        duty=_known("duty", "0.00"),
    )

    result = _require_failure(
        calculate_landed_cost(
            costs,
            _rates(),
            display_currency="GBP",
            budget_max=Decimal("90"),
            budget_currency="JPY",
        )
    )

    assert result.kind == "FAILURE"
    assert result.code is PricingFailureCode.UNKNOWN_COST
    assert tuple((item.component, item.reason) for item in result.unknown_costs) == (
        ("shipping", "CARRIER:NOT_DISCLOSED"),
        ("tax", "UPSTREAM/TAX_PENDING"),
    )
    assert result.missing_currencies == ()
    assert not hasattr(result, "display_exact")


def test_all_missing_direct_rates_are_reported_once_in_semantic_order() -> None:
    result = _require_failure(
        calculate_landed_cost(
            _complete_costs(currency="CAD"),
            _rates("EUR"),
            display_currency="GBP",
            budget_max=Decimal("100"),
            budget_currency="JPY",
        )
    )

    assert result.code is PricingFailureCode.MISSING_EXCHANGE_RATE
    assert result.missing_currencies == ("CAD", "GBP", "JPY")
    assert result.unknown_costs == ()


def test_same_currency_is_not_assumed_to_have_rate_one_when_snapshot_omits_it() -> None:
    result = _require_failure(
        calculate_landed_cost(
            _complete_costs(currency="CAD"),
            _rates("EUR"),
            display_currency="CAD",
        )
    )

    assert result.code is PricingFailureCode.MISSING_EXCHANGE_RATE
    assert result.missing_currencies == ("CAD",)


def test_pricing_v1_trace_is_evidence_complete_and_recomputable() -> None:
    result = _require_success(
        calculate_landed_cost(
            _complete_costs(),
            _rates("EUR", "GBP", "JPY"),
            display_currency="GBP",
            budget_max=Decimal("14000"),
            budget_currency="JPY",
        )
    )
    trace = result.trace

    assert PRICING_ALGORITHM_VERSION == "pricing-v1"
    assert trace.algorithm_version == "pricing-v1"
    assert trace.snapshot_version == SNAPSHOT_VERSION
    assert trace.base_currency == "USD"
    assert trace.source_currency == "EUR"
    assert trace.display_currency == "GBP"
    assert trace.budget_currency == "JPY"
    assert trace.source_rate.evidence_id == "ev-rate-eur"
    assert trace.display_rate.evidence_id == "ev-rate-gbp"
    assert trace.budget_rate.evidence_id == "ev-rate-jpy"
    assert tuple(item.component for item in trace.components) == (
        "item_price",
        "shipping",
        "tax",
        "duty",
    )
    assert tuple(item.evidence_id for item in trace.components) == (
        "ev-cost-item_price",
        "ev-cost-shipping",
        "ev-cost-tax",
        "ev-cost-duty",
    )

    with localcontext(pricing_context()):
        for item in trace.components:
            assert item.display_exact == (
                item.source_amount
                * trace.source_rate.base_per_unit
                / trace.display_rate.base_per_unit
            )
            assert item.budget_exact == (
                item.source_amount
                * trace.source_rate.base_per_unit
                / trace.budget_rate.base_per_unit
            )
        assert (
            sum(
                (item.display_exact for item in trace.components),
                start=Decimal(0),
            )
            == result.display_exact
        )
        assert (
            sum(
                (item.budget_exact for item in trace.components),
                start=Decimal(0),
            )
            == result.budget_exact
        )


def test_pricing_results_and_trace_are_frozen_tuple_only_models() -> None:
    result = _require_success(
        calculate_landed_cost(
            _complete_costs(),
            _rates("EUR", "GBP"),
            display_currency="GBP",
        )
    )

    assert isinstance(result.trace.components, tuple)
    assert all(type(item) is CostComponentTrace for item in result.trace.components)
    assert not hasattr(result, "__dict__")
    assert not hasattr(result.trace, "__dict__")
    with pytest.raises(FrozenInstanceError):
        result.display_exact = Decimal("0")  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.trace.snapshot_version = "changed"  # type: ignore[misc]


def test_calculation_rejects_a_cost_breakdown_subclass_that_overrides_items() -> None:
    class BypassCostBreakdown(CostBreakdown):
        def items(self) -> tuple:  # type: ignore[override]
            return _complete_costs(currency="USD").items()

    bypass = BypassCostBreakdown(
        currency="USD",
        item_price=UnknownCost(reason="HIDDEN"),
        shipping=UnknownCost(reason="HIDDEN"),
        tax=UnknownCost(reason="HIDDEN"),
        duty=UnknownCost(reason="HIDDEN"),
    )

    with pytest.raises(TypeError):
        calculate_landed_cost(
            bypass,
            _rates(),
            display_currency="USD",
        )


def test_pricing_trace_revalidates_forged_nested_component_and_rate() -> None:
    result = _require_success(
        calculate_landed_cost(
            _complete_costs(),
            _rates("EUR", "GBP"),
            display_currency="GBP",
        )
    )
    trace = result.trace
    forged_component = object.__new__(CostComponentTrace)
    object.__setattr__(forged_component, "component", "item_price")
    object.__setattr__(forged_component, "source_amount", Decimal("-1"))
    object.__setattr__(forged_component, "evidence_id", "")
    object.__setattr__(forged_component, "display_exact", Decimal("80"))
    object.__setattr__(forged_component, "budget_exact", Decimal("80"))
    forged_rate = object.__new__(ExchangeRateTrace)
    object.__setattr__(forged_rate, "currency", "GBP")
    object.__setattr__(forged_rate, "base_per_unit", Decimal("-1"))
    object.__setattr__(forged_rate, "minor_units", 2)
    object.__setattr__(forged_rate, "evidence_id", "")
    object.__setattr__(forged_rate, "snapshot_ordinal", -1)

    with pytest.raises((TypeError, ValueError)):
        PricingTrace(
            snapshot_version=trace.snapshot_version,
            base_currency=trace.base_currency,
            source_currency=trace.source_currency,
            display_currency=trace.display_currency,
            budget_currency=trace.budget_currency,
            components=(forged_component, *trace.components[1:]),
            source_rate=trace.source_rate,
            display_rate=trace.display_rate,
            budget_rate=trace.budget_rate,
        )
    with pytest.raises((TypeError, ValueError)):
        PricingTrace(
            snapshot_version=trace.snapshot_version,
            base_currency=trace.base_currency,
            source_currency=trace.source_currency,
            display_currency=trace.display_currency,
            budget_currency=trace.budget_currency,
            components=trace.components,
            source_rate=trace.source_rate,
            display_rate=forged_rate,
            budget_rate=trace.budget_rate,
        )


def test_landed_cost_recomputes_totals_from_its_nested_trace() -> None:
    result = _require_success(
        calculate_landed_cost(
            _complete_costs(),
            _rates("EUR", "GBP"),
            display_currency="GBP",
        )
    )

    with pytest.raises(ValueError):
        LandedCost(
            display_currency=result.display_currency,
            budget_currency=result.budget_currency,
            display_exact=result.display_exact + Decimal("1"),
            budget_exact=result.budget_exact,
            display_quantized=result.display_quantized,
            budget_max=result.budget_max,
            within_budget=result.within_budget,
            trace=result.trace,
        )


def test_pricing_uses_local_context_and_does_not_mutate_the_caller() -> None:
    expected = calculate_landed_cost(
        _complete_costs(item_price="1", shipping="0.00", tax="0.00", duty="0.00"),
        _rates("EUR", "GBP", "JPY"),
        display_currency="GBP",
        budget_currency="JPY",
    )

    with localcontext() as caller:
        caller.prec = 6
        caller.rounding = ROUND_UP
        actual = calculate_landed_cost(
            _complete_costs(item_price="1", shipping="0.00", tax="0.00", duty="0.00"),
            _rates("EUR", "GBP", "JPY"),
            display_currency="GBP",
            budget_currency="JPY",
        )

        assert actual == expected
        assert caller.prec == 6
        assert caller.rounding == ROUND_UP


@pytest.mark.parametrize(
    ("budget_max", "error_type"),
    [
        (1, TypeError),
        (1.0, TypeError),
        (True, TypeError),
        (Decimal("-1"), ValueError),
        (Decimal("-0"), ValueError),
        (Decimal("NaN"), ValueError),
        (Decimal("Infinity"), ValueError),
    ],
)
def test_budget_max_rejects_non_decimal_and_invalid_values(
    budget_max: object,
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type):
        calculate_landed_cost(
            _complete_costs(),
            _rates("EUR", "GBP"),
            display_currency="GBP",
            budget_max=budget_max,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("display_currency", "budget_currency", "error_type"),
    [
        ("gbp", None, ValueError),
        ("美金", None, ValueError),
        (None, None, TypeError),
        ("GBP", "jpy", ValueError),
        ("GBP", 123, TypeError),
    ],
)
def test_pricing_rejects_invalid_currency_arguments(
    display_currency: object,
    budget_currency: object,
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type):
        calculate_landed_cost(
            _complete_costs(),
            _rates("EUR", "GBP", "JPY"),
            display_currency=display_currency,  # type: ignore[arg-type]
            budget_currency=budget_currency,  # type: ignore[arg-type]
        )


def test_decimal_arithmetic_failures_are_normalized_to_a_pricing_failure() -> None:
    result = _require_failure(
        calculate_landed_cost(
            _complete_costs(
                currency="USD",
                item_price="1E+100",
                shipping="0.00",
                tax="0.00",
                duty="0.00",
            ),
            _rates(),
            display_currency="USD",
        )
    )

    assert result.code is PricingFailureCode.DECIMAL_ARITHMETIC
