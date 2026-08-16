"""Fixed-loopback PostgreSQL truth store for the opt-in Durable runtime."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, Final, cast
from urllib.parse import urlsplit

from glodex.infrastructure.migrations import apply_migrations
from glodex.memory.identity import LocalSession, LocalUser, validate_user_id
from glodex.memory.models import (
    ConversationRole,
    ConversationThreadInfo,
    ConversationTurn,
    ConversationTurnPage,
    MemoryCandidate,
    ThreadSummary,
    UserMemoryCategory,
    UserMemoryEntry,
    UserMemoryOrigin,
    canonical_memory_key,
    explicit_memory_entry_id,
    memory_entry_id,
)
from glodex.memory.normalization import persisted_memory_content
from glodex.observability.runtime import (
    CostEstimate,
    M6AlertCode,
    M6BreakerSnapshot,
    M6BreakerState,
    M6BreakerView,
    M6CostStatus,
    M6CostTotal,
    M6Operation,
    M6OperationOutcome,
    M6OperationsView,
    M6OperationsWindow,
    M6PermitDecision,
    M6ReceiptStatus,
    M6RetentionReport,
    M6RunTrace,
    M6TraceEvent,
    M6TraceEventDraft,
    M6TraceEventKind,
    ModelPriceTableEntry,
    ProviderUsageReceipt,
    acquire_breaker,
    m6_operations_alerts,
    record_breaker_outcome,
)
from glodex.quality.runtime import (
    M7_SUMMARY_SCHEMA_VERSION,
    M7JudgeStatus,
    M7OfflineSummary,
    M7P2Dimension,
    M7P2ReasonCode,
    M7P2Score,
)
from glodex.runtime.contracts import (
    TERMINAL_RUN_STATES,
    DurableCheckpoint,
    DurableCheckpointState,
    DurableEvent,
    DurableRun,
    DurableRunState,
    DurableStoreError,
    LoopKind,
    validate_identifier,
)

_DEFAULT_DSN: Final = "postgresql://glodex@127.0.0.1:5433/glodex"
_LEASE_SECONDS: Final = 300


def validate_postgres_dsn(value: object) -> str:
    """Accept only the one Durable local PostgreSQL service without credentials."""

    if type(value) is not str or not value or len(value) > 160:
        raise ValueError("Durable PostgreSQL DSN is invalid")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or parsed.hostname != "127.0.0.1"
        or parsed.port != 5433
        or parsed.username != "glodex"
        or parsed.password is not None
        or parsed.path != "/glodex"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Durable PostgreSQL DSN must be the fixed loopback service")
    return _DEFAULT_DSN


class DurablePostgresStore:
    """One asyncpg owner with an intentionally finite Durable query surface."""

    def __init__(self, *, dsn: str = _DEFAULT_DSN, pool: Any | None = None) -> None:
        self._dsn = validate_postgres_dsn(dsn)
        self._pool = pool

    async def open(self) -> None:
        if self._pool is not None:
            return
        try:
            import asyncpg  # type: ignore[import-untyped]

            self._pool = await asyncpg.create_pool(
                dsn=self._dsn,
                min_size=1,
                max_size=4,
                command_timeout=5.0,
                max_inactive_connection_lifetime=30.0,
            )
        except Exception as error:
            raise DurableStoreError("DURABLE_STORE_UNAVAILABLE") from error

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is None:
            return
        try:
            await pool.close()
        except Exception:
            return

    async def health(self) -> None:
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection:
                value = await connection.fetchval("SELECT 1")
            if value != 1:
                raise ValueError("health response is invalid")
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("DURABLE_STORE_UNAVAILABLE") from error

    async def migrate(self) -> int:
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection, connection.transaction():
                return await apply_migrations(connection)
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("DURABLE_MIGRATION_FAILED") from error

    async def store_m7_offline_summary(self, *, summary: M7OfflineSummary) -> None:
        """Persist only the bounded display summary, never the private M7 report."""

        if type(summary) is not M7OfflineSummary:
            raise TypeError("M7 offline summary is invalid")
        scores = {value.dimension: value.score for value in summary.p2_scores}
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection:
                await connection.execute(
                    """
                    INSERT INTO m7_offline_summaries(
                        run_id, schema_version, judge_status, p0_passed,
                        p0_failure_count, p1_failure_count, need_coverage_score,
                        scenario_fit_score, decision_value_score, reward,
                        training_candidate, judge_model, generated_at
                    ) VALUES(
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13
                    )
                    ON CONFLICT (run_id) DO UPDATE SET
                        schema_version = EXCLUDED.schema_version,
                        judge_status = EXCLUDED.judge_status,
                        p0_passed = EXCLUDED.p0_passed,
                        p0_failure_count = EXCLUDED.p0_failure_count,
                        p1_failure_count = EXCLUDED.p1_failure_count,
                        need_coverage_score = EXCLUDED.need_coverage_score,
                        scenario_fit_score = EXCLUDED.scenario_fit_score,
                        decision_value_score = EXCLUDED.decision_value_score,
                        reward = EXCLUDED.reward,
                        training_candidate = EXCLUDED.training_candidate,
                        judge_model = EXCLUDED.judge_model,
                        generated_at = EXCLUDED.generated_at
                    """,
                    summary.run_id,
                    M7_SUMMARY_SCHEMA_VERSION,
                    summary.judge_status.value,
                    summary.p0_passed,
                    summary.p0_failure_count,
                    summary.p1_failure_count,
                    scores.get(M7P2Dimension.NEED_COVERAGE),
                    scores.get(M7P2Dimension.SCENARIO_FIT),
                    scores.get(M7P2Dimension.DECISION_VALUE),
                    summary.reward,
                    summary.training_candidate,
                    summary.judge_model,
                    summary.generated_at,
                )
        except Exception as error:
            raise DurableStoreError("DURABLE_M7_SUMMARY_UNAVAILABLE") from error

    async def load_m7_offline_summary(self, *, run_id: str) -> M7OfflineSummary | None:
        """Read one safe M7 summary; the HTTP route enforces Run ownership."""

        validate_identifier(run_id, name="M7 summary run ID")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection:
                row = await connection.fetchrow(
                    "SELECT * FROM m7_offline_summaries WHERE run_id = $1",
                    run_id,
                )
            return None if row is None else _m7_offline_summary_from_row(row)
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("DURABLE_M7_SUMMARY_UNAVAILABLE") from error

    async def ensure_m6_trace(self, *, run_id: str) -> M6RunTrace:
        """Create the one safe trace header for an existing durable run, idempotently."""

        validate_identifier(run_id, name="M6 run ID")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection, connection.transaction():
                durable_run = await connection.fetchrow(
                    "SELECT run_id FROM durable_runs WHERE run_id = $1 FOR KEY SHARE",
                    run_id,
                )
                if durable_run is None:
                    raise DurableStoreError("DURABLE_RUN_NOT_FOUND")
                await connection.execute(
                    """
                    INSERT INTO m6_run_traces(run_id, schema_version)
                    VALUES($1, 'glodex.m6-safe-trace.v1')
                    ON CONFLICT (run_id) DO NOTHING
                    """,
                    run_id,
                )
                header = await connection.fetchrow(
                    "SELECT run_id, terminal_state FROM m6_run_traces WHERE run_id = $1",
                    run_id,
                )
            return M6RunTrace(
                run_id=str(header["run_id"]),
                events=(),
                terminal_state=(
                    None if header["terminal_state"] is None else str(header["terminal_state"])
                ),
            )
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("DURABLE_M6_TRACE_UNAVAILABLE") from error

    async def append_m6_trace_event(
        self,
        *,
        run_id: str,
        draft: M6TraceEventDraft,
    ) -> M6TraceEvent:
        """Allocate one append-only safe event under the trace header row lock."""

        validate_identifier(run_id, name="M6 run ID")
        if type(draft) is not M6TraceEventDraft:
            raise TypeError("M6 trace draft is invalid")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection, connection.transaction():
                header = await connection.fetchrow(
                    "SELECT event_sequence FROM m6_run_traces WHERE run_id = $1 FOR UPDATE",
                    run_id,
                )
                if header is None:
                    raise DurableStoreError("DURABLE_M6_TRACE_NOT_FOUND")
                sequence = int(header["event_sequence"]) + 1
                event = M6TraceEvent(run_id=run_id, sequence=sequence, draft=draft)
                receipt = draft.receipt
                cost = draft.cost
                await connection.execute(
                    """
                    INSERT INTO m6_trace_events(
                        run_id, sequence, kind, operation, outcome, safe_code, duration_ms,
                        version, receipt_status, provider, model, input_tokens, output_tokens,
                        total_tokens, cost_status, price_table_version, currency,
                        input_micro_units, output_micro_units
                    ) VALUES(
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16,
                        $17, $18, $19
                    )
                    """,
                    event.run_id,
                    event.sequence,
                    draft.kind.value,
                    None if draft.operation is None else draft.operation.value,
                    None if draft.outcome is None else draft.outcome.value,
                    draft.safe_code,
                    draft.duration_ms,
                    draft.version,
                    None if receipt is None else receipt.status.value,
                    None if receipt is None else receipt.provider,
                    None if receipt is None else receipt.model,
                    None if receipt is None else receipt.input_tokens,
                    None if receipt is None else receipt.output_tokens,
                    None if receipt is None else receipt.total_tokens,
                    None if cost is None else cost.status.value,
                    None if cost is None else cost.price_table_version,
                    None if cost is None else cost.currency,
                    None if cost is None else cost.input_micro_units,
                    None if cost is None else cost.output_micro_units,
                )
                terminal_state = (
                    draft.safe_code if draft.kind is M6TraceEventKind.TERMINAL else None
                )
                await connection.execute(
                    """
                    UPDATE m6_run_traces
                    SET event_sequence = $2,
                        terminal_state = COALESCE($3, terminal_state),
                        terminal_at = CASE WHEN $3 IS NULL THEN terminal_at
                            ELSE CURRENT_TIMESTAMP END
                    WHERE run_id = $1
                    """,
                    run_id,
                    sequence,
                    terminal_state,
                )
            return event
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("DURABLE_M6_TRACE_WRITE_FAILED") from error

    async def load_m6_trace(self, *, run_id: str) -> M6RunTrace:
        """Read only the bounded safe trace; Durable routes enforce the owner separately."""

        validate_identifier(run_id, name="M6 run ID")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection:
                header = await connection.fetchrow(
                    "SELECT run_id, terminal_state FROM m6_run_traces WHERE run_id = $1",
                    run_id,
                )
                if header is None:
                    raise DurableStoreError("DURABLE_M6_TRACE_NOT_FOUND")
                rows = await connection.fetch(
                    """
                    SELECT run_id, sequence, kind, operation, outcome, safe_code, duration_ms,
                        version, receipt_status, provider, model, input_tokens, output_tokens,
                        total_tokens, cost_status, price_table_version, currency,
                        input_micro_units, output_micro_units
                    FROM m6_trace_events WHERE run_id = $1 ORDER BY sequence ASC
                    """,
                    run_id,
                )
            return M6RunTrace(
                run_id=str(header["run_id"]),
                events=tuple(_m6_trace_event_from_row(row) for row in rows),
                terminal_state=(
                    None if header["terminal_state"] is None else str(header["terminal_state"])
                ),
            )
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("DURABLE_M6_TRACE_UNAVAILABLE") from error

    async def claim_m6_trace_export(self, *, run_id: str) -> bool:
        """Claim one terminal trace export forever; exporter faults never release it."""

        validate_identifier(run_id, name="M6 export run ID")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection:
                row = await connection.fetchrow(
                    """
                    UPDATE m6_run_traces
                    SET export_attempted = TRUE
                    WHERE run_id = $1 AND terminal_state IS NOT NULL AND export_attempted = FALSE
                    RETURNING run_id
                    """,
                    run_id,
                )
            return row is not None
        except Exception as error:
            raise DurableStoreError("DURABLE_M6_EXPORT_UNAVAILABLE") from error

    async def prune_m6_retention(self, *, now: datetime) -> M6RetentionReport:
        """Apply fixed 14-day detail and 30-day aggregate retention outside requests."""

        if now.tzinfo is None:
            raise TypeError("M6 retention clock is invalid")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection, connection.transaction():
                deleted_event_count = await connection.fetchval(
                    """
                    WITH deleted AS (
                        DELETE FROM m6_trace_events
                        WHERE run_id IN (
                            SELECT run_id FROM m6_run_traces WHERE expires_at <= $1
                        )
                        RETURNING 1
                    ) SELECT COUNT(*)::integer FROM deleted
                    """,
                    now,
                )
                deleted_trace_count = await connection.fetchval(
                    """
                    WITH deleted AS (
                        DELETE FROM m6_run_traces WHERE expires_at <= $1 RETURNING 1
                    ) SELECT COUNT(*)::integer FROM deleted
                    """,
                    now,
                )
                deleted_metric_bucket_count = await connection.fetchval(
                    """
                    WITH deleted AS (
                        DELETE FROM m6_metric_buckets WHERE expires_at <= $1 RETURNING 1
                    ) SELECT COUNT(*)::integer FROM deleted
                    """,
                    now,
                )
                await connection.execute(
                    "DELETE FROM m6_maintenance_alerts WHERE code = 'RETENTION_FAILED'"
                )
            if any(
                type(value) is not int or value < 0
                for value in (deleted_trace_count, deleted_event_count, deleted_metric_bucket_count)
            ):
                raise DurableStoreError("DURABLE_STORE_DATA_INVALID")
            return M6RetentionReport(
                deleted_trace_count=deleted_trace_count,
                deleted_event_count=deleted_event_count,
                deleted_metric_bucket_count=deleted_metric_bucket_count,
            )
        except DurableStoreError:
            await self._record_m6_retention_failure_safely()
            raise
        except Exception as error:
            await self._record_m6_retention_failure_safely()
            raise DurableStoreError("DURABLE_M6_RETENTION_UNAVAILABLE") from error

    async def load_m6_operations(
        self,
        *,
        window: M6OperationsWindow,
        now: datetime,
    ) -> M6OperationsView:
        """Read an aggregate-only M6 operations view; no run/user/thread value is selected."""

        if type(window) is not M6OperationsWindow or now.tzinfo is None:
            raise TypeError("M6 operations read input is invalid")
        window_seconds = 3_600 if window is M6OperationsWindow.ONE_HOUR else 86_400
        since = now - timedelta(seconds=window_seconds)
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection:
                terminal_counts = await connection.fetchrow(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE terminal_state = 'COMPLETED')::integer
                            AS completed_count,
                        COUNT(*) FILTER (WHERE terminal_state = 'NO_MATCH')::integer
                            AS no_match_count,
                        COUNT(*) FILTER (WHERE terminal_state = 'FAILED')::integer AS failed_count,
                        COUNT(*) FILTER (WHERE terminal_state = 'ABORTED')::integer AS aborted_count
                    FROM m6_run_traces
                    WHERE terminal_at IS NOT NULL AND terminal_at >= $1
                    """,
                    since,
                )
                event_counts = await connection.fetchrow(
                    """
                    SELECT
                        COUNT(*) FILTER (
                            WHERE kind = 'OPERATION_FINISHED'
                        )::integer AS operation_count,
                        COUNT(*) FILTER (
                            WHERE outcome = 'FAILURE'
                        )::integer AS operation_failure_count,
                        COUNT(*) FILTER (
                            WHERE receipt_status = 'REPORTED'
                        )::integer AS receipt_reported_count,
                        COUNT(*) FILTER (
                            WHERE receipt_status = 'UNAVAILABLE'
                        )::integer AS receipt_unavailable_count
                    FROM m6_trace_events WHERE created_at >= $1
                    """,
                    since,
                )
                cost_rows = await connection.fetch(
                    """
                    SELECT currency,
                        SUM(input_micro_units + output_micro_units)::bigint AS micro_units
                    FROM m6_trace_events
                    WHERE created_at >= $1 AND cost_status = 'REPORTED' AND currency IS NOT NULL
                    GROUP BY currency ORDER BY currency ASC
                    """,
                    since,
                )
                breaker_rows = await connection.fetch(
                    "SELECT operation, state FROM m6_breakers ORDER BY operation ASC"
                )
                maintenance_alert_rows = await connection.fetch(
                    "SELECT code FROM m6_maintenance_alerts WHERE active = TRUE ORDER BY code ASC"
                )
            if terminal_counts is None or event_counts is None:
                raise DurableStoreError("DURABLE_STORE_DATA_INVALID")
            breakers = tuple(
                M6BreakerView(
                    operation=M6Operation(str(row["operation"])),
                    state=M6BreakerState(str(row["state"])),
                )
                for row in breaker_rows
            )
            operation_count = int(event_counts["operation_count"])
            operation_failure_count = int(event_counts["operation_failure_count"])
            receipt_unavailable_count = int(event_counts["receipt_unavailable_count"])
            maintenance_alerts = tuple(
                M6AlertCode(str(row["code"])) for row in maintenance_alert_rows
            )
            return M6OperationsView(
                window=window,
                completed_count=int(terminal_counts["completed_count"]),
                no_match_count=int(terminal_counts["no_match_count"]),
                failed_count=int(terminal_counts["failed_count"]),
                aborted_count=int(terminal_counts["aborted_count"]),
                operation_count=operation_count,
                operation_failure_count=operation_failure_count,
                receipt_reported_count=int(event_counts["receipt_reported_count"]),
                receipt_unavailable_count=receipt_unavailable_count,
                cost_totals=tuple(
                    M6CostTotal(currency=str(row["currency"]), micro_units=int(row["micro_units"]))
                    for row in cost_rows
                ),
                breakers=breakers,
                alerts=m6_operations_alerts(
                    operation_count=operation_count,
                    operation_failure_count=operation_failure_count,
                    receipt_unavailable_count=receipt_unavailable_count,
                    breakers=breakers,
                    maintenance_alerts=maintenance_alerts,
                ),
            )
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("DURABLE_M6_OPERATIONS_UNAVAILABLE") from error

    async def _record_m6_retention_failure_safely(self) -> None:
        """Preserve only the stable maintenance code when the store remains reachable."""

        try:
            pool = await self._require_pool()
            async with pool.acquire() as connection:
                await connection.execute(
                    """
                    INSERT INTO m6_maintenance_alerts(code, active)
                    VALUES('RETENTION_FAILED', TRUE)
                    ON CONFLICT (code) DO UPDATE
                    SET active = TRUE, updated_at = CURRENT_TIMESTAMP
                    """
                )
        except Exception:
            return

    async def upsert_m6_price_table(self, *, entry: ModelPriceTableEntry) -> None:
        """Persist one explicit operator price row; no provider billing data is queried."""

        if type(entry) is not ModelPriceTableEntry:
            raise TypeError("M6 price table entry is invalid")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection:
                await connection.execute(
                    """
                    INSERT INTO m6_price_tables(
                        model, price_table_version, currency, input_micro_units_per_token,
                        output_micro_units_per_token, source_label
                    ) VALUES($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (model, price_table_version) DO UPDATE
                    SET currency = EXCLUDED.currency,
                        input_micro_units_per_token = EXCLUDED.input_micro_units_per_token,
                        output_micro_units_per_token = EXCLUDED.output_micro_units_per_token,
                        source_label = EXCLUDED.source_label,
                        effective_at = CURRENT_TIMESTAMP
                    """,
                    entry.model,
                    entry.price_table_version,
                    entry.currency,
                    entry.input_micro_units_per_token,
                    entry.output_micro_units_per_token,
                    entry.source_label,
                )
        except Exception as error:
            raise DurableStoreError("DURABLE_M6_PRICE_TABLE_UNAVAILABLE") from error

    async def acquire_m6_breaker(
        self,
        *,
        operation: M6Operation,
        now: datetime,
    ) -> M6PermitDecision:
        """Persist the permit transition under the one operation row lock."""

        if type(operation) is not M6Operation or now.tzinfo is None:
            raise TypeError("M6 breaker permit input is invalid")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection, connection.transaction():
                snapshot = await _locked_m6_breaker(connection, operation=operation)
                decision, updated = acquire_breaker(snapshot=snapshot, now=now)
                if updated != snapshot:
                    await _store_m6_breaker(connection, snapshot=updated)
            return decision
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("DURABLE_M6_BREAKER_UNAVAILABLE") from error

    async def record_m6_breaker(
        self,
        *,
        operation: M6Operation,
        now: datetime,
        retryable_failure: bool,
    ) -> M6BreakerSnapshot:
        """Persist exactly one post-call breaker outcome without retrying the call."""

        if (
            type(operation) is not M6Operation
            or now.tzinfo is None
            or type(retryable_failure) is not bool
        ):
            raise TypeError("M6 breaker record input is invalid")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection, connection.transaction():
                snapshot = await _locked_m6_breaker(connection, operation=operation)
                updated = record_breaker_outcome(
                    snapshot=snapshot,
                    now=now,
                    retryable_failure=retryable_failure,
                )
                await _store_m6_breaker(connection, snapshot=updated)
            return updated
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("DURABLE_M6_BREAKER_UNAVAILABLE") from error

    async def reserve_root_run(
        self,
        *,
        run_id: str,
        thread_id: str,
        request_payload: dict[str, object],
        asset_version: str,
        config_fingerprint: str,
    ) -> DurableRun:
        validate_identifier(run_id, name="run ID")
        validate_identifier(thread_id, name="thread ID")
        if (
            type(request_payload) is not dict
            or type(asset_version) is not str
            or not asset_version
            or type(config_fingerprint) is not str
            or len(config_fingerprint) != 64
        ):
            raise ValueError("durable run reservation is invalid")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection, connection.transaction():
                row = await connection.fetchrow(
                    """
                        INSERT INTO durable_runs(
                            run_id, thread_id, root_run_id, parent_run_id, child_id,
                            depth, loop_kind, task_scope_digest, state, asset_version,
                            config_fingerprint, request_payload, lease_expires_at
                        ) VALUES($1, $2, $1, NULL, NULL, 0, 'root', NULL, 'ACCEPTED',
                            $3, $4, $5::jsonb, $6)
                        RETURNING *
                        """,
                    run_id,
                    thread_id,
                    asset_version,
                    config_fingerprint,
                    _json_text(request_payload),
                    _lease_deadline(),
                )
            return _run_from_row(row)
        except Exception as error:
            if _unique_violation(error):
                raise DurableStoreError("DURABLE_RUN_ALREADY_ACTIVE") from None
            raise DurableStoreError("DURABLE_STORE_UNAVAILABLE") from error

    async def reserve_child_run(
        self,
        *,
        run_id: str,
        thread_id: str,
        root_run_id: str,
        parent_run_id: str,
        child_id: str,
        depth: int,
        task_scope_digest: str,
        request_payload: dict[str, object],
        asset_version: str,
        config_fingerprint: str,
    ) -> DurableRun:
        """Persist an internal child run under an already existing durable tree."""

        for value, name in (
            (run_id, "run ID"),
            (thread_id, "thread ID"),
            (root_run_id, "root run ID"),
            (parent_run_id, "parent run ID"),
            (child_id, "child ID"),
        ):
            validate_identifier(value, name=name)
        if (
            run_id == root_run_id
            or type(depth) is not int
            or isinstance(depth, bool)
            or not 1 <= depth <= 2
            or type(task_scope_digest) is not str
            or len(task_scope_digest) != 64
            or any(character not in "0123456789abcdef" for character in task_scope_digest)
            or type(request_payload) is not dict
            or type(asset_version) is not str
            or not asset_version
            or type(config_fingerprint) is not str
            or len(config_fingerprint) != 64
        ):
            raise ValueError("durable child run reservation is invalid")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection, connection.transaction():
                parent = await connection.fetchrow(
                    """
                    SELECT run_id, root_run_id, depth, state, cancel_requested
                    FROM durable_runs WHERE run_id = $1 FOR KEY SHARE
                    """,
                    parent_run_id,
                )
                if parent is None:
                    raise DurableStoreError("DURABLE_PARENT_RUN_NOT_FOUND")
                if str(parent["root_run_id"]) != root_run_id or int(parent["depth"]) + 1 != depth:
                    raise DurableStoreError("DURABLE_CHILD_TREE_INVALID")
                if (
                    bool(parent["cancel_requested"])
                    or DurableRunState(str(parent["state"])) in TERMINAL_RUN_STATES
                ):
                    raise DurableStoreError("DURABLE_PARENT_RUN_NOT_ACTIVE")
                row = await connection.fetchrow(
                    """
                    INSERT INTO durable_runs(
                        run_id, thread_id, root_run_id, parent_run_id, child_id, depth,
                        loop_kind, task_scope_digest, state, asset_version,
                        config_fingerprint, request_payload, lease_expires_at
                    ) VALUES($1, $2, $3, $4, $5, $6, 'child', $7, 'ACCEPTED', $8,
                        $9, $10::jsonb, $11)
                    RETURNING *
                    """,
                    run_id,
                    thread_id,
                    root_run_id,
                    parent_run_id,
                    child_id,
                    depth,
                    task_scope_digest,
                    asset_version,
                    config_fingerprint,
                    _json_text(request_payload),
                    _lease_deadline(),
                )
            return _run_from_row(row)
        except DurableStoreError:
            raise
        except Exception as error:
            if _unique_violation(error):
                raise DurableStoreError("DURABLE_CHILD_RUN_ALREADY_EXISTS") from None
            raise DurableStoreError("DURABLE_STORE_UNAVAILABLE") from error

    async def load_run(self, *, run_id: str) -> DurableRun:
        validate_identifier(run_id, name="run ID")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection:
                row = await connection.fetchrow(
                    "SELECT * FROM durable_runs WHERE run_id = $1",
                    run_id,
                )
            if row is None:
                raise DurableStoreError("DURABLE_RUN_NOT_FOUND")
            return _run_from_row(row)
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("DURABLE_STORE_UNAVAILABLE") from error

    async def load_run_tree(self, *, root_run_id: str) -> tuple[DurableRun, ...]:
        """Load the root and every internal child in deterministic tree order."""

        validate_identifier(root_run_id, name="root run ID")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection:
                rows = await connection.fetch(
                    """
                    SELECT * FROM durable_runs
                    WHERE root_run_id = $1
                    ORDER BY depth ASC, created_at ASC, run_id ASC
                    """,
                    root_run_id,
                )
            if not rows:
                raise DurableStoreError("DURABLE_RUN_NOT_FOUND")
            return tuple(_run_from_row(row) for row in rows)
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("DURABLE_STORE_UNAVAILABLE") from error

    async def load_events(
        self,
        *,
        run_id: str,
        after_sequence: int = 0,
    ) -> tuple[DurableEvent, ...]:
        validate_identifier(run_id, name="run ID")
        if (
            type(after_sequence) is not int
            or isinstance(after_sequence, bool)
            or after_sequence < 0
        ):
            raise ValueError("event cursor is invalid")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection:
                rows = await connection.fetch(
                    """
                    SELECT run_id, sequence, payload, payload_hash
                    FROM durable_events WHERE run_id = $1 AND sequence > $2 ORDER BY sequence ASC
                    """,
                    run_id,
                    after_sequence,
                )
            return tuple(_event_from_row(row) for row in rows)
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("DURABLE_STORE_UNAVAILABLE") from error

    async def latest_checkpoint(self, *, run_id: str) -> DurableCheckpoint | None:
        validate_identifier(run_id, name="run ID")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection:
                row = await connection.fetchrow(
                    """
                    SELECT * FROM durable_checkpoints WHERE run_id = $1
                    ORDER BY checkpoint_number DESC LIMIT 1
                    """,
                    run_id,
                )
            return None if row is None else _checkpoint_from_row(row)
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("DURABLE_STORE_UNAVAILABLE") from error

    async def append_event_checkpoint(
        self,
        *,
        event: DurableEvent | None,
        checkpoint: DurableCheckpoint,
        next_state: DurableRunState,
    ) -> DurableRun:
        if type(checkpoint) is not DurableCheckpoint or type(next_state) is not DurableRunState:
            raise TypeError("durable append inputs are invalid")
        if event is not None and type(event) is not DurableEvent:
            raise TypeError("durable event is invalid")
        if event is not None and (
            event.run_id != checkpoint.run_id or event.sequence != checkpoint.event_sequence
        ):
            raise ValueError("event and checkpoint do not share a cursor")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection, connection.transaction():
                current = await connection.fetchrow(
                    "SELECT * FROM durable_runs WHERE run_id = $1 FOR UPDATE",
                    checkpoint.run_id,
                )
                if current is None:
                    raise DurableStoreError("DURABLE_RUN_NOT_FOUND")
                if bool(current["cancel_requested"]):
                    raise DurableStoreError("DURABLE_RUN_CANCELLED")
                previous_sequence = int(current["event_sequence"])
                if event is None:
                    if checkpoint.event_sequence != previous_sequence:
                        raise DurableStoreError("DURABLE_CHECKPOINT_CURSOR_INVALID")
                elif event.sequence != previous_sequence + 1:
                    raise DurableStoreError("DURABLE_EVENT_SEQUENCE_INVALID")
                checkpoint_number = await _next_checkpoint_number(connection, checkpoint.run_id)
                if checkpoint.checkpoint_number != checkpoint_number:
                    raise DurableStoreError("DURABLE_CHECKPOINT_SEQUENCE_INVALID")
                if event is not None:
                    await connection.execute(
                        """
                            INSERT INTO durable_events(run_id, sequence, payload, payload_hash)
                            VALUES($1, $2, $3::jsonb, $4)
                            """,
                        event.run_id,
                        event.sequence,
                        _json_text(event.payload),
                        event.payload_hash,
                    )
                await _insert_checkpoint(connection, checkpoint)
                row = await connection.fetchrow(
                    """
                        UPDATE durable_runs SET state = $2, event_sequence = $3,
                            lease_expires_at = $4, updated_at = CURRENT_TIMESTAMP
                        WHERE run_id = $1 RETURNING *
                        """,
                    checkpoint.run_id,
                    next_state.value,
                    checkpoint.event_sequence,
                    _lease_deadline() if next_state not in TERMINAL_RUN_STATES else None,
                )
            return _run_from_row(row)
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("DURABLE_STORE_WRITE_FAILED") from error

    async def finish_run(
        self,
        *,
        event: DurableEvent,
        checkpoint: DurableCheckpoint,
        state: DurableRunState,
        response: dict[str, object] | None,
        error_code: str | None,
    ) -> DurableRun:
        if state not in TERMINAL_RUN_STATES or type(event) is not DurableEvent:
            raise ValueError("durable terminal transition is invalid")
        if state is DurableRunState.ABORTED and response is not None:
            raise ValueError("aborted run cannot store a response")
        if state is not DurableRunState.ABORTED and type(response) is not dict:
            raise ValueError("business terminal requires response")
        if (
            state in {DurableRunState.COMPLETED, DurableRunState.NO_MATCH}
            and error_code is not None
        ):
            raise ValueError("successful terminal cannot have an error code")
        if state is DurableRunState.ABORTED and (type(error_code) is not str or not error_code):
            raise ValueError("aborted terminal requires an error code")
        if state is DurableRunState.FAILED and (type(error_code) is not str or not error_code):
            raise ValueError("failed terminal requires an error code")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection, connection.transaction():
                current = await connection.fetchrow(
                    "SELECT event_sequence, cancel_requested FROM durable_runs "
                    "WHERE run_id = $1 FOR UPDATE",
                    event.run_id,
                )
                if current is None:
                    raise DurableStoreError("DURABLE_RUN_NOT_FOUND")
                if bool(current["cancel_requested"]) and state is not DurableRunState.ABORTED:
                    raise DurableStoreError("DURABLE_RUN_CANCELLED")
                if event.sequence != int(current["event_sequence"]) + 1:
                    raise DurableStoreError("DURABLE_EVENT_SEQUENCE_INVALID")
                if checkpoint.state not in {
                    DurableCheckpointState.TERMINAL,
                    DurableCheckpointState.CANCELLED,
                }:
                    raise DurableStoreError("DURABLE_CHECKPOINT_TERMINAL_INVALID")
                checkpoint_number = await _next_checkpoint_number(connection, event.run_id)
                if checkpoint.checkpoint_number != checkpoint_number:
                    raise DurableStoreError("DURABLE_CHECKPOINT_SEQUENCE_INVALID")
                await connection.execute(
                    """
                        INSERT INTO durable_events(run_id, sequence, payload, payload_hash)
                        VALUES($1, $2, $3::jsonb, $4)
                        """,
                    event.run_id,
                    event.sequence,
                    _json_text(event.payload),
                    event.payload_hash,
                )
                await _insert_checkpoint(connection, checkpoint)
                row = await connection.fetchrow(
                    """
                        UPDATE durable_runs SET state = $2, event_sequence = $3,
                            terminal_response = $4::jsonb, terminal_error_code = $5,
                            lease_expires_at = NULL, terminal_at = CURRENT_TIMESTAMP,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE run_id = $1 RETURNING *
                        """,
                    event.run_id,
                    state.value,
                    event.sequence,
                    None if response is None else _json_text(response),
                    error_code,
                )
            return _run_from_row(row)
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("DURABLE_STORE_WRITE_FAILED") from error

    async def request_cancel(self, *, run_id: str) -> DurableRun:
        validate_identifier(run_id, name="run ID")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection, connection.transaction():
                row = await connection.fetchrow(
                    "SELECT * FROM durable_runs WHERE run_id = $1 FOR UPDATE",
                    run_id,
                )
                if row is None:
                    raise DurableStoreError("DURABLE_RUN_NOT_FOUND")
                current = _run_from_row(row)
                if current.state in TERMINAL_RUN_STATES:
                    return current
                row = await connection.fetchrow(
                    """
                        UPDATE durable_runs SET state = 'CANCEL_REQUESTED', cancel_requested = TRUE,
                            updated_at = CURRENT_TIMESTAMP WHERE run_id = $1 RETURNING *
                        """,
                    run_id,
                )
            return _run_from_row(row)
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("DURABLE_STORE_WRITE_FAILED") from error

    async def set_recoverable(self, *, run_id: str) -> DurableRun:
        """Convert only a confirmed, non-cancelled active run to RECOVERABLE."""

        checkpoint = await self.latest_checkpoint(run_id=run_id)
        if checkpoint is None or checkpoint.state is not DurableCheckpointState.CONFIRMED:
            raise DurableStoreError("DURABLE_REMOTE_STEP_UNCERTAIN")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection:
                row = await connection.fetchrow(
                    """
                    UPDATE durable_runs SET state = 'RECOVERABLE', lease_expires_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE run_id = $1 AND cancel_requested = FALSE
                      AND state IN ('ACCEPTED', 'RUNNING', 'RECOVERABLE')
                    RETURNING *
                    """,
                    run_id,
                )
            if row is None:
                raise DurableStoreError("DURABLE_RUN_NOT_RECOVERABLE")
            return _run_from_row(row)
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("DURABLE_STORE_WRITE_FAILED") from error

    async def register_local_user(
        self,
        *,
        user: LocalUser,
        password_hash: str,
    ) -> LocalUser:
        """Persist one local account; password verification remains above the adapter."""

        if type(user) is not LocalUser or type(password_hash) is not str or not password_hash:
            raise ValueError("local user registration is invalid")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection:
                row = await connection.fetchrow(
                    """
                    INSERT INTO local_users(user_id, username, password_hash)
                    VALUES($1, $2, $3)
                    RETURNING user_id, username
                    """,
                    user.user_id,
                    user.username,
                    password_hash,
                )
            return _local_user_from_row(row)
        except Exception as error:
            if _unique_violation(error):
                raise DurableStoreError("DURABLE_USERNAME_TAKEN") from None
            raise DurableStoreError("DURABLE_AUTH_UNAVAILABLE") from error

    async def ensure_local_user(
        self,
        *,
        user: LocalUser,
        password_hash: str,
    ) -> LocalUser:
        """Create or restore one application-owned default local account."""

        if type(user) is not LocalUser or type(password_hash) is not str or not password_hash:
            raise ValueError("default local user is invalid")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection, connection.transaction():
                row = await connection.fetchrow(
                    """
                    INSERT INTO local_users(user_id, username, password_hash)
                    VALUES($1, $2, $3)
                    ON CONFLICT (username) DO UPDATE SET
                        password_hash = EXCLUDED.password_hash,
                        deleted_at = NULL
                    RETURNING user_id, username
                    """,
                    user.user_id,
                    user.username,
                    password_hash,
                )
                await connection.execute(
                    """
                    UPDATE local_sessions SET revoked_at = CURRENT_TIMESTAMP
                    WHERE user_id = $1 AND revoked_at IS NULL
                    """,
                    str(row["user_id"]),
                )
            return _local_user_from_row(row)
        except Exception as error:
            raise DurableStoreError("DURABLE_AUTH_UNAVAILABLE") from error

    async def password_hash_for_username(self, *, username: str) -> tuple[LocalUser, str] | None:
        """Return one active account hash for an authentication service, never a public route."""

        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection:
                row = await connection.fetchrow(
                    """
                    SELECT user_id, username, password_hash FROM local_users
                    WHERE username = $1 AND deleted_at IS NULL
                    """,
                    username,
                )
            if row is None:
                return None
            return _local_user_from_row(row), str(row["password_hash"])
        except Exception as error:
            raise DurableStoreError("DURABLE_AUTH_UNAVAILABLE") from error

    async def issue_local_session(
        self,
        *,
        token_hash: str,
        user_id: str,
        expires_at: datetime,
    ) -> LocalSession:
        """Store only a hash of a freshly generated session token."""

        validate_user_id(user_id)
        if type(token_hash) is not str or len(token_hash) != 64 or expires_at.tzinfo is None:
            raise ValueError("local session is invalid")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection:
                row = await connection.fetchrow(
                    """
                    INSERT INTO local_sessions(token_hash, user_id, expires_at)
                    SELECT $1, user_id, $3 FROM local_users
                    WHERE user_id = $2 AND deleted_at IS NULL
                    RETURNING user_id, expires_at,
                        (SELECT username FROM local_users WHERE user_id = $2) AS username
                    """,
                    token_hash,
                    user_id,
                    expires_at,
                )
            if row is None:
                raise DurableStoreError("DURABLE_AUTH_INVALID")
            return _local_session_from_row(row)
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("DURABLE_AUTH_UNAVAILABLE") from error

    async def session_for_token_hash(self, *, token_hash: str) -> LocalSession | None:
        """Resolve only an active, unexpired local session."""

        if type(token_hash) is not str or len(token_hash) != 64:
            raise ValueError("session token hash is invalid")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection:
                row = await connection.fetchrow(
                    """
                    SELECT session.user_id, session.expires_at, users.username
                    FROM local_sessions AS session
                    JOIN local_users AS users ON users.user_id = session.user_id
                    WHERE session.token_hash = $1 AND session.revoked_at IS NULL
                      AND session.expires_at > CURRENT_TIMESTAMP AND users.deleted_at IS NULL
                    """,
                    token_hash,
                )
            return None if row is None else _local_session_from_row(row)
        except Exception as error:
            raise DurableStoreError("DURABLE_AUTH_UNAVAILABLE") from error

    async def revoke_local_session(self, *, token_hash: str) -> None:
        """Make a session unusable without ever returning its owner/token."""

        if type(token_hash) is not str or len(token_hash) != 64:
            raise ValueError("session token hash is invalid")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection:
                await connection.execute(
                    """
                    UPDATE local_sessions SET revoked_at = CURRENT_TIMESTAMP
                    WHERE token_hash = $1 AND revoked_at IS NULL
                    """,
                    token_hash,
                )
        except Exception as error:
            raise DurableStoreError("DURABLE_AUTH_UNAVAILABLE") from error

    async def bind_user_thread(self, *, user_id: str, thread_id: str) -> None:
        """Bind only a fresh thread, never allowing an old anonymous thread claim."""

        validate_user_id(user_id)
        validate_identifier(thread_id, name="thread ID")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection, connection.transaction():
                user = await connection.fetchrow(
                    "SELECT user_id FROM local_users WHERE user_id = $1 AND deleted_at IS NULL",
                    user_id,
                )
                if user is None:
                    raise DurableStoreError("DURABLE_AUTH_INVALID")
                existing = await connection.fetchrow(
                    """
                    SELECT user_id, deleted_at FROM user_thread_bindings
                    WHERE thread_id = $1 FOR UPDATE
                    """,
                    thread_id,
                )
                if existing is not None:
                    if existing["deleted_at"] is not None or str(existing["user_id"]) != user_id:
                        raise DurableStoreError("DURABLE_OWNER_FORBIDDEN")
                    return
                existing_durable_run = await connection.fetchrow(
                    "SELECT run_id FROM durable_runs WHERE thread_id = $1 LIMIT 1 FOR KEY SHARE",
                    thread_id,
                )
                if existing_durable_run is not None:
                    raise DurableStoreError("DURABLE_OWNER_FORBIDDEN")
                await connection.execute(
                    """
                    INSERT INTO user_thread_bindings(thread_id, user_id)
                    VALUES($1, $2)
                    """,
                    thread_id,
                    user_id,
                )
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("DURABLE_AUTH_UNAVAILABLE") from error

    async def thread_owner(self, *, thread_id: str) -> str | None:
        """Return only the opaque owner for a bound live thread."""

        validate_identifier(thread_id, name="thread ID")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection:
                row = await connection.fetchrow(
                    """
                    SELECT user_id FROM user_thread_bindings
                    WHERE thread_id = $1 AND deleted_at IS NULL
                    """,
                    thread_id,
                )
            return None if row is None else validate_user_id(str(row["user_id"]))
        except Exception as error:
            raise DurableStoreError("DURABLE_AUTH_UNAVAILABLE") from error

    async def delete_local_user(self, *, user_id: str) -> None:
        """Tombstone an account and revoke every session without deleting durable truth."""

        validate_user_id(user_id)
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection, connection.transaction():
                result = await connection.execute(
                    """
                    UPDATE local_users SET deleted_at = CURRENT_TIMESTAMP
                    WHERE user_id = $1 AND deleted_at IS NULL
                    """,
                    user_id,
                )
                if result.endswith(" 0"):
                    raise DurableStoreError("DURABLE_AUTH_INVALID")
                await connection.execute(
                    """
                    UPDATE local_sessions SET revoked_at = CURRENT_TIMESTAMP
                    WHERE user_id = $1 AND revoked_at IS NULL
                    """,
                    user_id,
                )
                await connection.execute(
                    """
                    UPDATE user_thread_bindings SET deleted_at = CURRENT_TIMESTAMP
                    WHERE user_id = $1 AND deleted_at IS NULL
                    """,
                    user_id,
                )
                await connection.execute(
                    """
                    UPDATE conversation_turns SET deleted_at = CURRENT_TIMESTAMP
                    WHERE thread_id IN (
                        SELECT thread_id FROM user_thread_bindings WHERE user_id = $1
                    ) AND deleted_at IS NULL
                    """,
                    user_id,
                )
                await connection.execute(
                    """
                    UPDATE thread_summaries SET deleted_at = CURRENT_TIMESTAMP
                    WHERE thread_id IN (
                        SELECT thread_id FROM user_thread_bindings WHERE user_id = $1
                    ) AND deleted_at IS NULL
                    """,
                    user_id,
                )
                await connection.execute(
                    """
                    UPDATE user_memory_entries SET deleted_at = CURRENT_TIMESTAMP
                    WHERE user_id = $1 AND deleted_at IS NULL
                    """,
                    user_id,
                )
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("DURABLE_AUTH_UNAVAILABLE") from error

    async def append_conversation_turn(
        self,
        *,
        thread_id: str,
        role: ConversationRole,
        display_content: str,
        terminal_run_id: str | None = None,
    ) -> ConversationTurn:
        """Append exactly one owner-bound display turn with a serialized ordinal."""

        if type(role) is not ConversationRole:
            raise ValueError("conversation role is invalid")
        validate_identifier(thread_id, name="thread ID")
        if terminal_run_id is not None:
            validate_identifier(terminal_run_id, name="terminal run ID")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection, connection.transaction():
                binding = await connection.fetchrow(
                    """
                    SELECT thread_id FROM user_thread_bindings
                    WHERE thread_id = $1 AND deleted_at IS NULL FOR UPDATE
                    """,
                    thread_id,
                )
                if binding is None:
                    raise DurableStoreError("DURABLE_OWNER_FORBIDDEN")
                ordinal_value = await connection.fetchval(
                    """
                    SELECT COALESCE(MAX(ordinal), 0) + 1 FROM conversation_turns
                    WHERE thread_id = $1
                    """,
                    thread_id,
                )
                if type(ordinal_value) is not int or ordinal_value < 1:
                    raise DurableStoreError("DURABLE_STORE_DATA_INVALID")
                row = await connection.fetchrow(
                    """
                    INSERT INTO conversation_turns(
                        thread_id, ordinal, role, display_content, terminal_run_id
                    ) VALUES($1, $2, $3, $4, $5)
                    RETURNING thread_id, ordinal, role, display_content, terminal_run_id
                    """,
                    thread_id,
                    ordinal_value,
                    role.value,
                    display_content,
                    terminal_run_id,
                )
            return _conversation_turn_from_row(row)
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("DURABLE_HISTORY_UNAVAILABLE") from error

    async def list_conversation_turns(
        self,
        *,
        thread_id: str,
        limit: int = 64,
    ) -> tuple[ConversationTurn, ...]:
        """Load chronological owner-bound display turns through one fixed cap."""

        validate_identifier(thread_id, name="thread ID")
        if type(limit) is not int or isinstance(limit, bool) or not 1 <= limit <= 64:
            raise ValueError("conversation turn limit is invalid")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection:
                rows = await connection.fetch(
                    """
                    SELECT thread_id, ordinal, role, display_content, terminal_run_id
                    FROM conversation_turns
                    WHERE thread_id = $1 AND deleted_at IS NULL
                    ORDER BY ordinal DESC LIMIT $2
                    """,
                    thread_id,
                    limit,
                )
            return tuple(_conversation_turn_from_row(row) for row in reversed(rows))
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("DURABLE_HISTORY_UNAVAILABLE") from error

    async def latest_conversation_ordinal(self, *, thread_id: str) -> int:
        """Read the latest active ordinal only; cached history is never trusted without it."""

        validate_identifier(thread_id, name="thread ID")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection:
                value = await connection.fetchval(
                    """
                    SELECT COALESCE(MAX(ordinal), 0) FROM conversation_turns
                    WHERE thread_id = $1 AND deleted_at IS NULL
                    """,
                    thread_id,
                )
            if type(value) is not int or value < 0:
                raise DurableStoreError("DURABLE_STORE_DATA_INVALID")
            return value
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("DURABLE_HISTORY_UNAVAILABLE") from error

    async def list_user_threads(
        self,
        *,
        user_id: str,
        limit: int = 32,
    ) -> tuple[ConversationThreadInfo, ...]:
        """List only active threads belonging to one active local account."""

        validate_user_id(user_id)
        if type(limit) is not int or isinstance(limit, bool) or not 1 <= limit <= 64:
            raise ValueError("conversation thread limit is invalid")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection:
                rows = await connection.fetch(
                    """
                    SELECT binding.thread_id, COUNT(turn.ordinal)::integer AS turn_count
                    FROM user_thread_bindings AS binding
                    LEFT JOIN conversation_turns AS turn
                        ON turn.thread_id = binding.thread_id AND turn.deleted_at IS NULL
                    WHERE binding.user_id = $1 AND binding.deleted_at IS NULL
                    GROUP BY binding.thread_id, binding.created_at
                    ORDER BY MAX(turn.created_at) DESC NULLS LAST, binding.created_at DESC
                    LIMIT $2
                    """,
                    user_id,
                    limit,
                )
            return tuple(
                ConversationThreadInfo(
                    thread_id=str(row["thread_id"]), turn_count=int(row["turn_count"])
                )
                for row in rows
            )
        except Exception as error:
            raise DurableStoreError("DURABLE_HISTORY_UNAVAILABLE") from error

    async def list_conversation_turn_page(
        self,
        *,
        thread_id: str,
        limit: int = 24,
        before_ordinal: int | None = None,
    ) -> ConversationTurnPage:
        """Load one owner-checked reverse page and return it in chronological order."""

        validate_identifier(thread_id, name="thread ID")
        if type(limit) is not int or isinstance(limit, bool) or not 1 <= limit <= 32:
            raise ValueError("conversation turn page limit is invalid")
        if before_ordinal is not None and (
            type(before_ordinal) is not int
            or isinstance(before_ordinal, bool)
            or before_ordinal < 1
        ):
            raise ValueError("conversation turn cursor is invalid")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection:
                rows = await connection.fetch(
                    """
                    SELECT thread_id, ordinal, role, display_content, terminal_run_id
                    FROM conversation_turns
                    WHERE thread_id = $1 AND deleted_at IS NULL
                      AND ($2::integer IS NULL OR ordinal < $2)
                    ORDER BY ordinal DESC LIMIT $3
                    """,
                    thread_id,
                    before_ordinal,
                    limit + 1,
                )
            selected = tuple(_conversation_turn_from_row(row) for row in rows[:limit])
            turns = tuple(reversed(selected))
            return ConversationTurnPage(
                thread_id=thread_id,
                turns=turns,
                next_before_ordinal=(turns[0].ordinal if len(rows) > limit and turns else None),
            )
        except Exception as error:
            raise DurableStoreError("DURABLE_HISTORY_UNAVAILABLE") from error

    async def delete_user_thread(self, *, user_id: str, thread_id: str) -> bool:
        """Tombstone an owned thread and automatic memory derived only from it."""

        validate_user_id(user_id)
        validate_identifier(thread_id, name="thread ID")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection, connection.transaction():
                binding = await connection.fetchrow(
                    """
                    SELECT thread_id FROM user_thread_bindings
                    WHERE thread_id = $1 AND user_id = $2 AND deleted_at IS NULL FOR UPDATE
                    """,
                    thread_id,
                    user_id,
                )
                if binding is None:
                    return False
                await connection.execute(
                    """
                    UPDATE user_thread_bindings SET deleted_at = CURRENT_TIMESTAMP
                    WHERE thread_id = $1
                    """,
                    thread_id,
                )
                await connection.execute(
                    """
                    UPDATE conversation_turns SET deleted_at = CURRENT_TIMESTAMP
                    WHERE thread_id = $1 AND deleted_at IS NULL
                    """,
                    thread_id,
                )
                await connection.execute(
                    """
                    UPDATE thread_summaries SET deleted_at = CURRENT_TIMESTAMP
                    WHERE thread_id = $1 AND deleted_at IS NULL
                    """,
                    thread_id,
                )
                await connection.execute(
                    """
                    UPDATE user_memory_entries SET deleted_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = $1 AND source_thread_id = $2 AND deleted_at IS NULL
                    """,
                    user_id,
                    thread_id,
                )
            return True
        except Exception as error:
            raise DurableStoreError("DURABLE_HISTORY_UNAVAILABLE") from error

    async def latest_thread_summary(self, *, thread_id: str) -> ThreadSummary | None:
        """Return only the active strict-model summary for one owner-bound thread."""

        validate_identifier(thread_id, name="thread ID")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection:
                row = await connection.fetchrow(
                    """
                    SELECT thread_id, revision, covered_through_ordinal, summary_json
                    FROM thread_summaries
                    WHERE thread_id = $1 AND deleted_at IS NULL
                    """,
                    thread_id,
                )
            return None if row is None else _thread_summary_from_row(row)
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("DURABLE_HISTORY_UNAVAILABLE") from error

    async def save_thread_summary(
        self,
        *,
        summary: ThreadSummary,
        expected_revision: int,
    ) -> ThreadSummary:
        """Persist a model-produced summary while retaining full display turn history."""

        if (
            type(summary) is not ThreadSummary
            or type(expected_revision) is not int
            or isinstance(expected_revision, bool)
            or expected_revision < 0
            or summary.revision != expected_revision + 1
        ):
            raise ValueError("thread summary is invalid")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection, connection.transaction():
                binding = await connection.fetchrow(
                    """
                    SELECT thread_id FROM user_thread_bindings
                    WHERE thread_id = $1 AND deleted_at IS NULL FOR UPDATE
                    """,
                    summary.thread_id,
                )
                if binding is None:
                    raise DurableStoreError("DURABLE_OWNER_FORBIDDEN")
                current = await connection.fetchrow(
                    """
                    SELECT revision, covered_through_ordinal FROM thread_summaries
                    WHERE thread_id = $1 FOR UPDATE
                    """,
                    summary.thread_id,
                )
                if expected_revision == 0:
                    if current is not None:
                        raise DurableStoreError("DURABLE_SUMMARY_CONFLICT")
                    row = await connection.fetchrow(
                        """
                        INSERT INTO thread_summaries(
                            thread_id, revision, covered_through_ordinal, summary_json, deleted_at
                        ) VALUES($1, $2, $3, $4::jsonb, NULL)
                        RETURNING thread_id, revision, covered_through_ordinal, summary_json
                        """,
                        summary.thread_id,
                        summary.revision,
                        summary.covered_through_ordinal,
                        _json_text({"summary": summary.summary}),
                    )
                else:
                    if (
                        current is None
                        or int(current["revision"]) != expected_revision
                        or int(current["covered_through_ordinal"])
                        >= summary.covered_through_ordinal
                    ):
                        raise DurableStoreError("DURABLE_SUMMARY_CONFLICT")
                    row = await connection.fetchrow(
                        """
                        UPDATE thread_summaries SET
                            revision = $2,
                            covered_through_ordinal = $3,
                            summary_json = $4::jsonb,
                            deleted_at = NULL,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE thread_id = $1
                        RETURNING thread_id, revision, covered_through_ordinal, summary_json
                        """,
                        summary.thread_id,
                        summary.revision,
                        summary.covered_through_ordinal,
                        _json_text({"summary": summary.summary}),
                    )
            return _thread_summary_from_row(row)
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("DURABLE_HISTORY_UNAVAILABLE") from error

    async def list_active_memory(
        self,
        *,
        user_id: str,
        limit: int = 64,
    ) -> tuple[UserMemoryEntry, ...]:
        """Load only memory that is allowed to affect a future run."""

        validate_user_id(user_id)
        if type(limit) is not int or isinstance(limit, bool) or not 1 <= limit <= 128:
            raise ValueError("user memory limit is invalid")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection:
                rows = await connection.fetch(
                    """
                    SELECT user_id, entry_id, category, origin, canonical_key, content,
                        source_thread_id, source_ordinal, confidence, revision
                    FROM user_memory_entries
                    WHERE user_id = $1 AND deleted_at IS NULL
                      AND origin IN ('manual', 'explicit_reflect')
                    ORDER BY category ASC, canonical_key ASC LIMIT $2
                    """,
                    user_id,
                    limit,
                )
            return tuple(_user_memory_from_row(row) for row in rows)
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("DURABLE_MEMORY_UNAVAILABLE") from error

    async def create_manual_memory(
        self,
        *,
        user_id: str,
        category: UserMemoryCategory,
        content: str,
    ) -> UserMemoryEntry:
        """Create or explicitly restore one owner-authored manual memory."""

        validate_user_id(user_id)
        if type(category) is not UserMemoryCategory:
            raise ValueError("user memory category is invalid")
        canonical_key = canonical_memory_key(category=category, content=content)
        entry_id = memory_entry_id(category=category, content=content)
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection, connection.transaction():
                user = await connection.fetchrow(
                    "SELECT user_id FROM local_users WHERE user_id = $1 AND deleted_at IS NULL",
                    user_id,
                )
                if user is None:
                    raise DurableStoreError("DURABLE_AUTH_INVALID")
                active = await connection.fetchrow(
                    """
                    SELECT user_id, entry_id, category, origin, canonical_key, content,
                        source_thread_id, source_ordinal, confidence, revision
                    FROM user_memory_entries
                    WHERE user_id = $1 AND category = $2 AND canonical_key = $3
                      AND deleted_at IS NULL AND origin IN ('manual', 'explicit_reflect')
                    FOR UPDATE
                    """,
                    user_id,
                    category.value,
                    canonical_key,
                )
                if active is not None:
                    if (
                        str(active["origin"]) == UserMemoryOrigin.MANUAL.value
                        and str(active["content"]) == content
                    ):
                        row = active
                    else:
                        row = await connection.fetchrow(
                            """
                            UPDATE user_memory_entries SET origin = 'manual', content = $3,
                                source_thread_id = NULL, source_ordinal = NULL,
                                confidence = 1.0, revision = revision + 1,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE user_id = $1 AND entry_id = $2 AND deleted_at IS NULL
                            RETURNING user_id, entry_id, category, origin, canonical_key, content,
                                source_thread_id, source_ordinal, confidence, revision
                            """,
                            user_id,
                            str(active["entry_id"]),
                            content,
                        )
                else:
                    existing = await connection.fetchrow(
                        """
                        SELECT entry_id FROM user_memory_entries
                        WHERE user_id = $1 AND entry_id = $2 FOR UPDATE
                        """,
                        user_id,
                        entry_id,
                    )
                if active is None and existing is None:
                    row = await connection.fetchrow(
                        """
                        INSERT INTO user_memory_entries(
                            user_id, entry_id, category, origin, canonical_key, content,
                            source_thread_id, source_ordinal, confidence, revision, deleted_at
                        ) VALUES($1, $2, $3, 'manual', $4, $5, NULL, NULL, 1.0, 1, NULL)
                        RETURNING user_id, entry_id, category, origin, canonical_key, content,
                            source_thread_id, source_ordinal, confidence, revision
                        """,
                        user_id,
                        entry_id,
                        category.value,
                        canonical_key,
                        content,
                    )
                elif active is None:
                    row = await connection.fetchrow(
                        """
                        UPDATE user_memory_entries SET category = $3, origin = 'manual',
                            canonical_key = $4, content = $5, source_thread_id = NULL,
                            source_ordinal = NULL, confidence = 1.0, revision = revision + 1,
                            deleted_at = NULL, updated_at = CURRENT_TIMESTAMP
                        WHERE user_id = $1 AND entry_id = $2
                        RETURNING user_id, entry_id, category, origin, canonical_key, content,
                            source_thread_id, source_ordinal, confidence, revision
                        """,
                        user_id,
                        entry_id,
                        category.value,
                        canonical_key,
                        content,
                    )
            return _user_memory_from_row(row)
        except DurableStoreError:
            raise
        except Exception as error:
            if _unique_violation(error):
                raise DurableStoreError("DURABLE_MEMORY_CONFLICT") from None
            raise DurableStoreError("DURABLE_MEMORY_UNAVAILABLE") from error

    async def write_explicit_reflect_memory(
        self,
        *,
        user_id: str,
        candidates: tuple[MemoryCandidate, ...],
        source_thread_id: str,
        source_ordinal: int,
    ) -> tuple[UserMemoryEntry, ...]:
        """Atomically persist a validated Reflect batch."""

        validate_user_id(user_id)
        if (
            type(candidates) is not tuple
            or not 1 <= len(candidates) <= 3
            or any(type(candidate) is not MemoryCandidate for candidate in candidates)
            or any(candidate.category is UserMemoryCategory.HISTORY for candidate in candidates)
            or type(source_thread_id) is not str
            or not source_thread_id
            or type(source_ordinal) is not int
            or isinstance(source_ordinal, bool)
            or source_ordinal < 1
        ):
            raise ValueError("explicit memory write is invalid")
        persisted_candidates = tuple(
            (candidate, persisted_memory_content(candidate)) for candidate in candidates
        )
        persisted_keys = tuple(
            canonical_memory_key(category=candidate.category, content=content)
            for candidate, content in persisted_candidates
        )
        if len(set(persisted_keys)) != len(candidates):
            raise ValueError("explicit memory write is invalid")
        validate_identifier(source_thread_id, name="thread ID")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection, connection.transaction():
                binding = await connection.fetchrow(
                    """
                    SELECT thread_id FROM user_thread_bindings
                    WHERE thread_id = $1 AND user_id = $2 AND deleted_at IS NULL FOR UPDATE
                    """,
                    source_thread_id,
                    user_id,
                )
                if binding is None:
                    raise DurableStoreError("DURABLE_OWNER_FORBIDDEN")

                existing_by_key: dict[str, Any] = {}
                for (candidate, _content), canonical_key in zip(
                    persisted_candidates, persisted_keys, strict=True
                ):
                    rows = await connection.fetch(
                        """
                        SELECT user_id, entry_id, category, origin, canonical_key, content,
                            source_thread_id, source_ordinal, confidence, revision, deleted_at
                        FROM user_memory_entries
                        WHERE user_id = $1 AND category = $2 AND canonical_key = $3
                        FOR UPDATE
                        """,
                        user_id,
                        candidate.category.value,
                        canonical_key,
                    )
                    if any(row["deleted_at"] is not None for row in rows):
                        return ()
                    active_rows = tuple(
                        row for row in rows if str(row["origin"]) in {"manual", "explicit_reflect"}
                    )
                    if any(str(row["origin"]) == "manual" for row in active_rows):
                        return ()
                    if active_rows:
                        if len(active_rows) != 1:
                            return ()
                        existing_by_key[canonical_key] = active_rows[0]

                persisted: list[UserMemoryEntry] = []
                for (candidate, content), canonical_key in zip(
                    persisted_candidates, persisted_keys, strict=True
                ):
                    existing = existing_by_key.get(canonical_key)
                    if existing is not None:
                        if str(existing["content"]) == content:
                            persisted.append(_user_memory_from_row(existing))
                            continue
                        row = await connection.fetchrow(
                            """
                            UPDATE user_memory_entries SET content = $3,
                                source_thread_id = $4, source_ordinal = $5,
                                confidence = 1.0, revision = revision + 1,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE user_id = $1 AND entry_id = $2
                              AND deleted_at IS NULL AND origin = 'explicit_reflect'
                            RETURNING user_id, entry_id, category, origin, canonical_key, content,
                                source_thread_id, source_ordinal, confidence, revision
                            """,
                            user_id,
                            str(existing["entry_id"]),
                            content,
                            source_thread_id,
                            source_ordinal,
                        )
                        persisted.append(_user_memory_from_row(row))
                        continue
                    row = await connection.fetchrow(
                        """
                        INSERT INTO user_memory_entries(
                            user_id, entry_id, category, origin, canonical_key, content,
                            source_thread_id, source_ordinal, confidence, revision, deleted_at
                        ) VALUES($1, $2, $3, 'explicit_reflect', $4, $5, $6, $7, 1.0, 1, NULL)
                        RETURNING user_id, entry_id, category, origin, canonical_key, content,
                            source_thread_id, source_ordinal, confidence, revision
                        """,
                        user_id,
                        explicit_memory_entry_id(
                            category=candidate.category,
                            content=content,
                            source_thread_id=source_thread_id,
                            source_ordinal=source_ordinal,
                        ),
                        candidate.category.value,
                        canonical_key,
                        content,
                        source_thread_id,
                        source_ordinal,
                    )
                    persisted.append(_user_memory_from_row(row))
            return tuple(persisted)
        except DurableStoreError:
            raise
        except Exception as error:
            if _unique_violation(error):
                raise DurableStoreError("DURABLE_MEMORY_CONFLICT") from None
            raise DurableStoreError("DURABLE_MEMORY_UNAVAILABLE") from error

    async def tombstone_memory(self, *, user_id: str, entry_id: str) -> bool:
        """Tombstone one entry, scoped by its owner, while preserving audit provenance."""

        validate_user_id(user_id)
        if type(entry_id) is not str or not entry_id:
            raise ValueError("user memory entry ID is invalid")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection:
                changed = await connection.fetchrow(
                    """
                    UPDATE user_memory_entries SET deleted_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = $1 AND entry_id = $2 AND deleted_at IS NULL
                    RETURNING entry_id
                    """,
                    user_id,
                    entry_id,
                )
            return changed is not None
        except Exception as error:
            raise DurableStoreError("DURABLE_MEMORY_UNAVAILABLE") from error

    async def replace_memory_as_manual(
        self,
        *,
        user_id: str,
        entry_id: str,
        category: UserMemoryCategory,
        content: str,
    ) -> UserMemoryEntry | None:
        """Edit a manual entry in place without accepting client provenance or vectors."""

        validate_user_id(user_id)
        if type(entry_id) is not str or not entry_id or type(category) is not UserMemoryCategory:
            raise ValueError("user memory replacement is invalid")
        canonical_key = canonical_memory_key(category=category, content=content)
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection, connection.transaction():
                existing = await connection.fetchrow(
                    """
                    SELECT entry_id FROM user_memory_entries
                    WHERE user_id = $1 AND entry_id = $2 AND deleted_at IS NULL FOR UPDATE
                    """,
                    user_id,
                    entry_id,
                )
                if existing is None:
                    return None
                conflicting = await connection.fetchrow(
                    """
                    SELECT entry_id FROM user_memory_entries
                    WHERE user_id = $1 AND category = $2 AND canonical_key = $3
                      AND deleted_at IS NULL AND origin IN ('manual', 'explicit_reflect')
                      AND entry_id <> $4
                    FOR UPDATE
                    """,
                    user_id,
                    category.value,
                    canonical_key,
                    entry_id,
                )
                if conflicting is not None:
                    raise DurableStoreError("DURABLE_MEMORY_CONFLICT")
                row = await connection.fetchrow(
                    """
                    UPDATE user_memory_entries SET category = $3, origin = 'manual',
                        canonical_key = $4, content = $5, source_thread_id = NULL,
                        source_ordinal = NULL, confidence = 1.0, revision = revision + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = $1 AND entry_id = $2 AND deleted_at IS NULL
                    RETURNING user_id, entry_id, category, origin, canonical_key, content,
                        source_thread_id, source_ordinal, confidence, revision
                    """,
                    user_id,
                    entry_id,
                    category.value,
                    canonical_key,
                    content,
                )
            return None if row is None else _user_memory_from_row(row)
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("DURABLE_MEMORY_UNAVAILABLE") from error

    async def _require_pool(self) -> Any:
        await self.open()
        if self._pool is None:
            raise DurableStoreError("DURABLE_STORE_UNAVAILABLE")
        return self._pool


