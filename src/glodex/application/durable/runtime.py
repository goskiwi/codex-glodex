"""Opt-in M2b coordinator around the existing M1d/M2a Agent runtime."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from glodex.api.agent_events import AgentEventProjector
from glodex.application.agent.contracts import (
    AgentEventKind,
    AgentExecution,
    AgentRunEvent,
)
from glodex.application.agent.ports import AgentEventObserver
from glodex.application.durable.context import digest_safe_events
from glodex.application.durable.contracts import (
    M2B_CONTEXT_CACHE_TTL_SECONDS,
    M2B_CONTEXT_VERSION,
    TERMINAL_RUN_STATES,
    DurableCheckpoint,
    DurableCheckpointState,
    DurableEvent,
    DurableRun,
    DurableRunState,
    DurableStoreError,
    cache_key,
    canonical_hash,
)
from glodex.application.durable.ports import DurableStorePort, RetrievalCachePort
from glodex.contracts import SearchRequest


class DurableAgentExecutor(Protocol):
    """The existing Agent service shape needed by the durable coordinator."""

    async def execute_run(
        self,
        request: SearchRequest,
        *,
        run_id: str,
        observer: AgentEventObserver | None = None,
    ) -> AgentExecution: ...


@dataclass(slots=True)
class _EventCollector(AgentEventObserver):
    events: list[AgentRunEvent] = field(default_factory=list)

    def on_event(self, event: AgentRunEvent) -> None:
        if type(event) is not AgentRunEvent:
            raise TypeError("durable event collector requires exact AgentRunEvent")
        self.events.append(event)


@dataclass(slots=True)
class DurableAgentCoordinator:
    """Persist/replay/cancel an explicit Agent run without changing normal M1d APIs."""

    store: DurableStorePort
    executor: DurableAgentExecutor
    projector: AgentEventProjector
    asset_version: str
    config_fingerprint: str
    context_cache: RetrievalCachePort | None = None
    run_id_factory: Callable[[], str] = lambda: f"run-{uuid.uuid4().hex}"
    _tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if (
            not callable(self.executor.execute_run)
            or type(self.projector) is not AgentEventProjector
            or type(self.asset_version) is not str
            or not self.asset_version
            or type(self.config_fingerprint) is not str
            or len(self.config_fingerprint) != 64
            or not callable(self.run_id_factory)
        ):
            raise TypeError("durable coordinator inputs are invalid")

    async def submit(
        self,
        *,
        request: SearchRequest,
        thread_id: str,
        profile_id: str | None,
        profile_revision: int,
    ) -> DurableRun:
        if type(request) is not SearchRequest:
            raise TypeError("durable submission requires an exact SearchRequest")
        run_id = self.run_id_factory()
        run = await self.store.reserve_run(
            run_id=run_id,
            thread_id=thread_id,
            request_payload=request.model_dump(mode="json"),
            asset_version=self.asset_version,
            config_fingerprint=self.config_fingerprint,
            profile_id=profile_id,
            profile_revision=profile_revision,
        )
        await self.store.append_event_checkpoint(
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
        self._start(run_id=run.run_id, request=request)
        return await self.store.load_run(run_id=run.run_id)

    async def resume(self, *, run_id: str) -> DurableRun:
        run = await self.store.load_run(run_id=run_id)
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
        if checkpoint.event_sequence != 0:
            await self._abort(run=run, code="DURABLE_CHECKPOINT_UNSUPPORTED")
            return await self.store.load_run(run_id=run_id)
        recovered = await self.store.set_recoverable(run_id=run_id)
        request = _request_from_private_payload(recovered.request_payload)
        self._start(run_id=run_id, request=request)
        return await self.store.load_run(run_id=run_id)

    async def cancel(self, *, run_id: str) -> DurableRun:
        run = await self.store.request_cancel(run_id=run_id)
        if run.state in {
            DurableRunState.COMPLETED,
            DurableRunState.NO_MATCH,
            DurableRunState.FAILED,
            DurableRunState.ABORTED,
        }:
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

    def _start(self, *, run_id: str, request: SearchRequest) -> None:
        if run_id in self._tasks:
            raise DurableStoreError("M2B_RUN_ALREADY_ACTIVE")
        task = asyncio.create_task(self._execute(run_id=run_id, request=request))
        self._tasks[run_id] = task

    async def _execute(self, *, run_id: str, request: SearchRequest) -> None:
        try:
            run = await self.store.load_run(run_id=run_id)
            if run.cancel_requested:
                await self._abort(run=run, code="RUN_CANCELLED")
                return
            latest = await self.store.latest_checkpoint(run_id=run_id)
            if latest is None:
                raise DurableStoreError("M2B_CHECKPOINT_MISSING")
            await self.store.append_event_checkpoint(
                event=None,
                checkpoint=_checkpoint_for(
                    run=run,
                    number=latest.checkpoint_number + 1,
                    state=DurableCheckpointState.REMOTE_PENDING,
                    event_sequence=run.event_sequence,
                    phase="EXTERNAL_AGENT_EXECUTION",
                ),
                next_state=DurableRunState.RUNNING,
            )
            await self._cache_context(run_id=run_id, events=())
            collector = _EventCollector()
            execution = await self.executor.execute_run(
                request,
                run_id=run_id,
                observer=collector,
            )
            await self._persist_execution(
                run_id=run_id,
                execution=execution,
                events=collector.events,
            )
        except asyncio.CancelledError:
            await self._abort_for_run_id(run_id=run_id, code="RUN_CANCELLED")
        except DurableStoreError:
            await self._abort_for_run_id(run_id=run_id, code="DURABLE_EXECUTION_FAILED")
        except Exception:
            await self._abort_for_run_id(run_id=run_id, code="DURABLE_EXECUTION_FAILED")
        finally:
            self._tasks.pop(run_id, None)

    async def _persist_execution(
        self,
        *,
        run_id: str,
        execution: AgentExecution,
        events: list[AgentRunEvent],
    ) -> None:
        if type(execution) is not AgentExecution or not events:
            raise DurableStoreError("M2B_EXECUTION_INVALID")
        run = await self.store.load_run(run_id=run_id)
        if run.cancel_requested:
            await self._abort(run=run, code="RUN_CANCELLED")
            return
        for position, event in enumerate(events):
            if event.run_id != run_id:
                raise DurableStoreError("M2B_EXECUTION_INVALID")
            run = await self.store.load_run(run_id=run_id)
            durable_event = _project_event(
                projector=self.projector,
                thread_id=run.thread_id,
                event=event,
                sequence=run.event_sequence + 1,
            )
            terminal = event.kind in {AgentEventKind.AGENT_RESULT, AgentEventKind.AGENT_ERROR}
            if terminal:
                if position != len(events) - 1:
                    raise DurableStoreError("M2B_EXECUTION_INVALID")
                await self._finish_execution(
                    run=run,
                    event=event,
                    durable_event=durable_event,
                    execution=execution,
                    events=events,
                )
                return
            latest = await self.store.latest_checkpoint(run_id=run_id)
            if latest is None:
                raise DurableStoreError("M2B_CHECKPOINT_MISSING")
            await self.store.append_event_checkpoint(
                event=durable_event,
                checkpoint=_checkpoint_for(
                    run=run,
                    number=latest.checkpoint_number + 1,
                    state=DurableCheckpointState.CONFIRMED,
                    event_sequence=durable_event.sequence,
                    phase=event.kind.value,
                ),
                next_state=DurableRunState.RUNNING,
            )
            await self._cache_context(run_id=run_id, events=events[: position + 1])
        raise DurableStoreError("M2B_TERMINAL_EVENT_MISSING")

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
            raise DurableStoreError("M2B_CHECKPOINT_MISSING")
        if event.kind is AgentEventKind.AGENT_RESULT:
            if event.status not in {"COMPLETED", "NO_MATCH"}:
                raise DurableStoreError("M2B_EXECUTION_INVALID")
            state = DurableRunState(event.status)
            error_code = None
        else:
            if event.status != "FAILED" or event.safe_code is None:
                raise DurableStoreError("M2B_EXECUTION_INVALID")
            state = DurableRunState.FAILED
            error_code = event.safe_code
        await self.store.finish_run(
            event=durable_event,
            checkpoint=_checkpoint_for(
                run=run,
                number=latest.checkpoint_number + 1,
                state=DurableCheckpointState.TERMINAL,
                event_sequence=durable_event.sequence,
                phase="TERMINAL",
            ),
            state=state,
            response=execution.response.model_dump(mode="json"),
            error_code=error_code,
        )
        await self._cache_context(run_id=run.run_id, events=events)

    async def _abort_for_run_id(self, *, run_id: str, code: str) -> None:
        try:
            run = await self.store.load_run(run_id=run_id)
        except DurableStoreError:
            return
        if run.state not in {
            DurableRunState.COMPLETED,
            DurableRunState.NO_MATCH,
            DurableRunState.FAILED,
            DurableRunState.ABORTED,
        }:
            await self._abort(run=run, code=code)

    async def _abort(self, *, run: DurableRun, code: str) -> None:
        latest = await self.store.latest_checkpoint(run_id=run.run_id)
        if latest is None:
            raise DurableStoreError("M2B_CHECKPOINT_MISSING")
        event = AgentRunEvent(
            kind=AgentEventKind.AGENT_ERROR,
            run_id=run.run_id,
            status="ABORTED",
            safe_code=code,
        )
        durable_event = _project_event(
            projector=self.projector,
            thread_id=run.thread_id,
            event=event,
            sequence=run.event_sequence + 1,
        )
        await self.store.finish_run(
            event=durable_event,
            checkpoint=_checkpoint_for(
                run=run,
                number=latest.checkpoint_number + 1,
                state=(
                    DurableCheckpointState.CANCELLED
                    if code == "RUN_CANCELLED"
                    else DurableCheckpointState.TERMINAL
                ),
                event_sequence=durable_event.sequence,
                phase="ABORTED",
            ),
            state=DurableRunState.ABORTED,
            response=None,
            error_code=code,
        )
        await self._cache_context(run_id=run.run_id, events=(event,))

    async def _cache_context(
        self,
        *,
        run_id: str,
        events: tuple[AgentRunEvent, ...] | list[AgentRunEvent],
    ) -> None:
        if self.context_cache is None:
            return
        try:
            digest = digest_safe_events(tuple(events))
            await self.context_cache.set(
                key=cache_key(
                    namespace="context",
                    material={"run_id": run_id, "event_count": digest.event_count},
                ),
                value=digest.cache_value(),
                ttl_seconds=M2B_CONTEXT_CACHE_TTL_SECONDS,
            )
        except Exception:
            return


def _checkpoint_for(
    *,
    run: DurableRun,
    number: int,
    state: DurableCheckpointState,
    event_sequence: int,
    phase: str,
) -> DurableCheckpoint:
    payload = {
        "checkpoint_context": M2B_CONTEXT_VERSION,
        "event_sequence": event_sequence,
        "phase": phase,
        "run_id": run.run_id,
    }
    value = {
        "asset_version": run.asset_version,
        "checkpoint_number": number,
        "config_fingerprint": run.config_fingerprint,
        "event_sequence": event_sequence,
        "profile_revision": run.profile_revision,
        "runtime_payload": payload,
        "run_id": run.run_id,
        "schema_version": "glodex.m2b.v1",
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
        profile_revision=run.profile_revision,
        payload_hash=canonical_hash(value),
    )


def _project_event(
    *,
    projector: AgentEventProjector,
    thread_id: str,
    event: AgentRunEvent,
    sequence: int,
) -> DurableEvent:
    projected = projector.project_event(thread_id=thread_id, event=event, sequence=sequence)
    payload = projected.model_dump(mode="json", by_alias=True, exclude_none=True)
    return DurableEvent(
        run_id=event.run_id,
        sequence=sequence,
        payload=payload,
        payload_hash=canonical_hash(payload),
    )


def _request_from_private_payload(payload: dict[str, object]) -> SearchRequest:
    try:
        return SearchRequest.model_validate(payload)
    except Exception as error:
        raise DurableStoreError("M2B_REQUEST_CHECKPOINT_INVALID") from error


__all__ = ["DurableAgentCoordinator", "DurableAgentExecutor"]
