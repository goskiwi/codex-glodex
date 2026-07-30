"""Bounded process-local registry and coordinator for the M1d Agent API."""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from functools import partial
from typing import Protocol

from pydantic import TypeAdapter, ValidationError

from glodex.api.agent_contracts import AgentRunStatusResponse
from glodex.api.agent_events import (
    AgentErrorEvent,
    AgentEventProjector,
    AgentPublicEvent,
    AgentResultEvent,
    AgentStartedEvent,
    ForkFinishedEvent,
    ForkStartedEvent,
    ModelFinishedEvent,
    ModelStartedEvent,
    ToolFinishedEvent,
    ToolStartedEvent,
)
from glodex.api.contracts import ApiError, ProjectionStatus, RunState
from glodex.api.settings import ApiSettings
from glodex.application.agent.contracts import (
    AgentDemoResponse,
    AgentEventKind,
    AgentEventScope,
    AgentExecution,
    AgentRunEvent,
    ToolName,
)
from glodex.application.agent.ports import AgentEventObserver
from glodex.application.ports import Clock, RunIdProvider
from glodex.contracts import Identifier, RunStatus, SearchRequest

_IDENTIFIER_ADAPTER = TypeAdapter(Identifier)
_TERMINAL_STATES = frozenset(
    {
        RunState.COMPLETED,
        RunState.NO_MATCH,
        RunState.FAILED,
        RunState.ABORTED,
    }
)
_TERMINAL_EVENT_KINDS = frozenset(
    {
        AgentEventKind.AGENT_RESULT,
        AgentEventKind.AGENT_ERROR,
    }
)
_RUN_ABORTED_CODE = "RUN_ABORTED"
_RUN_ABORTED_MESSAGE = "Run execution was aborted."


class AgentRunNotFound(LookupError):
    """The requested Agent Run is unknown, expired, or evicted."""


class AgentRunAlreadyActive(RuntimeError):
    """A Thread already owns an active Agent Run."""


class AgentRunCapacityExceeded(RuntimeError):
    """The process-local active Agent Run capacity is full."""


class DuplicateAgentRunId(RuntimeError):
    """A generated Agent Run ID would overwrite an existing resource."""


class AgentSubscriberCapacityExceeded(RuntimeError):
    """An Agent Run already has the configured maximum subscribers."""


class AgentCoordinatorShuttingDown(RuntimeError):
    """The Agent coordinator no longer accepts work."""


@dataclass(frozen=True, slots=True)
class AgentEventBatch:
    """One subscriber read from the shared retained event prefix."""

    events: tuple[AgentPublicEvent, ...]
    terminal: bool
    projection_degraded: bool


@dataclass(eq=False, slots=True)
class AgentRunSubscription:
    """Per-subscriber cursor plus one bounded wake-up token."""

    run_id: str
    next_sequence: int
    notification: asyncio.Queue[None]
    closed: bool = False
    invalidated: bool = False


class _StepPhase(StrEnum):
    READY = "READY"
    MODEL_STARTED = "MODEL_STARTED"
    MODEL_FINISHED = "MODEL_FINISHED"
    TOOL_STARTED = "TOOL_STARTED"


@dataclass(slots=True)
class _StepFlow:
    phase: _StepPhase = _StepPhase.READY
    last_round: int = 0
    current_round: int | None = None
    tool_name: ToolName | None = None

    def clone(self) -> _StepFlow:
        return _StepFlow(
            phase=self.phase,
            last_round=self.last_round,
            current_round=self.current_round,
            tool_name=self.tool_name,
        )


@dataclass(slots=True)
class _ChildFlow:
    depth: int
    step: _StepFlow = field(default_factory=_StepFlow)

    def clone(self) -> _ChildFlow:
        return _ChildFlow(depth=self.depth, step=self.step.clone())