async def _next_checkpoint_number(connection: Any, run_id: str) -> int:
    value = await connection.fetchval(
        "SELECT COALESCE(MAX(checkpoint_number), 0) + 1 FROM durable_checkpoints WHERE run_id = $1",
        run_id,
    )
    if type(value) is not int or value < 1:
        raise DurableStoreError("DURABLE_CHECKPOINT_SEQUENCE_INVALID")
    return value


async def _insert_checkpoint(connection: Any, checkpoint: DurableCheckpoint) -> None:
    await connection.execute(
        """
        INSERT INTO durable_checkpoints(
            run_id, checkpoint_number, state, event_sequence, runtime_payload, asset_version,
            config_fingerprint, profile_revision, payload_hash
        ) VALUES($1, $2, $3, $4, $5::jsonb, $6, $7, 0, $8)
        """,
        checkpoint.run_id,
        checkpoint.checkpoint_number,
        checkpoint.state.value,
        checkpoint.event_sequence,
        _json_text(checkpoint.runtime_payload),
        checkpoint.asset_version,
        checkpoint.config_fingerprint,
        checkpoint.payload_hash,
    )


async def _locked_m6_breaker(connection: Any, *, operation: M6Operation) -> M6BreakerSnapshot:
    """Create then lock the finite operation row; the caller owns the transaction."""

    await connection.execute(
        """
        INSERT INTO m6_breakers(operation, state)
        VALUES($1, 'CLOSED') ON CONFLICT (operation) DO NOTHING
        """,
        operation.value,
    )
    row = await connection.fetchrow(
        """
        SELECT operation, state, failure_timestamps, cooldown_until, half_open_probe
        FROM m6_breakers WHERE operation = $1 FOR UPDATE
        """,
        operation.value,
    )
    return _m6_breaker_from_row(row)


