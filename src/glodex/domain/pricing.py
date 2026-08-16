"""Strict immutable money values and deterministic decimal formatting."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DecimalException,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from glodex.domain.catalog import ExchangeRate, ExchangeRateTable

PRICING_PRECISION: Final = 28
PRICING_ROUNDING: Final = ROUND_HALF_EVEN
PRICING_ALGORITHM_VERSION: Final = "pricing-v1"
_MAX_COST_METADATA_LENGTH: Final = 128
_MAX_CANONICAL_AMOUNT_LENGTH: Final = 128
_MIN_MINOR_UNITS: Final = 0
_MAX_MINOR_UNITS: Final = 4
_PRICING_EMIN: Final = -999_999
_PRICING_EMAX: Final = 999_999

type CostComponentName = Literal["item_price", "shipping", "tax", "duty"]
type BudgetMode = Literal["maximum", "around", "range"]
_COST_COMPONENT_ORDER: Final[tuple[CostComponentName, ...]] = (
    "item_price",
    "shipping",
    "tax",
    "duty",
)


@dataclass(frozen=True, slots=True)
class KnownCost:
    """A non-negative exact amount supported by one evidence record."""

    amount: Decimal
    evidence_id: str
    kind: Literal["KNOWN"] = field(default="KNOWN", init=False)

    def __post_init__(self) -> None:
        _validate_known_cost(self)


@dataclass(frozen=True, slots=True)
class UnknownCost:
    """An explicitly unknown amount that cannot be treated as known zero."""

    reason: str
    kind: Literal["UNKNOWN"] = field(default="UNKNOWN", init=False)

    def __post_init__(self) -> None:
        _validate_unknown_cost(self)


type CostValue = KnownCost | UnknownCost


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """The fixed, ordered set of landed-cost inputs in one source currency."""

    currency: str
    item_price: CostValue
    shipping: CostValue
    tax: CostValue
    duty: CostValue

    def __post_init__(self) -> None:
        _require_currency(self.currency)
        for name, value in _fixed_cost_items(self):
            if type(value) is KnownCost:
                _validate_known_cost(value)
            elif type(value) is UnknownCost:
                _validate_unknown_cost(value)
            else:
                raise TypeError(f"{name} must be KnownCost or UnknownCost")

    def items(self) -> tuple[tuple[CostComponentName, CostValue], ...]:
        """Return cost components in their stable calculation order."""

        return _fixed_cost_items(self)

    @property
    def is_complete(self) -> bool:
        """Whether every component is a known, evidenced amount."""

        for _, value in _fixed_cost_items(self):
            if type(value) is not KnownCost:
                return False
            try:
                _validate_known_cost(value)
            except (TypeError, ValueError):
                return False
        return True

    @property
    def unknown_components(self) -> tuple[CostComponentName, ...]:
        """Return unknown component names in stable calculation order."""

        return tuple(name for name, value in _fixed_cost_items(self) if type(value) is UnknownCost)


class PricingFailureCode(StrEnum):
    """Fail-closed business outcomes produced by the pricing calculation."""

    UNKNOWN_COST = "UNKNOWN_COST"
    MISSING_EXCHANGE_RATE = "MISSING_EXCHANGE_RATE"
    DECIMAL_ARITHMETIC = "DECIMAL_ARITHMETIC"


@dataclass(frozen=True, slots=True)
class UnknownCostDetail:
    """One unknown component and its unmodified source reason."""

    component: CostComponentName
    reason: str

    def __post_init__(self) -> None:
        _require_component_name(self.component)
        _require_text(
            self.reason,
            "unknown cost reason",
            maximum=_MAX_COST_METADATA_LENGTH,
        )


@dataclass(frozen=True, slots=True)
class ExchangeRateTrace:
    """The exact versioned FX input used for one pricing target."""

    currency: str
    base_per_unit: Decimal
    minor_units: int
    evidence_id: str
    snapshot_ordinal: int

    def __post_init__(self) -> None:
        _require_currency(self.currency)
        _require_positive_decimal(self.base_per_unit, "trace base_per_unit")
        _require_minor_units(self.minor_units)
        _require_text(
            self.evidence_id,
            "exchange-rate evidence ID",
            maximum=_MAX_COST_METADATA_LENGTH,
        )
        _require_ordinal(self.snapshot_ordinal, "exchange-rate snapshot ordinal")


@dataclass(frozen=True, slots=True)
class CostComponentTrace:
    """One evidenced source component and both unquantized conversions."""

    component: CostComponentName
    source_amount: Decimal
    evidence_id: str
    display_exact: Decimal
    budget_exact: Decimal

    def __post_init__(self) -> None:
        _require_component_name(self.component)
        _require_non_negative_decimal(self.source_amount, "trace source amount")
        if self.source_amount.is_zero() and self.source_amount.as_tuple().exponent != -2:
            raise ValueError("trace source known zero must be explicit as 0.00")
        _require_text(
            self.evidence_id,
            "cost evidence ID",
            maximum=_MAX_COST_METADATA_LENGTH,
        )
        _require_non_negative_decimal(self.display_exact, "trace display amount")
        _require_non_negative_decimal(self.budget_exact, "trace budget amount")


@dataclass(frozen=True, slots=True)
class PricingTrace:
    """Closed inputs and intermediate values required to replay pricing-v1."""

    snapshot_version: str
    base_currency: str
    source_currency: str
    display_currency: str
    budget_currency: str
    components: tuple[CostComponentTrace, ...]
    source_rate: ExchangeRateTrace
    display_rate: ExchangeRateTrace
    budget_rate: ExchangeRateTrace
    algorithm_version: Literal["pricing-v1"] = field(
        default=PRICING_ALGORITHM_VERSION,
        init=False,
    )

    def __post_init__(self) -> None:
        _require_text(
            self.snapshot_version,
            "pricing snapshot version",
            maximum=_MAX_COST_METADATA_LENGTH,
        )
        _require_currency(self.base_currency)
        _require_currency(self.source_currency)
        _require_currency(self.display_currency)
        _require_currency(self.budget_currency)
        if type(self.components) is not tuple or any(
            type(component) is not CostComponentTrace for component in self.components
        ):
            raise TypeError("pricing trace components must be a tuple of CostComponentTrace")
        if tuple(component.component for component in self.components) != _COST_COMPONENT_ORDER:
            raise ValueError("pricing trace must contain four components in fixed order")
        for component in self.components:
            CostComponentTrace.__post_init__(component)
        if len({component.evidence_id for component in self.components}) != len(self.components):
            raise ValueError("pricing trace cost evidence IDs must be unique")
        if type(self.source_rate) is not ExchangeRateTrace:
            raise TypeError("source_rate must be an ExchangeRateTrace")
        if type(self.display_rate) is not ExchangeRateTrace:
            raise TypeError("display_rate must be an ExchangeRateTrace")
        if type(self.budget_rate) is not ExchangeRateTrace:
            raise TypeError("budget_rate must be an ExchangeRateTrace")
        ExchangeRateTrace.__post_init__(self.source_rate)
        ExchangeRateTrace.__post_init__(self.display_rate)
        ExchangeRateTrace.__post_init__(self.budget_rate)
        if self.source_rate.currency != self.source_currency:
            raise ValueError("source rate currency does not match pricing trace")
        if self.display_rate.currency != self.display_currency:
            raise ValueError("display rate currency does not match pricing trace")
        if self.budget_rate.currency != self.budget_currency:
            raise ValueError("budget rate currency does not match pricing trace")
        _require_discriminator(
            self.algorithm_version,
            PRICING_ALGORITHM_VERSION,
            "pricing trace",
        )
        try:
            with localcontext(pricing_context()):
                for component in self.components:
                    expected_display = (
                        component.source_amount
                        * self.source_rate.base_per_unit
                        / self.display_rate.base_per_unit
                    )
                    expected_budget = (
                        component.source_amount
                        * self.source_rate.base_per_unit
                        / self.budget_rate.base_per_unit
                    )
                    if (
                        component.display_exact != expected_display
                        or component.budget_exact != expected_budget
                    ):
                        raise ValueError("pricing trace component conversions cannot be replayed")
        except DecimalException as error:
            raise ValueError("pricing trace FX arithmetic is invalid") from error


@dataclass(frozen=True, slots=True)
class LandedCost:
    """A complete landed-cost result in display and effective budget currencies."""

    display_currency: str
    budget_currency: str
    display_exact: Decimal
    budget_exact: Decimal
    display_quantized: Decimal
    budget_mode: BudgetMode | None
    budget_target_amount: Decimal | None
    budget_lower_bound: Decimal | None
    budget_upper_bound: Decimal | None
    within_budget: bool | None
    trace: PricingTrace
    kind: Literal["SUCCESS"] = field(default="SUCCESS", init=False)

    def __post_init__(self) -> None:
        _require_currency(self.display_currency)
        _require_currency(self.budget_currency)
        _require_non_negative_decimal(self.display_exact, "display exact amount")
        _require_non_negative_decimal(self.budget_exact, "budget exact amount")
        _require_non_negative_decimal(
            self.display_quantized,
            "display quantized amount",
        )
        _validate_budget_constraint(
            mode=self.budget_mode,
            target_amount=self.budget_target_amount,
            lower_bound=self.budget_lower_bound,
            upper_bound=self.budget_upper_bound,
        )
        if self.within_budget is not None and type(self.within_budget) is not bool:
            raise TypeError("within_budget must be bool or None")
        expected_within_budget = _within_budget(
            self.budget_exact,
            mode=self.budget_mode,
            lower_bound=self.budget_lower_bound,
            upper_bound=self.budget_upper_bound,
        )
        if self.within_budget is not expected_within_budget:
            raise ValueError("within_budget must use the exact inclusive constraint")
        if type(self.trace) is not PricingTrace:
            raise TypeError("trace must be a PricingTrace")
        try:
            PricingTrace.__post_init__(self.trace)
        except AttributeError as error:
            raise TypeError("trace is missing required fields") from error
        if (
            self.trace.display_currency != self.display_currency
            or self.trace.budget_currency != self.budget_currency
        ):
            raise ValueError("landed-cost currencies must match its pricing trace")
        try:
            with localcontext(pricing_context()):
                replayed_display = Decimal(0)
                replayed_budget = Decimal(0)
                for component in self.trace.components:
                    replayed_display += component.display_exact
                    replayed_budget += component.budget_exact
                quantum = Decimal(1).scaleb(-self.trace.display_rate.minor_units)
                replayed_quantized = replayed_display.quantize(quantum)
        except DecimalException as error:
            raise ValueError("landed-cost trace arithmetic is invalid") from error
        if (
            self.display_exact != replayed_display
            or self.budget_exact != replayed_budget
            or self.display_quantized != replayed_quantized
        ):
            raise ValueError("landed-cost values do not match the pricing trace")
        if self.display_quantized.as_tuple().exponent != -self.trace.display_rate.minor_units:
            raise ValueError("display quantized amount must preserve minor units")
        _require_discriminator(self.kind, "SUCCESS", "landed cost")

    @property
    def display_exact_json(self) -> str:
        return canonical_exact_amount(self.display_exact)

    @property
    def budget_exact_json(self) -> str:
        return canonical_exact_amount(self.budget_exact)

    @property
    def display_json(self) -> str:
        return format(
            self.display_quantized,
            f".{self.trace.display_rate.minor_units}f",
        )


@dataclass(frozen=True, slots=True)
class PricingFailure:
    """An expected fail-closed result with no invented monetary value."""

    code: PricingFailureCode
    unknown_costs: tuple[UnknownCostDetail, ...] = ()
    missing_currencies: tuple[str, ...] = ()
    kind: Literal["FAILURE"] = field(default="FAILURE", init=False)

    def __post_init__(self) -> None:
        if type(self.code) is not PricingFailureCode:
            raise TypeError("pricing failure code must be PricingFailureCode")
        if type(self.unknown_costs) is not tuple or any(
            type(item) is not UnknownCostDetail for item in self.unknown_costs
        ):
            raise TypeError("unknown_costs must be a tuple of UnknownCostDetail")
        for item in self.unknown_costs:
            UnknownCostDetail.__post_init__(item)
        if type(self.missing_currencies) is not tuple:
            raise TypeError("missing_currencies must be a tuple")
        for currency in self.missing_currencies:
            _require_currency(currency)
        if len(set(self.missing_currencies)) != len(self.missing_currencies):
            raise ValueError("missing currencies must be unique")
        if self.code is PricingFailureCode.UNKNOWN_COST:
            if not self.unknown_costs or self.missing_currencies:
                raise ValueError("unknown-cost failure requires only unknown costs")
        elif self.code is PricingFailureCode.MISSING_EXCHANGE_RATE:
            if self.unknown_costs or not self.missing_currencies:
                raise ValueError("missing-rate failure requires only missing currencies")
        elif self.unknown_costs or self.missing_currencies:
            raise ValueError("decimal failure cannot contain cost or currency details")
        _require_discriminator(self.kind, "FAILURE", "pricing failure")


type PricingResult = LandedCost | PricingFailure


def _validate_budget_constraint(
    *,
    mode: BudgetMode | None,
    target_amount: Decimal | None,
    lower_bound: Decimal | None,
    upper_bound: Decimal | None,
) -> None:
    values = (target_amount, lower_bound, upper_bound)
    if mode is None:
        if any(value is not None for value in values):
            raise ValueError("budget amounts require an explicit budget mode")
        return
    if mode not in {"maximum", "around", "range"}:
        raise ValueError("budget mode must be maximum, around, or range")
    if target_amount is None or upper_bound is None:
        raise ValueError("budget mode requires target and upper amounts")
    _require_positive_decimal(target_amount, "budget target amount")
    _require_positive_decimal(upper_bound, "budget upper bound")
    if lower_bound is not None:
        _require_positive_decimal(lower_bound, "budget lower bound")
    if mode == "maximum":
        if lower_bound is not None or upper_bound != target_amount:
            raise ValueError("maximum budget requires target equal to its sole upper bound")
        return
    if mode == "around":
        if lower_bound != target_amount * Decimal("0.90") or upper_bound != target_amount * Decimal(
            "1.10"
        ):
            raise ValueError("around budget requires exact inclusive ten-percent bounds")
        return
    if lower_bound is None or lower_bound >= upper_bound or target_amount != upper_bound:
        raise ValueError("range budget requires explicit ordered bounds and target upper bound")


def _within_budget(
    amount: Decimal,
    *,
    mode: BudgetMode | None,
    lower_bound: Decimal | None,
    upper_bound: Decimal | None,
) -> bool | None:
    if mode is None:
        return None
    if upper_bound is None:
        raise ValueError("budget mode requires an upper bound")
    if mode == "maximum":
        return amount <= upper_bound
    if mode in {"around", "range"} and lower_bound is not None:
        return lower_bound <= amount <= upper_bound
    raise ValueError("bounded budget mode requires a lower bound")


def calculate_landed_cost(
    costs: CostBreakdown,
    exchange_rates: ExchangeRateTable,
    *,
    display_currency: str,
    budget_mode: BudgetMode | None = None,
    budget_target_amount: Decimal | None = None,
    budget_lower_bound: Decimal | None = None,
    budget_upper_bound: Decimal | None = None,
    budget_currency: str | None = None,
) -> PricingResult:
    """Calculate an evidenced landed cost without gates, I/O, or implicit FX."""

    _require_currency(display_currency)
    effective_budget_currency = (
        display_currency if budget_currency is None else _require_currency(budget_currency)
    )
    _validate_budget_constraint(
        mode=budget_mode,
        target_amount=budget_target_amount,
        lower_bound=budget_lower_bound,
        upper_bound=budget_upper_bound,
    )
    costs = _require_cost_breakdown(costs)
    table = _require_exchange_rate_table(exchange_rates)

    unknown_costs = tuple(
        UnknownCostDetail(component=name, reason=cost.reason)
        for name, cost in _fixed_cost_items(costs)
        if type(cost) is UnknownCost
    )
    if unknown_costs:
        return PricingFailure(
            code=PricingFailureCode.UNKNOWN_COST,
            unknown_costs=unknown_costs,
        )
    if not costs.is_complete:
        raise ValueError("costs contain a malformed component")

    rates_by_currency = {rate.currency: rate for rate in table.rates}
    needed_currencies = tuple(
        dict.fromkeys(
            (
                costs.currency,
                display_currency,
                effective_budget_currency,
            )
        )
    )
    missing_currencies = tuple(
        currency for currency in needed_currencies if currency not in rates_by_currency
    )
    if missing_currencies:
        return PricingFailure(
            code=PricingFailureCode.MISSING_EXCHANGE_RATE,
            missing_currencies=missing_currencies,
        )

    source_rate = rates_by_currency[costs.currency]
    display_rate = rates_by_currency[display_currency]
    budget_rate = rates_by_currency[effective_budget_currency]
    try:
        with localcontext(pricing_context()):
            component_traces: list[CostComponentTrace] = []
            display_exact = Decimal(0)
            budget_exact = Decimal(0)
            for component, cost in _fixed_cost_items(costs):
                if type(cost) is not KnownCost:
                    raise AssertionError("complete costs must contain KnownCost values")
                converted_display = (
                    cost.amount * source_rate.base_per_unit / display_rate.base_per_unit
                )
                converted_budget = (
                    cost.amount * source_rate.base_per_unit / budget_rate.base_per_unit
                )
                component_traces.append(
                    CostComponentTrace(
                        component=component,
                        source_amount=cost.amount,
                        evidence_id=cost.evidence_id,
                        display_exact=converted_display,
                        budget_exact=converted_budget,
                    )
                )
                display_exact += converted_display
                budget_exact += converted_budget
            quantum = Decimal(1).scaleb(-display_rate.minor_units)
            display_quantized = display_exact.quantize(quantum)
    except (DecimalException, ValueError):
        return PricingFailure(code=PricingFailureCode.DECIMAL_ARITHMETIC)

    try:
        trace = PricingTrace(
            snapshot_version=table.snapshot_version,
            base_currency=table.base_currency,
            source_currency=costs.currency,
            display_currency=display_currency,
            budget_currency=effective_budget_currency,
            components=tuple(component_traces),
            source_rate=_exchange_rate_trace(source_rate),
            display_rate=_exchange_rate_trace(display_rate),
            budget_rate=_exchange_rate_trace(budget_rate),
        )
        within_budget = _within_budget(
            budget_exact,
            mode=budget_mode,
            lower_bound=budget_lower_bound,
            upper_bound=budget_upper_bound,
        )
        return LandedCost(
            display_currency=display_currency,
            budget_currency=effective_budget_currency,
            display_exact=display_exact,
            budget_exact=budget_exact,
            display_quantized=display_quantized,
            budget_mode=budget_mode,
            budget_target_amount=budget_target_amount,
            budget_lower_bound=budget_lower_bound,
            budget_upper_bound=budget_upper_bound,
            within_budget=within_budget,
            trace=trace,
        )
    except (DecimalException, ValueError):
        return PricingFailure(code=PricingFailureCode.DECIMAL_ARITHMETIC)


def pricing_context() -> Context:
    """Return a fresh context for all pricing calculations."""

    return Context(
        prec=PRICING_PRECISION,
        rounding=PRICING_ROUNDING,
        Emin=_PRICING_EMIN,
        Emax=_PRICING_EMAX,
        capitals=1,
        clamp=0,
        flags=[],
        traps=[InvalidOperation, DivisionByZero, Overflow],
    )


def canonical_exact_amount(amount: Decimal) -> str:
    """Render an exact amount as canonical fixed-point JSON text."""

    _require_non_negative_decimal(amount, "amount")
    if amount.is_zero():
        return "0"
    rendered = format(amount, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def quantize_display_amount(amount: Decimal, minor_units: int) -> Decimal:
    """Quantize an amount for display without changing the caller's context."""

    _require_non_negative_decimal(amount, "amount")
    _require_minor_units(minor_units)
    try:
        with localcontext(pricing_context()):
            quantum = Decimal(1).scaleb(-minor_units)
            return amount.quantize(quantum)
    except DecimalException as error:
        raise ValueError("amount cannot be represented at pricing precision") from error


