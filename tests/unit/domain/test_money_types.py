from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import (
    ROUND_HALF_EVEN,
    ROUND_UP,
    Decimal,
    DefaultContext,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
    localcontext,
)

import pytest

from glodex.domain.catalog import ExchangeRate
from glodex.domain.pricing import (
    PRICING_PRECISION,
    PRICING_ROUNDING,
    CostBreakdown,
    KnownCost,
    UnknownCost,
    canonical_exact_amount,
    format_display_amount,
    pricing_context,
    quantize_display_amount,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec("GLO-P0-005"),
]


def _known(amount: str = "10.00", evidence_id: str = "ev-cost") -> KnownCost:
    return KnownCost(amount=Decimal(amount), evidence_id=evidence_id)


def _rate(
    *,
    base_per_unit: object = Decimal("1"),
    minor_units: object = 2,
) -> ExchangeRate:
    return ExchangeRate(
        snapshot_version="m0-v1",
        currency="USD",
        base_per_unit=base_per_unit,  # type: ignore[arg-type]
        minor_units=minor_units,  # type: ignore[arg-type]
        evidence_id="ev-rate-usd",
        snapshot_ordinal=0,
    )


def test_known_cost_accepts_exact_decimal_and_evidenced_explicit_zero() -> None:
    amount = KnownCost(amount=Decimal("12.3400"), evidence_id="ev-item")
    zero = KnownCost(amount=Decimal("0.00"), evidence_id="ev-shipping")

    assert amount.amount == Decimal("12.3400")
    assert amount.kind == "KNOWN"
    assert zero.amount.as_tuple().exponent == -2
    assert zero.evidence_id == "ev-shipping"


@pytest.mark.parametrize(
    ("value", "error_type"),
    [
        (1, TypeError),
        (1.0, TypeError),
        (True, TypeError),
        ("1.00", TypeError),
        (Decimal("-1"), ValueError),
        (Decimal("-0.00"), ValueError),
        (Decimal("NaN"), ValueError),
        (Decimal("sNaN"), ValueError),
        (Decimal("Infinity"), ValueError),
        (Decimal("-Infinity"), ValueError),
    ],
)
def test_known_cost_rejects_non_decimal_non_finite_and_negative_amounts(
    value: object,
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type):
        KnownCost(amount=value, evidence_id="ev-cost")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "zero",
    [
        Decimal("0"),
        Decimal("0.0"),
        Decimal("0.000"),
        Decimal("0E+2"),
    ],
)
def test_known_zero_requires_the_explicit_two_decimal_representation(zero: Decimal) -> None:
    with pytest.raises(ValueError):
        KnownCost(amount=zero, evidence_id="ev-zero")


@pytest.mark.parametrize(
    ("evidence_id", "error_type"),
    [
        (None, TypeError),
        ("", ValueError),
        ("   ", ValueError),
        ("x" * 129, ValueError),
    ],
)
def test_every_known_cost_requires_a_bounded_evidence_id(
    evidence_id: object,
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type):
        KnownCost(
            amount=Decimal("0.00"),
            evidence_id=evidence_id,  # type: ignore[arg-type]
        )


def test_unknown_cost_is_a_distinct_evidence_free_variant() -> None:
    unknown = UnknownCost(reason="NOT_DISCLOSED")
    zero = KnownCost(amount=Decimal("0.00"), evidence_id="ev-zero")

    assert unknown.kind == "UNKNOWN"
    assert unknown != zero
    assert not hasattr(unknown, "amount")
    assert not hasattr(unknown, "evidence_id")


@pytest.mark.parametrize(
    ("reason", "error_type"),
    [
        (None, TypeError),
        ("", ValueError),
        ("   ", ValueError),
        ("x" * 129, ValueError),
    ],
)
def test_unknown_cost_requires_a_bounded_reason(
    reason: object,
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type):
        UnknownCost(reason=reason)  # type: ignore[arg-type]


def test_cost_discriminators_are_not_constructor_or_mutation_inputs() -> None:
    known = _known()
    unknown = UnknownCost(reason="NOT_DISCLOSED")

    with pytest.raises(TypeError):
        KnownCost(  # type: ignore[call-arg]
            amount=Decimal("1"),
            evidence_id="ev-cost",
            kind="UNKNOWN",
        )
    with pytest.raises(TypeError):
        UnknownCost(reason="NOT_DISCLOSED", kind="KNOWN")  # type: ignore[call-arg]
    with pytest.raises(FrozenInstanceError):
        known.kind = "UNKNOWN"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        unknown.kind = "KNOWN"  # type: ignore[misc]