async def _store_m6_breaker(connection: Any, *, snapshot: M6BreakerSnapshot) -> None:
    await connection.execute(
        """
        UPDATE m6_breakers
        SET state = $2, failure_timestamps = $3::timestamptz[], cooldown_until = $4,
            half_open_probe = $5, updated_at = CURRENT_TIMESTAMP
        WHERE operation = $1
        """,
        snapshot.operation.value,
        snapshot.state.value,
        list(snapshot.failure_timestamps),
        snapshot.cooldown_until,
        snapshot.half_open_probe,
    )


def _m6_trace_event_from_row(row: Any) -> M6TraceEvent:
    if row is None:
        raise DurableStoreError("DURABLE_STORE_DATA_INVALID")
    try:
        receipt_status = row["receipt_status"]
        receipt = (
            None
            if receipt_status is None
            else ProviderUsageReceipt(
                status=M6ReceiptStatus(str(receipt_status)),
                provider=None if row["provider"] is None else str(row["provider"]),
                model=None if row["model"] is None else str(row["model"]),
                input_tokens=(None if row["input_tokens"] is None else int(row["input_tokens"])),
                output_tokens=(None if row["output_tokens"] is None else int(row["output_tokens"])),
                total_tokens=(None if row["total_tokens"] is None else int(row["total_tokens"])),
            )
        )
        cost_status = row["cost_status"]
        cost = (
            None
            if cost_status is None
            else CostEstimate(
                status=M6CostStatus(str(cost_status)),
                price_table_version=(
                    None if row["price_table_version"] is None else str(row["price_table_version"])
                ),
                currency=None if row["currency"] is None else str(row["currency"]),
                input_micro_units=(
                    None if row["input_micro_units"] is None else int(row["input_micro_units"])
                ),
                output_micro_units=(
                    None if row["output_micro_units"] is None else int(row["output_micro_units"])
                ),
            )
        )
        draft = M6TraceEventDraft(
            kind=M6TraceEventKind(str(row["kind"])),
            operation=(None if row["operation"] is None else M6Operation(str(row["operation"]))),
            outcome=(None if row["outcome"] is None else M6OperationOutcome(str(row["outcome"]))),
            safe_code=None if row["safe_code"] is None else str(row["safe_code"]),
            duration_ms=None if row["duration_ms"] is None else int(row["duration_ms"]),
            version=None if row["version"] is None else str(row["version"]),
            receipt=receipt,
            cost=cost,
        )
        return M6TraceEvent(
            run_id=str(row["run_id"]),
            sequence=int(row["sequence"]),
            draft=draft,
        )
    except Exception as error:
        raise DurableStoreError("DURABLE_STORE_DATA_INVALID") from error


