"""Immutable application-owned run journal."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from glodex.application.state import (
    TERMINAL_STATES,
    RunState,
    state_for_status,
    status_for_state,
    transition,
)
from glodex.contracts import RunStatus


class RunEventKind(StrEnum):
    """The seven event kinds approved for the M0 journal."""

    RUN_STARTED = "run_started"
    STAGE_STARTED = "stage_started"
    STAGE_COMPLETED = "stage_completed"
    STAGE_DEGRADED = "stage_degraded"
    RUN_COMPLETED = "run_completed"
    RUN_NO_MATCH = "run_no_match"
    RUN_FAILED = "run_failed"


_TERMINAL_EVENT_FOR_STATE: Final[dict[RunState, RunEventKind]] = {
    RunState.COMPLETED: RunEventKind.RUN_COMPLETED,
    RunState.NO_MATCH: RunEventKind.RUN_NO_MATCH,
    RunState.FAILED: RunEventKind.RUN_FAILED,
}
_TERMINAL_EVENTS: Final[frozenset[RunEventKind]] = frozenset(_TERMINAL_EVENT_FOR_STATE.values())


@dataclass(frozen=True, slots=True)
class StageResult:
    """Candidate funnel facts derived from one completed application stage."""

    before: int | None = None
    after: int | None = None
    degraded: bool = False
    issue_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name, value in (("before", self.before), ("after", self.after)):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer")
        if self.before is not None and self.after is not None and self.after > self.before:
            raise ValueError("stage result after cannot exceed before")
        if any(not code for code in self.issue_codes):
            raise ValueError("stage result issue codes must be non-empty")


@dataclass(frozen=True, slots=True)
class RunEvent:
    """One immutable fact in a run's strictly ordered journal."""

    run_id: str
    kind: RunEventKind
    stage: str
    sequence: int
    snapshot_version: str
    occurred_at: datetime
    duration_ms: int | None = None
    result: StageResult | None = None

    def __post_init__(self) -> None:
        if not self.run_id or not self.snapshot_version or not self.stage:
            raise ValueError("run event identifiers must be non-empty")
        if isinstance(self.sequence, bool) or self.sequence < 1:
            raise ValueError("run event sequence must be a positive integer")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() != timedelta(0):
            raise ValueError("run event time must be timezone-aware UTC")
        if self.duration_ms is not None and (
            isinstance(self.duration_ms, bool) or self.duration_ms < 0
        ):
            raise ValueError("run event duration must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class RunJournal:
    """Run state and events updated only through copy-on-write methods."""

    run_id: str
    snapshot_version: str
    state: RunState = RunState.NEW
    events: tuple[RunEvent, ...] = ()

    def __post_init__(self) -> None:
        if not self.run_id or not self.snapshot_version:
            raise ValueError("journal identifiers must be non-empty")
        expected_sequences = tuple(range(1, len(self.events) + 1))
        actual_sequences = tuple(event.sequence for event in self.events)
        if actual_sequences != expected_sequences:
            raise ValueError("journal event sequences must be contiguous from one")
        if any(event.run_id != self.run_id for event in self.events):
            raise ValueError("journal event run_id mismatch")
        if any(event.snapshot_version != self.snapshot_version for event in self.events):
            raise ValueError("journal event snapshot_version mismatch")

        self._validate_event_flow()

    @classmethod
    def new(cls, *, run_id: str, snapshot_version: str) -> RunJournal:
        return cls(run_id=run_id, snapshot_version=snapshot_version)

    @property
    def terminal_status(self) -> RunStatus | None:
        if self.state not in TERMINAL_STATES:
            return None
        return status_for_state(self.state)

    def start(self, occurred_at: datetime) -> RunJournal:
        target = transition(self.state, RunState.RUNNING)
        return self._append(
            kind=RunEventKind.RUN_STARTED,
            stage="run",
            occurred_at=occurred_at,
            target_state=target,
        )

    def stage_started(self, stage: str, occurred_at: datetime) -> RunJournal:
        self._require_running()
        return self._append(
            kind=RunEventKind.STAGE_STARTED,
            stage=stage,
            occurred_at=occurred_at,
        )

    def stage_completed(
        self,
        stage: str,
        occurred_at: datetime,
        *,
        duration_ms: int,
        result: StageResult,
    ) -> RunJournal:
        self._require_running()
        return self._append(
            kind=RunEventKind.STAGE_COMPLETED,
            stage=stage,
            occurred_at=occurred_at,
            duration_ms=duration_ms,
            result=result,
        )

    def stage_degraded(
        self,
        stage: str,
        occurred_at: datetime,
        *,
        result: StageResult,
    ) -> RunJournal:
        self._require_running()
        if not result.degraded:
            raise ValueError("stage_degraded requires a degraded stage result")
        return self._append(
            kind=RunEventKind.STAGE_DEGRADED,
            stage=stage,
            occurred_at=occurred_at,
            result=result,
        )

    def finish(self, status: RunStatus, occurred_at: datetime) -> RunJournal:
        target = state_for_status(status)
        transition(self.state, target)
        return self._append(
            kind=_TERMINAL_EVENT_FOR_STATE[target],
            stage="run",
            occurred_at=occurred_at,
            target_state=target,
        )

    def _append(
        self,
        *,
        kind: RunEventKind,
        stage: str,
        occurred_at: datetime,
        duration_ms: int | None = None,
        result: StageResult | None = None,
        target_state: RunState | None = None,
    ) -> RunJournal:
        event = RunEvent(
            run_id=self.run_id,
            kind=kind,
            stage=stage,
            sequence=len(self.events) + 1,
            snapshot_version=self.snapshot_version,
            occurred_at=occurred_at,
            duration_ms=duration_ms,
            result=result,
        )
        return RunJournal(
            run_id=self.run_id,
            snapshot_version=self.snapshot_version,
            state=self.state if target_state is None else target_state,
            events=(*self.events, event),
        )

    def _require_running(self) -> None:
        if self.state is not RunState.RUNNING:
            raise ValueError(f"journal is not running: {self.state.value}")

    def _validate_event_flow(self) -> None:
        if self.state is RunState.NEW:
            if self.events:
                raise ValueError("NEW journal cannot contain events")
            return
        if not self.events or self.events[0].kind is not RunEventKind.RUN_STARTED:
            raise ValueError("non-NEW journal must begin with run_started")

        active_stages: set[str] = set()
        terminal_events: list[RunEvent] = []
        for index, event in enumerate(self.events):
            if event.kind is RunEventKind.RUN_STARTED:
                if index != 0 or event.stage != "run":
                    raise ValueError("run_started must be the first run event")
            elif event.kind is RunEventKind.STAGE_STARTED:
                if event.stage == "run" or event.stage in active_stages:
                    raise ValueError(f"stage is already active: {event.stage}")
                active_stages.add(event.stage)
            elif event.kind is RunEventKind.STAGE_DEGRADED:
                if event.stage not in active_stages:
                    raise ValueError(f"stage is not active: {event.stage}")
                if event.result is None or not event.result.degraded:
                    raise ValueError("stage_degraded must carry a degraded result")
            elif event.kind is RunEventKind.STAGE_COMPLETED:
                if event.stage not in active_stages:
                    raise ValueError(f"stage is not active: {event.stage}")
                if event.duration_ms is None or event.result is None:
                    raise ValueError("stage_completed must carry duration and result")
                active_stages.remove(event.stage)
            elif event.kind in _TERMINAL_EVENTS:
                terminal_events.append(event)
                if index != len(self.events) - 1 or event.stage != "run":
                    raise ValueError("terminal event must be the final run event")

        if self.state is RunState.RUNNING:
            if terminal_events:
                raise ValueError("RUNNING journal cannot contain a terminal event")
        else:
            expected = _TERMINAL_EVENT_FOR_STATE.get(self.state)
            if len(terminal_events) != 1 or terminal_events[0].kind is not expected:
                raise ValueError("journal terminal event does not match state")
            if active_stages:
                raise ValueError("journal cannot finish with active stages")


__all__ = [
    "RunEvent",
    "RunEventKind",
    "RunJournal",
    "StageResult",
]
