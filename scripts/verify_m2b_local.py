"""Exercise the real local M2b PostgreSQL/Redis services with a deterministic Agent."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime

from glodex.adapters.m2b_postgres import M2bPostgresStore
from glodex.adapters.m2b_redis import M2bRedisCache
from glodex.api.agent_events import AgentEventProjector
from glodex.application.agent.contracts import (
    AgentAnswer,
    AgentAnswerKind,
    AgentDemoResponse,
    AgentEventKind,
    AgentExecution,
    AgentRunEvent,
    AgentRunRecord,
)
from glodex.application.agent.ports import AgentEventObserver
from glodex.application.durable.contracts import DurableRun, DurableRunState
from glodex.application.durable.runtime import DurableAgentCoordinator
from glodex.contracts import RunStatus, SearchRequest


class _Clock:
    def now_utc(self) -> datetime:
        return datetime(2026, 7, 30, 12, 0, tzinfo=UTC)

    def monotonic_ns(self) -> int:
        return 1


class _DeterministicExecutor:
    async def execute_run(
        self,
        request: SearchRequest,
        *,
        run_id: str,
        observer: AgentEventObserver | None = None,
    ) -> AgentExecution:
        del request
        events = (
            AgentRunEvent(kind=AgentEventKind.AGENT_STARTED, run_id=run_id),
            AgentRunEvent(
                kind=AgentEventKind.AGENT_RESULT,
                run_id=run_id,
                status="COMPLETED",
            ),
        )
        assert observer is not None
        for event in events:
            observer.on_event(event)
        response = AgentDemoResponse(
            run_id=run_id,
            status=RunStatus.COMPLETED,
            answer=AgentAnswer(
                kind=AgentAnswerKind.CHAT_FALLBACK,
                text="Deterministic local durable smoke completed.",
            ),
        )
        return AgentExecution(
            response=response,
            record=AgentRunRecord(
                run_id=run_id,
                status=RunStatus.COMPLETED,
                model_calls=0,
                tool_calls=0,
                child_runs=0,
                events=events,
            ),
        )


async def _wait_for_terminal(store: M2bPostgresStore, run_id: str) -> DurableRun:
    for _attempt in range(100):
        run = await store.load_run(run_id=run_id)
        if run.state in {
            DurableRunState.COMPLETED,
            DurableRunState.NO_MATCH,
            DurableRunState.FAILED,
            DurableRunState.ABORTED,
        }:
            return run
        await asyncio.sleep(0.05)
    raise RuntimeError("M2B_LOCAL_TIMEOUT")


async def run_smoke() -> dict[str, object]:
    store = M2bPostgresStore()
    cache = M2bRedisCache()
    try:
        await store.migrate()
        await store.health()
        coordinator = DurableAgentCoordinator(
            store=store,
            executor=_DeterministicExecutor(),
            projector=AgentEventProjector(clock=_Clock()),
            asset_version="m2b-local-smoke-v1",
            config_fingerprint="f" * 64,
            context_cache=cache,
        )
        submitted = await coordinator.submit(
            request=SearchRequest(query="local durable smoke", top_k=1),
            thread_id=f"thread-m2b-local-{uuid.uuid4().hex}",
            profile_id=None,
            profile_revision=0,
        )
        terminal = await _wait_for_terminal(store, submitted.run_id)
        events = await store.load_events(run_id=submitted.run_id)
        suffix = await store.load_events(run_id=submitted.run_id, after_sequence=1)
        assert terminal.state is DurableRunState.COMPLETED
        assert len(events) == 2
        assert len(suffix) == 1
        await coordinator.shutdown()
        return {
            "event_count": len(events),
            "postgres": "ok",
            "redis_context_cache": "best-effort",
            "sse_suffix_events": len(suffix),
            "status": terminal.state.value,
        }
    finally:
        await cache.close()
        await store.close()


def main() -> int:
    try:
        result = asyncio.run(run_smoke())
    except Exception:
        print(json.dumps({"code": "M2B_LOCAL_SMOKE_FAILED", "status": "FAILED"}))
        return 1
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