def _m6_breaker_from_row(row: Any) -> M6BreakerSnapshot:
    if row is None:
        raise DurableStoreError("DURABLE_STORE_DATA_INVALID")
    try:
        raw_failures = row["failure_timestamps"]
        if type(raw_failures) not in {list, tuple}:
            raise ValueError("M6 failure timestamp collection is invalid")
        failures = tuple(raw_failures)
        if any(type(value) is not datetime for value in failures):
            raise ValueError("M6 failure timestamp is invalid")
        cooldown_until = row["cooldown_until"]
        if cooldown_until is not None and type(cooldown_until) is not datetime:
            raise ValueError("M6 cooldown is invalid")
        return M6BreakerSnapshot(
            operation=M6Operation(str(row["operation"])),
            state=M6BreakerState(str(row["state"])),
            failure_timestamps=cast("tuple[datetime, ...]", failures),
            cooldown_until=cooldown_until,
            half_open_probe=bool(row["half_open_probe"]),
        )
    except Exception as error:
        raise DurableStoreError("DURABLE_STORE_DATA_INVALID") from error


def _m7_offline_summary_from_row(row: Any) -> M7OfflineSummary:
    if row is None or str(row["schema_version"]) != M7_SUMMARY_SCHEMA_VERSION:
        raise DurableStoreError("DURABLE_STORE_DATA_INVALID")
    try:
        raw_scores = (
            (M7P2Dimension.NEED_COVERAGE, row["need_coverage_score"]),
            (M7P2Dimension.SCENARIO_FIT, row["scenario_fit_score"]),
            (M7P2Dimension.DECISION_VALUE, row["decision_value_score"]),
        )
        scores = tuple(
            M7P2Score(
                dimension=dimension,
                score=int(value),
                reason_code=(
                    M7P2ReasonCode.STRONG
                    if int(value) >= 4
                    else M7P2ReasonCode.PARTIAL
                    if int(value) == 3
                    else M7P2ReasonCode.WEAK
                ),
            )
            for dimension, value in raw_scores
            if value is not None
        )
        generated_at = row["generated_at"]
        if type(generated_at) is not datetime:
            raise ValueError("M7 summary timestamp is invalid")
        return M7OfflineSummary(
            run_id=str(row["run_id"]),
            judge_status=M7JudgeStatus(str(row["judge_status"])),
            p0_passed=bool(row["p0_passed"]),
            p0_failure_count=int(row["p0_failure_count"]),
            p1_failure_count=int(row["p1_failure_count"]),
            p2_scores=scores,
            reward=None if row["reward"] is None else int(row["reward"]),
            training_candidate=bool(row["training_candidate"]),
            judge_model=str(row["judge_model"]),
            generated_at=generated_at,
        )
    except Exception as error:
        raise DurableStoreError("DURABLE_STORE_DATA_INVALID") from error


