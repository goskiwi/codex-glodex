"""Offline M2b coordinator evidence with deterministic storage and Agent fakes."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from glodex.api.agent_events import AgentEventProjector
from glodex.application.agent.contracts import (
    AgentAnswer,
    AgentAnswerKind,
    AgentDemoResponse,
    AgentEventKind,
    AgentExecution,
    AgentRunEvent,
    AgentRunRecord,
    ToolName,
)
from glodex.application.agent.ports import AgentEventObserver
from glodex.application.durable.contracts import (
    DurableCheckpoint,
    DurableCheckpointState,
    DurableEvent,
    DurableProfileSnapshot,
    DurableRun,
    DurableRunState,
    DurableStoreError,
    cache_key,
)
from glodex.application.durable.runtime import DurableAgentCoordinator, _checkpoint_for
from glodex.contracts import RunStatus, SearchRequest

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec(
        "GLO-M2B-P0-002",
        "GLO-M2B-P0-003",
        "GLO-M2B-P0-004",
        "GLO-M2B-NFR-001",
        "GLO-M2B-NFR-002",
        "M2B-AC-003",
        "M2B-AC-004",
    ),
]


class _Clock:
    def now_utc(self) -> datetime:
        return datetime(2026, 7, 30, 12, 0, tzinfo=UTC)

    def monotonic_ns(self) -> int:
        return 1


class _Store:
    def __init__(self) -> None:
        self.runs: dict[str, DurableRun] = {}
        self.events: dict[str, list[DurableEvent]] = {}
        self.checkpoints: dict[str, list[DurableCheckpoint]] = {}

    async def reserve_run(
        self,
        *,
        run_id: str,
        thread_id: str,
        request_payload: dict[str, object],
        asset_version: str,
        config_fingerprint: str,
        profile_id: str | None,
        profile_revision: int,
    ) -> DurableRun:
        if any(
            run.thread_id == thread_id
            and run.state
            not in {
                DurableRunState.COMPLETED,
                DurableRunState.NO_MATCH,
                DurableRunState.FAILED,
                DurableRunState.ABORTED,
            }
            for run in self.runs.values()
        ):
            raise DurableStoreError("M2B_RUN_ALREADY_ACTIVE")
        run = DurableRun(
            run_id=run_id,
            thread_id=thread_id,
            state=DurableRunState.ACCEPTED,
            attempt=1,
            event_sequence=0,
            asset_version=asset_version,
            config_fingerprint=config_fingerprint,
            profile_id=profile_id,
            profile_revision=profile_revision,
            request_payload=request_payload,
            terminal_response=None,
            terminal_error_code=None,
            cancel_requested=False,
        )
        self.runs[run_id] = run
        self.events[run_id] = []
        self.checkpoints[run_id] = []
        return run

    async def load_run(self, *, run_id: str) -> DurableRun:
        try:
            return self.runs[run_id]
        except KeyError as error:
            raise DurableStoreError("M2B_RUN_NOT_FOUND") from error

    async def load_events(
        self,
        *,
        run_id: str,
        after_sequence: int = 0,
    ) -> tuple[DurableEvent, ...]:
        await self.load_run(run_id=run_id)
        return tuple(event for event in self.events[run_id] if event.sequence > after_sequence)

    async def latest_checkpoint(self, *, run_id: str) -> DurableCheckpoint | None:
        await self.load_run(run_id=run_id)
        values = self.checkpoints[run_id]
        return values[-1] if values else None

    async def append_event_checkpoint(
        self,
        *,
        event: DurableEvent | None,
        checkpoint: DurableCheckpoint,
        next_state: DurableRunState,
    ) -> DurableRun:
        current = await self.load_run(run_id=checkpoint.run_id)
        if checkpoint.checkpoint_number != len(self.checkpoints[current.run_id]) + 1:
            raise DurableStoreError("M2B_CHECKPOINT_SEQUENCE_INVALID")
        if event is not None:
            if event.sequence != current.event_sequence + 1:
                raise DurableStoreError("M2B_EVENT_SEQUENCE_INVALID")
            self.events[current.run_id].append(event)
        self.checkpoints[current.run_id].append(checkpoint)
        updated = replace(
            current,
            state=next_state,
            event_sequence=checkpoint.event_sequence,
        )
        self.runs[current.run_id] = updated
        return updated

    async def finish_run(
        self,
        *,
        event: DurableEvent,
        checkpoint: DurableCheckpoint,
        state: DurableRunState,
        response: dict[str, object] | None,
        error_code: str | None,
    ) -> DurableRun:
        current = await self.load_run(run_id=event.run_id)
        if event.sequence != current.event_sequence + 1:
            raise DurableStoreError("M2B_EVENT_SEQUENCE_INVALID")
        self.events[event.run_id].append(event)
        self.checkpoints[event.run_id].append(checkpoint)
        updated = replace(
            current,
            state=state,
            event_sequence=event.sequence,
            terminal_response=response,
            terminal_error_code=error_code,
        )
        self.runs[event.run_id] = updated
        return updated

    async def request_cancel(self, *, run_id: str) -> DurableRun:
        current = await self.load_run(run_id=run_id)
        if current.state in {
            DurableRunState.COMPLETED,
            DurableRunState.NO_MATCH,
            DurableRunState.FAILED,
            DurableRunState.ABORTED,
        }:
            return current
        updated = replace(
            current,
            state=DurableRunState.CANCEL_REQUESTED,
            cancel_requested=True,
        )
        self.runs[run_id] = updated
        return updated

    async def set_recoverable(self, *, run_id: str) -> DurableRun:
        current = await self.load_run(run_id=run_id)
        latest = await self.latest_checkpoint(run_id=run_id)
        if (
            current.cancel_requested
            or latest is None
            or latest.state is not DurableCheckpointState.CONFIRMED
        ):
            raise DurableStoreError("M2B_RUN_NOT_RECOVERABLE")
        updated = replace(current, state=DurableRunState.RECOVERABLE)
        self.runs[run_id] = updated
        return updated

    async def profile_snapshot(self, *, profile_id: str) -> DurableProfileSnapshot:
        return DurableProfileSnapshot(profile_id=profile_id, revision=0, entries=())


class _CompletedExecutor:
    def __init__(self) -> None:
        self.calls = 0

    async def execute_run(
        self,
        request: SearchRequest,
        *,
        run_id: str,
        observer: AgentEventObserver | None = None,
    ) -> AgentExecution:
        del request
        self.calls += 1
        events = (
            AgentRunEvent(kind=AgentEventKind.AGENT_STARTED, run_id=run_id),
            AgentRunEvent(
                kind=AgentEventKind.TOOL_STARTED,
                run_id=run_id,
                tool_name=ToolName.SHOPPING_SUMMARY,
            ),
            AgentRunEvent(
                kind=AgentEventKind.TOOL_FINISHED,
                run_id=run_id,
                tool_name=ToolName.SHOPPING_SUMMARY,
                safe_code="COMPLETED",
            ),
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
                text="Durable test completed.",
            ),
        )
        return AgentExecution(
            response=response,
            record=AgentRunRecord(
                run_id=run_id,
                status=RunStatus.COMPLETED,
                model_calls=0,
                tool_calls=1,
                child_runs=0,
                events=events,
            ),
        )


class _BlockingExecutor:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute_run(
        self,
        request: SearchRequest,
        *,
        run_id: str,
        observer: AgentEventObserver | None = None,
    ) -> AgentExecution:
        del request, observer
        self.started.set()
        await self.release.wait()
        raise AssertionError("cancelled run must not reach a terminal response")


def _coordinator(store: _Store, executor: object) -> DurableAgentCoordinator:
    return DurableAgentCoordinator(
        store=store,
        executor=executor,  # type: ignore[arg-type]
        projector=AgentEventProjector(clock=_Clock()),
        asset_version="m2b-test-assets-v1",
        config_fingerprint="a" * 64,
        run_id_factory=lambda: "run-m2b-test-1",
    )


async def _wait_for_terminal(store: _Store, run_id: str) -> DurableRun:
    for _attempt in range(100):
        run = await store.load_run(run_id=run_id)
        if run.state in {
            DurableRunState.COMPLETED,
            DurableRunState.NO_MATCH,
            DurableRunState.FAILED,
            DurableRunState.ABORTED,
        }:
            return run
        await asyncio.sleep(0)
    raise AssertionError("durable test run did not become terminal")


def test_completed_run_persists_contiguous_safe_events_and_terminal_response() -> None:
    async def scenario() -> None:
        store = _Store()
        executor = _CompletedExecutor()
        coordinator = _coordinator(store, executor)
        submitted = await coordinator.submit(
            request=SearchRequest(query="durable phone", top_k=1),
            thread_id="thread-m2b-test-1",
            profile_id=None,
            profile_revision=0,
        )
        terminal = await _wait_for_terminal(store, submitted.run_id)
        events = await store.load_events(run_id=submitted.run_id)
        checkpoint = await store.latest_checkpoint(run_id=submitted.run_id)

        assert terminal.state is DurableRunState.COMPLETED
        assert terminal.terminal_response is not None
        assert tuple(event.sequence for event in events) == (1, 2, 3, 4)
        assert tuple(event.payload["type"] for event in events) == (
            "AGENT_STARTED",
            "TOOL_STARTED",
            "TOOL_FINISHED",
            "AGENT_RESULT",
        )
        assert checkpoint is not None
        assert checkpoint.state is DurableCheckpointState.TERMINAL
        assert executor.calls == 1
        await coordinator.shutdown()

    asyncio.run(scenario())


def test_confirmed_initial_checkpoint_can_explicitly_resume_same_run_id() -> None:
    async def scenario() -> None:
        store = _Store()
        executor = _CompletedExecutor()
        coordinator = _coordinator(store, executor)
        run = await store.reserve_run(
            run_id="run-m2b-test-1",
            thread_id="thread-m2b-test-1",
            request_payload=SearchRequest(query="durable phone", top_k=1).model_dump(mode="json"),
            asset_version="m2b-test-assets-v1",
            config_fingerprint="a" * 64,
            profile_id=None,
            profile_revision=0,
        )
        await store.append_event_checkpoint(
            event=None,
            checkpoint=_checkpoint_for(
                run=run,
                number=1,
                state=DurableCheckpointState.CONFIRMED,
                event_sequence=0,
                phase="ACCEPTED",
            ),
            next_state=DurableRunState.ACCEPTED,
        )
        resumed = await coordinator.resume(run_id=run.run_id)
        terminal = await _wait_for_terminal(store, run.run_id)

        assert resumed.run_id == run.run_id
        assert terminal.state is DurableRunState.COMPLETED
        assert executor.calls == 1
        await coordinator.shutdown()

    asyncio.run(scenario())


def test_cancel_creates_one_aborted_terminal_event_without_business_response() -> None:
    async def scenario() -> None:
        store = _Store()
        executor = _BlockingExecutor()
        coordinator = _coordinator(store, executor)
        submitted = await coordinator.submit(
            request=SearchRequest(query="durable phone", top_k=1),
            thread_id="thread-m2b-test-1",
            profile_id=None,
            profile_revision=0,
        )
        await executor.started.wait()
        await coordinator.cancel(run_id=submitted.run_id)
        terminal = await _wait_for_terminal(store, submitted.run_id)
        events = await store.load_events(run_id=submitted.run_id)

        assert terminal.state is DurableRunState.ABORTED
        assert terminal.terminal_response is None
        assert terminal.terminal_error_code == "RUN_CANCELLED"
        assert len(events) == 1
        assert events[0].payload["type"] == "AGENT_ERROR"
        assert events[0].payload["safeCode"] == "RUN_CANCELLED"
        await coordinator.shutdown()

    asyncio.run(scenario())


def test_remote_pending_checkpoint_aborts_without_reexecuting_the_agent() -> None:
    async def scenario() -> None:
        store = _Store()
        executor = _CompletedExecutor()
        coordinator = _coordinator(store, executor)
        run = await store.reserve_run(
            run_id="run-m2b-test-1",
            thread_id="thread-m2b-test-1",
            request_payload=SearchRequest(query="durable phone", top_k=1).model_dump(mode="json"),
            asset_version="m2b-test-assets-v1",
            config_fingerprint="a" * 64,
            profile_id=None,
            profile_revision=0,
        )
        await store.append_event_checkpoint(
            event=None,
            checkpoint=_checkpoint_for(
                run=run,
                number=1,
                state=DurableCheckpointState.CONFIRMED,
                event_sequence=0,
                phase="ACCEPTED",
            ),
            next_state=DurableRunState.ACCEPTED,
        )
        await store.append_event_checkpoint(
            event=None,
            checkpoint=_checkpoint_for(
                run=run,
                number=2,
                state=DurableCheckpointState.REMOTE_PENDING,
                event_sequence=0,
                phase="EXTERNAL_AGENT_EXECUTION",
            ),
            next_state=DurableRunState.RUNNING,
        )
        terminal = await coordinator.resume(run_id=run.run_id)
        events = await store.load_events(run_id=run.run_id)

        assert terminal.state is DurableRunState.ABORTED
        assert terminal.terminal_error_code == "DURABLE_REMOTE_STEP_UNCERTAIN"
        assert executor.calls == 0
        assert len(events) == 1
        await coordinator.shutdown()

    asyncio.run(scenario())


def test_cache_key_is_digest_only_and_never_contains_query_text() -> None:
    key = cache_key(namespace="retrieval", material={"query": "私人偏好文本"})

    assert key.startswith("m2b:retrieval:v1:")
    assert len(key) == len("m2b:retrieval:v1:") + 64
    assert "私人" not in key