@dataclass(slots=True)
class _ProjectionFlow:
    started: bool = False
    terminal: bool = False
    root: _StepFlow = field(default_factory=_StepFlow)
    children: dict[str, _ChildFlow] = field(default_factory=dict)
    closed_child_ids: set[str] = field(default_factory=set)

    def clone(self) -> _ProjectionFlow:
        return _ProjectionFlow(
            started=self.started,
            terminal=self.terminal,
            root=self.root.clone(),
            children={child_id: child.clone() for child_id, child in self.children.items()},
            closed_child_ids=set(self.closed_child_ids),
        )


@dataclass(slots=True)
class _AgentRunResource:
    thread_id: str
    run_id: str
    state: RunState
    projection_status: ProjectionStatus
    created_at: datetime
    created_monotonic_ns: int
    response: AgentDemoResponse | None = None
    error: ApiError | None = None
    events: list[AgentPublicEvent] = field(default_factory=list)
    subscribers: set[AgentRunSubscription] = field(default_factory=set)
    flow: _ProjectionFlow = field(default_factory=_ProjectionFlow)
    terminal_monotonic_ns: int | None = None
    terminal_order: int | None = None


class AgentRunRegistry:
    """Own all process-local Agent Run mutations without internal awaits."""

    def __init__(self, *, settings: ApiSettings, clock: Clock) -> None:
        self._settings = settings
        self._clock = clock
        self._runs: dict[str, _AgentRunResource] = {}
        self._active_by_thread: dict[str, str] = {}
        self._terminal_order = 0

    def reserve(self, *, thread_id: str, run_id: str) -> AgentRunStatusResponse:
        self._cleanup_expired()
        checked_thread_id = _validated_identifier(thread_id, name="thread ID")
        checked_run_id = _validated_identifier(run_id, name="run ID")
        if checked_run_id in self._runs:
            raise DuplicateAgentRunId("generated Agent Run ID already exists")
        if checked_thread_id in self._active_by_thread:
            raise AgentRunAlreadyActive("Thread already has an active Agent Run")
        if len(self._active_by_thread) >= self._settings.max_active_runs:
            raise AgentRunCapacityExceeded("active Agent Run capacity is full")

        resource = _AgentRunResource(
            thread_id=checked_thread_id,
            run_id=checked_run_id,
            state=RunState.ACCEPTED,
            projection_status=ProjectionStatus.OK,
            created_at=self._clock.now_utc(),
            created_monotonic_ns=self._clock.monotonic_ns(),
        )
        self._runs[checked_run_id] = resource
        self._active_by_thread[checked_thread_id] = checked_run_id
        return self._status(resource)

    def status(self, run_id: str) -> AgentRunStatusResponse:
        return self._status(self._get(run_id))

    def next_sequence(self, run_id: str) -> int:
        return len(self._get(run_id).events) + 1

    def try_mark_running(self, run_id: str) -> bool:
        resource = self._get(run_id)
        if resource.state is not RunState.ACCEPTED:
            return False
        resource.state = RunState.RUNNING
        return True

    def append_events(
        self,
        run_id: str,
        events: tuple[AgentPublicEvent, ...],
    ) -> bool:
        resource = self._get(run_id)
        if resource.state in _TERMINAL_STATES:
            return False
        if any(isinstance(event, (AgentResultEvent, AgentErrorEvent)) for event in events):
            self._degrade_projection(resource)
            return False
        return self._append_projection(resource, events)

    def mark_projection_degraded(self, run_id: str) -> None:
        self._degrade_projection(self._get(run_id))

    def try_commit_execution(
        self,
        run_id: str,
        execution: AgentExecution,
        terminal_event: AgentResultEvent | AgentErrorEvent | None,
    ) -> bool:
        resource = self._get(run_id)
        if resource.state in _TERMINAL_STATES or resource.state is not RunState.RUNNING:
            return False
        if type(execution) is not AgentExecution:
            raise TypeError("execution must be an exact AgentExecution")
        if execution.response.run_id != resource.run_id:
            raise ValueError("AgentExecution run ID does not match reserved Agent Run")

        if not _terminal_matches_execution(terminal_event, execution):
            self._degrade_projection(resource)
        elif terminal_event is not None:
            self._append_projection(resource, (terminal_event,))

        resource.state = RunState(execution.response.status.value)
        resource.response = execution.response
        resource.error = None
        self._finalize_terminal(resource)
        return True

    def try_commit_aborted(
        self,
        run_id: str,
        terminal_event: AgentErrorEvent | None,
    ) -> bool:
        resource = self._get(run_id)
        if resource.state in _TERMINAL_STATES:
            return False
        if resource.state not in {RunState.ACCEPTED, RunState.RUNNING}:
            return False

        if (
            terminal_event is None
            or terminal_event.status != "ABORTED"
            or terminal_event.safe_code != _RUN_ABORTED_CODE
        ):
            self._degrade_projection(resource)
        else:
            self._append_projection(resource, (terminal_event,))

        resource.state = RunState.ABORTED
        resource.response = None
        resource.error = ApiError(
            code=_RUN_ABORTED_CODE,
            message=_RUN_ABORTED_MESSAGE,
        )
        self._finalize_terminal(resource)
        return True

    def subscribe(
        self,
        run_id: str,
        *,
        after_sequence: int,
    ) -> AgentRunSubscription:
        resource = self._get(run_id)
        if (
            isinstance(after_sequence, bool)
            or not isinstance(after_sequence, int)
            or after_sequence < 0
            or after_sequence > len(resource.events)
        ):
            raise ValueError("subscriber sequence is outside retained event prefix")
        if len(resource.subscribers) >= self._settings.max_subscribers_per_run:
            raise AgentSubscriberCapacityExceeded("subscriber capacity is full")

        subscription = AgentRunSubscription(
            run_id=resource.run_id,
            next_sequence=after_sequence,
            notification=asyncio.Queue(maxsize=1),
        )
        resource.subscribers.add(subscription)
        return subscription

    def read(self, subscription: AgentRunSubscription) -> AgentEventBatch:
        if subscription.closed:
            raise ValueError("subscription is closed")
        resource = self._get(subscription.run_id)
        if subscription.invalidated or subscription not in resource.subscribers:
            raise AgentRunNotFound("Agent Run is not retained")

        _drain_notification(subscription)
        events = tuple(resource.events[subscription.next_sequence :])
        if events:
            subscription.next_sequence = events[-1].sequence
        return AgentEventBatch(
            events=events,
            terminal=resource.state in _TERMINAL_STATES,
            projection_degraded=(resource.projection_status is ProjectionStatus.DEGRADED),
        )

    def unsubscribe(self, subscription: AgentRunSubscription) -> None:
        if subscription.closed:
            return
        resource = self._runs.get(subscription.run_id)
        if resource is not None:
            resource.subscribers.discard(subscription)
        subscription.closed = True
        _drain_notification(subscription)

    def _get(self, run_id: str) -> _AgentRunResource:
        self._cleanup_expired()
        resource = self._runs.get(run_id)
        if resource is None:
            raise AgentRunNotFound("Agent Run is unknown, expired, or evicted")
        return resource

    @staticmethod
    def _status(resource: _AgentRunResource) -> AgentRunStatusResponse:
        last_event_id = (
            None if not resource.events else f"{resource.run_id}:{resource.events[-1].sequence}"
        )
        return AgentRunStatusResponse(
            thread_id=resource.thread_id,
            run_id=resource.run_id,
            state=resource.state,
            projection_status=resource.projection_status,
            last_event_id=last_event_id,
            response=resource.response,
            error=resource.error,
        )

    def _append_projection(
        self,
        resource: _AgentRunResource,
        events: tuple[AgentPublicEvent, ...],
    ) -> bool:
        if resource.projection_status is ProjectionStatus.DEGRADED:
            return False
        candidate_flow = resource.flow.clone()
        if (
            not events
            or len(resource.events) + len(events) > self._settings.max_events_per_run
            or not self._valid_event_batch(resource, events, candidate_flow)
        ):
            self._degrade_projection(resource)
            return False

        resource.events.extend(events)
        resource.flow = candidate_flow
        self._notify(resource)
        return True

    @staticmethod
    def _valid_event_batch(
        resource: _AgentRunResource,
        events: tuple[AgentPublicEvent, ...],
        flow: _ProjectionFlow,
    ) -> bool:
        expected_sequence = len(resource.events) + 1
        for event in events:
            if (
                event.thread_id != resource.thread_id
                or event.run_id != resource.run_id
                or event.sequence != expected_sequence
                or not _advance_flow(flow, event)
            ):
                return False
            expected_sequence += 1
        return True

    def _degrade_projection(self, resource: _AgentRunResource) -> None:
        if resource.projection_status is ProjectionStatus.DEGRADED:
            return
        resource.projection_status = ProjectionStatus.DEGRADED
        self._notify(resource)

    def _finalize_terminal(self, resource: _AgentRunResource) -> None:
        if self._active_by_thread.get(resource.thread_id) == resource.run_id:
            del self._active_by_thread[resource.thread_id]
        self._terminal_order += 1
        resource.terminal_order = self._terminal_order
        resource.terminal_monotonic_ns = self._clock.monotonic_ns()
        self._notify(resource)
        self._enforce_terminal_capacity()

    def _cleanup_expired(self) -> None:
        now = self._clock.monotonic_ns()
        ttl_ns = self._settings.terminal_ttl_seconds * 1_000_000_000
        expired = tuple(
            resource.run_id
            for resource in self._runs.values()
            if resource.state in _TERMINAL_STATES
            and resource.terminal_monotonic_ns is not None
            and now - resource.terminal_monotonic_ns >= ttl_ns
        )
        for run_id in expired:
            self._evict(run_id)

    def _enforce_terminal_capacity(self) -> None:
        terminal = sorted(
            (
                resource
                for resource in self._runs.values()
                if resource.state in _TERMINAL_STATES and resource.terminal_order is not None
            ),
            key=lambda resource: (resource.terminal_order, resource.run_id),
        )
        excess = len(terminal) - self._settings.max_terminal_runs
        for resource in terminal[: max(excess, 0)]:
            self._evict(resource.run_id)

    def _evict(self, run_id: str) -> None:
        resource = self._runs.pop(run_id, None)
        if resource is None:
            return
        for subscription in tuple(resource.subscribers):
            subscription.invalidated = True
            _notify_subscription(subscription)
        resource.subscribers.clear()

    @staticmethod
    def _notify(resource: _AgentRunResource) -> None:
        for subscription in tuple(resource.subscribers):
            _notify_subscription(subscription)