def _run_from_row(row: Any) -> DurableRun:
    if row is None:
        raise DurableStoreError("DURABLE_RUN_NOT_FOUND")
    return DurableRun(
        run_id=str(row["run_id"]),
        thread_id=str(row["thread_id"]),
        root_run_id=str(row["root_run_id"]),
        parent_run_id=(None if row["parent_run_id"] is None else str(row["parent_run_id"])),
        child_id=None if row["child_id"] is None else str(row["child_id"]),
        depth=int(row["depth"]),
        loop_kind=LoopKind(str(row["loop_kind"])),
        task_scope_digest=(
            None if row["task_scope_digest"] is None else str(row["task_scope_digest"])
        ),
        state=DurableRunState(str(row["state"])),
        attempt=int(row["attempt"]),
        event_sequence=int(row["event_sequence"]),
        asset_version=str(row["asset_version"]),
        config_fingerprint=str(row["config_fingerprint"]),
        request_payload=_json_object(row["request_payload"]),
        terminal_response=(
            None if row["terminal_response"] is None else _json_object(row["terminal_response"])
        ),
        terminal_error_code=(
            None if row["terminal_error_code"] is None else str(row["terminal_error_code"])
        ),
        cancel_requested=bool(row["cancel_requested"]),
    )


def _event_from_row(row: Any) -> DurableEvent:
    payload = _json_object(row["payload"])
    return DurableEvent(
        run_id=str(row["run_id"]),
        sequence=int(row["sequence"]),
        payload=payload,
        payload_hash=str(row["payload_hash"]),
    )


