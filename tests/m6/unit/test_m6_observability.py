"""Offline M6 value and rolling-breaker evidence with a deterministic UTC clock."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from glodex.observability.runtime import (
    M6_BREAKER_COOLDOWN_SECONDS,
    CostEstimate,
    M6AlertCode,
    M6BreakerSnapshot,
    M6BreakerState,
    M6BreakerView,
    M6CircuitOpen,
    M6CostStatus,
    M6CostTotal,
    M6Operation,
    M6OperationOutcome,
    M6OperationRecorder,
    M6OperationsView,
    M6OperationsWindow,
    M6PermitDecision,
    M6ReceiptStatus,
    M6TraceEventDraft,
    M6TraceEventKind,
    ModelPriceTableEntry,
    ProviderUsageReceipt,
    acquire_breaker,
    estimate_cost,
    m6_operations_alerts,
    record_breaker_outcome,
    usage_receipt_from_response,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec("GLO-M6-P0-002", "GLO-M6-P0-003", "GLO-M6-NFR-003", "GLO-M6-NFR-004"),
]


def _now() -> datetime:
    return datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


class _Clock:
    def __init__(self) -> None:
        self.now = _now()
        self.monotonic = 0

    def now_utc(self) -> datetime:
        return self.now

    def monotonic_ns(self) -> int:
        return self.monotonic


class _OperationStore:
    def __init__(self) -> None:
        self.breakers = {
            operation: M6BreakerSnapshot(operation=operation, state=M6BreakerState.CLOSED)
            for operation in M6Operation
        }
        self.events: list[M6TraceEventDraft] = []

    async def acquire_m6_breaker(
        self,
        *,
        operation: M6Operation,
        now: datetime,
    ) -> M6PermitDecision:
        decision, snapshot = acquire_breaker(snapshot=self.breakers[operation], now=now)
        self.breakers[operation] = snapshot
        return decision

    async def record_m6_breaker(
        self,
        *,
        operation: M6Operation,
        now: datetime,
        retryable_failure: bool,
    ) -> M6BreakerSnapshot:
        snapshot = record_breaker_outcome(
            snapshot=self.breakers[operation],
            now=now,
            retryable_failure=retryable_failure,
        )
        self.breakers[operation] = snapshot
        return snapshot

    async def append_m6_trace_event(
        self,
        *,
        run_id: str,
        draft: M6TraceEventDraft,
    ) -> None:
        assert run_id == "run-m6-test-1"
        self.events.append(draft)


def test_cost_is_reported_only_for_verified_usage_and_exact_operator_price_model() -> None:
    receipt = ProviderUsageReceipt(
        status=M6ReceiptStatus.REPORTED,
        provider="openai_compatible",
        model="test-model",
        input_tokens=11,
        output_tokens=7,
        total_tokens=18,
    )
    price = ModelPriceTableEntry(
        model="test-model",
        price_table_version="operator-2026-07-31",
        currency="USD",
        input_micro_units_per_token=2,
        output_micro_units_per_token=3,
        source_label="operator-approved",
    )

    estimate = estimate_cost(receipt=receipt, price_table=price)

    assert estimate.status is M6CostStatus.REPORTED
    assert estimate.input_micro_units == 22
    assert estimate.output_micro_units == 21
    assert estimate.total_micro_units == 43
    assert estimate_cost(receipt=ProviderUsageReceipt.unavailable(), price_table=price) == (
        CostEstimate.unavailable()
    )
    assert (
        estimate_cost(
            receipt=receipt,
            price_table=ModelPriceTableEntry(
                model="different-model",
                price_table_version="operator-2026-07-31",
                currency="USD",
                input_micro_units_per_token=2,
                output_micro_units_per_token=3,
                source_label="operator-approved",
            ),
        )
        == CostEstimate.unavailable()
    )


def test_receipt_and_trace_values_reject_guessed_partial_or_private_payloads() -> None:
    with pytest.raises(ValueError):
        ProviderUsageReceipt(
            status=M6ReceiptStatus.REPORTED,
            provider="openai_compatible",
            model="test-model",
            input_tokens=1,
            output_tokens=2,
            total_tokens=2,
        )
    with pytest.raises(ValueError):
        M6TraceEventDraft(
            kind=M6TraceEventKind.OPERATION_FINISHED,
            operation=M6Operation.LLM_TOOL_CALL,
            outcome=M6OperationOutcome.SUCCESS,
            duration_ms=1,
            safe_code="this is an exception body",
        )
    with pytest.raises(ValueError):
        M6TraceEventDraft(
            kind=M6TraceEventKind.RUN_STARTED,
            version="query=private shopping request",
        )


@pytest.mark.acceptance
@pytest.mark.spec("M6-AC-002")
def test_three_failures_open_the_single_operation_then_one_half_open_probe_closes() -> None:
    now = _now()
    snapshot = M6BreakerSnapshot(
        operation=M6Operation.LLM_TOOL_CALL,
        state=M6BreakerState.CLOSED,
    )
    for offset in (0, 10, 20):
        permit, snapshot = acquire_breaker(snapshot=snapshot, now=now + timedelta(seconds=offset))
        assert permit is M6PermitDecision.ALLOW
        snapshot = record_breaker_outcome(
            snapshot=snapshot,
            now=now + timedelta(seconds=offset),
            retryable_failure=True,
        )

    assert snapshot.state is M6BreakerState.OPEN
    denied, same_snapshot = acquire_breaker(snapshot=snapshot, now=now + timedelta(seconds=21))
    assert denied is M6PermitDecision.CIRCUIT_OPEN
    assert same_snapshot == snapshot

    probe_at = now + timedelta(seconds=20 + M6_BREAKER_COOLDOWN_SECONDS)
    allowed, probe = acquire_breaker(snapshot=snapshot, now=probe_at)
    assert allowed is M6PermitDecision.ALLOW
    assert probe.state is M6BreakerState.HALF_OPEN and probe.half_open_probe
    second, _ = acquire_breaker(snapshot=probe, now=probe_at)
    assert second is M6PermitDecision.CIRCUIT_OPEN
    recovered = record_breaker_outcome(snapshot=probe, now=probe_at, retryable_failure=False)
    assert recovered == M6BreakerSnapshot(
        operation=M6Operation.LLM_TOOL_CALL,
        state=M6BreakerState.CLOSED,
    )


def test_breaker_failure_window_and_operations_are_isolated() -> None:
    now = _now()
    snapshot = M6BreakerSnapshot(
        operation=M6Operation.BGE_RERANK,
        state=M6BreakerState.CLOSED,
    )
    for offset in (0, 61, 62):
        snapshot = record_breaker_outcome(
            snapshot=snapshot,
            now=now + timedelta(seconds=offset),
            retryable_failure=True,
        )
    assert snapshot.state is M6BreakerState.CLOSED
    assert len(snapshot.failure_timestamps) == 2
    assert (
        M6BreakerSnapshot(
            operation=M6Operation.OPENSEARCH_RETRIEVAL,
            state=M6BreakerState.CLOSED,
        ).operation
        is not snapshot.operation
    )


def test_operations_alerts_are_deterministic_and_never_carry_identity_or_text() -> None:
    alerts = m6_operations_alerts(
        operation_count=4,
        operation_failure_count=2,
        receipt_unavailable_count=1,
        breakers=(
            M6BreakerView(
                operation=M6Operation.BGE_RERANK,
                state=M6BreakerState.OPEN,
            ),
        ),
        maintenance_alerts=(M6AlertCode.RETENTION_FAILED,),
    )

    assert tuple(alert.code for alert in alerts) == (
        M6AlertCode.BREAKER_OPEN,
        M6AlertCode.FAILURE_RATE_HIGH,
        M6AlertCode.RECEIPT_UNAVAILABLE,
        M6AlertCode.RETENTION_FAILED,
    )


@pytest.mark.acceptance
@pytest.mark.spec("M6-AC-001")
def test_operation_recorder_records_real_response_usage_or_explicit_unavailable() -> None:
    async def scenario() -> None:
        clock = _Clock()
        store = _OperationStore()
        recorder = M6OperationRecorder(store=store, run_id="run-m6-test-1", clock=clock)
        lease = await recorder.acquire(operation=M6Operation.LLM_TOOL_CALL)
        clock.monotonic = 4_000_000
        await recorder.success(
            lease=lease,
            receipt=usage_receipt_from_response(
                b'{"model":"test-model","usage":{"prompt_tokens":4,'
                b'"completion_tokens":3,"total_tokens":7}}'
            ),
            version="test-model",
        )

        event = store.events[-1]
        assert event.outcome is M6OperationOutcome.SUCCESS
        assert event.duration_ms == 4
        assert event.receipt is not None
        assert event.receipt.status is M6ReceiptStatus.REPORTED
        assert event.cost == CostEstimate.unavailable()
        assert (
            usage_receipt_from_response(
                b'{"model":"test-model","usage":{"prompt_tokens":4}}'
            ).status
            is M6ReceiptStatus.UNAVAILABLE
        )

    import asyncio

    asyncio.run(scenario())


def test_operation_recorder_rejects_before_a_fourth_provider_call_when_breaker_is_open() -> None:
    async def scenario() -> None:
        clock = _Clock()
        store = _OperationStore()
        recorder = M6OperationRecorder(store=store, run_id="run-m6-test-1", clock=clock)
        for offset in (0, 10, 20):
            clock.now = _now() + timedelta(seconds=offset)
            lease = await recorder.acquire(operation=M6Operation.LLM_TOOL_CALL)
            await recorder.failure(
                lease=lease,
                safe_code="PROVIDER_UNAVAILABLE",
                retryable_failure=True,
            )
        clock.now = _now() + timedelta(seconds=21)
        with pytest.raises(M6CircuitOpen):
            await recorder.acquire(operation=M6Operation.LLM_TOOL_CALL)
        rejected = store.events[-1]
        assert rejected.outcome is M6OperationOutcome.REJECTED
        assert rejected.safe_code == "CIRCUIT_OPEN"
        assert rejected.duration_ms == 0

    import asyncio

    asyncio.run(scenario())


@pytest.mark.spec("GLO-M6-P0-006")
def test_retention_report_has_only_bounded_identity_free_counts() -> None:
    from glodex.observability.runtime import M6RetentionReport

    assert (
        M6RetentionReport(
            deleted_trace_count=2,
            deleted_event_count=5,
            deleted_metric_bucket_count=1,
        ).deleted_event_count
        == 5
    )
    with pytest.raises(ValueError):
        M6RetentionReport(
            deleted_trace_count=-1,
            deleted_event_count=0,
            deleted_metric_bucket_count=0,
        )


@dataclass
class _M6Redis:
    values: dict[str, bytes] = field(default_factory=dict)

    async def get(self, key: str) -> bytes | None:
        return self.values.get(key)

    async def set(self, key: str, value: bytes, *, ex: int) -> None:
        assert ex == 300
        self.values[key] = value


def test_redis_operations_cache_contains_only_rebuildable_identity_free_aggregate() -> None:
    from glodex.infrastructure.redis import DurableRedisCache

    value = M6OperationsView(
        window=M6OperationsWindow.ONE_HOUR,
        completed_count=1,
        no_match_count=0,
        failed_count=0,
        aborted_count=0,
        operation_count=2,
        operation_failure_count=0,
        receipt_reported_count=1,
        receipt_unavailable_count=0,
        cost_totals=(M6CostTotal(currency="USD", micro_units=12),),
        breakers=(
            M6BreakerView(
                operation=M6Operation.LLM_TOOL_CALL,
                state=M6BreakerState.CLOSED,
            ),
        ),
        alerts=(),
    )
    redis = _M6Redis()
    cache = DurableRedisCache(client=redis)

    import asyncio

    asyncio.run(cache.set_m6_operations_projection(value=value, ttl_seconds=300))
    assert (
        asyncio.run(cache.get_m6_operations_projection(window=M6OperationsWindow.ONE_HOUR)) == value
    )
    assert len(redis.values) == 1
    key, payload = next(iter(redis.values.items()))
    assert "run" not in key and "user" not in key
    rendered = payload.decode("ascii").casefold()
    for forbidden in ("query", "prompt", "memory", "history", "credential", "vector", "score"):
        assert forbidden not in rendered