class AgentExecutor(Protocol):
    """Structural application boundary accepted by the Agent API."""

    async def execute_run(
        self,
        request: SearchRequest,
        *,
        run_id: str,
        observer: AgentEventObserver | None = None,
    ) -> AgentExecution: ...


class _TimeoutFactory(Protocol):
    def __call__(
        self,
        delay: float | None,
        /,
    ) -> AbstractAsyncContextManager[object]: ...


@dataclass(slots=True)
class _ProjectingAgentObserver:
    registry: AgentRunRegistry
    projector: AgentEventProjector
    thread_id: str
    run_id: str
    max_events: int
    observed_events: list[AgentRunEvent] = field(default_factory=list)
    terminal_seen: bool = False
    observation_overflow: bool = False

    def on_event(self, event: AgentRunEvent) -> None:
        try:
            if type(event) is not AgentRunEvent or event.run_id != self.run_id:
                raise ValueError("Agent observer received an invalid event")
            if len(self.observed_events) >= self.max_events:
                self.observation_overflow = True
                raise ValueError("Agent observer event capacity exceeded")
            self.observed_events.append(event)
            if event.kind in _TERMINAL_EVENT_KINDS:
                if self.terminal_seen:
                    raise ValueError("Agent observer received more than one terminal event")
                self.terminal_seen = True
                return
            if self.terminal_seen:
                raise ValueError("Agent observer received an event after terminal")
            projected = self.projector.project_event(
                thread_id=self.thread_id,
                event=event,
                sequence=self.registry.next_sequence(self.run_id),
            )
            self.registry.append_events(self.run_id, (projected,))
        except Exception:
            try:
                self.registry.mark_projection_degraded(self.run_id)
            except AgentRunNotFound:
                return