def test_cost_breakdown_has_fixed_order_and_explicit_completeness() -> None:
    breakdown = CostBreakdown(
        currency="USD",
        item_price=_known("699.00", "ev-item"),
        shipping=_known("0.00", "ev-shipping"),
        tax=UnknownCost(reason="NOT_DISCLOSED"),
        duty=_known("0.00", "ev-duty"),
    )

    assert tuple(name for name, _ in breakdown.items()) == (
        "item_price",
        "shipping",
        "tax",
        "duty",
    )
    assert isinstance(breakdown.items(), tuple)
    assert not breakdown.is_complete
    assert breakdown.unknown_components == ("tax",)

    complete = CostBreakdown(
        currency="USD",
        item_price=_known("699.00", "ev-item"),
        shipping=_known("0.00", "ev-shipping"),
        tax=_known("55.92", "ev-tax"),
        duty=_known("0.00", "ev-duty"),
    )
    assert complete.is_complete
    assert complete.unknown_components == ()


@pytest.mark.parametrize("invalid_component", [None, Decimal("1"), {}, []])
def test_cost_breakdown_rejects_none_and_non_variant_components(
    invalid_component: object,
) -> None:
    with pytest.raises(TypeError):
        CostBreakdown(
            currency="USD",
            item_price=invalid_component,  # type: ignore[arg-type]
            shipping=_known("0.00", "ev-shipping"),
            tax=_known("0.00", "ev-tax"),
            duty=_known("0.00", "ev-duty"),
        )


@pytest.mark.parametrize(
    ("currency", "error_type"),
    [
        (None, TypeError),
        ("usd", ValueError),
        ("US", ValueError),
        ("美金", ValueError),
    ],
)
def test_cost_breakdown_requires_an_ascii_uppercase_currency(
    currency: object,
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type):
        CostBreakdown(
            currency=currency,  # type: ignore[arg-type]
            item_price=_known(),
            shipping=_known(),
            tax=_known(),
            duty=_known(),
        )


def test_money_models_are_frozen_and_do_not_expose_mutable_collections() -> None:
    known = _known()
    unknown = UnknownCost(reason="NOT_DISCLOSED")
    breakdown = CostBreakdown(
        currency="USD",
        item_price=known,
        shipping=known,
        tax=unknown,
        duty=known,
    )

    with pytest.raises(FrozenInstanceError):
        known.amount = Decimal("2")  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        unknown.reason = "CHANGED"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        breakdown.tax = known  # type: ignore[misc]
    assert not hasattr(known, "__dict__")
    assert not hasattr(unknown, "__dict__")
    assert not hasattr(breakdown, "__dict__")
    assert isinstance(breakdown.items(), tuple)


@pytest.mark.parametrize(
    "base_per_unit",
    [
        1,
        1.0,
        True,
        Decimal("0"),
        Decimal("-1"),
        Decimal("NaN"),
        Decimal("Infinity"),
    ],
)
def test_exchange_rate_rejects_non_decimal_non_positive_and_non_finite_values(
    base_per_unit: object,
) -> None:
    with pytest.raises(ValueError):
        _rate(base_per_unit=base_per_unit)


@pytest.mark.parametrize("minor_units", [True, -1, 5, 2.0])
def test_exchange_rate_rejects_illegal_minor_units(minor_units: object) -> None:
    with pytest.raises(ValueError):
        _rate(minor_units=minor_units)


def test_pricing_context_is_fresh_precision_28_and_half_even() -> None:
    first = pricing_context()
    second = pricing_context()

    assert PRICING_PRECISION == 28
    assert PRICING_ROUNDING == ROUND_HALF_EVEN
    assert first.prec == second.prec == 28
    assert first.rounding == second.rounding == ROUND_HALF_EVEN
    assert first is not second

    first.prec = 7
    first.rounding = ROUND_UP
    assert second.prec == 28
    assert second.rounding == ROUND_HALF_EVEN


def test_pricing_context_is_independent_from_mutable_default_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(DefaultContext, "prec", 3)
    monkeypatch.setattr(DefaultContext, "rounding", ROUND_UP)
    monkeypatch.setattr(DefaultContext, "Emin", -2)
    monkeypatch.setattr(DefaultContext, "Emax", 2)
    monkeypatch.setattr(DefaultContext, "capitals", 0)
    monkeypatch.setattr(DefaultContext, "clamp", 1)
    monkeypatch.setitem(DefaultContext.traps, Inexact, True)

    context = pricing_context()

    assert context.prec == 28
    assert context.rounding == ROUND_HALF_EVEN
    assert context.Emin == -999_999
    assert context.Emax == 999_999
    assert context.capitals == 1
    assert context.clamp == 0
    assert context.traps[InvalidOperation]
    assert context.traps[DivisionByZero]
    assert context.traps[Overflow]
    assert not context.traps[Inexact]
    assert not any(context.flags.values())
    assert format_display_amount(Decimal("1.225"), 2) == "1.22"


