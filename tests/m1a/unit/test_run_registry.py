from __future__ import annotations

import asyncio
import gc
import weakref
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self

import pytest

from glodex.api.contracts import ProjectionStatus, RunState
from glodex.api.events import (
    PublicEvent,
    RunErrorEvent,
    RunFinishedEvent,
    RunFinishedResult,
    RunStartedEvent,
    StateSnapshot,
    StateSnapshotEvent,
    StepStartedEvent,
)
from glodex.api.runtime import (
    CoordinatorShuttingDown,
    DuplicateRunId,
    RunAlreadyActive,
    RunCapacityExceeded,
    RunCoordinator,
    RunNotFound,
    RunRegistry,
    SubscriberCapacityExceeded,
)
from glodex.api.settings import ApiSettings
from glodex.application.journal import RunEvent, RunEventKind, RunJournal
from glodex.application.search_service import SearchExecution
from glodex.contracts import (
    EvidenceSummary,
    MoneySummary,
    OfferSummary,
    RunStatus,
    SearchRequest,
    SearchResponse,
    SearchResult,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec(
        "GLO-M1-P0-002",
        "GLO-M1-P0-003",
        "GLO-M1-P0-004",
        "GLO-M1-P0-006",
        "GLO-M1-P0-007",
        "GLO-M1-NFR-002",
        "GLO-M1-NFR-003",
        "GLO-M1-NFR-004",
        "GLO-M1-NFR-006",
        "GLO-M1-NFR-009",
    ),
]

THREAD_ID = "thread-test-001"
RUN_ID = "run-test-001"
NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


@dataclass(slots=True)
class _ManualClock:
    wall_time: datetime = NOW
    monotonic: int = 0

    def now_utc(self) -> datetime:
        return self.wall_time

    def monotonic_ns(self) -> int:
        return self.monotonic

    def advance(self, *, seconds: int) -> None:
        self.wall_time += timedelta(seconds=seconds)
        self.monotonic += seconds * 1_000_000_000


@dataclass(slots=True)
class _RunIds:
    values: list[str]

    def next_run_id(self) -> str:
        if not self.values:
            raise AssertionError("unexpected run ID request")
        return self.values.pop(0)


def _response(run_id: str, status: RunStatus = RunStatus.NO_MATCH) -> SearchResponse:
    results: tuple[SearchResult, ...] = ()
    if status is RunStatus.COMPLETED:
        cost = MoneySummary(currency="USD", exact="1", display="1.00")
        offer = OfferSummary(
            offer_id="offer-1",
            provider_id="provider-1",
            market="US",
            landed_cost=cost,
        )
        results = (
            SearchResult(
                product_id="product-1",
                title="TravelBook",
                category="laptop",
                selected_offer=offer,
                eligible_offers=(offer,),
                landed_cost=cost,
                reason="Verified match.",
                evidence=(EvidenceSummary(evidence_id="evidence-1"),),
            ),
        )
    return SearchResponse(
        run_id=run_id,
        status=status,
        snapshot_version="m0-v1",
        config_fingerprint="a" * 64,
        algorithm_version="phase-d-v1",
        results=results,
    )


def _execution(run_id: str, status: RunStatus = RunStatus.NO_MATCH) -> SearchExecution:
    journal = RunJournal.new(run_id=run_id, snapshot_version="m0-v1")
    journal = journal.start(NOW)
    journal = journal.finish(status, NOW)
    return SearchExecution(response=_response(run_id, status), journal=journal)


def _run_started(run_id: str, sequence: int, *, thread_id: str = THREAD_ID) -> RunStartedEvent:
    return RunStartedEvent(
        thread_id=thread_id,
        run_id=run_id,
        sequence=sequence,
        timestamp=1,
    )


def _step_started(run_id: str, sequence: int, *, thread_id: str = THREAD_ID) -> StepStartedEvent:
    return StepStartedEvent(
        thread_id=thread_id,
        run_id=run_id,
        sequence=sequence,
        timestamp=sequence,
        step_name="ranking",
    )


def _terminal_events(
    execution: SearchExecution,
    first_sequence: int,
    *,
    thread_id: str = THREAD_ID,
) -> tuple[PublicEvent, PublicEvent]:
    snapshot = StateSnapshotEvent(
        thread_id=thread_id,
        run_id=execution.response.run_id,
        sequence=first_sequence,
        timestamp=first_sequence,
        snapshot=StateSnapshot(response=execution.response),
    )
    if execution.response.status in {RunStatus.COMPLETED, RunStatus.NO_MATCH}:
        terminal: PublicEvent = RunFinishedEvent(
            thread_id=thread_id,
            run_id=execution.response.run_id,
            sequence=first_sequence + 1,
            timestamp=first_sequence + 1,
            result=RunFinishedResult(status=execution.response.status),
        )
    else:
        terminal = RunErrorEvent(
            thread_id=thread_id,
            run_id=execution.response.run_id,
            sequence=first_sequence + 1,
            timestamp=first_sequence + 1,
            code="SEARCH_FAILED",
            message="Search execution failed.",
        )
    return snapshot, terminal