def format_display_amount(amount: Decimal, minor_units: int) -> str:
    """Render a display amount with exactly the currency's minor units."""

    quantized = quantize_display_amount(amount, minor_units)
    return format(quantized, f".{minor_units}f")


def _fixed_cost_items(
    value: CostBreakdown,
) -> tuple[tuple[CostComponentName, CostValue], ...]:
    try:
        return (
            ("item_price", value.item_price),
            ("shipping", value.shipping),
            ("tax", value.tax),
            ("duty", value.duty),
        )
    except AttributeError as error:
        raise TypeError("cost breakdown is missing required fields") from error


def _require_cost_breakdown(value: object) -> CostBreakdown:
    from glodex.domain.catalog import CostComponents as CatalogCostComponents

    if type(value) is not CostBreakdown and type(value) is not CatalogCostComponents:
        raise TypeError("costs must be an exact CostBreakdown or CostComponents")
    if not isinstance(value, CostBreakdown):
        raise TypeError("catalog cost components must inherit CostBreakdown")
    CostBreakdown.__post_init__(value)
    return value


def _require_non_negative_decimal(value: object, name: str) -> Decimal:
    if type(value) is not Decimal:
        raise TypeError(f"{name} must be Decimal")
    if not value.is_finite() or value.is_signed():
        raise ValueError(f"{name} must be a finite non-negative Decimal")
    if _canonical_fixed_length(value) > _MAX_CANONICAL_AMOUNT_LENGTH:
        raise ValueError(
            f"{name} canonical fixed-point form exceeds {_MAX_CANONICAL_AMOUNT_LENGTH} characters"
        )
    return value