def test_cost_breakdown_revalidates_forged_nested_variants() -> None:
    forged_known = object.__new__(KnownCost)
    object.__setattr__(forged_known, "amount", Decimal("-1"))
    object.__setattr__(forged_known, "evidence_id", "")
    object.__setattr__(forged_known, "kind", "UNKNOWN")
    forged_unknown = object.__new__(UnknownCost)
    object.__setattr__(forged_unknown, "reason", "")
    object.__setattr__(forged_unknown, "kind", "KNOWN")

    with pytest.raises((TypeError, ValueError)):
        CostBreakdown(
            currency="USD",
            item_price=forged_known,
            shipping=_known("0.00", "ev-shipping"),
            tax=_known("0.00", "ev-tax"),
            duty=_known("0.00", "ev-duty"),
        )
    with pytest.raises((TypeError, ValueError)):
        CostBreakdown(
            currency="USD",
            item_price=_known(),
            shipping=_known("0.00", "ev-shipping"),
            tax=forged_unknown,
            duty=_known("0.00", "ev-duty"),
        )


def test_cost_completeness_fails_closed_after_nested_variant_tampering() -> None:
    known = _known()
    breakdown = CostBreakdown(
        currency="USD",
        item_price=known,
        shipping=_known("0.00", "ev-shipping"),
        tax=_known("0.00", "ev-tax"),
        duty=_known("0.00", "ev-duty"),
    )

    object.__setattr__(known, "kind", "UNKNOWN")

    assert not breakdown.is_complete


@pytest.mark.parametrize(
    "amount",
    [
        Decimal("1E+128"),
        Decimal("1E-127"),
        Decimal("9" * 129),
        Decimal("1E+1000000000"),
    ],
)
def test_domain_amounts_reject_fixed_point_output_over_128_characters(
    amount: Decimal,
) -> None:
    with pytest.raises(ValueError):
        KnownCost(amount=amount, evidence_id="ev-huge")
    with pytest.raises(ValueError):
        canonical_exact_amount(amount)


@pytest.mark.parametrize("amount", [Decimal("1E+127"), Decimal("1E-126")])
def test_domain_amount_fixed_point_boundary_is_128_characters(amount: Decimal) -> None:
    assert len(canonical_exact_amount(amount)) == 128


def test_canonical_length_handles_a_large_trailing_zero_coefficient() -> None:
    amount = Decimal(f"1.{'0' * 10_000}")

    known = KnownCost(amount=amount, evidence_id="ev-trailing-zeroes")

    assert known.amount is amount
    assert canonical_exact_amount(amount) == "1"


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (Decimal("0.00"), "0"),
        (Decimal("1"), "1"),
        (Decimal("1.2300"), "1.23"),
        (Decimal("1E+3"), "1000"),
        (Decimal("0.00100"), "0.001"),
        (Decimal("100.000"), "100"),
    ],
)
def test_canonical_exact_amount_is_fixed_point_without_redundant_zeroes(
    amount: Decimal,
    expected: str,
) -> None:
    rendered = canonical_exact_amount(amount)

    assert rendered == expected
    assert "e" not in rendered.lower()


@pytest.mark.parametrize(
    ("amount", "minor_units", "expected"),
    [
        (Decimal("1"), 2, "1.00"),
        (Decimal("1.2"), 2, "1.20"),
        (Decimal("1.225"), 2, "1.22"),
        (Decimal("1.235"), 2, "1.24"),
        (Decimal("2.5"), 0, "2"),
        (Decimal("3.5"), 0, "4"),
        (Decimal("1.23456"), 4, "1.2346"),
    ],
)
def test_display_amount_uses_half_even_and_preserves_minor_units(
    amount: Decimal,
    minor_units: int,
    expected: str,
) -> None:
    assert format_display_amount(amount, minor_units) == expected


def test_display_quantization_uses_a_local_context_without_mutating_the_caller() -> None:
    with localcontext() as caller:
        caller.prec = 6
        caller.rounding = ROUND_UP

        quantized = quantize_display_amount(Decimal("1.225"), 2)
        rendered = format_display_amount(Decimal("1.225"), 2)

        assert quantized == Decimal("1.22")
        assert rendered == "1.22"
        assert caller.prec == 6
        assert caller.rounding == ROUND_UP


@pytest.mark.parametrize(
    ("minor_units", "error_type"),
    [
        (True, TypeError),
        (2.0, TypeError),
        (-1, ValueError),
        (5, ValueError),
    ],
)
def test_display_formatting_rejects_illegal_minor_units(
    minor_units: object,
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type):
        format_display_amount(Decimal("1"), minor_units)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("amount", "error_type"),
    [
        (1, TypeError),
        (1.0, TypeError),
        (True, TypeError),
        (Decimal("-1"), ValueError),
        (Decimal("-0.00"), ValueError),
        (Decimal("NaN"), ValueError),
        (Decimal("Infinity"), ValueError),
    ],
)
def test_amount_formatters_reject_invalid_decimal_values(
    amount: object,
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type):
        canonical_exact_amount(amount)  # type: ignore[arg-type]
    with pytest.raises(error_type):
        format_display_amount(amount, 2)  # type: ignore[arg-type]


def test_display_quantization_normalizes_decimal_failures_to_value_error() -> None:
    with pytest.raises(ValueError):
        quantize_display_amount(Decimal("1E+100"), 2)