def _local_user_from_row(row: Any) -> LocalUser:
    if row is None:
        raise DurableStoreError("DURABLE_STORE_DATA_INVALID")
    try:
        return LocalUser(user_id=str(row["user_id"]), username=str(row["username"]))
    except Exception as error:
        raise DurableStoreError("DURABLE_STORE_DATA_INVALID") from error


def _local_session_from_row(row: Any) -> LocalSession:
    if row is None:
        raise DurableStoreError("DURABLE_STORE_DATA_INVALID")
    expires_at = row["expires_at"]
    if type(expires_at) is not datetime:
        raise DurableStoreError("DURABLE_STORE_DATA_INVALID")
    try:
        return LocalSession(
            user=LocalUser(user_id=str(row["user_id"]), username=str(row["username"])),
            expires_at=expires_at,
        )
    except Exception as error:
        raise DurableStoreError("DURABLE_STORE_DATA_INVALID") from error


def _conversation_turn_from_row(row: Any) -> ConversationTurn:
    if row is None:
        raise DurableStoreError("DURABLE_STORE_DATA_INVALID")
    try:
        return ConversationTurn(
            thread_id=str(row["thread_id"]),
            ordinal=int(row["ordinal"]),
            role=ConversationRole(str(row["role"])),
            display_content=str(row["display_content"]),
            terminal_run_id=(
                None if row["terminal_run_id"] is None else str(row["terminal_run_id"])
            ),
        )
    except Exception as error:
        raise DurableStoreError("DURABLE_STORE_DATA_INVALID") from error


