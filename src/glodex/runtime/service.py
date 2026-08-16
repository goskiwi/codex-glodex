"""Durable v2 coordinator for Agent runs and their safe public projection."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Protocol, cast

from glodex.agent.contracts import (
    AgentDemoResponse,
    AgentEventKind,
    AgentEventScope,
    AgentExecution,
    AgentRunEvent,
)
from glodex.agent.ports import AgentEventObserver
from glodex.api.agent_events import AgentEventProjector
from glodex.contracts import SearchRequest
from glodex.memory.terminal import MemoryTerminalWriterPort
from glodex.observability.exporter import (
    DisabledTraceExporter,
    M6TraceExporter,
    M6TraceExportStorePort,
    export_terminal_trace,
)
from glodex.observability.runtime import M6TraceEventDraft, M6TraceEventKind
from glodex.runtime.context import (
    digest_safe_events,
    thread_context_key,
    update_thread_context,
)
from glodex.runtime.contracts import (
    DURABLE_CONTEXT_CACHE_TTL_SECONDS,
    DURABLE_MAX_TREE_RUNS,
    DURABLE_SCHEMA_VERSION,
    TERMINAL_RUN_STATES,
    DurableCheckpoint,
    DurableCheckpointState,
    DurableCheckpointWriter,
    DurableEvent,
    DurableExecutionContext,
    DurableRun,
    DurableRunState,
    DurableRuntimeSnapshot,
    DurableStoreError,
    LoopKind,
    cache_key,
    canonical_hash,
    runtime_checkpoint_payload,
    validate_digest,
    validate_identifier,
)
from glodex.runtime.ports import (
    DurableStorePort,
    M6TraceStorePort,
    RetrievalCachePort,
)
from glodex.runtime.request_payload import (
    durable_request_payload,
    search_request_from_durable_payload,
)

_LOGGER = logging.getLogger(__name__)
_CHILD_HANDOFF_SCHEMA = "glodex.child-handoff.v2"


class DurableAgentExecutor(Protocol):
    """The explicit v2 Agent service boundary required by the coordinator.

    The executor receives its last confirmed root checkpoint and a context that
    includes the complete safe run tree.  Agent code uses
    ``runtime_context.checkpoint_writer`` to persist local root/child state at
    safe boundaries.  This protocol intentionally has no v1 overload.
    """

    async def execute_run(
        self,
        request: SearchRequest,
        *,
        run_id: str,
        observer: AgentEventObserver | None,
        checkpoint: DurableCheckpoint,
        runtime_context: DurableExecutionContext,
    ) -> AgentExecution: ...


@dataclass(slots=True)
class _EventCollector(AgentEventObserver):
    events: list[AgentRunEvent] = field(default_factory=list)
    pending: asyncio.Queue[AgentRunEvent] = field(default_factory=asyncio.Queue)

    def on_event(self, event: AgentRunEvent) -> None:
        if type(event) is not AgentRunEvent:
            raise TypeError("durable event collector requires exact AgentRunEvent")
        self.events.append(event)
        self.pending.put_nowait(event)


@dataclass(frozen=True, slots=True)
class _CoordinatorCheckpointWriter(DurableCheckpointWriter):
    """Agent-facing checkpoint sink scoped to exactly one durable root tree."""

    coordinator: DurableAgentCoordinator
    root_run_id: str

    async def persist(
        self,
        *,
        run_id: str,
        snapshot: DurableRuntimeSnapshot,
        state: DurableCheckpointState = DurableCheckpointState.CONFIRMED,
    ) -> DurableCheckpoint:
        return await self.coordinator._persist_runtime_snapshot(
            root_run_id=self.root_run_id,
            run_id=run_id,
            snapshot=snapshot,
            state=state,
        )

    async def reserve_child(
        self,
        *,
        run_id: str,
        thread_id: str,
        parent_run_id: str,
        child_id: str,
        depth: int,
        task_scope_digest: str,
        initial_snapshot: DurableRuntimeSnapshot,
    ) -> DurableRun:
        return await self.coordinator._reserve_runtime_child(
            root_run_id=self.root_run_id,
            run_id=run_id,
            thread_id=thread_id,
            parent_run_id=parent_run_id,
            child_id=child_id,
            depth=depth,
            task_scope_digest=task_scope_digest,
            initial_snapshot=initial_snapshot,
        )


@dataclass(slots=True)
class DurableAgentCoordinator:
    """Persist, resume and project one root Agent run plus any stored tree records."""

    store: DurableStorePort
    executor: DurableAgentExecutor
    projector: AgentEventProjector
    asset_version: str
    config_fingerprint: str
    context_cache: RetrievalCachePort | None = None
    terminal_writer: MemoryTerminalWriterPort | None = None
    m6_trace_store: M6TraceStorePort | None = None
    m6_trace_exporter: M6TraceExporter | None = None
    run_id_factory: Callable[[], str] = lambda: f"run-{uuid.uuid4().hex}"
    _tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict, init=False)
    _write_locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if (
            not callable(self.executor.execute_run)
            or type(self.projector) is not AgentEventProjector
            or type(self.asset_version) is not str
            or not self.asset_version
            or type(self.config_fingerprint) is not str
            or len(self.config_fingerprint) != 64
            or not callable(self.run_id_factory)
            or (
                self.terminal_writer is not None
                and not callable(getattr(self.terminal_writer, "write_terminal", None))
            )
            or (
                self.m6_trace_store is not None
                and (
                    not callable(getattr(self.m6_trace_store, "ensure_m6_trace", None))
                    or not callable(getattr(self.m6_trace_store, "append_m6_trace_event", None))
                )
            )
            or (
                self.m6_trace_exporter is not None
                and not callable(getattr(self.m6_trace_exporter, "export_trace", None))
            )
            or (self.m6_trace_exporter is not None and self.m6_trace_store is None)
        ):
            raise TypeError("durable coordinator inputs are invalid")

    async def submit(
        self,
        *,
        request: SearchRequest,
        display_query: str,
        task_turns: tuple[str, ...],
        thread_id: str,
    ) -> DurableRun:
        if type(request) is not SearchRequest:
            raise TypeError("durable submission requires an exact SearchRequest")
        run_id = self.run_id_factory()
        run = await self.store.reserve_root_run(
            run_id=run_id,
            thread_id=thread_id,
            request_payload=durable_request_payload(
                request=request,
                display_query=display_query,
                task_turns=task_turns,
            ),
            asset_version=self.asset_version,
            config_fingerprint=self.config_fingerprint,
        )
        tree = await self.store.load_run_tree(root_run_id=run.run_id)
        await self.store.append_event_checkpoint(
            event=None,
            checkpoint=_checkpoint_for(
                run=run,
                tree=tree,
                number=1,
                state=DurableCheckpointState.CONFIRMED,
                event_sequence=0,
                snapshot=DurableRuntimeSnapshot(phase="ACCEPTED"),
            ),
            next_state=DurableRunState.ACCEPTED,
        )
        await self._begin_m6_trace(run=run)
        self._start(run_id=run.run_id, request=request)
        return await self.store.load_run(run_id=run.run_id)

    async def resume(self, *, run_id: str) -> DurableRun:
        if run_id in self._tasks:
            raise DurableStoreError("DURABLE_RUN_ALREADY_ACTIVE")
        run = await self.store.load_run(run_id=run_id)
        if run.loop_kind is not LoopKind.ROOT:
            raise DurableStoreError("DURABLE_CHILD_RESUME_REQUIRES_ROOT")
        if run.state in TERMINAL_RUN_STATES:
            return run
        if (
            run.asset_version != self.asset_version
            or run.config_fingerprint != self.config_fingerprint
        ):
            await self._abort(run=run, code="DURABLE_RUNTIME_IDENTITY_MISMATCH")
            return await self.store.load_run(run_id=run_id)
        if run.cancel_requested:
            await self._abort(run=run, code="RUN_CANCELLED")
            return await self.store.load_run(run_id=run_id)
        checkpoint = await self.store.latest_checkpoint(run_id=run_id)
        if checkpoint is None or checkpoint.state is not DurableCheckpointState.CONFIRMED:
            code = (
                "DURABLE_REMOTE_STEP_UNCERTAIN"
                if (
                    checkpoint is not None
                    and checkpoint.state is DurableCheckpointState.REMOTE_PENDING
                )
                else "DURABLE_CHECKPOINT_UNSUPPORTED"
            )
            await self._abort(run=run, code=code)
            return await self.store.load_run(run_id=run_id)
        tree = await self.store.load_run_tree(root_run_id=run_id)
        try:
            snapshot = _snapshot_from_payload(checkpoint.runtime_payload)
            local_state = snapshot.local_state
            initial_checkpoint = snapshot.phase == "ACCEPTED" and (
                local_state is None or local_state == {}
            )
            valid_loop_checkpoint = (
                type(local_state) is dict
                and set(local_state) == {"agent_model_calls", "agent_session_snapshot"}
                and type(local_state["agent_model_calls"]) is int
                and not isinstance(local_state["agent_model_calls"], bool)
                and 0 <= local_state["agent_model_calls"] <= 14
                and type(local_state["agent_session_snapshot"]) is dict
            )
            if not initial_checkpoint and not valid_loop_checkpoint:
                raise ValueError("durable Agent checkpoint local state is invalid")
        except (DurableStoreError, TypeError, ValueError):
            await self._abort(run=run, code="DURABLE_CHECKPOINT_INVALID")
            return await self.store.load_run(run_id=run_id)
        for child in tree:
            if child.loop_kind is not LoopKind.CHILD:
                continue
            child_checkpoint = await self.store.latest_checkpoint(run_id=child.run_id)
            if child_checkpoint is None or child_checkpoint.state not in {
                DurableCheckpointState.CONFIRMED,
                DurableCheckpointState.TERMINAL,
            }:
                await self._abort(run=run, code="DURABLE_CHECKPOINT_TREE_INVALID")
                return await self.store.load_run(run_id=run_id)
            if child.state in {DurableRunState.FAILED, DurableRunState.ABORTED}:
                await self._abort(run=run, code="DURABLE_CHILD_RECOVERY_FAILED")
                return await self.store.load_run(run_id=run_id)
        recovered = await self.store.set_recoverable(run_id=run_id)
        request = _request_from_private_payload(recovered.request_payload)
        self._start(run_id=run_id, request=request)
        return await self.store.load_run(run_id=run_id)

    async def cancel(self, *, run_id: str) -> DurableRun:
        run = await self.store.load_run(run_id=run_id)
        if run.loop_kind is not LoopKind.ROOT:
            raise DurableStoreError("DURABLE_CHILD_CANCEL_REQUIRES_ROOT")
        run = await self.store.request_cancel(run_id=run_id)
        if run.state in TERMINAL_RUN_STATES:
            return run
        task = self._tasks.get(run_id)
        if task is None:
            await self._abort(run=run, code="RUN_CANCELLED")
        else:
            task.cancel()
        return await self.store.load_run(run_id=run_id)

    async def shutdown(self) -> None:
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._write_locks.clear()

    def _start(self, *, run_id: str, request: SearchRequest) -> None:
        if run_id in self._tasks:
            raise DurableStoreError("DURABLE_RUN_ALREADY_ACTIVE")
        task = asyncio.create_task(self._execute(run_id=run_id, request=request))
        self._tasks[run_id] = task

    async def _execute(self, *, run_id: str, request: SearchRequest) -> None:
        collector: _EventCollector | None = None
        try:
            run = await self.store.load_run(run_id=run_id)
            if run.loop_kind is not LoopKind.ROOT:
                raise DurableStoreError("DURABLE_EXECUTOR_REQUIRES_ROOT")
            if run.cancel_requested:
                await self._abort(run=run, code="RUN_CANCELLED")
                return
            latest = await self.store.latest_checkpoint(run_id=run_id)
            if latest is None or latest.state is not DurableCheckpointState.CONFIRMED:
                raise DurableStoreError("DURABLE_CHECKPOINT_MISSING")
            execution_context = await self._execution_context(run=run, checkpoint=latest)
            tree = await self.store.load_run_tree(root_run_id=run_id)
            await self.store.append_event_checkpoint(
                event=None,
                checkpoint=_checkpoint_for(
                    run=run,
                    tree=tree,
                    number=latest.checkpoint_number + 1,
                    state=DurableCheckpointState.REMOTE_PENDING,
                    event_sequence=run.event_sequence,
                    snapshot=DurableRuntimeSnapshot(
                        phase="EXTERNAL_AGENT_EXECUTION",
                        journal=_snapshot_from_payload(latest.runtime_payload).journal,
                        budget=_snapshot_from_payload(latest.runtime_payload).budget,
                        local_state=_snapshot_from_payload(latest.runtime_payload).local_state,
                        handoffs=_snapshot_from_payload(latest.runtime_payload).handoffs,
                    ),
                    next_run_state=DurableRunState.RUNNING,
                ),
                next_state=DurableRunState.RUNNING,
            )
            await self._cache_context(root_run_id=run_id, events=())
            collector = _EventCollector()
            execution, terminal_event = await self._execute_with_live_events(
                request,
                root_run_id=run_id,
                checkpoint=latest,
                runtime_context=execution_context,
                collector=collector,
            )
            await self._finish_collected_execution(
                root_run_id=run_id,
                execution=execution,
                events=collector.events,
                terminal_event=terminal_event,
            )
        except asyncio.CancelledError:
            await self._abort_for_run_id(
                run_id=run_id,
                code="RUN_CANCELLED",
                events=() if collector is None else collector.events,
            )
        except DurableStoreError:
            await self._abort_for_run_id(
                run_id=run_id,
                code="DURABLE_EXECUTION_FAILED",
                events=() if collector is None else collector.events,
            )
        except Exception:
            _LOGGER.exception("durable Agent execution failed after runtime start")
            await self._abort_for_run_id(
                run_id=run_id,
                code="DURABLE_EXECUTION_FAILED",
                events=() if collector is None else collector.events,
            )
        finally:
            self._tasks.pop(run_id, None)

    async def _execution_context(
        self,
        *,
        run: DurableRun,
        checkpoint: DurableCheckpoint,
    ) -> DurableExecutionContext:
        tree = await self.store.load_run_tree(root_run_id=run.run_id)
        child_checkpoints: list[DurableCheckpoint] = []
        for child in tree:
            if child.loop_kind is LoopKind.CHILD:
                latest = await self.store.latest_checkpoint(run_id=child.run_id)
                if latest is not None:
                    child_checkpoints.append(latest)
        return DurableExecutionContext(
            run=run,
            checkpoint=checkpoint,
            tree=tree,
            child_checkpoints=tuple(child_checkpoints),
            resume_payload=checkpoint.runtime_payload,
            checkpoint_writer=_CoordinatorCheckpointWriter(
                coordinator=self,
                root_run_id=run.run_id,
            ),
        )

    async def _persist_runtime_snapshot(
        self,
        *,
        root_run_id: str,
        run_id: str,
        snapshot: DurableRuntimeSnapshot,
        state: DurableCheckpointState,
    ) -> DurableCheckpoint:
        validate_identifier(root_run_id, name="root run ID")
        validate_identifier(run_id, name="run ID")
        if type(snapshot) is not DurableRuntimeSnapshot or state not in {
            DurableCheckpointState.CONFIRMED,
            DurableCheckpointState.REMOTE_PENDING,
        }:
            raise ValueError("durable runtime snapshot write is invalid")
        async with self._write_lock(run_id):
            run = await self.store.load_run(run_id=run_id)
            if (
                run.root_run_id != root_run_id
                or run.state in TERMINAL_RUN_STATES
                or run.cancel_requested
            ):
                raise DurableStoreError("DURABLE_RUNTIME_SNAPSHOT_NOT_ACTIVE")
            latest = await self.store.latest_checkpoint(run_id=run_id)
            if latest is None:
                raise DurableStoreError("DURABLE_CHECKPOINT_MISSING")
            tree = await self.store.load_run_tree(root_run_id=root_run_id)
            checkpoint = _checkpoint_for(
                run=run,
                tree=tree,
                number=latest.checkpoint_number + 1,
                state=state,
                event_sequence=run.event_sequence,
                snapshot=snapshot,
                next_run_state=DurableRunState.RUNNING,
            )
            await self.store.append_event_checkpoint(
                event=None,
                checkpoint=checkpoint,
                next_state=DurableRunState.RUNNING,
            )
            return checkpoint

    def _write_lock(self, run_id: str) -> asyncio.Lock:
        validate_identifier(run_id, name="run ID")
        lock = self._write_locks.get(run_id)
        if lock is None:
            lock = asyncio.Lock()
            self._write_locks[run_id] = lock
        return lock

    async def _reserve_runtime_child(
        self,
        *,
        root_run_id: str,
        run_id: str,
        thread_id: str,
        parent_run_id: str,
        child_id: str,
        depth: int,
        task_scope_digest: str,
        initial_snapshot: DurableRuntimeSnapshot,
    ) -> DurableRun:
        """Register and checkpoint one child before its first lifecycle event.

        A child is never inferred from an event.  This makes its private
        checkpoint and its parentage durable before an observer can expose
        ``CHILD_RUN_STARTED`` to the root stream.
        """

        for value, name in (
            (root_run_id, "root run ID"),
            (run_id, "run ID"),
            (thread_id, "thread ID"),
            (parent_run_id, "parent run ID"),
            (child_id, "child ID"),
        ):
            validate_identifier(value, name=name)
        validate_digest(task_scope_digest, name="task scope digest")
        if (
            run_id == root_run_id
            or type(depth) is not int
            or isinstance(depth, bool)
            or not 1 <= depth <= 2
            or type(initial_snapshot) is not DurableRuntimeSnapshot
        ):
            raise ValueError("durable child reservation is invalid")

        root = await self.store.load_run(run_id=root_run_id)
        if (
            root.run_id != root_run_id
            or root.loop_kind is not LoopKind.ROOT
            or root.state in TERMINAL_RUN_STATES
            or root.cancel_requested
        ):
            raise DurableStoreError("DURABLE_ROOT_TREE_NOT_ACTIVE")
        parent = await self.store.load_run(run_id=parent_run_id)
        if (
            parent.root_run_id != root_run_id
            or parent.state in TERMINAL_RUN_STATES
            or parent.cancel_requested
            or parent.depth + 1 != depth
            or thread_id == parent.thread_id
        ):
            raise DurableStoreError("DURABLE_CHILD_TREE_INVALID")
        tree = await self.store.load_run_tree(root_run_id=root_run_id)
        if len(tree) >= DURABLE_MAX_TREE_RUNS:
            raise DurableStoreError("DURABLE_CHILD_TREE_CAPACITY_EXCEEDED")
        if any(item.parent_run_id == parent_run_id and item.child_id == child_id for item in tree):
            raise DurableStoreError("DURABLE_CHILD_ID_ALREADY_REGISTERED")

        child = await self.store.reserve_child_run(
            run_id=run_id,
            thread_id=thread_id,
            root_run_id=root_run_id,
            parent_run_id=parent_run_id,
            child_id=child_id,
            depth=depth,
            task_scope_digest=task_scope_digest,
            request_payload=root.request_payload,
            asset_version=root.asset_version,
            config_fingerprint=root.config_fingerprint,
        )
        child_tree = await self.store.load_run_tree(root_run_id=root_run_id)
        await self.store.append_event_checkpoint(
            event=None,
            checkpoint=_checkpoint_for(
                run=child,
                tree=child_tree,
                number=1,
                state=DurableCheckpointState.CONFIRMED,
                event_sequence=0,
                snapshot=initial_snapshot,
            ),
            next_state=DurableRunState.ACCEPTED,
        )
        return await self.store.load_run(run_id=child.run_id)

    async def _execute_with_live_events(
        self,
        request: SearchRequest,
        *,
        root_run_id: str,
        checkpoint: DurableCheckpoint,
        runtime_context: DurableExecutionContext,
        collector: _EventCollector,
    ) -> tuple[AgentExecution, AgentRunEvent]:
        """Persist root and child events as their synchronous observer emits them."""

        if (
            type(request) is not SearchRequest
            or type(checkpoint) is not DurableCheckpoint
            or type(runtime_context) is not DurableExecutionContext
            or type(collector) is not _EventCollector
        ):
            raise TypeError("live event execution inputs are invalid")
        execution_task = asyncio.create_task(
            self.executor.execute_run(
                request,
                run_id=root_run_id,
                observer=collector,
                checkpoint=checkpoint,
                runtime_context=runtime_context,
            )
        )
        next_event_task = asyncio.create_task(collector.pending.get())
        handled_events: list[AgentRunEvent] = []
        terminal_event: AgentRunEvent | None = None
        try:
            while True:
                completed, _ = await asyncio.wait(
                    {execution_task, next_event_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if next_event_task in completed:
                    event = next_event_task.result()
                    handled_events.append(event)
                    next_event_task = asyncio.create_task(collector.pending.get())
                    if event.kind in {AgentEventKind.AGENT_RESULT, AgentEventKind.AGENT_ERROR}:
                        if terminal_event is not None or event.run_id != root_run_id:
                            raise DurableStoreError("DURABLE_EXECUTION_INVALID")
                        terminal_event = event
                    else:
                        if terminal_event is not None:
                            raise DurableStoreError("DURABLE_EXECUTION_INVALID")
                        await self._persist_live_event(
                            root_run_id=root_run_id,
                            event=event,
                            events=handled_events,
                        )
                    continue
                if execution_task in completed:
                    next_event_task.cancel()
                    await asyncio.gather(next_event_task, return_exceptions=True)
                    break
            execution = execution_task.result()
        finally:
            if not execution_task.done():
                execution_task.cancel()
            if not next_event_task.done():
                next_event_task.cancel()
            await asyncio.gather(execution_task, next_event_task, return_exceptions=True)

        if (
            type(execution) is not AgentExecution
            or terminal_event is None
            or not handled_events
            or handled_events[-1] is not terminal_event
            or len(handled_events) != len(collector.events)
        ):
            raise DurableStoreError("DURABLE_EXECUTION_INVALID")
        return execution, terminal_event

    async def _persist_live_event(
        self,
        *,
        root_run_id: str,
        event: AgentRunEvent,
        events: list[AgentRunEvent],
    ) -> None:
        """Persist the owning child stream and the root aggregate SSE stream.

        A child event therefore receives two independent cursors: its private
        per-child cursor for recovery and the root cursor consumed by the user
        facing SSE endpoint.  Its payload always exposes the child ``runId``.
        """

        root = await self.store.load_run(run_id=root_run_id)
        if root.cancel_requested:
            raise DurableStoreError("DURABLE_RUN_CANCELLED")
        emitter = await self._resolve_event_run(root=root, event=event)
        if emitter.loop_kind is LoopKind.ROOT:
            await self._append_stream_event(
                stream_run=emitter,
                emitter_run=emitter,
                event=event,
                events=events,
            )
        else:
            if _is_child_terminal_event(event):
                await self._finish_child_event(
                    child=emitter,
                    event=event,
                    events=events,
                )
            else:
                await self._append_stream_event(
                    stream_run=emitter,
                    emitter_run=emitter,
                    event=event,
                    events=events,
                )
            # The root's aggregate stream makes child lifecycle visible to SSE
            # while retaining the child's own private sequence/checkpoint.
            root = await self.store.load_run(run_id=root_run_id)
            await self._append_stream_event(
                stream_run=root,
                emitter_run=emitter,
                event=event,
                events=events,
            )
        await self._cache_context(root_run_id=root_run_id, events=events)

    async def _resolve_event_run(
        self,
        *,
        root: DurableRun,
        event: AgentRunEvent,
    ) -> DurableRun:
        if type(event) is not AgentRunEvent:
            raise TypeError("durable event requires exact AgentRunEvent")
        if event.run_id == root.run_id:
            if event.kind in {
                AgentEventKind.CHILD_RUN_STARTED,
                AgentEventKind.CHILD_CHECKPOINT_CONFIRMED,
                AgentEventKind.CHILD_HANDOFF_READY,
                AgentEventKind.CHILD_FAILED,
            }:
                raise DurableStoreError("DURABLE_CHILD_EVENT_ROOT_ID_INVALID")
            _validate_parent_event(root, event)
            return root
        try:
            child = await self.store.load_run(run_id=event.run_id)
        except DurableStoreError as error:
            if error.args == ("DURABLE_RUN_NOT_FOUND",):
                raise DurableStoreError("DURABLE_CHILD_NOT_REGISTERED") from None
            raise
        if child.root_run_id != root.run_id or child.loop_kind is not LoopKind.CHILD:
            raise DurableStoreError("DURABLE_CHILD_EVENT_TREE_INVALID")
        _validate_child_event(child, event)
        return child

    async def _append_stream_event(
        self,
        *,
        stream_run: DurableRun,
        emitter_run: DurableRun,
        event: AgentRunEvent,
        events: tuple[AgentRunEvent, ...] | list[AgentRunEvent],
    ) -> DurableRun:
        async with self._write_lock(stream_run.run_id):
            stream_run = await self.store.load_run(run_id=stream_run.run_id)
            if stream_run.state in TERMINAL_RUN_STATES or stream_run.cancel_requested:
                raise DurableStoreError("DURABLE_STREAM_NOT_ACTIVE")
            latest = await self.store.latest_checkpoint(run_id=stream_run.run_id)
            if latest is None:
                raise DurableStoreError("DURABLE_CHECKPOINT_MISSING")
            next_sequence = stream_run.event_sequence + 1
            projected = self.projector.project_event(
                run=emitter_run,
                event=event,
                sequence=next_sequence,
            )
            payload = projected.model_dump(mode="json", by_alias=True, exclude_none=True)
            durable_event = DurableEvent(
                run_id=stream_run.run_id,
                sequence=next_sequence,
                payload=payload,
                payload_hash=canonical_hash(payload),
            )
            tree = await self.store.load_run_tree(root_run_id=stream_run.root_run_id)
            snapshot = _event_snapshot(
                prior=_snapshot_from_payload(latest.runtime_payload),
                owner=stream_run,
                event=event,
            )
            return await self.store.append_event_checkpoint(
                event=durable_event,
                checkpoint=_checkpoint_for(
                    run=stream_run,
                    tree=tree,
                    number=latest.checkpoint_number + 1,
                    state=DurableCheckpointState.CONFIRMED,
                    event_sequence=next_sequence,
                    snapshot=snapshot,
                    next_run_state=DurableRunState.RUNNING,
                ),
                next_state=DurableRunState.RUNNING,
            )

    async def _finish_child_event(
        self,
        *,
        child: DurableRun,
        event: AgentRunEvent,
        events: tuple[AgentRunEvent, ...] | list[AgentRunEvent],
    ) -> DurableRun:
        latest = await self.store.latest_checkpoint(run_id=child.run_id)
        if latest is None:
            raise DurableStoreError("DURABLE_CHECKPOINT_MISSING")
        state, error_code, response = _child_terminal(state_event=event)
        next_sequence = child.event_sequence + 1
        projected = self.projector.project_event(run=child, event=event, sequence=next_sequence)
        payload = projected.model_dump(mode="json", by_alias=True, exclude_none=True)
        durable_event = DurableEvent(
            run_id=child.run_id,
            sequence=next_sequence,
            payload=payload,
            payload_hash=canonical_hash(payload),
        )
        tree = await self.store.load_run_tree(root_run_id=child.root_run_id)
        return await self.store.finish_run(
            event=durable_event,
            checkpoint=_checkpoint_for(
                run=child,
                tree=tree,
                number=latest.checkpoint_number + 1,
                state=(
                    DurableCheckpointState.CANCELLED
                    if state is DurableRunState.ABORTED
                    else DurableCheckpointState.TERMINAL
                ),
                event_sequence=next_sequence,
                snapshot=_event_snapshot(
                    prior=_snapshot_from_payload(latest.runtime_payload),
                    owner=child,
                    event=event,
                ),
                next_run_state=state,
            ),
            state=state,
            response=response,
            error_code=error_code,
        )

    async def _finish_collected_execution(
        self,
        *,
        root_run_id: str,
        execution: AgentExecution,
        events: list[AgentRunEvent],
        terminal_event: AgentRunEvent,
    ) -> None:
        if (
            type(execution) is not AgentExecution
            or not events
            or events[-1] is not terminal_event
            or terminal_event.kind not in {AgentEventKind.AGENT_RESULT, AgentEventKind.AGENT_ERROR}
        ):
            raise DurableStoreError("DURABLE_EXECUTION_INVALID")
        run = await self.store.load_run(run_id=root_run_id)
        if run.cancel_requested:
            await self._abort(run=run, code="RUN_CANCELLED", events=events)
            return
        durable_event = _project_durable_event(
            projector=self.projector,
            stream_run=run,
            emitter_run=run,
            event=terminal_event,
            sequence=run.event_sequence + 1,
        )
        await self._finish_execution(
            run=run,
            event=terminal_event,
            durable_event=durable_event,
            execution=execution,
            events=events,
        )

    async def _finish_execution(
        self,
        *,
        run: DurableRun,
        event: AgentRunEvent,
        durable_event: DurableEvent,
        execution: AgentExecution,
        events: list[AgentRunEvent],
    ) -> None:
        latest = await self.store.latest_checkpoint(run_id=run.run_id)
        if latest is None:
            raise DurableStoreError("DURABLE_CHECKPOINT_MISSING")
        if event.kind is AgentEventKind.AGENT_RESULT:
            if event.status not in {"COMPLETED", "NO_MATCH"}:
                raise DurableStoreError("DURABLE_EXECUTION_INVALID")
            state = DurableRunState(event.status)
            error_code = None
        else:
            if event.status != "FAILED" or event.safe_code is None:
                raise DurableStoreError("DURABLE_EXECUTION_INVALID")
            state = DurableRunState.FAILED
            error_code = event.safe_code
        tree = await self.store.load_run_tree(root_run_id=run.run_id)
        terminal_run = await self.store.finish_run(
            event=durable_event,
            checkpoint=_checkpoint_for(
                run=run,
                tree=tree,
                number=latest.checkpoint_number + 1,
                state=DurableCheckpointState.TERMINAL,
                event_sequence=durable_event.sequence,
                snapshot=_event_snapshot(
                    prior=_snapshot_from_payload(latest.runtime_payload),
                    owner=run,
                    event=event,
                ),
                next_run_state=state,
            ),
            state=state,
            response=execution.response.model_dump(mode="json"),
            error_code=error_code,
        )
        if state in {DurableRunState.COMPLETED, DurableRunState.NO_MATCH}:
            await self._write_terminal_memory(run=terminal_run, response=execution.response)
        await self._record_m6_terminal(run=terminal_run, terminal_state=state.value)
        await self._cache_context(root_run_id=terminal_run.run_id, events=events)
        await self._cache_thread_context(run=terminal_run, events=events)

    async def _abort_for_run_id(
        self,
        *,
        run_id: str,
        code: str,
        events: tuple[AgentRunEvent, ...] | list[AgentRunEvent] = (),
    ) -> None:
        try:
            run = await self.store.load_run(run_id=run_id)
        except DurableStoreError:
            return
        if run.state not in TERMINAL_RUN_STATES:
            await self._abort(
                run=run,
                code="RUN_CANCELLED" if run.cancel_requested else code,
                events=events,
            )

    async def _abort(
        self,
        *,
        run: DurableRun,
        code: str,
        events: tuple[AgentRunEvent, ...] | list[AgentRunEvent] = (),
    ) -> None:
        if run.loop_kind is not LoopKind.ROOT:
            raise DurableStoreError("DURABLE_ABORT_REQUIRES_ROOT")
        await self._abort_active_children(root_run=run, code=code)
        latest = await self.store.latest_checkpoint(run_id=run.run_id)
        if latest is None:
            raise DurableStoreError("DURABLE_CHECKPOINT_MISSING")
        projected = self.projector.project_aborted(
            run=run,
            sequence=run.event_sequence + 1,
            safe_code=code,
        )
        payload = projected.model_dump(mode="json", by_alias=True, exclude_none=True)
        durable_event = DurableEvent(
            run_id=run.run_id,
            sequence=run.event_sequence + 1,
            payload=payload,
            payload_hash=canonical_hash(payload),
        )
        synthetic_event = AgentRunEvent(
            kind=AgentEventKind.AGENT_ERROR,
            run_id=run.run_id,
            status="ABORTED",
            safe_code=code,
        )
        tree = await self.store.load_run_tree(root_run_id=run.run_id)
        terminal_run = await self.store.finish_run(
            event=durable_event,
            checkpoint=_checkpoint_for(
                run=run,
                tree=tree,
                number=latest.checkpoint_number + 1,
                state=(
                    DurableCheckpointState.CANCELLED
                    if code == "RUN_CANCELLED"
                    else DurableCheckpointState.TERMINAL
                ),
                event_sequence=durable_event.sequence,
                snapshot=_event_snapshot(
                    prior=_snapshot_from_payload(latest.runtime_payload),
                    owner=run,
                    event=synthetic_event,
                ),
                next_run_state=DurableRunState.ABORTED,
            ),
            state=DurableRunState.ABORTED,
            response=None,
            error_code=code,
        )
        await self._record_m6_terminal(
            run=terminal_run,
            terminal_state=DurableRunState.ABORTED.value,
        )
        prefix = tuple(
            prior
            for prior in events
            if prior.kind not in {AgentEventKind.AGENT_RESULT, AgentEventKind.AGENT_ERROR}
        )
        completed_events = (*prefix, synthetic_event)
        await self._cache_context(root_run_id=terminal_run.run_id, events=completed_events)
        await self._cache_thread_context(run=terminal_run, events=completed_events)

    async def _abort_active_children(self, *, root_run: DurableRun, code: str) -> None:
        """Terminally fence active children before the root is marked aborted."""

        tree = await self.store.load_run_tree(root_run_id=root_run.run_id)
        for child in tree:
            if child.loop_kind is not LoopKind.CHILD or child.state in TERMINAL_RUN_STATES:
                continue
            latest = await self.store.latest_checkpoint(run_id=child.run_id)
            if latest is None:
                continue
            event = AgentRunEvent(
                kind=AgentEventKind.CHILD_HANDOFF_READY,
                run_id=child.run_id,
                scope=AgentEventScope.CHILD,
                child_id=cast(str, child.child_id),
                depth=child.depth,
                parent_run_id=cast(str, child.parent_run_id),
                task_scope_digest=cast(str, child.task_scope_digest),
                status="ABORTED",
            )
            durable_event = _project_durable_event(
                projector=self.projector,
                stream_run=child,
                emitter_run=child,
                event=event,
                sequence=child.event_sequence + 1,
            )
            current_tree = await self.store.load_run_tree(root_run_id=root_run.run_id)
            await self.store.finish_run(
                event=durable_event,
                checkpoint=_checkpoint_for(
                    run=child,
                    tree=current_tree,
                    number=latest.checkpoint_number + 1,
                    state=DurableCheckpointState.CANCELLED,
                    event_sequence=durable_event.sequence,
                    snapshot=_event_snapshot(
                        prior=_snapshot_from_payload(latest.runtime_payload),
                        owner=child,
                        event=event,
                    ),
                    next_run_state=DurableRunState.ABORTED,
                ),
                state=DurableRunState.ABORTED,
                response=None,
                error_code=code,
            )

    async def _cache_context(
        self,
        *,
        root_run_id: str,
        events: tuple[AgentRunEvent, ...] | list[AgentRunEvent],
    ) -> None:
        if self.context_cache is None:
            return
        try:
            digest = digest_safe_events(tuple(events))
            await self.context_cache.set(
                key=cache_key(
                    namespace="context",
                    material={"root_run_id": root_run_id, "event_count": digest.event_count},
                ),
                value=digest.cache_value(),
                ttl_seconds=DURABLE_CONTEXT_CACHE_TTL_SECONDS,
            )
        except Exception:
            return

    async def _cache_thread_context(
        self,
        *,
        run: DurableRun,
        events: tuple[AgentRunEvent, ...] | list[AgentRunEvent],
    ) -> None:
        if self.context_cache is None:
            return
        try:
            # Child working notes must never be merged into user-thread context.
            root_events = tuple(event for event in events if event.run_id == run.run_id)
            key = thread_context_key(thread_id=run.thread_id)
            previous = await self.context_cache.get(key=key, namespace="context")
            value = update_thread_context(
                previous=previous,
                run_id=run.run_id,
                events=root_events,
            )
            await self.context_cache.set(
                key=key,
                value=value,
                ttl_seconds=DURABLE_CONTEXT_CACHE_TTL_SECONDS,
            )
        except Exception:
            return

    async def _write_terminal_memory(
        self,
        *,
        run: DurableRun,
        response: AgentDemoResponse,
    ) -> None:
        if self.terminal_writer is None:
            return
        try:
            await self.terminal_writer.write_terminal(run=run, response=response)
        except Exception:
            return

    async def _begin_m6_trace(self, *, run: DurableRun) -> None:
        if self.m6_trace_store is None:
            return
        try:
            trace = await self.m6_trace_store.ensure_m6_trace(run_id=run.run_id)
            if trace.events:
                return
            await self.m6_trace_store.append_m6_trace_event(
                run_id=run.run_id,
                draft=M6TraceEventDraft(
                    kind=M6TraceEventKind.RUN_STARTED,
                    version=run.asset_version,
                ),
            )
        except Exception:
            return

    async def _record_m6_terminal(self, *, run: DurableRun, terminal_state: str) -> None:
        if self.m6_trace_store is None:
            return
        try:
            trace = await self.m6_trace_store.ensure_m6_trace(run_id=run.run_id)
            if trace.terminal_state is None:
                await self.m6_trace_store.append_m6_trace_event(
                    run_id=run.run_id,
                    draft=M6TraceEventDraft(
                        kind=M6TraceEventKind.TERMINAL,
                        safe_code=terminal_state,
                        version=run.asset_version,
                    ),
                )
        except Exception:
            return
        await self._export_m6_terminal(run_id=run.run_id)

    async def _export_m6_terminal(self, *, run_id: str) -> None:
        if self.m6_trace_store is None or self.m6_trace_exporter is None:
            return
        if type(self.m6_trace_exporter) is DisabledTraceExporter:
            return
        try:
            await export_terminal_trace(
                store=cast("M6TraceExportStorePort", self.m6_trace_store),
                exporter=self.m6_trace_exporter,
                run_id=run_id,
            )
        except Exception:
            return


def _checkpoint_for(
    *,
    run: DurableRun,
    tree: tuple[DurableRun, ...],
    number: int,
    state: DurableCheckpointState,
    event_sequence: int,
    snapshot: DurableRuntimeSnapshot,
    next_run_state: DurableRunState | None = None,
) -> DurableCheckpoint:
    """Build an exact v2 checkpoint with post-write cursor and run state."""

    if (
        type(number) is not int
        or number < 1
        or type(event_sequence) is not int
        or event_sequence < 0
        or (next_run_state is not None and type(next_run_state) is not DurableRunState)
    ):
        raise ValueError("durable checkpoint cursor is invalid")
    adjusted_run = replace(
        run,
        event_sequence=event_sequence,
        state=run.state if next_run_state is None else next_run_state,
    )
    adjusted_tree = tuple(adjusted_run if item.run_id == run.run_id else item for item in tree)
    payload = runtime_checkpoint_payload(run=adjusted_run, tree=adjusted_tree, snapshot=snapshot)
    value = {
        "asset_version": run.asset_version,
        "checkpoint_number": number,
        "config_fingerprint": run.config_fingerprint,
        "event_sequence": event_sequence,
        "runtime_payload": payload,
        "run_id": run.run_id,
        "schema_version": DURABLE_SCHEMA_VERSION,
        "state": state.value,
    }
    return DurableCheckpoint(
        run_id=run.run_id,
        checkpoint_number=number,
        state=state,
        event_sequence=event_sequence,
        runtime_payload=payload,
        asset_version=run.asset_version,
        config_fingerprint=run.config_fingerprint,
        payload_hash=canonical_hash(value),
    )


def _snapshot_from_payload(payload: dict[str, object]) -> DurableRuntimeSnapshot:
    """Rehydrate only already validated v2 safe runtime fields."""

    try:
        return DurableRuntimeSnapshot(
            phase=cast(str, payload["phase"]),
            journal=tuple(cast(list[str], payload["journal"])),
            budget=cast(dict[str, int], payload["budget"]),
            local_state=cast(dict[str, object], payload["local_state"]),
            handoffs=tuple(cast(list[dict[str, object]], payload["handoffs"])),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise DurableStoreError("DURABLE_CHECKPOINT_PAYLOAD_INVALID") from error


def _event_snapshot(
    *,
    prior: DurableRuntimeSnapshot,
    owner: DurableRun,
    event: AgentRunEvent,
) -> DurableRuntimeSnapshot:
    journal = (*prior.journal, event.kind.value)[-_MAX_JOURNAL:]
    budget = dict(prior.budget or {})
    budget["event_count"] = owner.event_sequence + 1
    local_state = dict(prior.local_state or {})
    local_state["last_event"] = event.kind.value
    if event.child_id is not None:
        local_state["last_child_id"] = event.child_id
    if event.kind is AgentEventKind.FORK_REQUESTED:
        local_state["pending_forks"] = _add_pending_fork(
            existing=local_state.get("pending_forks"),
            event=event,
        )
    elif event.kind in {
        AgentEventKind.CHILD_HANDOFF_READY,
        AgentEventKind.CHILD_FAILED,
        AgentEventKind.FORK_JOINED,
    }:
        local_state["pending_forks"] = _remove_pending_fork(
            existing=local_state.get("pending_forks"),
            child_id=event.child_id,
        )
    handoffs = prior.handoffs
    if _is_child_terminal_event(event):
        terminal_handoff: dict[str, object] = {
            "child_run_id": event.run_id,
            "status": cast(str, event.status),
            "safe_code": event.safe_code,
        }
        handoffs = (*handoffs, terminal_handoff)[-_MAX_HANDOFFS:]
    return DurableRuntimeSnapshot(
        phase=event.kind.value,
        journal=journal,
        budget=budget,
        local_state=local_state,
        handoffs=handoffs,
    )


def _add_pending_fork(*, existing: object, event: AgentRunEvent) -> list[dict[str, object]]:
    if (
        event.child_id is None
        or event.parent_run_id is None
        or event.task_scope_digest is None
        or event.depth is None
    ):
        raise DurableStoreError("DURABLE_PARENT_FORK_EVENT_INVALID")
    current = _pending_forks(existing)
    if any(item["child_id"] == event.child_id for item in current):
        raise DurableStoreError("DURABLE_DUPLICATE_PENDING_CHILD")
    return [
        *current,
        {
            "child_id": event.child_id,
            "parent_run_id": event.parent_run_id,
            "task_scope_digest": event.task_scope_digest,
            "depth": event.depth,
        },
    ][-_MAX_PENDING_FORKS:]


def _remove_pending_fork(*, existing: object, child_id: str | None) -> list[dict[str, object]]:
    if child_id is None:
        raise DurableStoreError("DURABLE_CHILD_EVENT_TREE_INVALID")
    return [item for item in _pending_forks(existing) if item["child_id"] != child_id]


def _pending_forks(value: object) -> list[dict[str, object]]:
    if value is None:
        return []
    if type(value) is not list or len(value) > _MAX_PENDING_FORKS:
        raise DurableStoreError("DURABLE_PENDING_FORK_STATE_INVALID")
    result: list[dict[str, object]] = []
    for item in value:
        if type(item) is not dict or frozenset(item) != {
            "child_id",
            "parent_run_id",
            "task_scope_digest",
            "depth",
        }:
            raise DurableStoreError("DURABLE_PENDING_FORK_STATE_INVALID")
        child_id = item["child_id"]
        parent_run_id = item["parent_run_id"]
        task_scope_digest = item["task_scope_digest"]
        depth = item["depth"]
        if (
            type(child_id) is not str
            or type(parent_run_id) is not str
            or type(task_scope_digest) is not str
            or type(depth) is not int
            or isinstance(depth, bool)
        ):
            raise DurableStoreError("DURABLE_PENDING_FORK_STATE_INVALID")
        result.append(dict(item))
    return result


def _project_durable_event(
    *,
    projector: AgentEventProjector,
    stream_run: DurableRun,
    emitter_run: DurableRun,
    event: AgentRunEvent,
    sequence: int,
) -> DurableEvent:
    projected = projector.project_event(run=emitter_run, event=event, sequence=sequence)
    payload = projected.model_dump(mode="json", by_alias=True, exclude_none=True)
    return DurableEvent(
        run_id=stream_run.run_id,
        sequence=sequence,
        payload=payload,
        payload_hash=canonical_hash(payload),
    )


def _validate_parent_event(parent: DurableRun, event: AgentRunEvent) -> None:
    if event.kind in {AgentEventKind.FORK_REQUESTED, AgentEventKind.FORK_JOINED}:
        if (
            event.parent_run_id != parent.run_id
            or event.task_scope_digest is None
            or event.child_id is None
            or event.depth != parent.depth + 1
        ):
            raise DurableStoreError("DURABLE_PARENT_FORK_EVENT_INVALID")
    elif event.scope is not AgentEventScope.ROOT:
        raise DurableStoreError("DURABLE_ROOT_EVENT_SCOPE_INVALID")


def _validate_child_event(child: DurableRun, event: AgentRunEvent) -> None:
    """Validate an event against its immediate durable parent.

    A child emits ordinary model/tool facts in ``CHILD`` scope.  When it forks
    again, it is itself the parent, so its ``FORK_*`` facts remain ``ROOT``
    scoped from that child loop's point of view.  Treating every event as a
    lifecycle event of the original root made recursive homogeneous loops
    impossible.
    """

    if event.kind in {AgentEventKind.FORK_REQUESTED, AgentEventKind.FORK_JOINED}:
        if (
            event.scope is not AgentEventScope.ROOT
            or event.parent_run_id != child.run_id
            or event.task_scope_digest is None
            or event.child_id is None
            or event.depth != child.depth + 1
        ):
            raise DurableStoreError("DURABLE_PARENT_FORK_EVENT_INVALID")
        return
    if (
        event.scope is not AgentEventScope.CHILD
        or event.child_id != child.child_id
        or event.depth != child.depth
    ):
        raise DurableStoreError("DURABLE_CHILD_EVENT_TREE_INVALID")
    if event.kind in {
        AgentEventKind.CHILD_RUN_STARTED,
        AgentEventKind.CHILD_CHECKPOINT_CONFIRMED,
        AgentEventKind.CHILD_HANDOFF_READY,
        AgentEventKind.CHILD_FAILED,
    } and (
        event.parent_run_id != child.parent_run_id
        or event.task_scope_digest != child.task_scope_digest
    ):
        raise DurableStoreError("DURABLE_CHILD_EVENT_TREE_INVALID")


def _is_child_terminal_event(event: AgentRunEvent) -> bool:
    return event.kind in {AgentEventKind.CHILD_HANDOFF_READY, AgentEventKind.CHILD_FAILED}


def _child_terminal(
    *,
    state_event: AgentRunEvent,
) -> tuple[DurableRunState, str | None, dict[str, object] | None]:
    if state_event.kind is AgentEventKind.CHILD_FAILED:
        if state_event.safe_code is None:
            raise DurableStoreError("DURABLE_CHILD_TERMINAL_INVALID")
        return (
            DurableRunState.FAILED,
            state_event.safe_code,
            {
                "schema_version": _CHILD_HANDOFF_SCHEMA,
                "child_run_id": state_event.run_id,
                "status": "FAILED",
            },
        )
    if state_event.kind is not AgentEventKind.CHILD_HANDOFF_READY or state_event.status is None:
        raise DurableStoreError("DURABLE_CHILD_TERMINAL_INVALID")
    if state_event.status == "COMPLETED":
        return (
            DurableRunState.COMPLETED,
            None,
            {
                "schema_version": _CHILD_HANDOFF_SCHEMA,
                "child_run_id": state_event.run_id,
                "status": "COMPLETED",
            },
        )
    if state_event.status == "FAILED":
        return (
            DurableRunState.FAILED,
            "CHILD_HANDOFF_FAILED",
            {
                "schema_version": _CHILD_HANDOFF_SCHEMA,
                "child_run_id": state_event.run_id,
                "status": "FAILED",
            },
        )
    if state_event.status == "ABORTED":
        return DurableRunState.ABORTED, "CHILD_ABORTED", None
    raise DurableStoreError("DURABLE_CHILD_TERMINAL_INVALID")


def _request_from_private_payload(payload: dict[str, object]) -> SearchRequest:
    try:
        return search_request_from_durable_payload(payload)
    except Exception as error:
        raise DurableStoreError("DURABLE_REQUEST_CHECKPOINT_INVALID") from error


_MAX_JOURNAL = 64
_MAX_HANDOFFS = 10
_MAX_PENDING_FORKS = 10

__all__ = ["DurableAgentCoordinator", "DurableAgentExecutor"]