class _Projector:
    def __init__(
        self,
        *,
        fail_journal: bool = False,
        fail_execution: bool = False,
        fail_aborted: bool = False,
    ) -> None:
        self.fail_journal = fail_journal
        self.fail_execution = fail_execution
        self.fail_aborted = fail_aborted

    def project_journal(
        self,
        *,
        thread_id: str,
        event: RunEvent,
        sequence: int,
    ) -> PublicEvent:
        if self.fail_journal:
            raise RuntimeError("projection failed")
        if event.kind is RunEventKind.RUN_STARTED:
            return _run_started(event.run_id, sequence, thread_id=thread_id)
        return _step_started(event.run_id, sequence, thread_id=thread_id)

    def project_execution(
        self,
        *,
        thread_id: str,
        execution: SearchExecution,
        first_sequence: int,
    ) -> tuple[PublicEvent, PublicEvent]:
        if self.fail_execution:
            raise RuntimeError("projection failed")
        return _terminal_events(execution, first_sequence, thread_id=thread_id)

    def project_aborted(
        self,
        *,
        thread_id: str,
        run_id: str,
        sequence: int,
    ) -> RunErrorEvent:
        if self.fail_aborted:
            raise RuntimeError("projection failed")
        return RunErrorEvent(
            thread_id=thread_id,
            run_id=run_id,
            sequence=sequence,
            timestamp=sequence,
            code="RUN_ABORTED",
            message="Run execution was aborted.",
        )


class _ControlledService:
    def __init__(
        self,
        execution: SearchExecution,
        *,
        error: Exception | None = None,
    ) -> None:
        self.execution = execution
        self.error = error
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute_run(
        self,
        request: SearchRequest,
        *,
        run_id: str,
        observer: object | None = None,
    ) -> SearchExecution:
        del request
        self.calls += 1
        assert run_id == self.execution.response.run_id
        if observer is not None:
            journal = RunJournal.new(run_id=run_id, snapshot_version="m0-v1").start(NOW)
            observer.on_event(journal.events[-1])  # type: ignore[attr-defined]
        self.started.set()
        await self.release.wait()
        if self.error is not None:
            raise self.error
        return self.execution


class _ControlledTimeout:
    def __init__(self, trigger: asyncio.Event) -> None:
        self.trigger = trigger
        self.owner: asyncio.Task[object] | None = None
        self.watcher: asyncio.Task[None] | None = None
        self.fired = False

    async def __aenter__(self) -> Self:
        owner = asyncio.current_task()
        assert owner is not None
        self.owner = owner
        self.watcher = asyncio.create_task(self._cancel_on_trigger())
        return self

    async def _cancel_on_trigger(self) -> None:
        await self.trigger.wait()
        self.fired = True
        assert self.owner is not None
        self.owner.cancel()

    async def __aexit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del error, traceback
        assert self.watcher is not None
        self.watcher.cancel()
        await asyncio.gather(self.watcher, return_exceptions=True)
        if error_type is asyncio.CancelledError and self.fired:
            raise TimeoutError
        return False


class _TimeoutFactory:
    def __init__(self) -> None:
        self.trigger = asyncio.Event()
        self.seconds: list[float | None] = []

    def __call__(self, seconds: float | None) -> AbstractAsyncContextManager[None]:
        self.seconds.append(seconds)
        return _ControlledTimeout(self.trigger)


async def _checkpoint() -> None:
    loop = asyncio.get_running_loop()
    checkpoint: asyncio.Future[None] = loop.create_future()
    loop.call_soon(checkpoint.set_result, None)
    await checkpoint


def test_registry_enforces_state_payload_and_first_terminal_wins() -> None:
    registry = RunRegistry(settings=ApiSettings(), clock=_ManualClock())
    accepted = registry.reserve(thread_id=THREAD_ID, run_id=RUN_ID)

    assert accepted.state is RunState.ACCEPTED
    assert accepted.response is None
    assert accepted.error is None
    assert registry.try_mark_running(RUN_ID)
    assert registry.status(RUN_ID).state is RunState.RUNNING

    execution = _execution(RUN_ID, RunStatus.NO_MATCH)
    assert registry.try_commit_execution(
        RUN_ID,
        execution,
        _terminal_events(execution, registry.next_sequence(RUN_ID)),
    )
    terminal = registry.status(RUN_ID)
    assert terminal.state is RunState.NO_MATCH
    assert terminal.response is execution.response
    assert terminal.error is None
    assert not registry.try_commit_aborted(RUN_ID, None)
    assert not registry.try_commit_execution(RUN_ID, execution, None)
    assert not registry.append_events(RUN_ID, (_run_started(RUN_ID, 3),))