def _require_positive_decimal(value: object, name: str) -> Decimal:
    decimal_value = _require_non_negative_decimal(value, name)
    if decimal_value <= 0:
        raise ValueError(f"{name} must be positive")
    return decimal_value


def _validate_known_cost(value: KnownCost) -> None:
    try:
        amount = value.amount
        evidence_id = value.evidence_id
        kind = value.kind
    except AttributeError as error:
        raise TypeError("known cost is missing required fields") from error
    _require_non_negative_decimal(amount, "known cost amount")
    if amount.is_zero() and amount.as_tuple().exponent != -2:
        raise ValueError("known zero must be explicit as 0.00")
    _require_text(
        evidence_id,
        "known cost evidence ID",
        maximum=_MAX_COST_METADATA_LENGTH,
    )
    _require_discriminator(kind, "KNOWN", "known cost")


def _validate_unknown_cost(value: UnknownCost) -> None:
    try:
        reason = value.reason
        kind = value.kind
    except AttributeError as error:
        raise TypeError("unknown cost is missing required fields") from error
    _require_text(
        reason,
        "unknown cost reason",
        maximum=_MAX_COST_METADATA_LENGTH,
    )
    _require_discriminator(kind, "UNKNOWN", "unknown cost")


def _canonical_fixed_length(value: Decimal) -> int:
    if value.is_zero():
        return 1
    decimal_tuple = value.as_tuple()
    digits = decimal_tuple.digits
    exponent = decimal_tuple.exponent
    if not isinstance(exponent, int):
        raise ValueError("amount must be finite")
    trailing_zero_count = 0
    for digit in reversed(digits):
        if digit != 0:
            break
        trailing_zero_count += 1
    digit_count = len(digits) - trailing_zero_count
    exponent += trailing_zero_count
    if exponent >= 0:
        return digit_count + exponent
    if digit_count + exponent > 0:
        return digit_count + 1
    return 2 - exponent


