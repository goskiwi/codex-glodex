"""Bounded, private-free M6 observability values and pure breaker transitions.

This module deliberately has no HTTP, database, Redis, model, or framework import.
The Durable PostgreSQL adapter owns persistence and locking; callers can only construct
the finite, allow-listed values below.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final, Protocol, cast

from glodex._json import loads_unique
from glodex.runtime.contracts import validate_identifier

M6_TRACE_SCHEMA_VERSION: Final = "glodex.m6-safe-trace.v2"
M6_MAX_TRACE_EVENTS: Final = 96
M6_MAX_DURATION_MS: Final = 120_000
M6_BREAKER_FAILURE_THRESHOLD: Final = 3
M6_BREAKER_WINDOW_SECONDS: Final = 60
M6_BREAKER_COOLDOWN_SECONDS: Final = 30
_MAX_SAFE_CODE_LENGTH: Final = 96
_MAX_MODEL_LENGTH: Final = 128
_MAX_VERSION_LENGTH: Final = 128
_MAX_TOKEN_COUNT: Final = 10_000_000
_MAX_MICRO_UNITS: Final = 10_000_000_000_000
_VERSION_CHARACTERS: Final = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
_MAX_USAGE_RESPONSE_BYTES: Final = 65_536


class M6Operation(StrEnum):
    """The finite dependency-operation key set protected by M6."""

    LLM_TOOL_CALL = "llm_tool_call"
    LLM_REFLECT = "llm_reflect"
    LLM_SUMMARY = "llm_summary"
    LLM_JUDGE = "llm_judge"
    LLM_TRANSLATION = "llm_translation"
    BGE_EMBEDDING = "bge_embedding"
    BGE_RERANK = "bge_rerank"
    OPENSEARCH_RETRIEVAL = "opensearch_retrieval"


class M6TraceEventKind(StrEnum):
    """A safe trace never stores a request, response, or free-form exception."""

    RUN_STARTED = "RUN_STARTED"
    OPERATION_FINISHED = "OPERATION_FINISHED"
    TERMINAL = "TERMINAL"
    ALERT = "ALERT"
    EXPORT = "EXPORT"


class M6OperationOutcome(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    REJECTED = "REJECTED"


class M6ReceiptStatus(StrEnum):
    REPORTED = "REPORTED"
    UNAVAILABLE = "UNAVAILABLE"


class M6CostStatus(StrEnum):
    REPORTED = "REPORTED"
    UNAVAILABLE = "UNAVAILABLE"


class M6BreakerState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class M6AlertCode(StrEnum):
    """Finite, identity-free operations alerts derived from M6 truth."""

    BREAKER_OPEN = "BREAKER_OPEN"
    FAILURE_RATE_HIGH = "FAILURE_RATE_HIGH"
    RECEIPT_UNAVAILABLE = "RECEIPT_UNAVAILABLE"
    RETENTION_FAILED = "RETENTION_FAILED"


class M6OperationsWindow(StrEnum):
    ONE_HOUR = "1h"
    TWENTY_FOUR_HOURS = "24h"


class M6PermitDecision(StrEnum):
    ALLOW = "ALLOW"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"


class M6CircuitOpen(RuntimeError):
    """A stable pre-network rejection for one fixed dependency operation."""


class M6OperationStorePort(Protocol):
    """The finite Durable truth operations required around one dependency call."""

    async def acquire_m6_breaker(
        self,
        *,
        operation: M6Operation,
        now: datetime,
    ) -> M6PermitDecision: ...

    async def record_m6_breaker(
        self,
        *,
        operation: M6Operation,
        now: datetime,
        retryable_failure: bool,
    ) -> M6BreakerSnapshot: ...

    async def append_m6_trace_event(
        self,
        *,
        run_id: str,
        draft: M6TraceEventDraft,
    ) -> M6TraceEvent: ...


class M6Clock(Protocol):
    def now_utc(self) -> datetime: ...

    def monotonic_ns(self) -> int: ...


def _validate_safe_code(value: str | None, *, name: str) -> None:
    if value is None:
        return
    if (
        type(value) is not str
        or not value
        or len(value) > _MAX_SAFE_CODE_LENGTH
        or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for character in value)
    ):
        raise ValueError(f"{name} is invalid")


def _validate_version(value: str | None, *, name: str) -> None:
    if value is None:
        return
    if (
        type(value) is not str
        or not value
        or len(value) > _MAX_VERSION_LENGTH
        or any(character not in _VERSION_CHARACTERS for character in value)
    ):
        raise ValueError(f"{name} is invalid")


def _validate_nonnegative_int(value: int | None, *, name: str, maximum: int) -> None:
    if value is None:
        return
    if type(value) is not int or isinstance(value, bool) or not 0 <= value <= maximum:
        raise ValueError(f"{name} is invalid")


@dataclass(frozen=True, slots=True)
class ProviderUsageReceipt:
    """Only verified provider `usage` counts; no derived/tokenizer estimate exists."""

    status: M6ReceiptStatus
    provider: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    def __post_init__(self) -> None:
        if type(self.status) is not M6ReceiptStatus:
            raise TypeError("M6 receipt status is invalid")
        if self.status is M6ReceiptStatus.UNAVAILABLE:
            if any(
                value is not None
                for value in (
                    self.provider,
                    self.model,
                    self.input_tokens,
                    self.output_tokens,
                    self.total_tokens,
                )
            ):
                raise ValueError("unavailable receipt cannot contain a partial value")
            return
        if type(self.provider) is not str or type(self.model) is not str or not self.model:
            raise ValueError("reported receipt provider/model is invalid")
        _validate_version(self.provider, name="reported receipt provider")
        if len(self.model) > _MAX_MODEL_LENGTH or "\x00" in self.model:
            raise ValueError("reported receipt model is invalid")
        _validate_nonnegative_int(
            self.input_tokens,
            name="receipt input tokens",
            maximum=_MAX_TOKEN_COUNT,
        )
        _validate_nonnegative_int(
            self.output_tokens, name="receipt output tokens", maximum=_MAX_TOKEN_COUNT
        )
        _validate_nonnegative_int(
            self.total_tokens,
            name="receipt total tokens",
            maximum=_MAX_TOKEN_COUNT,
        )
        if (
            self.input_tokens is None
            or self.output_tokens is None
            or self.total_tokens is None
            or self.total_tokens != self.input_tokens + self.output_tokens
        ):
            raise ValueError("reported receipt totals are invalid")

    @classmethod
    def unavailable(cls) -> ProviderUsageReceipt:
        return cls(status=M6ReceiptStatus.UNAVAILABLE)


@dataclass(frozen=True, slots=True)
class ModelPriceTableEntry:
    """An explicit operator-maintained price row in integer micro currency units/token."""

    model: str
    price_table_version: str
    currency: str
    input_micro_units_per_token: int
    output_micro_units_per_token: int
    source_label: str

    def __post_init__(self) -> None:
        for name, value, maximum in (
            ("price table model", self.model, _MAX_MODEL_LENGTH),
            ("price table version", self.price_table_version, _MAX_VERSION_LENGTH),
            ("price table source", self.source_label, _MAX_VERSION_LENGTH),
        ):
            if type(value) is not str or not value or len(value) > maximum or "\x00" in value:
                raise ValueError(f"{name} is invalid")
        if type(self.currency) is not str or len(self.currency) != 3 or not self.currency.isupper():
            raise ValueError("price table currency is invalid")
        _validate_nonnegative_int(
            self.input_micro_units_per_token,
            name="input micro unit price",
            maximum=_MAX_MICRO_UNITS,
        )
        _validate_nonnegative_int(
            self.output_micro_units_per_token,
            name="output micro unit price",
            maximum=_MAX_MICRO_UNITS,
        )


@dataclass(frozen=True, slots=True)
class CostEstimate:
    """A local estimate only when a verified receipt exactly matches a local price row."""

    status: M6CostStatus
    price_table_version: str | None = None
    currency: str | None = None
    input_micro_units: int | None = None
    output_micro_units: int | None = None

    def __post_init__(self) -> None:
        if type(self.status) is not M6CostStatus:
            raise TypeError("M6 cost status is invalid")
        values = (
            self.price_table_version,
            self.currency,
            self.input_micro_units,
            self.output_micro_units,
        )
        if self.status is M6CostStatus.UNAVAILABLE:
            if any(value is not None for value in values):
                raise ValueError("unavailable cost cannot contain a partial value")
            return
        _validate_version(self.price_table_version, name="price table version")
        if type(self.currency) is not str or len(self.currency) != 3 or not self.currency.isupper():
            raise ValueError("cost currency is invalid")
        _validate_nonnegative_int(
            self.input_micro_units, name="input cost", maximum=_MAX_MICRO_UNITS
        )
        _validate_nonnegative_int(
            self.output_micro_units, name="output cost", maximum=_MAX_MICRO_UNITS
        )
        if self.input_micro_units is None or self.output_micro_units is None:
            raise ValueError("reported cost is incomplete")

    @property
    def total_micro_units(self) -> int | None:
        if self.status is M6CostStatus.UNAVAILABLE:
            return None
        assert self.input_micro_units is not None and self.output_micro_units is not None
        return self.input_micro_units + self.output_micro_units

    @classmethod
    def unavailable(cls) -> CostEstimate:
        return cls(status=M6CostStatus.UNAVAILABLE)


def estimate_cost(
    *,
    receipt: ProviderUsageReceipt,
    price_table: ModelPriceTableEntry | None,
) -> CostEstimate:
    """Return an estimate only from the two independently verified source values."""

    if (
        receipt.status is not M6ReceiptStatus.REPORTED
        or price_table is None
        or receipt.model != price_table.model
    ):
        return CostEstimate.unavailable()
    assert receipt.input_tokens is not None and receipt.output_tokens is not None
    return CostEstimate(
        status=M6CostStatus.REPORTED,
        price_table_version=price_table.price_table_version,
        currency=price_table.currency,
        input_micro_units=receipt.input_tokens * price_table.input_micro_units_per_token,
        output_micro_units=receipt.output_tokens * price_table.output_micro_units_per_token,
    )


def usage_receipt_from_response(response: object) -> ProviderUsageReceipt:
    """Parse only explicit OpenAI-compatible response usage; ambiguity is unavailable."""

    if type(response) is not bytes or len(response) > _MAX_USAGE_RESPONSE_BYTES:
        return ProviderUsageReceipt.unavailable()
    try:
        envelope = loads_unique(response, reject_constants=True)
        if type(envelope) is not dict:
            return ProviderUsageReceipt.unavailable()
        root = cast("dict[str, object]", envelope)
        model = root.get("model")
        usage = root.get("usage")
        if type(model) is not str or type(usage) is not dict:
            return ProviderUsageReceipt.unavailable()
        usage_values = cast("dict[str, object]", usage)
        input_tokens = usage_values.get("prompt_tokens")
        output_tokens = usage_values.get("completion_tokens")
        total_tokens = usage_values.get("total_tokens")
        if any(
            type(value) is not int or isinstance(value, bool)
            for value in (
                input_tokens,
                output_tokens,
                total_tokens,
            )
        ):
            return ProviderUsageReceipt.unavailable()
        return ProviderUsageReceipt(
            status=M6ReceiptStatus.REPORTED,
            provider="openai_compatible",
            model=model,
            input_tokens=cast("int", input_tokens),
            output_tokens=cast("int", output_tokens),
            total_tokens=cast("int", total_tokens),
        )
    except Exception:
        return ProviderUsageReceipt.unavailable()


@dataclass(frozen=True, slots=True)
class M6OperationLease:
    """Opaque call-local timing facts, never persisted as a request/body payload."""

    operation: M6Operation
    started_at: datetime
    started_ns: int

    def __post_init__(self) -> None:
        if (
            type(self.operation) is not M6Operation
            or self.started_at.tzinfo is None
            or type(self.started_ns) is not int
            or isinstance(self.started_ns, bool)
            or self.started_ns < 0
        ):
            raise ValueError("M6 operation lease is invalid")


@dataclass(frozen=True, slots=True)
class M6OperationRecorder:
    """PostgreSQL-truth permit/record wrapper with no retry or business mutation."""

    store: M6OperationStorePort
    run_id: str
    clock: M6Clock
    price_table: ModelPriceTableEntry | None = None

    def __post_init__(self) -> None:
        validate_identifier(self.run_id, name="M6 recorder run ID")
        if (
            not callable(self.store.acquire_m6_breaker)
            or not callable(self.store.record_m6_breaker)
            or not callable(self.store.append_m6_trace_event)
            or not callable(self.clock.now_utc)
            or not callable(self.clock.monotonic_ns)
            or (self.price_table is not None and type(self.price_table) is not ModelPriceTableEntry)
        ):
            raise TypeError("M6 operation recorder inputs are invalid")

    async def acquire(self, *, operation: M6Operation) -> M6OperationLease:
        """Acquire before network work; an OPEN circuit never reaches the provider."""

        if type(operation) is not M6Operation:
            raise TypeError("M6 recorder operation is invalid")
        now = self.clock.now_utc()
        decision = await self.store.acquire_m6_breaker(operation=operation, now=now)
        if decision is M6PermitDecision.CIRCUIT_OPEN:
            await self._append(
                M6TraceEventDraft(
                    kind=M6TraceEventKind.OPERATION_FINISHED,
                    operation=operation,
                    outcome=M6OperationOutcome.REJECTED,
                    safe_code=M6PermitDecision.CIRCUIT_OPEN.value,
                    duration_ms=0,
                )
            )
            raise M6CircuitOpen(M6PermitDecision.CIRCUIT_OPEN.value)
        if decision is not M6PermitDecision.ALLOW:
            raise ValueError("M6 breaker permit is invalid")
        return M6OperationLease(
            operation=operation,
            started_at=now,
            started_ns=self.clock.monotonic_ns(),
        )

    async def success(
        self,
        *,
        lease: M6OperationLease,
        receipt: ProviderUsageReceipt | None = None,
        version: str | None = None,
    ) -> None:
        """Record one successful operation after its existing call has already returned."""

        if type(lease) is not M6OperationLease:
            raise TypeError("M6 success lease is invalid")
        effective_receipt = receipt
        if (
            lease.operation
            in {
                M6Operation.LLM_TOOL_CALL,
                M6Operation.LLM_REFLECT,
                M6Operation.LLM_SUMMARY,
                M6Operation.LLM_JUDGE,
                M6Operation.LLM_TRANSLATION,
            }
            and effective_receipt is None
        ):
            effective_receipt = ProviderUsageReceipt.unavailable()
        cost = (
            None
            if effective_receipt is None
            else estimate_cost(receipt=effective_receipt, price_table=self.price_table)
        )
        now = self.clock.now_utc()
        await self.store.record_m6_breaker(
            operation=lease.operation,
            now=now,
            retryable_failure=False,
        )
        await self._append(
            M6TraceEventDraft(
                kind=M6TraceEventKind.OPERATION_FINISHED,
                operation=lease.operation,
                outcome=M6OperationOutcome.SUCCESS,
                duration_ms=self._duration_ms(lease),
                version=version,
                receipt=effective_receipt,
                cost=cost,
            )
        )

    async def failure(
        self,
        *,
        lease: M6OperationLease,
        safe_code: str,
        retryable_failure: bool,
        version: str | None = None,
    ) -> None:
        """Record one classified failure; callers decide classification, never retry here."""

        if type(lease) is not M6OperationLease or type(retryable_failure) is not bool:
            raise TypeError("M6 failure inputs are invalid")
        now = self.clock.now_utc()
        await self.store.record_m6_breaker(
            operation=lease.operation,
            now=now,
            retryable_failure=retryable_failure,
        )
        await self._append(
            M6TraceEventDraft(
                kind=M6TraceEventKind.OPERATION_FINISHED,
                operation=lease.operation,
                outcome=M6OperationOutcome.FAILURE,
                safe_code=safe_code,
                duration_ms=self._duration_ms(lease),
                version=version,
            )
        )

    def _duration_ms(self, lease: M6OperationLease) -> int:
        elapsed_ns = self.clock.monotonic_ns() - lease.started_ns
        if elapsed_ns < 0:
            return 0
        return min(M6_MAX_DURATION_MS, elapsed_ns // 1_000_000)

    async def _append(self, draft: M6TraceEventDraft) -> None:
        await self.store.append_m6_trace_event(run_id=self.run_id, draft=draft)


@dataclass(frozen=True, slots=True)
class M6TraceEventDraft:
    """A bounded event without a caller-selectable sequence or arbitrary payload."""

    kind: M6TraceEventKind
    operation: M6Operation | None = None
    outcome: M6OperationOutcome | None = None
    safe_code: str | None = None
    duration_ms: int | None = None
    version: str | None = None
    receipt: ProviderUsageReceipt | None = None
    cost: CostEstimate | None = None

    def __post_init__(self) -> None:
        if type(self.kind) is not M6TraceEventKind:
            raise TypeError("M6 trace event kind is invalid")
        if self.operation is not None and type(self.operation) is not M6Operation:
            raise TypeError("M6 trace operation is invalid")
        if self.outcome is not None and type(self.outcome) is not M6OperationOutcome:
            raise TypeError("M6 trace outcome is invalid")
        _validate_safe_code(self.safe_code, name="M6 trace safe code")
        _validate_nonnegative_int(
            self.duration_ms, name="M6 trace duration", maximum=M6_MAX_DURATION_MS
        )
        _validate_version(self.version, name="M6 trace version")
        if self.receipt is not None and type(self.receipt) is not ProviderUsageReceipt:
            raise TypeError("M6 trace receipt is invalid")
        if self.cost is not None and type(self.cost) is not CostEstimate:
            raise TypeError("M6 trace cost is invalid")
        if self.kind is M6TraceEventKind.OPERATION_FINISHED:
            if self.operation is None or self.outcome is None or self.duration_ms is None:
                raise ValueError("M6 operation trace event is incomplete")
        elif self.operation is not None or self.outcome is not None or self.duration_ms is not None:
            raise ValueError("non-operation M6 trace event contains operation facts")
        if self.receipt is not None and self.operation not in {
            M6Operation.LLM_TOOL_CALL,
            M6Operation.LLM_REFLECT,
            M6Operation.LLM_SUMMARY,
            M6Operation.LLM_JUDGE,
            M6Operation.LLM_TRANSLATION,
        }:
            raise ValueError("M6 receipt operation is invalid")
        if self.cost is not None and self.receipt is None:
            raise ValueError("M6 cost requires an M6 receipt")


@dataclass(frozen=True, slots=True)
class M6TraceEvent:
    """One stored safe event. Sequence is allocated by the PostgreSQL truth store."""

    run_id: str
    sequence: int
    draft: M6TraceEventDraft

    def __post_init__(self) -> None:
        validate_identifier(self.run_id, name="M6 run ID")
        if (
            type(self.sequence) is not int
            or isinstance(self.sequence, bool)
            or not 1 <= self.sequence <= M6_MAX_TRACE_EVENTS
            or type(self.draft) is not M6TraceEventDraft
        ):
            raise ValueError("M6 trace event is invalid")


@dataclass(frozen=True, slots=True)
class M6RunTrace:
    """Owner authorization stays at Durable; this value itself contains no owner identity."""

    run_id: str
    events: tuple[M6TraceEvent, ...]
    terminal_state: str | None = None

    def __post_init__(self) -> None:
        validate_identifier(self.run_id, name="M6 run ID")
        if type(self.events) is not tuple or len(self.events) > M6_MAX_TRACE_EVENTS:
            raise ValueError("M6 trace event collection is invalid")
        if any(
            type(event) is not M6TraceEvent or event.run_id != self.run_id for event in self.events
        ):
            raise ValueError("M6 trace event owner is invalid")
        if tuple(event.sequence for event in self.events) != tuple(range(1, len(self.events) + 1)):
            raise ValueError("M6 trace sequence is invalid")
        _validate_safe_code(self.terminal_state, name="M6 terminal state")


@dataclass(frozen=True, slots=True)
class M6CostTotal:
    """One unidentifiable currency total from trace truth."""

    currency: str
    micro_units: int

    def __post_init__(self) -> None:
        if type(self.currency) is not str or len(self.currency) != 3 or not self.currency.isupper():
            raise ValueError("M6 cost total currency is invalid")
        _validate_nonnegative_int(self.micro_units, name="M6 cost total", maximum=_MAX_MICRO_UNITS)


@dataclass(frozen=True, slots=True)
class M6BreakerView:
    operation: M6Operation
    state: M6BreakerState

    def __post_init__(self) -> None:
        if type(self.operation) is not M6Operation or type(self.state) is not M6BreakerState:
            raise TypeError("M6 breaker view is invalid")


@dataclass(frozen=True, slots=True)
class M6AlertView:
    """One stable local alert code with no text, identity, or free-form metadata."""

    code: M6AlertCode

    def __post_init__(self) -> None:
        if type(self.code) is not M6AlertCode:
            raise TypeError("M6 alert view is invalid")


@dataclass(frozen=True, slots=True)
class M6OperationsView:
    """Aggregate-only operations read model, intentionally without run or owner identity."""

    window: M6OperationsWindow
    completed_count: int
    no_match_count: int
    failed_count: int
    aborted_count: int
    operation_count: int
    operation_failure_count: int
    receipt_reported_count: int
    receipt_unavailable_count: int
    cost_totals: tuple[M6CostTotal, ...]
    breakers: tuple[M6BreakerView, ...]
    alerts: tuple[M6AlertView, ...]

    def __post_init__(self) -> None:
        if type(self.window) is not M6OperationsWindow:
            raise TypeError("M6 operations window is invalid")
        for value in (
            self.completed_count,
            self.no_match_count,
            self.failed_count,
            self.aborted_count,
            self.operation_count,
            self.operation_failure_count,
            self.receipt_reported_count,
            self.receipt_unavailable_count,
        ):
            _validate_nonnegative_int(value, name="M6 operation count", maximum=_MAX_MICRO_UNITS)
        if (
            type(self.cost_totals) is not tuple
            or len(self.cost_totals) > 8
            or any(type(value) is not M6CostTotal for value in self.cost_totals)
            or len({value.currency for value in self.cost_totals}) != len(self.cost_totals)
            or type(self.breakers) is not tuple
            or len(self.breakers) > len(M6Operation)
            or any(type(value) is not M6BreakerView for value in self.breakers)
            or len({value.operation for value in self.breakers}) != len(self.breakers)
            or type(self.alerts) is not tuple
            or len(self.alerts) > len(M6AlertCode)
            or any(type(value) is not M6AlertView for value in self.alerts)
            or len({value.code for value in self.alerts}) != len(self.alerts)
        ):
            raise ValueError("M6 operations view is invalid")


def m6_operations_alerts(
    *,
    operation_count: int,
    operation_failure_count: int,
    receipt_unavailable_count: int,
    breakers: tuple[M6BreakerView, ...],
    maintenance_alerts: tuple[M6AlertCode, ...] = (),
) -> tuple[M6AlertView, ...]:
    """Derive fixed cards from aggregate facts without exposing an arbitrary threshold API."""

    for value in (operation_count, operation_failure_count, receipt_unavailable_count):
        _validate_nonnegative_int(value, name="M6 operations alert count", maximum=_MAX_MICRO_UNITS)
    if type(breakers) is not tuple or any(type(value) is not M6BreakerView for value in breakers):
        raise TypeError("M6 operations alert breakers are invalid")
    if (
        type(maintenance_alerts) is not tuple
        or any(type(value) is not M6AlertCode for value in maintenance_alerts)
        or any(value is not M6AlertCode.RETENTION_FAILED for value in maintenance_alerts)
    ):
        raise TypeError("M6 operations maintenance alerts are invalid")
    codes: list[M6AlertCode] = []
    if any(value.state is M6BreakerState.OPEN for value in breakers):
        codes.append(M6AlertCode.BREAKER_OPEN)
    if (
        operation_count >= M6_BREAKER_FAILURE_THRESHOLD
        and operation_failure_count * 2 >= operation_count
    ):
        codes.append(M6AlertCode.FAILURE_RATE_HIGH)
    if receipt_unavailable_count > 0:
        codes.append(M6AlertCode.RECEIPT_UNAVAILABLE)
    if M6AlertCode.RETENTION_FAILED in maintenance_alerts:
        codes.append(M6AlertCode.RETENTION_FAILED)
    return tuple(M6AlertView(code=code) for code in codes)


@dataclass(frozen=True, slots=True)
class M6RetentionReport:
    """The bounded, identity-free outcome of an explicit maintenance operation."""

    deleted_trace_count: int
    deleted_event_count: int
    deleted_metric_bucket_count: int

    def __post_init__(self) -> None:
        for value in (
            self.deleted_trace_count,
            self.deleted_event_count,
            self.deleted_metric_bucket_count,
        ):
            _validate_nonnegative_int(value, name="M6 retention count", maximum=_MAX_MICRO_UNITS)


@dataclass(frozen=True, slots=True)
class M6BreakerSnapshot:
    """The complete bounded breaker fact used by the row-lock adapter."""

    operation: M6Operation
    state: M6BreakerState
    failure_timestamps: tuple[datetime, ...] = ()
    cooldown_until: datetime | None = None
    half_open_probe: bool = False

    def __post_init__(self) -> None:
        if type(self.operation) is not M6Operation or type(self.state) is not M6BreakerState:
            raise TypeError("M6 breaker identity is invalid")
        if (
            type(self.failure_timestamps) is not tuple
            or len(self.failure_timestamps) > M6_BREAKER_FAILURE_THRESHOLD
            or any(value.tzinfo is None for value in self.failure_timestamps)
            or tuple(sorted(self.failure_timestamps)) != self.failure_timestamps
            or (self.cooldown_until is not None and self.cooldown_until.tzinfo is None)
            or type(self.half_open_probe) is not bool
        ):
            raise ValueError("M6 breaker snapshot is invalid")
        if self.state is M6BreakerState.CLOSED and (
            self.cooldown_until is not None or self.half_open_probe
        ):
            raise ValueError("closed M6 breaker contains open state")
        if self.state is M6BreakerState.OPEN and self.cooldown_until is None:
            raise ValueError("open M6 breaker lacks cooldown")
        if self.state is M6BreakerState.HALF_OPEN and self.cooldown_until is not None:
            raise ValueError("half-open M6 breaker contains cooldown")


def acquire_breaker(
    *,
    snapshot: M6BreakerSnapshot,
    now: datetime,
) -> tuple[M6PermitDecision, M6BreakerSnapshot]:
    """Pure permit transition; PostgreSQL holds the row lock around this operation."""

    if type(snapshot) is not M6BreakerSnapshot or now.tzinfo is None:
        raise TypeError("M6 breaker permit input is invalid")
    if snapshot.state is M6BreakerState.CLOSED:
        return M6PermitDecision.ALLOW, snapshot
    if snapshot.state is M6BreakerState.OPEN:
        assert snapshot.cooldown_until is not None
        if now < snapshot.cooldown_until:
            return M6PermitDecision.CIRCUIT_OPEN, snapshot
        return (
            M6PermitDecision.ALLOW,
            M6BreakerSnapshot(
                operation=snapshot.operation,
                state=M6BreakerState.HALF_OPEN,
                failure_timestamps=(),
                half_open_probe=True,
            ),
        )
    if snapshot.half_open_probe:
        return M6PermitDecision.CIRCUIT_OPEN, snapshot
    return (
        M6PermitDecision.ALLOW,
        M6BreakerSnapshot(
            operation=snapshot.operation,
            state=M6BreakerState.HALF_OPEN,
            failure_timestamps=(),
            half_open_probe=True,
        ),
    )


def record_breaker_outcome(
    *,
    snapshot: M6BreakerSnapshot,
    now: datetime,
    retryable_failure: bool,
) -> M6BreakerSnapshot:
    """Pure success/failure transition after one already-permitted dependency call."""

    if (
        type(snapshot) is not M6BreakerSnapshot
        or now.tzinfo is None
        or type(retryable_failure) is not bool
    ):
        raise TypeError("M6 breaker record input is invalid")
    if snapshot.state is M6BreakerState.OPEN:
        raise ValueError("M6 cannot record an unpermitted open breaker call")
    if snapshot.state is M6BreakerState.HALF_OPEN:
        if not snapshot.half_open_probe:
            raise ValueError("M6 half-open outcome lacks its probe lease")
        if not retryable_failure:
            return M6BreakerSnapshot(operation=snapshot.operation, state=M6BreakerState.CLOSED)
        return M6BreakerSnapshot(
            operation=snapshot.operation,
            state=M6BreakerState.OPEN,
            cooldown_until=now + timedelta(seconds=M6_BREAKER_COOLDOWN_SECONDS),
        )
    if not retryable_failure:
        return M6BreakerSnapshot(operation=snapshot.operation, state=M6BreakerState.CLOSED)
    window_start = now - timedelta(seconds=M6_BREAKER_WINDOW_SECONDS)
    prior_failures = tuple(value for value in snapshot.failure_timestamps if value >= window_start)
    failures = (*prior_failures, now)
    if len(failures) < M6_BREAKER_FAILURE_THRESHOLD:
        return M6BreakerSnapshot(
            operation=snapshot.operation,
            state=M6BreakerState.CLOSED,
            failure_timestamps=failures,
        )
    return M6BreakerSnapshot(
        operation=snapshot.operation,
        state=M6BreakerState.OPEN,
        cooldown_until=now + timedelta(seconds=M6_BREAKER_COOLDOWN_SECONDS),
    )


__all__ = [
    "M6_BREAKER_COOLDOWN_SECONDS",
    "M6_BREAKER_FAILURE_THRESHOLD",
    "M6_BREAKER_WINDOW_SECONDS",
    "M6_MAX_TRACE_EVENTS",
    "M6_TRACE_SCHEMA_VERSION",
    "CostEstimate",
    "M6BreakerSnapshot",
    "M6BreakerState",
    "M6BreakerView",
    "M6CircuitOpen",
    "M6Clock",
    "M6CostStatus",
    "M6CostTotal",
    "M6Operation",
    "M6OperationLease",
    "M6OperationOutcome",
    "M6OperationRecorder",
    "M6OperationStorePort",
    "M6OperationsView",
    "M6OperationsWindow",
    "M6PermitDecision",
    "M6ReceiptStatus",
    "M6RetentionReport",
    "M6RunTrace",
    "M6TraceEvent",
    "M6TraceEventDraft",
    "M6TraceEventKind",
    "ModelPriceTableEntry",
    "ProviderUsageReceipt",
    "acquire_breaker",
    "estimate_cost",
    "record_breaker_outcome",
    "usage_receipt_from_response",
]