@pytest.mark.parametrize(
    ("status", "expected_state"),
    [
        (RunStatus.COMPLETED, RunState.COMPLETED),
        (RunStatus.NO_MATCH, RunState.NO_MATCH),
        (RunStatus.FAILED, RunState.FAILED),
    ],
)
def test_registry_maps_all_business_terminal_states_and_releases_thread(
    status: RunStatus,
    expected_state: RunState,
) -> None:
    registry = RunRegistry(settings=ApiSettings(), clock=_ManualClock())
    registry.reserve(thread_id=THREAD_ID, run_id=RUN_ID)
    assert registry.try_mark_running(RUN_ID)
    execution = _execution(RUN_ID, status)

    assert registry.try_commit_execution(
        RUN_ID,
        execution,
        _terminal_events(execution, registry.next_sequence(RUN_ID)),
    )
    assert registry.status(RUN_ID).state is expected_state
    replacement = registry.reserve(thread_id=THREAD_ID, run_id="run-test-002")
    assert replacement.state is RunState.ACCEPTED


def test_registry_rejects_thread_conflict_duplicate_id_and_active_capacity() -> None:
    settings = ApiSettings(max_active_runs=2)
    registry = RunRegistry(settings=settings, clock=_ManualClock())
    registry.reserve(thread_id="thread-1", run_id="run-1")

    with pytest.raises(RunAlreadyActive):
        registry.reserve(thread_id="thread-1", run_id="run-2")
    with pytest.raises(DuplicateRunId):
        registry.reserve(thread_id="thread-2", run_id="run-1")

    registry.reserve(thread_id="thread-2", run_id="run-2")
    with pytest.raises(RunCapacityExceeded):
        registry.reserve(thread_id="thread-3", run_id="run-3")
    assert registry.status("run-1").state is RunState.ACCEPTED
    assert registry.status("run-2").state is RunState.ACCEPTED


def test_terminal_capacity_evicts_oldest_commit_even_when_clock_is_tied() -> None:
    registry = RunRegistry(
        settings=ApiSettings(max_terminal_runs=1),
        clock=_ManualClock(),
    )
    for index in (1, 2):
        run_id = f"run-{index}"
        registry.reserve(thread_id=f"thread-{index}", run_id=run_id)
        assert registry.try_mark_running(run_id)
        execution = _execution(run_id)
        assert registry.try_commit_execution(
            run_id,
            execution,
            _terminal_events(execution, registry.next_sequence(run_id)),
        )

    with pytest.raises(RunNotFound):
        registry.status("run-1")
    assert registry.status("run-2").state is RunState.NO_MATCH


def test_ttl_is_inclusive_and_never_evicts_active_runs() -> None:
    clock = _ManualClock()
    registry = RunRegistry(
        settings=ApiSettings(terminal_ttl_seconds=10),
        clock=clock,
    )
    registry.reserve(thread_id="thread-terminal", run_id="run-terminal")
    assert registry.try_mark_running("run-terminal")
    execution = _execution("run-terminal")
    assert registry.try_commit_execution(
        "run-terminal",
        execution,
        _terminal_events(execution, 1),
    )
    registry.reserve(thread_id="thread-active", run_id="run-active")

    clock.advance(seconds=9)
    assert registry.status("run-terminal").state is RunState.NO_MATCH
    clock.advance(seconds=1)
    with pytest.raises(RunNotFound):
        registry.status("run-terminal")
    assert registry.status("run-active").state is RunState.ACCEPTED


def test_event_capacity_rejects_whole_terminal_batch_and_preserves_business_result() -> None:
    registry = RunRegistry(
        settings=ApiSettings(max_events_per_run=2),
        clock=_ManualClock(),
    )
    registry.reserve(thread_id=THREAD_ID, run_id=RUN_ID)
    assert registry.try_mark_running(RUN_ID)
    assert registry.append_events(RUN_ID, (_run_started(RUN_ID, 1),))
    execution = _execution(RUN_ID)

    assert registry.try_commit_execution(
        RUN_ID,
        execution,
        _terminal_events(execution, 2),
    )
    status = registry.status(RUN_ID)
    assert status.state is RunState.NO_MATCH
    assert status.projection_status is ProjectionStatus.DEGRADED
    assert status.last_event_id == f"{RUN_ID}:1"