def _require_text(value: object, name: str, *, maximum: int) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be str")
    if not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must contain 1 to {maximum} characters")
    return value


def _require_currency(value: object) -> str:
    if type(value) is not str:
        raise TypeError("currency must be str")
    if len(value) != 3 or not value.isascii() or not value.isalpha() or not value.isupper():
        raise ValueError("currency must be three uppercase ASCII letters")
    return value


def _require_component_name(value: object) -> CostComponentName:
    if type(value) is not str or value not in _COST_COMPONENT_ORDER:
        raise ValueError("component must be a fixed landed-cost component name")
    return value


def _require_discriminator(value: object, expected: str, name: str) -> str:
    if type(value) is not str or value != expected:
        raise ValueError(f"{name} discriminator must be {expected}")
    return value


def _require_minor_units(value: object) -> int:
    if type(value) is not int:
        raise TypeError("minor_units must be int")
    if not _MIN_MINOR_UNITS <= value <= _MAX_MINOR_UNITS:
        raise ValueError("minor_units must be from zero to four")
    return value


def _require_ordinal(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _require_exchange_rate_table(value: object) -> ExchangeRateTable:
    from glodex.domain.catalog import (
        ExchangeRate as CatalogExchangeRate,
    )
    from glodex.domain.catalog import (
        ExchangeRateTable as CatalogExchangeRateTable,
    )

    if type(value) is not CatalogExchangeRateTable:
        raise TypeError("exchange_rates must be an ExchangeRateTable")
    for rate in value.rates:
        if type(rate) is not CatalogExchangeRate:
            raise TypeError("exchange rate table contains an invalid rate")
        CatalogExchangeRate.__post_init__(rate)
        _require_positive_decimal(rate.base_per_unit, "base_per_unit")
    CatalogExchangeRateTable.__post_init__(value)
    return value


def _exchange_rate_trace(rate: ExchangeRate) -> ExchangeRateTrace:
    return ExchangeRateTrace(
        currency=rate.currency,
        base_per_unit=rate.base_per_unit,
        minor_units=rate.minor_units,
        evidence_id=rate.evidence_id,
        snapshot_ordinal=rate.snapshot_ordinal,
    )


__all__ = [
    "PRICING_ALGORITHM_VERSION",
    "PRICING_PRECISION",
    "PRICING_ROUNDING",
    "BudgetMode",
    "CostBreakdown",
    "CostComponentName",
    "CostComponentTrace",
    "CostValue",
    "ExchangeRateTrace",
    "KnownCost",
    "LandedCost",
    "PricingFailure",
    "PricingFailureCode",
    "PricingResult",
    "PricingTrace",
    "UnknownCost",
    "UnknownCostDetail",
    "calculate_landed_cost",
    "canonical_exact_amount",
    "format_display_amount",
    "pricing_context",
    "quantize_display_amount",
]