class AgentRunCoordinator:
    """Track Agent background work while isolating event projection failures."""

    def __init__(
        self,
        *,
        registry: AgentRunRegistry,
        projector: AgentEventProjector,
        service: AgentExecutor,
        run_id_provider: RunIdProvider,
        settings: ApiSettings,
        timeout_factory: _TimeoutFactory | None = None,
    ) -> None:
        self._registry = registry
        self._projector = projector
        self._service = service
        self._run_id_provider = run_id_provider
        self._settings = settings
        self._timeout_factory = timeout_factory or _default_timeout
        self._accepting = True
        self._tasks: set[asyncio.Task[None]] = set()
        self._run_tasks: dict[str, asyncio.Task[None]] = {}
        self._start_gates: dict[str, asyncio.Event] = {}

    @property
    def in_flight_count(self) -> int:
        return len(self._tasks)

    def submit(
        self,
        request: SearchRequest,
        *,
        thread_id: str,
    ) -> AgentRunStatusResponse:
        if not self._accepting:
            raise AgentCoordinatorShuttingDown("Agent coordinator is shutting down")
        if not isinstance(request, SearchRequest):
            raise TypeError("Agent coordinator requires a validated SearchRequest")
        loop = asyncio.get_running_loop()
        run_id = _validated_identifier(
            self._run_id_provider.next_run_id(),
            name="run ID",
        )
        if ":" in run_id or "/" in run_id:
            raise ValueError("API-generated run ID must be one URL segment")

        accepted = self._registry.reserve(thread_id=thread_id, run_id=run_id)
        gate = asyncio.Event()
        task = loop.create_task(
            self._run(
                request=request,
                thread_id=accepted.thread_id,
                run_id=run_id,
                gate=gate,
            ),
            name=f"glodex-agent-run-{run_id}",
        )
        self._start_gates[run_id] = gate
        self._tasks.add(task)
        self._run_tasks[run_id] = task
        task.add_done_callback(partial(self._task_done, run_id))
        return accepted

    def release_start(self, run_id: str) -> bool:
        gate = self._start_gates.get(run_id)
        if gate is None or gate.is_set():
            return False
        try:
            state = self._registry.status(run_id).state
        except AgentRunNotFound:
            return False
        if state in _TERMINAL_STATES:
            return False
        gate.set()
        return True

    async def wait_idle(self) -> None:
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def shutdown(self) -> None:
        """Cancel work, await application cleanup, then retain ABORTED resources."""

        self._accepting = False
        run_ids = tuple(self._run_tasks)
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for run_id in run_ids:
            self._commit_aborted(run_id)

    async def _run(
        self,
        *,
        request: SearchRequest,
        thread_id: str,
        run_id: str,
        gate: asyncio.Event,
    ) -> None:
        try:
            async with self._timeout_factory(self._settings.run_timeout_seconds):
                await gate.wait()
                if not self._registry.try_mark_running(run_id):
                    return
                observer = _ProjectingAgentObserver(
                    registry=self._registry,
                    projector=self._projector,
                    thread_id=thread_id,
                    run_id=run_id,
                    max_events=self._settings.max_events_per_run,
                )
                execution = await self._service.execute_run(
                    request,
                    run_id=run_id,
                    observer=observer,
                )
                if not self._accepting:
                    self._commit_aborted(run_id)
                    return
                internal_terminal = _select_terminal_event(
                    execution=execution,
                    observed=tuple(observer.observed_events),
                    observation_overflow=observer.observation_overflow,
                )
                terminal_event: AgentResultEvent | AgentErrorEvent | None
                try:
                    terminal_projected = (
                        None
                        if internal_terminal is None
                        else self._projector.project_event(
                            thread_id=thread_id,
                            event=internal_terminal,
                            sequence=self._registry.next_sequence(run_id),
                        )
                    )
                    terminal_event = (
                        terminal_projected
                        if isinstance(
                            terminal_projected,
                            (AgentResultEvent, AgentErrorEvent),
                        )
                        else None
                    )
                except Exception:
                    terminal_event = None
                self._registry.try_commit_execution(
                    run_id,
                    execution,
                    terminal_event,
                )
        except asyncio.CancelledError:
            self._commit_aborted(run_id)
            raise
        except Exception:
            self._commit_aborted(run_id)

    def _commit_aborted(self, run_id: str) -> None:
        try:
            status = self._registry.status(run_id)
            if status.state in _TERMINAL_STATES:
                return
            try:
                terminal_event = self._projector.project_aborted(
                    thread_id=status.thread_id,
                    run_id=run_id,
                    sequence=self._registry.next_sequence(run_id),
                )
            except Exception:
                terminal_event = None
            self._registry.try_commit_aborted(run_id, terminal_event)
        except AgentRunNotFound:
            return

    def _task_done(
        self,
        run_id: str,
        task: asyncio.Task[None],
    ) -> None:
        self._tasks.discard(task)
        if self._run_tasks.get(run_id) is task:
            self._run_tasks.pop(run_id, None)
            self._start_gates.pop(run_id, None)
        try:
            task.exception()
        except asyncio.CancelledError:
            return