def _thread_summary_from_row(row: Any) -> ThreadSummary:
    if row is None:
        raise DurableStoreError("DURABLE_STORE_DATA_INVALID")
    try:
        payload = _json_object(row["summary_json"])
        if frozenset(payload) != {"summary"} or type(payload["summary"]) is not str:
            raise ValueError("summary payload is invalid")
        return ThreadSummary(
            thread_id=str(row["thread_id"]),
            revision=int(row["revision"]),
            covered_through_ordinal=int(row["covered_through_ordinal"]),
            summary=payload["summary"],
        )
    except Exception as error:
        raise DurableStoreError("DURABLE_STORE_DATA_INVALID") from error


def _user_memory_from_row(row: Any) -> UserMemoryEntry:
    if row is None:
        raise DurableStoreError("DURABLE_STORE_DATA_INVALID")
    try:
        return UserMemoryEntry(
            user_id=str(row["user_id"]),
            entry_id=str(row["entry_id"]),
            category=UserMemoryCategory(str(row["category"])),
            origin=UserMemoryOrigin(str(row["origin"])),
            canonical_key=str(row["canonical_key"]),
            content=str(row["content"]),
            source_thread_id=(
                None if row["source_thread_id"] is None else str(row["source_thread_id"])
            ),
            source_ordinal=(None if row["source_ordinal"] is None else int(row["source_ordinal"])),
            confidence=float(row["confidence"]),
            revision=int(row["revision"]),
        )
    except Exception as error:
        raise DurableStoreError("DURABLE_STORE_DATA_INVALID") from error


