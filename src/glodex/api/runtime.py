"""Single-process Run registry and coordinator for the M1a API adapter."""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import Protocol

from pydantic import TypeAdapter, ValidationError

from glodex.api.contracts import (
    ApiError,
    ProjectionStatus,
    RunState,
    RunStatusResponse,
)
from glodex.api.events import PublicEvent, RunErrorEvent
from glodex.api.settings import ApiSettings
from glodex.application.journal import RunEvent
from glodex.application.ports import Clock, RunEventObserver, RunIdProvider
from glodex.application.search_service import SearchExecution
from glodex.contracts import Identifier, SearchRequest, SearchResponse

_IDENTIFIER_ADAPTER = TypeAdapter(Identifier)
_TERMINAL_STATES = frozenset(
    {
        RunState.COMPLETED,
        RunState.NO_MATCH,
        RunState.FAILED,
        RunState.ABORTED,
    }
)
_RUN_ABORTED_CODE = "RUN_ABORTED"
_RUN_ABORTED_MESSAGE = "Run execution was aborted."


class RunNotFound(LookupError):
    """The requested Run is unknown, expired, or evicted."""


class RunAlreadyActive(RuntimeError):
    """A Thread already owns an active Run."""


class RunCapacityExceeded(RuntimeError):
    """The process-local active Run capacity is full."""


class DuplicateRunId(RuntimeError):
    """A generated Run ID would overwrite an existing resource."""


class SubscriberCapacityExceeded(RuntimeError):
    """A Run already has the configured maximum number of subscribers."""


class CoordinatorShuttingDown(RuntimeError):
    """The coordinator has stopped accepting new work."""


@dataclass(frozen=True, slots=True)
class EventBatch:
    """One subscriber read from the shared replay prefix."""

    events: tuple[PublicEvent, ...]
    terminal: bool
    projection_degraded: bool


@dataclass(eq=False, slots=True)
class RunSubscription:
    """Per-subscriber cursor plus one bounded wake-up token."""

    run_id: str
    next_sequence: int
    notification: asyncio.Queue[None]
    closed: bool = False
    invalidated: bool = False


@dataclass(slots=True)
class _RunResource:
    thread_id: str
    run_id: str
    state: RunState
    projection_status: ProjectionStatus
    created_at: datetime
    created_monotonic_ns: int
    response: SearchResponse | None = None
    error: ApiError | None = None
    events: list[PublicEvent] = field(default_factory=list)
    subscribers: set[RunSubscription] = field(default_factory=set)
    terminal_monotonic_ns: int | None = None
    terminal_order: int | None = None