def _select_terminal_event(
    *,
    execution: AgentExecution,
    observed: tuple[AgentRunEvent, ...],
    observation_overflow: bool,
) -> AgentRunEvent | None:
    terminals = tuple(
        event for event in execution.record.events if event.kind in _TERMINAL_EVENT_KINDS
    )
    if (
        observation_overflow
        or observed != execution.record.events
        or len(terminals) != 1
        or not execution.record.events
        or execution.record.events[-1] != terminals[0]
    ):
        return None
    terminal = terminals[0]
    if execution.response.status is RunStatus.FAILED:
        if (
            terminal.kind is not AgentEventKind.AGENT_ERROR
            or terminal.status != "FAILED"
            or terminal.safe_code != execution.record.terminal_code
            or terminal.safe_code == _RUN_ABORTED_CODE
        ):
            return None
    elif (
        terminal.kind is not AgentEventKind.AGENT_RESULT
        or terminal.status != execution.response.status.value
    ):
        return None
    return terminal


def _terminal_matches_execution(
    event: AgentResultEvent | AgentErrorEvent | None,
    execution: AgentExecution,
) -> bool:
    status = execution.response.status
    if status is RunStatus.FAILED:
        return (
            isinstance(event, AgentErrorEvent)
            and event.status == "FAILED"
            and event.safe_code == execution.record.terminal_code
            and event.safe_code != _RUN_ABORTED_CODE
        )
    return isinstance(event, AgentResultEvent) and event.status == status.value


