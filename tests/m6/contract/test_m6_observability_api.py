"""Offline M6 owner-trace and aggregate API evidence without a database or socket."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI

from glodex.agent.contracts import AgentExecution
from glodex.api.agent_events import AgentEventProjector
from glodex.api.durable import create_durable_agent_app
from glodex.memory.identity import LocalSession, LocalUser, session_token_hash
from glodex.observability.runtime import (
    CostEstimate,
    M6AlertCode,
    M6AlertView,
    M6BreakerState,
    M6BreakerView,
    M6Operation,
    M6OperationOutcome,
    M6OperationsView,
    M6OperationsWindow,
    M6RunTrace,
    M6TraceEvent,
    M6TraceEventDraft,
    M6TraceEventKind,
    ProviderUsageReceipt,
)
from glodex.runtime.contracts import DurableRun, DurableRunState, LoopKind

pytestmark = [
    pytest.mark.contract,
    pytest.mark.spec("GLO-M6-P0-001", "GLO-M6-P0-005", "GLO-M6-NFR-002"),
]


class _Clock:
    def now_utc(self) -> datetime:
        return datetime(2026, 7, 31, 12, 0, tzinfo=UTC)

    def monotonic_ns(self) -> int:
        return 0


class _UnusedExecutor:
    async def execute_run(self, *args: Any, **kwargs: Any) -> AgentExecution:
        del args, kwargs
        raise AssertionError("M6 API reads must not execute an Agent")


@dataclass
class _Store:
    owner: LocalUser
    other: LocalUser
    sessions: dict[str, LocalSession]

    async def session_for_token_hash(self, *, token_hash: str) -> LocalSession | None:
        return self.sessions.get(token_hash)

    async def load_run(self, *, run_id: str) -> DurableRun:
        assert run_id == "run-m6-api-1"
        return DurableRun(
            run_id=run_id,
            thread_id="thread-m6-api-1",
            root_run_id=run_id,
            parent_run_id=None,
            child_id=None,
            depth=0,
            loop_kind=LoopKind.ROOT,
            task_scope_digest=None,
            state=DurableRunState.COMPLETED,
            attempt=1,
            event_sequence=1,
            asset_version="durable-retrieval_model-agent-safe",
            config_fingerprint="a" * 64,
            request_payload={},
            terminal_response={},
            terminal_error_code=None,
            cancel_requested=False,
        )

    async def thread_owner(self, *, thread_id: str) -> str | None:
        return self.owner.user_id if thread_id == "thread-m6-api-1" else None

    async def load_m6_trace(self, *, run_id: str) -> M6RunTrace:
        assert run_id == "run-m6-api-1"
        return M6RunTrace(
            run_id=run_id,
            terminal_state="COMPLETED",
            events=(
                M6TraceEvent(
                    run_id=run_id,
                    sequence=1,
                    draft=M6TraceEventDraft(
                        kind=M6TraceEventKind.RUN_STARTED,
                        version="durable-retrieval_model-agent-safe",
                    ),
                ),
                M6TraceEvent(
                    run_id=run_id,
                    sequence=2,
                    draft=M6TraceEventDraft(
                        kind=M6TraceEventKind.OPERATION_FINISHED,
                        operation=M6Operation.LLM_TOOL_CALL,
                        outcome=M6OperationOutcome.SUCCESS,
                        duration_ms=12,
                        receipt=ProviderUsageReceipt.unavailable(),
                        cost=CostEstimate.unavailable(),
                    ),
                ),
            ),
        )

    async def load_m6_operations(
        self,
        *,
        window: M6OperationsWindow,
        now: datetime,
    ) -> M6OperationsView:
        assert now.tzinfo is not None
        return M6OperationsView(
            window=window,
            completed_count=1,
            no_match_count=0,
            failed_count=0,
            aborted_count=0,
            operation_count=1,
            operation_failure_count=0,
            receipt_reported_count=0,
            receipt_unavailable_count=1,
            cost_totals=(),
            breakers=(
                M6BreakerView(
                    operation=M6Operation.LLM_TOOL_CALL,
                    state=M6BreakerState.CLOSED,
                ),
            ),
            alerts=(M6AlertView(code=M6AlertCode.RECEIPT_UNAVAILABLE),),
        )


def _app(store: _Store) -> FastAPI:
    return create_durable_agent_app(
        executor=_UnusedExecutor(),
        store=cast(Any, store),
        projector=AgentEventProjector(clock=_Clock()),
        asset_version="m6-test",
        config_fingerprint="a" * 64,
    )


@pytest.mark.acceptance
@pytest.mark.spec("M6-AC-004")
def test_m6_trace_is_owner_scoped_and_its_public_schema_contains_no_private_input() -> None:
    owner = LocalUser(user_id="user-" + "1" * 32, username="owner.m6")
    other = LocalUser(user_id="user-" + "2" * 32, username="other.m6")
    owner_token = "a" * 32
    other_token = "b" * 32
    expiry = datetime.now(UTC) + timedelta(minutes=5)
    store = _Store(
        owner=owner,
        other=other,
        sessions={
            session_token_hash(owner_token): LocalSession(user=owner, expires_at=expiry),
            session_token_hash(other_token): LocalSession(user=other, expires_at=expiry),
        },
    )

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=_app(store), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://durable.test") as client:
            client.cookies.set("glodex_local_session", owner_token)
            response = await client.get("/api/v1/m6/runs/run-m6-api-1/trace")
            assert response.status_code == 200
            payload = response.json()
            assert payload["run_id"] == "run-m6-api-1"
            assert payload["events"][1] == {
                "sequence": 2,
                "kind": "OPERATION_FINISHED",
                "operation": "llm_tool_call",
                "outcome": "SUCCESS",
                "safe_code": None,
                "duration_ms": 12,
                "version": None,
                "receipt": {
                    "status": "UNAVAILABLE",
                    "provider": None,
                    "model": None,
                    "input_tokens": None,
                    "output_tokens": None,
                    "total_tokens": None,
                },
                "cost": {
                    "status": "UNAVAILABLE",
                    "price_table_version": None,
                    "currency": None,
                    "input_micro_units": None,
                    "output_micro_units": None,
                },
            }
            rendered = response.text.casefold()
            for forbidden in (
                "query",
                "prompt",
                "memory",
                "history",
                "credential",
                "vector",
                "score",
            ):
                assert forbidden not in rendered
            client.cookies.set("glodex_local_session", other_token)
            assert (await client.get("/api/v1/m6/runs/run-m6-api-1/trace")).status_code == 403

    asyncio.run(scenario())


def test_m6_operations_is_authenticated_but_has_no_owner_or_run_identity() -> None:
    owner = LocalUser(user_id="user-" + "1" * 32, username="owner.m6")
    other = LocalUser(user_id="user-" + "2" * 32, username="other.m6")
    token = "a" * 32
    store = _Store(
        owner=owner,
        other=other,
        sessions={
            session_token_hash(token): LocalSession(
                user=owner,
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        },
    )

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=_app(store), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://durable.test") as client:
            assert (await client.get("/api/v1/m6/operations?window=1h")).status_code == 401
            client.cookies.set("glodex_local_session", token)
            response = await client.get("/api/v1/m6/operations?window=24h")
            assert response.status_code == 200
            assert response.json() == {
                "schema_version": "glodex.m6-operations.v2",
                "window": "24h",
                "completed_count": 1,
                "no_match_count": 0,
                "failed_count": 0,
                "aborted_count": 0,
                "operation_count": 1,
                "operation_failure_count": 0,
                "receipt_reported_count": 0,
                "receipt_unavailable_count": 1,
                "cost_totals": [],
                "breakers": [{"operation": "llm_tool_call", "state": "CLOSED"}],
                "alerts": [{"code": "RECEIPT_UNAVAILABLE"}],
            }

    asyncio.run(scenario())