def test_invalid_or_over_limit_event_batch_degrades_without_partial_append() -> None:
    registry = RunRegistry(
        settings=ApiSettings(max_events_per_run=2),
        clock=_ManualClock(),
    )
    registry.reserve(thread_id=THREAD_ID, run_id=RUN_ID)
    assert registry.try_mark_running(RUN_ID)
    assert registry.append_events(RUN_ID, (_run_started(RUN_ID, 1),))

    assert not registry.append_events(
        RUN_ID,
        (_step_started(RUN_ID, 2), _step_started(RUN_ID, 3)),
    )
    assert registry.status(RUN_ID).projection_status is ProjectionStatus.DEGRADED
    assert registry.status(RUN_ID).last_event_id == f"{RUN_ID}:1"
    assert not registry.append_events(RUN_ID, (_step_started(RUN_ID, 2),))


def test_subscription_queue_is_bounded_and_reads_the_complete_suffix() -> None:
    registry = RunRegistry(
        settings=ApiSettings(max_subscribers_per_run=1),
        clock=_ManualClock(),
    )
    registry.reserve(thread_id=THREAD_ID, run_id=RUN_ID)
    subscription = registry.subscribe(RUN_ID, after_sequence=0)
    with pytest.raises(SubscriberCapacityExceeded):
        registry.subscribe(RUN_ID, after_sequence=0)

    assert subscription.notification.maxsize == 1
    assert registry.append_events(RUN_ID, (_run_started(RUN_ID, 1),))
    assert registry.append_events(RUN_ID, (_step_started(RUN_ID, 2),))
    assert subscription.notification.qsize() == 1
    batch = registry.read(subscription)
    assert tuple(event.sequence for event in batch.events) == (1, 2)
    assert subscription.notification.qsize() == 0
    assert not batch.terminal

    registry.unsubscribe(subscription)
    replacement = registry.subscribe(RUN_ID, after_sequence=2)
    assert registry.read(replacement).events == ()
    registry.unsubscribe(replacement)


def test_aborted_state_is_safe_and_not_a_business_response() -> None:
    registry = RunRegistry(settings=ApiSettings(), clock=_ManualClock())
    registry.reserve(thread_id=THREAD_ID, run_id=RUN_ID)
    event = RunErrorEvent(
        thread_id=THREAD_ID,
        run_id=RUN_ID,
        sequence=1,
        timestamp=1,
        code="RUN_ABORTED",
        message="Run execution was aborted.",
    )

    assert registry.try_commit_aborted(RUN_ID, event)
    status = registry.status(RUN_ID)
    assert status.state is RunState.ABORTED
    assert status.response is None
    assert status.error is not None
    assert status.error.code == "RUN_ABORTED"
    assert status.error.message == "Run execution was aborted."


def test_coordinator_start_gate_precedes_service_and_projects_live_event() -> None:
    async def exercise() -> None:
        clock = _ManualClock()
        registry = RunRegistry(settings=ApiSettings(), clock=clock)
        service = _ControlledService(_execution(RUN_ID))
        coordinator = RunCoordinator(
            registry=registry,
            projector=_Projector(),
            service=service,
            run_id_provider=_RunIds([RUN_ID]),
            settings=ApiSettings(),
        )
        request = SearchRequest(query="test")

        accepted = coordinator.submit(request, thread_id=THREAD_ID)
        await _checkpoint()
        assert accepted.state is RunState.ACCEPTED
        assert service.calls == 0
        assert coordinator.in_flight_count == 1

        assert coordinator.release_start(RUN_ID)
        await service.started.wait()
        assert registry.status(RUN_ID).state is RunState.RUNNING
        assert registry.status(RUN_ID).last_event_id == f"{RUN_ID}:1"
        service.release.set()
        await coordinator.wait_idle()

        assert registry.status(RUN_ID).state is RunState.NO_MATCH
        assert coordinator.in_flight_count == 0

    asyncio.run(exercise())


def test_coordinator_projection_failure_only_degrades_canonical_result() -> None:
    async def exercise() -> None:
        registry = RunRegistry(settings=ApiSettings(), clock=_ManualClock())
        service = _ControlledService(_execution(RUN_ID))
        service.release.set()
        coordinator = RunCoordinator(
            registry=registry,
            projector=_Projector(fail_journal=True, fail_execution=True),
            service=service,
            run_id_provider=_RunIds([RUN_ID]),
            settings=ApiSettings(),
        )

        coordinator.submit(SearchRequest(query="test"), thread_id=THREAD_ID)
        coordinator.release_start(RUN_ID)
        await coordinator.wait_idle()

        status = registry.status(RUN_ID)
        assert status.state is RunState.NO_MATCH
        assert status.projection_status is ProjectionStatus.DEGRADED
        assert status.response is service.execution.response
        assert status.last_event_id is None

    asyncio.run(exercise())