def _advance_flow(flow: _ProjectionFlow, event: AgentPublicEvent) -> bool:
    if flow.terminal:
        return False
    if isinstance(event, AgentStartedEvent):
        if flow.started:
            return False
        flow.started = True
        return True
    if isinstance(event, AgentErrorEvent) and event.status == "ABORTED" and not flow.started:
        flow.terminal = True
        return True
    if not flow.started:
        return False

    if isinstance(event, ForkStartedEvent):
        if (
            event.child_id in flow.children
            or event.child_id in flow.closed_child_ids
            or (
                event.depth == 1
                and (
                    flow.root.phase is not _StepPhase.TOOL_STARTED
                    or flow.root.tool_name is not ToolName.DISPATCH_TOOL
                )
            )
            or (
                event.depth == 2
                and not any(
                    child.depth == 1
                    and child.step.phase is _StepPhase.TOOL_STARTED
                    and child.step.tool_name is ToolName.DISPATCH_TOOL
                    for child in flow.children.values()
                )
            )
        ):
            return False
        flow.children[event.child_id] = _ChildFlow(depth=event.depth)
        return True
    if isinstance(event, ForkFinishedEvent):
        child = flow.children.get(event.child_id)
        if child is None or child.depth != event.depth:
            return False
        if event.status == "COMPLETED" and (
            child.step.phase is not _StepPhase.READY or child.step.last_round < 1
        ):
            return False
        del flow.children[event.child_id]
        flow.closed_child_ids.add(event.child_id)
        return True
    if isinstance(event, (AgentResultEvent, AgentErrorEvent)):
        if flow.children:
            return False
        if isinstance(event, AgentResultEvent) and (
            flow.root.phase is not _StepPhase.READY or flow.root.last_round < 1
        ):
            return False
        flow.terminal = True
        return True

    step = _step_for_event(flow, event)
    if step is None:
        return False
    if isinstance(event, ModelStartedEvent):
        if step.phase is not _StepPhase.READY or event.round != step.last_round + 1:
            return False
        step.phase = _StepPhase.MODEL_STARTED
        step.current_round = event.round
        return True
    if isinstance(event, ModelFinishedEvent):
        if step.phase is not _StepPhase.MODEL_STARTED or step.current_round != event.round:
            return False
        step.phase = _StepPhase.MODEL_FINISHED
        step.tool_name = event.tool_name
        return True
    if isinstance(event, ToolStartedEvent):
        if step.phase is not _StepPhase.MODEL_FINISHED or step.tool_name is not event.tool_name:
            return False
        step.phase = _StepPhase.TOOL_STARTED
        return True
    if isinstance(event, ToolFinishedEvent):
        if step.phase is not _StepPhase.TOOL_STARTED or step.tool_name is not event.tool_name:
            return False
        if (
            event.scope is AgentEventScope.ROOT
            and event.tool_name is ToolName.DISPATCH_TOOL
            and flow.children
        ):
            return False
        assert step.current_round is not None
        step.last_round = step.current_round
        step.current_round = None
        step.tool_name = None
        step.phase = _StepPhase.READY
        return True


