"""Offline Durable coordinator evidence with deterministic storage and Agent fakes."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from typing import Literal, cast

import pytest

from glodex.agent.contracts import (
    AgentAnswer,
    AgentAnswerKind,
    AgentDemoResponse,
    AgentEventKind,
    AgentEventScope,
    AgentExecution,
    AgentRunEvent,
    AgentRunRecord,
    Platform,
    ToolName,
)
from glodex.agent.ports import AgentEventObserver
from glodex.api.agent_events import AgentEventProjector
from glodex.contracts import RunStatus, SearchRequest
from glodex.observability.runtime import (
    M6RunTrace,
    M6TraceEvent,
    M6TraceEventDraft,
    M6TraceEventKind,
)
from glodex.runtime.contracts import (
    DurableCheckpoint,
    DurableCheckpointState,
    DurableEvent,
    DurableExecutionContext,
    DurableRun,
    DurableRunState,
    DurableRuntimeSnapshot,
    DurableStoreError,
    LoopKind,
    cache_key,
)
from glodex.runtime.request_payload import durable_request_payload
from glodex.runtime.service import (
    DurableAgentCoordinator,
    _checkpoint_for,
    _request_from_private_payload,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec(
        "GLO-DURABLE-P0-002",
        "GLO-DURABLE-P0-003",
        "GLO-DURABLE-P0-004",
        "GLO-DURABLE-NFR-001",
        "GLO-DURABLE-NFR-002",
        "DURABLE-AC-003",
        "DURABLE-AC-004",
    ),
]


class _Clock:
    def now_utc(self) -> datetime:
        return datetime(2026, 7, 30, 12, 0, tzinfo=UTC)

    def monotonic_ns(self) -> int:
        return 1


def _durable_request_payload() -> dict[str, object]:
    return durable_request_payload(
        request=SearchRequest(query="durable phone", top_k=1),
        display_query="durable phone",
        task_turns=("durable phone",),
    )


class _Store:
    def __init__(self) -> None:
        self.runs: dict[str, DurableRun] = {}
        self.events: dict[str, list[DurableEvent]] = {}
        self.checkpoints: dict[str, list[DurableCheckpoint]] = {}

    async def reserve_root_run(
        self,
        *,
        run_id: str,
        thread_id: str,
        request_payload: dict[str, object],
        asset_version: str,
        config_fingerprint: str,
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
            raise DurableStoreError("DURABLE_RUN_ALREADY_ACTIVE")
        run = DurableRun(
            run_id=run_id,
            thread_id=thread_id,
            root_run_id=run_id,
            parent_run_id=None,
            child_id=None,
            depth=0,
            loop_kind=LoopKind.ROOT,
            task_scope_digest=None,
            state=DurableRunState.ACCEPTED,
            attempt=1,
            event_sequence=0,
            asset_version=asset_version,
            config_fingerprint=config_fingerprint,
            request_payload=request_payload,
            terminal_response=None,
            terminal_error_code=None,
            cancel_requested=False,
        )
        self.runs[run_id] = run
        self.events[run_id] = []
        self.checkpoints[run_id] = []
        return run

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
        parent = await self.load_run(run_id=parent_run_id)
        if parent.root_run_id != root_run_id or parent.depth + 1 != depth:
            raise DurableStoreError("DURABLE_CHILD_TREE_INVALID")
        if run_id in self.runs:
            raise DurableStoreError("DURABLE_CHILD_RUN_ALREADY_EXISTS")
        run = DurableRun(
            run_id=run_id,
            thread_id=thread_id,
            root_run_id=root_run_id,
            parent_run_id=parent_run_id,
            child_id=child_id,
            depth=depth,
            loop_kind=LoopKind.CHILD,
            task_scope_digest=task_scope_digest,
            state=DurableRunState.ACCEPTED,
            attempt=1,
            event_sequence=0,
            asset_version=asset_version,
            config_fingerprint=config_fingerprint,
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
            raise DurableStoreError("DURABLE_RUN_NOT_FOUND") from error

    async def load_run_tree(self, *, root_run_id: str) -> tuple[DurableRun, ...]:
        values = tuple(run for run in self.runs.values() if run.root_run_id == root_run_id)
        if not values:
            raise DurableStoreError("DURABLE_RUN_NOT_FOUND")
        return tuple(sorted(values, key=lambda run: (run.depth, run.run_id)))

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
            raise DurableStoreError("DURABLE_CHECKPOINT_SEQUENCE_INVALID")
        if event is not None:
            if event.sequence != current.event_sequence + 1:
                raise DurableStoreError("DURABLE_EVENT_SEQUENCE_INVALID")
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
            raise DurableStoreError("DURABLE_EVENT_SEQUENCE_INVALID")
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
            raise DurableStoreError("DURABLE_RUN_NOT_RECOVERABLE")
        updated = replace(current, state=DurableRunState.RECOVERABLE)
        self.runs[run_id] = updated
        return updated


class _CompletedExecutor:
    def __init__(self) -> None:
        self.calls = 0

    async def execute_run(
        self,
        request: SearchRequest,
        *,
        run_id: str,
        observer: AgentEventObserver | None,
        checkpoint: DurableCheckpoint,
        runtime_context: DurableExecutionContext,
    ) -> AgentExecution:
        del request, checkpoint, runtime_context
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
        observer: AgentEventObserver | None,
        checkpoint: DurableCheckpoint,
        runtime_context: DurableExecutionContext,
    ) -> AgentExecution:
        del request, observer, checkpoint, runtime_context
        self.started.set()
        await self.release.wait()
        raise AssertionError("cancelled run must not reach a terminal response")


class _StreamingExecutor:
    def __init__(self) -> None:
        self.progress_emitted = asyncio.Event()
        self.release = asyncio.Event()

    async def execute_run(
        self,
        request: SearchRequest,
        *,
        run_id: str,
        observer: AgentEventObserver | None,
        checkpoint: DurableCheckpoint,
        runtime_context: DurableExecutionContext,
    ) -> AgentExecution:
        del request, checkpoint, runtime_context
        assert observer is not None
        prefix = (
            AgentRunEvent(kind=AgentEventKind.AGENT_STARTED, run_id=run_id),
            AgentRunEvent(kind=AgentEventKind.MODEL_STARTED, run_id=run_id, round=1),
            AgentRunEvent(kind=AgentEventKind.MODEL_STREAMING, run_id=run_id, round=1),
        )
        for event in prefix:
            observer.on_event(event)
        self.progress_emitted.set()
        await self.release.wait()
        suffix = (
            AgentRunEvent(
                kind=AgentEventKind.MODEL_FINISHED,
                run_id=run_id,
                round=1,
                tool_name=ToolName.SHOPPING_SUMMARY,
            ),
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
        for event in suffix:
            observer.on_event(event)
        events = (*prefix, *suffix)
        response = AgentDemoResponse(
            run_id=run_id,
            status=RunStatus.COMPLETED,
            answer=AgentAnswer(
                kind=AgentAnswerKind.CHAT_FALLBACK,
                text="Durable streaming test completed.",
            ),
        )
        return AgentExecution(
            response=response,
            record=AgentRunRecord(
                run_id=run_id,
                status=RunStatus.COMPLETED,
                model_calls=1,
                tool_calls=1,
                child_runs=0,
                events=events,
            ),
        )


class _ForkingExecutor:
    """Emit one complete v2 child lifecycle from a single root execution."""

    def __init__(self, *, fail_child: bool = False) -> None:
        self.context: DurableExecutionContext | None = None
        self.fail_child = fail_child

    async def execute_run(
        self,
        request: SearchRequest,
        *,
        run_id: str,
        observer: AgentEventObserver | None,
        checkpoint: DurableCheckpoint,
        runtime_context: DurableExecutionContext,
    ) -> AgentExecution:
        del request, checkpoint
        self.context = runtime_context
        assert observer is not None
        child_run_id = "run-child-durable-1"
        child_id = "child-durable-1"
        scope_digest = "b" * 64
        prefix = (
            AgentRunEvent(kind=AgentEventKind.AGENT_STARTED, run_id=run_id),
            AgentRunEvent(
                kind=AgentEventKind.FORK_REQUESTED,
                run_id=run_id,
                child_id=child_id,
                depth=1,
                parent_run_id=run_id,
                task_scope_digest=scope_digest,
                platforms=(Platform.AMAZON,),
            ),
        )
        for event in prefix:
            observer.on_event(event)
        await runtime_context.checkpoint_writer.reserve_child(
            run_id=child_run_id,
            thread_id="thread-child-durable-1",
            parent_run_id=run_id,
            child_id=child_id,
            depth=1,
            task_scope_digest=scope_digest,
            initial_snapshot=DurableRuntimeSnapshot(
                phase="ACCEPTED",
                local_state={"child_id": child_id},
            ),
        )
        await runtime_context.checkpoint_writer.persist(
            run_id=child_run_id,
            snapshot=DurableRuntimeSnapshot(
                phase="CHILD_READY",
                journal=("FORK_REQUESTED",),
                local_state={"child_id": child_id},
            ),
        )
        child_started = AgentRunEvent(
            kind=AgentEventKind.CHILD_RUN_STARTED,
            run_id=child_run_id,
            scope=AgentEventScope.CHILD,
            child_id=child_id,
            depth=1,
            parent_run_id=run_id,
            task_scope_digest=scope_digest,
        )
        child_events: tuple[AgentRunEvent, ...]
        join_status: Literal["COMPLETED", "FAILED"]
        if self.fail_child:
            child_terminal = AgentRunEvent(
                kind=AgentEventKind.CHILD_FAILED,
                run_id=child_run_id,
                scope=AgentEventScope.CHILD,
                child_id=child_id,
                depth=1,
                parent_run_id=run_id,
                task_scope_digest=scope_digest,
                status="FAILED",
                safe_code="FORK_FAILED",
            )
            join_status = "FAILED"
            child_events = (child_started, child_terminal)
        else:
            child_checkpoint = AgentRunEvent(
                kind=AgentEventKind.CHILD_CHECKPOINT_CONFIRMED,
                run_id=child_run_id,
                scope=AgentEventScope.CHILD,
                child_id=child_id,
                depth=1,
                parent_run_id=run_id,
                task_scope_digest=scope_digest,
            )
            child_terminal = AgentRunEvent(
                kind=AgentEventKind.CHILD_HANDOFF_READY,
                run_id=child_run_id,
                scope=AgentEventScope.CHILD,
                child_id=child_id,
                depth=1,
                parent_run_id=run_id,
                task_scope_digest=scope_digest,
                status="COMPLETED",
            )
            join_status = "COMPLETED"
            child_events = (child_started, child_checkpoint, child_terminal)
        suffix = (
            *child_events,
            AgentRunEvent(
                kind=AgentEventKind.FORK_JOINED,
                run_id=run_id,
                child_id=child_id,
                depth=1,
                parent_run_id=run_id,
                task_scope_digest=scope_digest,
                status=join_status,
            ),
            AgentRunEvent(
                kind=AgentEventKind.AGENT_RESULT,
                run_id=run_id,
                status="COMPLETED",
            ),
        )
        for event in suffix:
            observer.on_event(event)
        events = (*prefix, *suffix)
        response = AgentDemoResponse(
            run_id=run_id,
            status=RunStatus.COMPLETED,
            answer=AgentAnswer(
                kind=AgentAnswerKind.CHAT_FALLBACK,
                text="Durable child test completed.",
            ),
        )
        return AgentExecution(
            response=response,
            record=AgentRunRecord(
                run_id=run_id,
                status=RunStatus.COMPLETED,
                model_calls=0,
                tool_calls=0,
                child_runs=1,
                events=events,
            ),
        )


class _M6TraceStore:
    def __init__(self) -> None:
        self.traces: dict[str, M6RunTrace] = {}

    async def ensure_m6_trace(self, *, run_id: str) -> M6RunTrace:
        return self.traces.setdefault(run_id, M6RunTrace(run_id=run_id, events=()))

    async def append_m6_trace_event(
        self,
        *,
        run_id: str,
        draft: M6TraceEventDraft,
    ) -> M6TraceEvent:
        trace = await self.ensure_m6_trace(run_id=run_id)
        event = M6TraceEvent(run_id=run_id, sequence=len(trace.events) + 1, draft=draft)
        terminal_state = (
            draft.safe_code if draft.kind is M6TraceEventKind.TERMINAL else trace.terminal_state
        )
        self.traces[run_id] = M6RunTrace(
            run_id=run_id,
            events=(*trace.events, event),
            terminal_state=terminal_state,
        )
        return event


def _coordinator(
    store: _Store,
    executor: object,
    *,
    m6_trace_store: _M6TraceStore | None = None,
) -> DurableAgentCoordinator:
    return DurableAgentCoordinator(
        store=store,
        executor=executor,  # type: ignore[arg-type]
        projector=AgentEventProjector(clock=_Clock()),
        asset_version="durable-test-assets-v1",
        config_fingerprint="a" * 64,
        run_id_factory=lambda: "run-durable-test-1",
        m6_trace_store=m6_trace_store,
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


async def _wait_for_event_count(store: _Store, run_id: str, count: int) -> tuple[DurableEvent, ...]:
    for _attempt in range(100):
        events = await store.load_events(run_id=run_id)
        if len(events) >= count:
            return events
        await asyncio.sleep(0)
    raise AssertionError("durable test run did not persist the expected event prefix")


def test_completed_run_persists_contiguous_safe_events_and_terminal_response() -> None:
    async def scenario() -> None:
        store = _Store()
        executor = _CompletedExecutor()
        coordinator = _coordinator(store, executor)
        submitted = await coordinator.submit(
            request=SearchRequest(query="durable phone", top_k=1),
            display_query="durable phone",
            task_turns=("durable phone",),
            thread_id="thread-durable-test-1",
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


def test_progress_events_are_durable_before_the_agent_returns_a_terminal_response() -> None:
    async def scenario() -> None:
        store = _Store()
        executor = _StreamingExecutor()
        coordinator = _coordinator(store, executor)
        submitted = await coordinator.submit(
            request=SearchRequest(query="durable phone", top_k=1),
            display_query="durable phone",
            task_turns=("durable phone",),
            thread_id="thread-durable-test-1",
        )

        await executor.progress_emitted.wait()
        prefix = await _wait_for_event_count(store, submitted.run_id, 3)
        active = await store.load_run(run_id=submitted.run_id)

        assert active.state is DurableRunState.RUNNING
        assert active.terminal_response is None
        assert tuple(event.payload["type"] for event in prefix) == (
            "AGENT_STARTED",
            "MODEL_STARTED",
            "MODEL_STREAMING",
        )

        executor.release.set()
        terminal = await _wait_for_terminal(store, submitted.run_id)
        events = await store.load_events(run_id=submitted.run_id)

        assert terminal.state is DurableRunState.COMPLETED
        assert tuple(event.sequence for event in events) == (1, 2, 3, 4, 5, 6, 7)
        assert events[-1].payload["type"] == "AGENT_RESULT"
        await coordinator.shutdown()

    asyncio.run(scenario())


def test_child_events_have_private_child_checkpoints_and_root_sse_attribution() -> None:
    async def scenario() -> None:
        store = _Store()
        executor = _ForkingExecutor()
        coordinator = _coordinator(store, executor)
        root = await coordinator.submit(
            request=SearchRequest(query="durable phone", top_k=1),
            display_query="durable phone",
            task_turns=("durable phone",),
            thread_id="thread-durable-test-1",
        )
        terminal = await _wait_for_terminal(store, root.run_id)
        tree = await store.load_run_tree(root_run_id=root.run_id)
        root_events = await store.load_events(run_id=root.run_id)
        child = next(run for run in tree if run.loop_kind is LoopKind.CHILD)
        child_events = await store.load_events(run_id=child.run_id)
        child_checkpoint = await store.latest_checkpoint(run_id=child.run_id)
        root_checkpoint = await store.latest_checkpoint(run_id=root.run_id)

        assert executor.context is not None
        assert executor.context.run.run_id == root.run_id
        assert executor.context.checkpoint is not None
        assert terminal.state is DurableRunState.COMPLETED
        assert child.parent_run_id == root.run_id
        assert child.thread_id != root.thread_id
        assert child.task_scope_digest == "b" * 64
        assert child.state is DurableRunState.COMPLETED
        assert tuple(event.sequence for event in child_events) == (1, 2, 3)
        assert tuple(event.payload["type"] for event in child_events) == (
            "CHILD_RUN_STARTED",
            "CHILD_CHECKPOINT_CONFIRMED",
            "CHILD_HANDOFF_READY",
        )
        assert all(event.payload["runId"] == child.run_id for event in child_events)
        assert all(event.payload["rootRunId"] == root.run_id for event in child_events)
        assert "CHILD_RUN_STARTED" in tuple(event.payload["type"] for event in root_events)
        root_child_event = next(
            event for event in root_events if event.payload["type"] == "CHILD_RUN_STARTED"
        )
        assert root_child_event.payload["runId"] == child.run_id
        assert root_child_event.payload["sequence"] == root_child_event.sequence
        assert child_checkpoint is not None
        assert child_checkpoint.state is DurableCheckpointState.TERMINAL
        assert root_checkpoint is not None
        assert root_checkpoint.state is DurableCheckpointState.TERMINAL
        child_tree = cast(dict[str, object], child_checkpoint.runtime_payload["tree"])
        root_tree = cast(dict[str, object], root_checkpoint.runtime_payload["tree"])
        assert child_tree["root_run_id"] == root.run_id
        assert root_tree["root_run_id"] == root.run_id
        child_runs = cast(list[dict[str, object]], child_tree["runs"])
        root_runs = cast(list[dict[str, object]], root_tree["runs"])
        child_node = next(node for node in child_runs if node["run_id"] == child.run_id)
        root_node = next(node for node in root_runs if node["run_id"] == root.run_id)
        root_child_node = next(node for node in root_runs if node["run_id"] == child.run_id)
        assert child_node["state"] == "COMPLETED"
        assert root_node["state"] == "COMPLETED"
        assert root_child_node["state"] == "COMPLETED"
        await coordinator.shutdown()

    asyncio.run(scenario())


def test_failed_child_is_terminal_once_and_root_can_still_join_it() -> None:
    """A failed child emits CHILD_FAILED, not a second failed handoff event."""

    async def scenario() -> None:
        store = _Store()
        coordinator = _coordinator(store, _ForkingExecutor(fail_child=True))
        root = await coordinator.submit(
            request=SearchRequest(query="durable phone", top_k=1),
            display_query="durable phone",
            task_turns=("durable phone",),
            thread_id="thread-durable-test-1",
        )
        terminal = await _wait_for_terminal(store, root.run_id)
        tree = await store.load_run_tree(root_run_id=root.run_id)
        child = next(run for run in tree if run.loop_kind is LoopKind.CHILD)
        child_events = await store.load_events(run_id=child.run_id)
        root_events = await store.load_events(run_id=root.run_id)

        assert terminal.state is DurableRunState.COMPLETED
        assert child.state is DurableRunState.FAILED
        assert tuple(event.payload["type"] for event in child_events) == (
            "CHILD_RUN_STARTED",
            "CHILD_FAILED",
        )
        assert "CHILD_HANDOFF_READY" not in tuple(event.payload["type"] for event in child_events)
        assert "FORK_JOINED" in tuple(event.payload["type"] for event in root_events)
        await coordinator.shutdown()

    asyncio.run(scenario())


def test_child_lifecycle_event_requires_prior_writer_registration() -> None:
    async def scenario() -> None:
        store = _Store()
        coordinator = _coordinator(store, _CompletedExecutor())
        root = await store.reserve_root_run(
            run_id="run-durable-test-1",
            thread_id="thread-durable-test-1",
            request_payload=_durable_request_payload(),
            asset_version="durable-test-assets-v1",
            config_fingerprint="a" * 64,
        )
        event = AgentRunEvent(
            kind=AgentEventKind.CHILD_RUN_STARTED,
            run_id="run-child-durable-1",
            scope=AgentEventScope.CHILD,
            child_id="child-durable-1",
            depth=1,
            parent_run_id=root.run_id,
            task_scope_digest="b" * 64,
        )

        with pytest.raises(DurableStoreError, match=r"^DURABLE_CHILD_NOT_REGISTERED$"):
            await coordinator._resolve_event_run(root=root, event=event)

        assert await store.load_run_tree(root_run_id=root.run_id) == (root,)
        await coordinator.shutdown()

    asyncio.run(scenario())


def test_cancel_after_a_live_prefix_keeps_the_prefix_and_appends_only_aborted_terminal() -> None:
    async def scenario() -> None:
        store = _Store()
        executor = _StreamingExecutor()
        coordinator = _coordinator(store, executor)
        submitted = await coordinator.submit(
            request=SearchRequest(query="durable phone", top_k=1),
            display_query="durable phone",
            task_turns=("durable phone",),
            thread_id="thread-durable-test-1",
        )

        await executor.progress_emitted.wait()
        await _wait_for_event_count(store, submitted.run_id, 3)
        await coordinator.cancel(run_id=submitted.run_id)
        terminal = await _wait_for_terminal(store, submitted.run_id)
        events = await store.load_events(run_id=submitted.run_id)

        assert terminal.state is DurableRunState.ABORTED
        assert terminal.terminal_error_code == "RUN_CANCELLED"
        assert tuple(event.payload["type"] for event in events) == (
            "AGENT_STARTED",
            "MODEL_STARTED",
            "MODEL_STREAMING",
            "AGENT_ERROR",
        )
        assert events[-1].payload["safeCode"] == "RUN_CANCELLED"
        await coordinator.shutdown()

    asyncio.run(scenario())


def test_m6_trace_is_started_once_and_records_only_safe_terminal_lifecycle_facts() -> None:
    async def scenario() -> None:
        store = _Store()
        trace_store = _M6TraceStore()
        coordinator = _coordinator(store, _CompletedExecutor(), m6_trace_store=trace_store)
        submitted = await coordinator.submit(
            request=SearchRequest(query="durable phone", top_k=1),
            display_query="durable phone",
            task_turns=("durable phone",),
            thread_id="thread-durable-test-1",
        )
        await _wait_for_terminal(store, submitted.run_id)
        trace = await trace_store.ensure_m6_trace(run_id=submitted.run_id)

        assert tuple(event.draft.kind for event in trace.events) == (
            M6TraceEventKind.RUN_STARTED,
            M6TraceEventKind.TERMINAL,
        )
        assert trace.terminal_state == "COMPLETED"
        assert all(event.draft.operation is None for event in trace.events)
        assert all(event.draft.receipt is None for event in trace.events)
        await coordinator.shutdown()

    asyncio.run(scenario())


def test_confirmed_initial_checkpoint_can_explicitly_resume_same_run_id() -> None:
    async def scenario() -> None:
        store = _Store()
        executor = _CompletedExecutor()
        coordinator = _coordinator(store, executor)
        run = await store.reserve_root_run(
            run_id="run-durable-test-1",
            thread_id="thread-durable-test-1",
            request_payload=_durable_request_payload(),
            asset_version="durable-test-assets-v1",
            config_fingerprint="a" * 64,
        )
        await store.append_event_checkpoint(
            event=None,
            checkpoint=_checkpoint_for(
                run=run,
                tree=(run,),
                number=1,
                state=DurableCheckpointState.CONFIRMED,
                event_sequence=0,
                snapshot=DurableRuntimeSnapshot(phase="ACCEPTED"),
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


def test_request_checkpoint_restores_json_tuple_fields() -> None:
    request = SearchRequest(query="durable phone", top_k=1)

    restored = _request_from_private_payload(
        durable_request_payload(
            request=request,
            display_query="durable phone",
            task_turns=("durable phone",),
        )
    )

    assert restored == request


def test_later_confirmed_agent_loop_checkpoint_resumes_execution() -> None:
    async def scenario() -> None:
        store = _Store()
        executor = _CompletedExecutor()
        coordinator = _coordinator(store, executor)
        root = await store.reserve_root_run(
            run_id="run-durable-test-1",
            thread_id="thread-durable-test-1",
            request_payload=_durable_request_payload(),
            asset_version="durable-test-assets-v1",
            config_fingerprint="a" * 64,
        )
        await store.append_event_checkpoint(
            event=None,
            checkpoint=_checkpoint_for(
                run=root,
                tree=(root,),
                number=1,
                state=DurableCheckpointState.CONFIRMED,
                event_sequence=0,
                snapshot=DurableRuntimeSnapshot(phase="ACCEPTED"),
            ),
            next_state=DurableRunState.ACCEPTED,
        )
        active_root = await store.load_run(run_id=root.run_id)
        await store.append_event_checkpoint(
            event=None,
            checkpoint=_checkpoint_for(
                run=active_root,
                tree=(active_root,),
                number=2,
                state=DurableCheckpointState.CONFIRMED,
                event_sequence=0,
                snapshot=DurableRuntimeSnapshot(
                    phase="MODEL_FINISHED",
                    journal=("MODEL_FINISHED",),
                    local_state={
                        "agent_model_calls": 0,
                        "agent_session_snapshot": {
                            "schema": "agent_session_snapshot_v3",
                            "pages": [["state"]],
                        },
                    },
                ),
            ),
            next_state=DurableRunState.RUNNING,
        )

        await coordinator.resume(run_id=root.run_id)
        terminal = await _wait_for_terminal(store, root.run_id)

        assert terminal.state is DurableRunState.COMPLETED
        assert terminal.terminal_error_code is None
        assert executor.calls == 1
        await coordinator.shutdown()

    asyncio.run(scenario())


def test_confirmed_child_tree_no_longer_blocks_root_resume() -> None:
    async def scenario() -> None:
        store = _Store()
        executor = _CompletedExecutor()
        coordinator = _coordinator(store, executor)
        request = SearchRequest(query="parallel durable laptops", top_k=1)
        root = await store.reserve_root_run(
            run_id="run-durable-tree-resume",
            thread_id="thread-durable-tree-resume",
            request_payload=durable_request_payload(
                request=request,
                display_query="parallel durable laptops",
                task_turns=("parallel durable laptops",),
            ),
            asset_version="durable-test-assets-v1",
            config_fingerprint="a" * 64,
        )
        await store.append_event_checkpoint(
            event=None,
            checkpoint=_checkpoint_for(
                run=root,
                tree=(root,),
                number=1,
                state=DurableCheckpointState.CONFIRMED,
                event_sequence=0,
                snapshot=DurableRuntimeSnapshot(
                    phase="MODEL_FINISHED",
                    local_state={
                        "agent_model_calls": 0,
                        "agent_session_snapshot": {
                            "schema": "agent_session_snapshot_v3",
                            "pages": [["state"]],
                        },
                    },
                ),
            ),
            next_state=DurableRunState.RUNNING,
        )
        child = await store.reserve_child_run(
            run_id="run-durable-tree-child",
            thread_id="thread-durable-tree-child",
            root_run_id=root.run_id,
            parent_run_id=root.run_id,
            child_id="child-tree-1",
            depth=1,
            task_scope_digest="b" * 64,
            request_payload=durable_request_payload(
                request=request,
                display_query="parallel durable laptops",
                task_turns=("parallel durable laptops",),
            ),
            asset_version=root.asset_version,
            config_fingerprint=root.config_fingerprint,
        )
        tree = await store.load_run_tree(root_run_id=root.run_id)
        await store.append_event_checkpoint(
            event=None,
            checkpoint=_checkpoint_for(
                run=child,
                tree=tree,
                number=1,
                state=DurableCheckpointState.CONFIRMED,
                event_sequence=0,
                snapshot=DurableRuntimeSnapshot(
                    phase="TOOL_FINISHED",
                    local_state={
                        "agent_model_calls": 0,
                        "agent_session_snapshot": {
                            "schema": "agent_session_snapshot_v3",
                            "pages": [["state"]],
                        },
                    },
                ),
            ),
            next_state=DurableRunState.RUNNING,
        )

        await coordinator.resume(run_id=root.run_id)
        terminal = await _wait_for_terminal(store, root.run_id)

        assert terminal.state is DurableRunState.COMPLETED
        assert executor.calls == 1
        await coordinator.shutdown()

    asyncio.run(scenario())


def test_agentloop_checkpoint_and_event_writes_share_one_run_sequence() -> None:
    async def scenario() -> None:
        store = _Store()
        coordinator = _coordinator(store, _CompletedExecutor())
        root = await store.reserve_root_run(
            run_id="run-durable-write-lock",
            thread_id="thread-durable-write-lock",
            request_payload=_durable_request_payload(),
            asset_version="durable-test-assets-v1",
            config_fingerprint="a" * 64,
        )
        await store.append_event_checkpoint(
            event=None,
            checkpoint=_checkpoint_for(
                run=root,
                tree=(root,),
                number=1,
                state=DurableCheckpointState.CONFIRMED,
                event_sequence=0,
                snapshot=DurableRuntimeSnapshot(phase="ACCEPTED"),
            ),
            next_state=DurableRunState.ACCEPTED,
        )
        active = await store.load_run(run_id=root.run_id)
        event = AgentRunEvent(
            kind=AgentEventKind.MODEL_STARTED,
            run_id=root.run_id,
            round=1,
        )

        await asyncio.gather(
            coordinator._persist_runtime_snapshot(
                root_run_id=root.run_id,
                run_id=root.run_id,
                snapshot=DurableRuntimeSnapshot(
                    phase="MODEL_STARTED",
                    local_state={
                        "agent_model_calls": 1,
                        "agent_session_snapshot": {
                            "schema": "agent_session_snapshot_v3",
                            "pages": [["state"]],
                        },
                    },
                ),
                state=DurableCheckpointState.CONFIRMED,
            ),
            coordinator._append_stream_event(
                stream_run=active,
                emitter_run=active,
                event=event,
                events=(event,),
            ),
        )

        latest = await store.latest_checkpoint(run_id=root.run_id)
        assert latest is not None
        assert latest.checkpoint_number == 3
        assert (await store.load_run(run_id=root.run_id)).event_sequence == 1
        assert len(await store.load_events(run_id=root.run_id)) == 1
        await coordinator.shutdown()

    asyncio.run(scenario())


def test_cancel_creates_one_aborted_terminal_event_without_business_response() -> None:
    async def scenario() -> None:
        store = _Store()
        executor = _BlockingExecutor()
        coordinator = _coordinator(store, executor)
        submitted = await coordinator.submit(
            request=SearchRequest(query="durable phone", top_k=1),
            display_query="durable phone",
            task_turns=("durable phone",),
            thread_id="thread-durable-test-1",
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
        run = await store.reserve_root_run(
            run_id="run-durable-test-1",
            thread_id="thread-durable-test-1",
            request_payload=_durable_request_payload(),
            asset_version="durable-test-assets-v1",
            config_fingerprint="a" * 64,
        )
        await store.append_event_checkpoint(
            event=None,
            checkpoint=_checkpoint_for(
                run=run,
                tree=(run,),
                number=1,
                state=DurableCheckpointState.CONFIRMED,
                event_sequence=0,
                snapshot=DurableRuntimeSnapshot(phase="ACCEPTED"),
            ),
            next_state=DurableRunState.ACCEPTED,
        )
        await store.append_event_checkpoint(
            event=None,
            checkpoint=_checkpoint_for(
                run=run,
                tree=(run,),
                number=2,
                state=DurableCheckpointState.REMOTE_PENDING,
                event_sequence=0,
                snapshot=DurableRuntimeSnapshot(phase="EXTERNAL_AGENT_EXECUTION"),
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

    assert key.startswith("durable:retrieval:v3:")
    assert len(key) == len("durable:retrieval:v3:") + 64
    assert "私人" not in key