def test_coordinator_timeout_covers_an_unreleased_start_gate() -> None:
    async def exercise() -> None:
        timeout_factory = _TimeoutFactory()
        registry = RunRegistry(settings=ApiSettings(), clock=_ManualClock())
        service = _ControlledService(_execution(RUN_ID))
        coordinator = RunCoordinator(
            registry=registry,
            projector=_Projector(),
            service=service,
            run_id_provider=_RunIds([RUN_ID]),
            settings=ApiSettings(run_timeout_seconds=7),
            timeout_factory=timeout_factory,
        )

        coordinator.submit(SearchRequest(query="test"), thread_id=THREAD_ID)
        timeout_factory.trigger.set()
        await coordinator.wait_idle()

        assert timeout_factory.seconds == [7]
        assert service.calls == 0
        status = registry.status(RUN_ID)
        assert status.state is RunState.ABORTED
        assert status.error is not None
        assert status.error.code == "RUN_ABORTED"

    asyncio.run(exercise())


def test_coordinator_service_error_aborts_and_releases_request() -> None:
    async def exercise() -> weakref.ReferenceType[SearchRequest]:
        registry = RunRegistry(settings=ApiSettings(), clock=_ManualClock())
        service = _ControlledService(
            _execution(RUN_ID),
            error=RuntimeError("raw query must not survive"),
        )
        service.release.set()
        coordinator = RunCoordinator(
            registry=registry,
            projector=_Projector(),
            service=service,
            run_id_provider=_RunIds([RUN_ID]),
            settings=ApiSettings(),
        )
        request = SearchRequest(query="sensitive query")
        request_reference = weakref.ref(request)

        coordinator.submit(request, thread_id=THREAD_ID)
        coordinator.release_start(RUN_ID)
        del request
        await coordinator.wait_idle()

        status = registry.status(RUN_ID)
        assert status.state is RunState.ABORTED
        assert status.response is None
        assert coordinator.in_flight_count == 0
        return request_reference

    request_reference = asyncio.run(exercise())
    gc.collect()
    assert request_reference() is None


def test_coordinator_shutdown_aborts_tasks_and_stops_accepting() -> None:
    async def exercise() -> None:
        registry = RunRegistry(settings=ApiSettings(), clock=_ManualClock())
        service = _ControlledService(_execution(RUN_ID))
        coordinator = RunCoordinator(
            registry=registry,
            projector=_Projector(),
            service=service,
            run_id_provider=_RunIds([RUN_ID, "run-test-002"]),
            settings=ApiSettings(),
        )

        coordinator.submit(SearchRequest(query="test"), thread_id=THREAD_ID)
        await coordinator.shutdown()

        assert registry.status(RUN_ID).state is RunState.ABORTED
        assert coordinator.in_flight_count == 0
        with pytest.raises(CoordinatorShuttingDown):
            coordinator.submit(
                SearchRequest(query="other"),
                thread_id="thread-test-002",
            )
        await coordinator.shutdown()

    asyncio.run(exercise())


def test_coordinator_rejects_colon_and_duplicate_generated_run_ids_without_task() -> None:
    async def exercise() -> None:
        registry = RunRegistry(settings=ApiSettings(), clock=_ManualClock())
        service = _ControlledService(_execution(RUN_ID))
        first = RunCoordinator(
            registry=registry,
            projector=_Projector(),
            service=service,
            run_id_provider=_RunIds([RUN_ID, RUN_ID]),
            settings=ApiSettings(),
        )
        first.submit(SearchRequest(query="test"), thread_id=THREAD_ID)
        with pytest.raises(DuplicateRunId):
            first.submit(
                SearchRequest(query="other"),
                thread_id="thread-test-002",
            )
        assert first.in_flight_count == 1
        await first.shutdown()

        for invalid_run_id in ("run:invalid", "run/invalid"):
            second = RunCoordinator(
                registry=registry,
                projector=_Projector(),
                service=service,
                run_id_provider=_RunIds([invalid_run_id]),
                settings=ApiSettings(),
            )
            with pytest.raises(ValueError, match="unambiguous URL segment"):
                second.submit(
                    SearchRequest(query="other"),
                    thread_id="thread-test-003",
                )
            assert second.in_flight_count == 0

    asyncio.run(exercise())