class RunRegistry:
    """Own all process-local Run mutations without locks or internal awaits."""

    def __init__(self, *, settings: ApiSettings, clock: Clock) -> None:
        self._settings = settings
        self._clock = clock
        self._runs: dict[str, _RunResource] = {}
        self._active_by_thread: dict[str, str] = {}
        self._terminal_order = 0

    def reserve(self, *, thread_id: str, run_id: str) -> RunStatusResponse:
        """Atomically reserve one active slot and one Thread."""

        self._cleanup_expired()
        checked_thread_id = _validated_identifier(thread_id, name="thread ID")
        checked_run_id = _validated_identifier(run_id, name="run ID")
        if checked_run_id in self._runs:
            raise DuplicateRunId("generated Run ID already exists")
        if checked_thread_id in self._active_by_thread:
            raise RunAlreadyActive("Thread already has an active Run")
        if len(self._active_by_thread) >= self._settings.max_active_runs:
            raise RunCapacityExceeded("active Run capacity is full")

        resource = _RunResource(
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

    def status(self, run_id: str) -> RunStatusResponse:
        """Return an immutable public snapshot after lazy expiry cleanup."""

        return self._status(self._get(run_id))

    def next_sequence(self, run_id: str) -> int:
        """Return the next public event sequence for one retained Run."""

        return len(self._get(run_id).events) + 1

    def try_mark_running(self, run_id: str) -> bool:
        """Move ACCEPTED to RUNNING once; terminal or duplicate calls lose."""

        resource = self._get(run_id)
        if resource.state is not RunState.ACCEPTED:
            return False
        resource.state = RunState.RUNNING
        return True

    def append_events(
        self,
        run_id: str,
        events: tuple[PublicEvent, ...],
    ) -> bool:
        """Append a completely validated batch or degrade without a partial write."""

        resource = self._get(run_id)
        if resource.state in _TERMINAL_STATES:
            return False
        return self._append_projection(resource, events)

    def mark_projection_degraded(self, run_id: str) -> None:
        """Irreversibly degrade projection health and wake subscribers."""

        resource = self._get(run_id)
        self._degrade_projection(resource)

    def try_commit_execution(
        self,
        run_id: str,
        execution: SearchExecution,
        terminal_events: tuple[PublicEvent, PublicEvent] | None,
    ) -> bool:
        """Commit one canonical business result and an all-or-none event pair."""

        resource = self._get(run_id)
        if resource.state in _TERMINAL_STATES:
            return False
        if resource.state is not RunState.RUNNING:
            return False
        if execution.response.run_id != resource.run_id:
            raise ValueError("SearchExecution run ID does not match reserved Run")

        if terminal_events is None:
            self._degrade_projection(resource)
        else:
            self._append_projection(resource, terminal_events)

        resource.state = RunState(execution.response.status.value)
        resource.response = execution.response
        resource.error = None
        self._finalize_terminal(resource)
        return True

    def try_commit_aborted(
        self,
        run_id: str,
        terminal_event: RunErrorEvent | None,
    ) -> bool:
        """Commit the transport-only terminal without a business response."""

        resource = self._get(run_id)
        if resource.state in _TERMINAL_STATES:
            return False
        if resource.state not in {RunState.ACCEPTED, RunState.RUNNING}:
            return False

        if (
            terminal_event is None
            or terminal_event.code != _RUN_ABORTED_CODE
            or terminal_event.message != _RUN_ABORTED_MESSAGE
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

    def subscribe(self, run_id: str, *, after_sequence: int) -> RunSubscription:
        """Register one bounded subscriber at an already validated sequence."""

        resource = self._get(run_id)
        if (
            isinstance(after_sequence, bool)
            or not isinstance(after_sequence, int)
            or after_sequence < 0
            or after_sequence > len(resource.events)
        ):
            raise ValueError("subscriber sequence is outside the retained event prefix")
        if len(resource.subscribers) >= self._settings.max_subscribers_per_run:
            raise SubscriberCapacityExceeded("subscriber capacity is full")

        subscription = RunSubscription(
            run_id=resource.run_id,
            next_sequence=after_sequence,
            notification=asyncio.Queue(maxsize=1),
        )
        resource.subscribers.add(subscription)
        return subscription

    def read(self, subscription: RunSubscription) -> EventBatch:
        """Read and advance one subscriber without copying per-subscriber queues."""

        if subscription.closed:
            raise ValueError("subscription is closed")
        resource = self._get(subscription.run_id)
        if subscription.invalidated or subscription not in resource.subscribers:
            raise RunNotFound("Run is not retained")

        _drain_notification(subscription)
        events = tuple(resource.events[subscription.next_sequence :])
        if events:
            subscription.next_sequence = events[-1].sequence
        return EventBatch(
            events=events,
            terminal=resource.state in _TERMINAL_STATES,
            projection_degraded=resource.projection_status is ProjectionStatus.DEGRADED,
        )

    def unsubscribe(self, subscription: RunSubscription) -> None:
        """Idempotently release only the specified subscriber."""

        if subscription.closed:
            return
        resource = self._runs.get(subscription.run_id)
        if resource is not None:
            resource.subscribers.discard(subscription)
        subscription.closed = True
        _drain_notification(subscription)

    def _get(self, run_id: str) -> _RunResource:
        self._cleanup_expired()
        resource = self._runs.get(run_id)
        if resource is None:
            raise RunNotFound("Run is unknown, expired, or evicted")
        return resource

    def _status(self, resource: _RunResource) -> RunStatusResponse:
        last_event_id = (
            None if not resource.events else f"{resource.run_id}:{resource.events[-1].sequence}"
        )
        return RunStatusResponse(
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
        resource: _RunResource,
        events: tuple[PublicEvent, ...],
    ) -> bool:
        if resource.projection_status is ProjectionStatus.DEGRADED:
            return False
        if not events or not self._valid_event_batch(resource, events):
            self._degrade_projection(resource)
            return False
        if len(resource.events) + len(events) > self._settings.max_events_per_run:
            self._degrade_projection(resource)
            return False

        resource.events.extend(events)
        self._notify(resource)
        return True

    @staticmethod
    def _valid_event_batch(
        resource: _RunResource,
        events: tuple[PublicEvent, ...],
    ) -> bool:
        expected_sequence = len(resource.events) + 1
        for event in events:
            if (
                event.thread_id != resource.thread_id
                or event.run_id != resource.run_id
                or event.sequence != expected_sequence
            ):
                return False
            expected_sequence += 1
        return True

    def _degrade_projection(self, resource: _RunResource) -> None:
        if resource.projection_status is ProjectionStatus.DEGRADED:
            return
        resource.projection_status = ProjectionStatus.DEGRADED
        self._notify(resource)

    def _finalize_terminal(self, resource: _RunResource) -> None:
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
    def _notify(resource: _RunResource) -> None:
        for subscription in tuple(resource.subscribers):
            _notify_subscription(subscription)


class _SearchExecutor(Protocol):
    async def execute_run(
        self,
        request: SearchRequest,
        *,
        run_id: str,
        observer: RunEventObserver | None = None,
    ) -> SearchExecution: ...


class _EventProjector(Protocol):
    def project_journal(
        self,
        *,
        thread_id: str,
        event: RunEvent,
        sequence: int,
    ) -> PublicEvent: ...

    def project_execution(
        self,
        *,
        thread_id: str,
        execution: SearchExecution,
        first_sequence: int,
    ) -> tuple[PublicEvent, PublicEvent]: ...

    def project_aborted(
        self,
        *,
        thread_id: str,
        run_id: str,
        sequence: int,
    ) -> RunErrorEvent: ...


class _TimeoutFactory(Protocol):
    def __call__(
        self,
        delay: float | None,
        /,
    ) -> AbstractAsyncContextManager[object]: ...


@dataclass(slots=True)
class _ProjectingObserver:
    registry: RunRegistry
    projector: _EventProjector
    thread_id: str
    run_id: str

    def on_event(self, event: RunEvent) -> None:
        try:
            projected = self.projector.project_journal(
                thread_id=self.thread_id,
                event=event,
                sequence=self.registry.next_sequence(self.run_id),
            )
            self.registry.append_events(self.run_id, (projected,))
        except Exception:
            try:
                self.registry.mark_projection_degraded(self.run_id)
            except RunNotFound:
                return


class RunCoordinator:
    """Track bounded background Runs and isolate transport projection failures."""

    def __init__(
        self,
        *,
        registry: RunRegistry,
        projector: _EventProjector,
        service: _SearchExecutor,
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
    ) -> RunStatusResponse:
        """Reserve a Run and create a tracked task that initially waits on a gate."""

        if not self._accepting:
            raise CoordinatorShuttingDown("coordinator is shutting down")
        if not isinstance(request, SearchRequest):
            raise TypeError("coordinator requires a validated SearchRequest")
        loop = asyncio.get_running_loop()
        run_id = _validated_identifier(
            self._run_id_provider.next_run_id(),
            name="run ID",
        )
        if ":" in run_id or "/" in run_id:
            raise ValueError("API-generated run ID must be one unambiguous URL segment")

        accepted = self._registry.reserve(thread_id=thread_id, run_id=run_id)
        gate = asyncio.Event()
        task = loop.create_task(
            self._run(
                request=request,
                thread_id=accepted.thread_id,
                run_id=run_id,
                gate=gate,
            ),
            name=f"glodex-run-{run_id}",
        )
        self._start_gates[run_id] = gate
        self._tasks.add(task)
        self._run_tasks[run_id] = task
        task.add_done_callback(partial(self._task_done, run_id))
        return accepted

    def release_start(self, run_id: str) -> bool:
        """Release a POST response's start gate at most once."""

        gate = self._start_gates.get(run_id)
        if gate is None or gate.is_set():
            return False
        try:
            state = self._registry.status(run_id).state
        except RunNotFound:
            return False
        if state in _TERMINAL_STATES:
            return False
        gate.set()
        return True

    async def wait_idle(self) -> None:
        """Wait for the current tracked task set to finish."""

        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def shutdown(self) -> None:
        """Stop admission, abort retained active Runs, and reap all tasks."""

        self._accepting = False
        for run_id in tuple(self._run_tasks):
            self._commit_aborted(run_id)
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

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
                observer = _ProjectingObserver(
                    registry=self._registry,
                    projector=self._projector,
                    thread_id=thread_id,
                    run_id=run_id,
                )
                execution = await self._service.execute_run(
                    request,
                    run_id=run_id,
                    observer=observer,
                )
                if not self._accepting:
                    self._commit_aborted(run_id)
                    return
                terminal_events: tuple[PublicEvent, PublicEvent] | None
                try:
                    terminal_events = self._projector.project_execution(
                        thread_id=thread_id,
                        execution=execution,
                        first_sequence=self._registry.next_sequence(run_id),
                    )
                except Exception:
                    terminal_events = None
                self._registry.try_commit_execution(
                    run_id,
                    execution,
                    terminal_events,
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
        except RunNotFound:
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


def _validated_identifier(value: object, *, name: str) -> str:
    try:
        return _IDENTIFIER_ADAPTER.validate_python(value, strict=True)
    except ValidationError:
        raise ValueError(f"{name} is invalid") from None


def _notify_subscription(subscription: RunSubscription) -> None:
    try:
        subscription.notification.put_nowait(None)
    except asyncio.QueueFull:
        return


def _drain_notification(subscription: RunSubscription) -> None:
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
    "CoordinatorShuttingDown",
    "DuplicateRunId",
    "EventBatch",
    "RunAlreadyActive",
    "RunCapacityExceeded",
    "RunCoordinator",
    "RunNotFound",
    "RunRegistry",
    "RunSubscription",
    "SubscriberCapacityExceeded",
]