def _checkpoint_from_row(row: Any) -> DurableCheckpoint:
    checkpoint = DurableCheckpoint(
        run_id=str(row["run_id"]),
        checkpoint_number=int(row["checkpoint_number"]),
        state=DurableCheckpointState(str(row["state"])),
        event_sequence=int(row["event_sequence"]),
        runtime_payload=_json_object(row["runtime_payload"]),
        asset_version=str(row["asset_version"]),
        config_fingerprint=str(row["config_fingerprint"]),
        payload_hash=str(row["payload_hash"]),
    )
    return checkpoint


def _json_text(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_object(value: object) -> dict[str, object]:
    if type(value) is dict:
        return cast("dict[str, object]", value)
    if type(value) is str:
        decoded = json.loads(value)
        if type(decoded) is dict:
            return cast("dict[str, object]", decoded)
    raise DurableStoreError("DURABLE_STORE_DATA_INVALID")


def _lease_deadline() -> datetime:
    return datetime.now(UTC) + timedelta(seconds=_LEASE_SECONDS)


def _unique_violation(error: Exception) -> bool:
    return getattr(error, "sqlstate", None) == "23505"


class PostgresEvidenceStore:
    """asyncpg-backed durable ``product_evidence`` persistence.

    One row per (canonical_product_id, variant_hash, criterion_key, source_url,
    source_content_hash).  A cached hit means a later query never calls
    WebSearch again.
    """

    def __init__(self, *, dsn: str = _DEFAULT_DSN, pool: Any | None = None) -> None:
        self._dsn = validate_postgres_dsn(dsn)
        self._pool = pool

    async def open(self) -> None:
        if self._pool is not None:
            return
        try:
            import asyncpg

            self._pool = await asyncpg.create_pool(
                dsn=self._dsn,
                min_size=1,
                max_size=4,
                command_timeout=5.0,
                max_inactive_connection_lifetime=30.0,
            )
        except Exception as error:
            raise DurableStoreError("DURABLE_STORE_UNAVAILABLE") from error

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is None:
            return
        try:
            await pool.close()
        except Exception:
            return

    async def get(self, canonical_product_id: str, fact_key: str) -> tuple[dict[str, object], ...]:
        pool = await self._require_evidence_pool()
        try:
            async with pool.acquire() as connection:
                rows = await connection.fetch(
                    """
                    SELECT snippet, normalized_value, source, source_url,
                           captured_at, status, applicable_model
                    FROM product_evidence
                    WHERE canonical_product_id = $1 AND criterion_key = $2
                    ORDER BY captured_at DESC
                    LIMIT 50
                    """,
                    canonical_product_id,
                    fact_key,
                )
        except Exception as error:
            raise DurableStoreError("DURABLE_STORE_UNAVAILABLE") from error
        if not rows:
            return ()
        return tuple(
            {
                "schema_version": "glodex.product-evidence.v1",
                "canonical_product_id": canonical_product_id,
                "fact_key": fact_key,
                "status": row["status"],
                "claims": (
                    {
                        "source_url": row["source_url"],
                        "snippet": row["snippet"],
                        "normalized_value": row["normalized_value"],
                        "source": row["source"],
                        "captured_at": row["captured_at"].isoformat(),
                        "applicable_model": row["applicable_model"],
                    },
                ),
            }
            for row in rows
        )

    async def put(self, evidence: Mapping[str, object]) -> None:
        canonical = _text(evidence.get("canonical_product_id"), "evidence canonical ID")
        fact_key = _text(evidence.get("fact_key"), "evidence fact key")
        model = _text(evidence.get("model"), "evidence model")
        variant_hash = _variant_hash(evidence)
        claims = evidence.get("claims")
        if type(claims) is not list or not claims:
            raise ValueError("evidence claims are invalid")
        pool = await self._require_evidence_pool()
        try:
            async with pool.acquire() as connection:
                for claim in claims:
                    if type(claim) is not dict:
                        raise ValueError("evidence claim is invalid")
                    source_url = claim.get("source_url")
                    snippet = claim.get("snippet")
                    if type(source_url) is not str or type(snippet) is not str:
                        raise ValueError("evidence claim is invalid")
                    await connection.execute(
                        """
                        INSERT INTO product_evidence (
                            canonical_product_id, variant_hash, criterion_key,
                            source_url, source_content_hash, snippet,
                            normalized_value, source, captured_at, status,
                            applicable_model, expires_at
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                        ON CONFLICT DO NOTHING
                        """,
                        canonical,
                        variant_hash,
                        fact_key,
                        source_url,
                        _content_hash(snippet),
                        snippet,
                        claim.get("normalized_value"),
                        _text(claim.get("source"), "evidence source"),
                        datetime.now(UTC),
                        evidence.get("status", "KNOWN"),
                        model,
                        None,
                    )
        except ValueError:
            raise
        except Exception as error:
            raise DurableStoreError("DURABLE_STORE_UNAVAILABLE") from error

    async def _require_evidence_pool(self) -> Any:
        if self._pool is None:
            raise DurableStoreError("DURABLE_STORE_UNAVAILABLE")
        return self._pool


def _variant_hash(evidence: Mapping[str, object]) -> str:
    canonical = evidence.get("canonical_product_id")
    if type(canonical) is not str:
        raise ValueError("evidence canonical ID is invalid")
    return sha256(canonical.encode("utf-8")).hexdigest()


def _content_hash(snippet: str) -> str:
    return sha256(snippet.encode("utf-8")).hexdigest()


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} is invalid")
    return value


__all__ = [
    "DurablePostgresStore",
    "PostgresEvidenceStore",
    "validate_postgres_dsn",
]