def _step_for_event(
    flow: _ProjectionFlow,
    event: (ModelStartedEvent | ModelFinishedEvent | ToolStartedEvent | ToolFinishedEvent),
) -> _StepFlow | None:
    if event.scope is AgentEventScope.ROOT:
        return flow.root
    if event.child_id is None or event.depth is None:
        return None
    child = flow.children.get(event.child_id)
    if child is None or child.depth != event.depth:
        return None
    return child.step


def _validated_identifier(value: object, *, name: str) -> str:
    try:
        return _IDENTIFIER_ADAPTER.validate_python(value, strict=True)
    except ValidationError:
        raise ValueError(f"{name} is invalid") from None


def _notify_subscription(subscription: AgentRunSubscription) -> None:
    try:
        subscription.notification.put_nowait(None)
    except asyncio.QueueFull:
        return


def _drain_notification(subscription: AgentRunSubscription) -> None:
    while True:
        try:
            subscription.notification.get_nowait()
        except asyncio.QueueEmpty:
            return


def _default_timeout(
    delay: float | None,
) -> AbstractAsyncContextManager[object]:
    return asyncio.timeout(delay)


__all__ = [
    "AgentCoordinatorShuttingDown",
    "AgentEventBatch",
    "AgentExecutor",
    "AgentRunAlreadyActive",
    "AgentRunCapacityExceeded",
    "AgentRunCoordinator",
    "AgentRunNotFound",
    "AgentRunRegistry",
    "AgentRunSubscription",
    "AgentSubscriberCapacityExceeded",
    "DuplicateAgentRunId",
]
