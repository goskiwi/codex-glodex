"""Strict outbound DTOs for the Glodex-owned realtime event contract."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from glodex.application.journal import RunEvent, RunEventKind
from glodex.application.search_service import SearchExecution
from glodex.contracts import (
    Identifier,
    IssueSeverity,
    NonEmptyString,
    RunStatus,
    SearchResponse,
)

StrictSequence = Annotated[int, Field(strict=True, ge=1)]
EpochMilliseconds = Annotated[int, Field(strict=True, ge=0)]
_CURSOR_PATTERN = re.compile(r"(?P<run_id>[A-Za-z0-9][A-Za-z0-9._/-]*):(?P<sequence>[1-9][0-9]*)\Z")
_MAX_CURSOR_LENGTH = 160
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_TERMINAL_JOURNAL_KINDS = frozenset(
    {
        RunEventKind.RUN_COMPLETED,
        RunEventKind.RUN_NO_MATCH,
        RunEventKind.RUN_FAILED,
    }
)
_RUN_FAILED_CODE = "RUN_FAILED"
_RUN_FAILED_MESSAGE = "Run execution failed."
_RUN_ABORTED_CODE = "RUN_ABORTED"
_RUN_ABORTED_MESSAGE = "Run execution was aborted."


class EventDTO(BaseModel):
    """Immutable event model serialized with the approved camelCase aliases."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
        validate_default=True,
    )


class BaseEvent(EventDTO):
    """Fields shared by every glodex.event.v1 event."""

    schema_version: Literal["glodex.event.v1"] = "glodex.event.v1"
    thread_id: Identifier
    run_id: Identifier
    sequence: StrictSequence
    timestamp: EpochMilliseconds


class RunStartedEvent(BaseEvent):
    type: Literal["RUN_STARTED"] = "RUN_STARTED"


class StepStartedEvent(BaseEvent):
    type: Literal["STEP_STARTED"] = "STEP_STARTED"
    step_name: Identifier


class StepFinishedEvent(BaseEvent):
    type: Literal["STEP_FINISHED"] = "STEP_FINISHED"
    step_name: Identifier


class StepDegradedEvent(BaseEvent):
    type: Literal["STEP_DEGRADED"] = "STEP_DEGRADED"
    step_name: Identifier
    issue_codes: Annotated[tuple[Identifier, ...], Field(min_length=1)]


class StateSnapshot(EventDTO):
    response: SearchResponse


class StateSnapshotEvent(BaseEvent):
    type: Literal["STATE_SNAPSHOT"] = "STATE_SNAPSHOT"
    snapshot: StateSnapshot


class RunFinishedResult(EventDTO):
    status: Literal[RunStatus.COMPLETED, RunStatus.NO_MATCH]


class RunFinishedEvent(BaseEvent):
    type: Literal["RUN_FINISHED"] = "RUN_FINISHED"
    result: RunFinishedResult


class RunErrorEvent(BaseEvent):
    type: Literal["RUN_ERROR"] = "RUN_ERROR"
    code: Identifier
    message: NonEmptyString


PublicEvent = Annotated[
    RunStartedEvent
    | StepStartedEvent
    | StepFinishedEvent
    | StepDegradedEvent
    | StateSnapshotEvent
    | RunFinishedEvent
    | RunErrorEvent,
    Field(discriminator="type"),
]


class InvalidEventCursor(ValueError):
    """Raised when a Last-Event-ID cannot name a retained event in this Run."""


def parse_event_cursor(
    value: str | None,
    run_id: str,
    last_sequence: int,
) -> int:
    """Return the sequence after which replay starts, rejecting unsafe cursors."""

    if isinstance(last_sequence, bool) or not isinstance(last_sequence, int) or last_sequence < 0:
        raise ValueError("last_sequence must be a non-negative integer")
    if value is None:
        return 0
    if type(value) is not str or not 3 <= len(value) <= _MAX_CURSOR_LENGTH:
        raise InvalidEventCursor("Invalid event cursor.")

    match = _CURSOR_PATTERN.fullmatch(value)
    if match is None or match["run_id"] != run_id:
        raise InvalidEventCursor("Invalid event cursor.")

    sequence = int(match["sequence"])
    if sequence > last_sequence:
        raise InvalidEventCursor("Invalid event cursor.")
    return sequence


class EventProjector:
    """Stateless projection from application facts to strict public event DTOs."""

    @staticmethod
    def project_journal(
        thread_id: str,
        event: RunEvent,
        sequence: int,
    ) -> RunStartedEvent | StepStartedEvent | StepFinishedEvent | StepDegradedEvent:
        """Project one live, non-terminal journal event."""

        if not isinstance(event, RunEvent):
            raise TypeError("event must be a RunEvent")
        if event.kind in _TERMINAL_JOURNAL_KINDS:
            raise ValueError("terminal journal events must be projected from SearchExecution")

        timestamp = _epoch_milliseconds(event.occurred_at)
        projected: RunStartedEvent | StepStartedEvent | StepFinishedEvent | StepDegradedEvent
        if event.kind is RunEventKind.RUN_STARTED:
            projected = RunStartedEvent(
                thread_id=thread_id,
                run_id=event.run_id,
                sequence=sequence,
                timestamp=timestamp,
            )
        elif event.kind is RunEventKind.STAGE_STARTED:
            projected = StepStartedEvent(
                thread_id=thread_id,
                run_id=event.run_id,
                sequence=sequence,
                timestamp=timestamp,
                step_name=event.stage,
            )
        elif event.kind is RunEventKind.STAGE_COMPLETED:
            projected = StepFinishedEvent(
                thread_id=thread_id,
                run_id=event.run_id,
                sequence=sequence,
                timestamp=timestamp,
                step_name=event.stage,
            )
        elif event.kind is RunEventKind.STAGE_DEGRADED:
            if event.result is None:
                raise ValueError("degraded journal event must contain a stage result")
            projected = StepDegradedEvent(
                thread_id=thread_id,
                run_id=event.run_id,
                sequence=sequence,
                timestamp=timestamp,
                step_name=event.stage,
                issue_codes=event.result.issue_codes,
            )
        else:
            raise ValueError(f"unsupported journal event kind: {event.kind!r}")

        _validate_json_projection(projected)
        return projected

    @staticmethod
    def project_execution(
        thread_id: str,
        execution: SearchExecution,
        first_sequence: int,
    ) -> tuple[StateSnapshotEvent, RunFinishedEvent | RunErrorEvent]:
        """Prebuild the complete snapshot and business-terminal event pair."""

        if not isinstance(execution, SearchExecution):
            raise TypeError("execution must be a SearchExecution")

        response = execution.response
        terminal_fact = execution.journal.events[-1]
        timestamp = _epoch_milliseconds(terminal_fact.occurred_at)
        snapshot = StateSnapshotEvent(
            thread_id=thread_id,
            run_id=response.run_id,
            sequence=first_sequence,
            timestamp=timestamp,
            snapshot=StateSnapshot(response=response),
        )

        terminal: RunFinishedEvent | RunErrorEvent
        if response.status is RunStatus.COMPLETED:
            terminal = RunFinishedEvent(
                thread_id=thread_id,
                run_id=response.run_id,
                sequence=first_sequence + 1,
                timestamp=timestamp,
                result=RunFinishedResult(status=RunStatus.COMPLETED),
            )
        elif response.status is RunStatus.NO_MATCH:
            terminal = RunFinishedEvent(
                thread_id=thread_id,
                run_id=response.run_id,
                sequence=first_sequence + 1,
                timestamp=timestamp,
                result=RunFinishedResult(status=RunStatus.NO_MATCH),
            )
        else:
            issue = next(
                (
                    candidate
                    for candidate in response.warnings
                    if candidate.severity is IssueSeverity.ERROR
                ),
                None,
            )
            terminal = RunErrorEvent(
                thread_id=thread_id,
                run_id=response.run_id,
                sequence=first_sequence + 1,
                timestamp=timestamp,
                code=_RUN_FAILED_CODE if issue is None else issue.code,
                message=_RUN_FAILED_MESSAGE if issue is None else issue.message,
            )

        _validate_json_projection(snapshot)
        _validate_json_projection(terminal)
        return snapshot, terminal

    @staticmethod
    def project_aborted(
        thread_id: str,
        run_id: str,
        sequence: int,
    ) -> RunErrorEvent:
        """Project every transport-only interruption to one stable safe error."""

        projected = RunErrorEvent(
            thread_id=thread_id,
            run_id=run_id,
            sequence=sequence,
            timestamp=_epoch_milliseconds(datetime.now(UTC)),
            code=_RUN_ABORTED_CODE,
            message=_RUN_ABORTED_MESSAGE,
        )
        _validate_json_projection(projected)
        return projected


def _epoch_milliseconds(value: datetime) -> int:
    delta = value - _EPOCH
    return ((delta.days * 86_400 + delta.seconds) * 1_000) + delta.microseconds // 1_000


def _validate_json_projection(event: EventDTO) -> None:
    event.model_dump(mode="json", by_alias=True)


__all__ = [
    "BaseEvent",
    "EventProjector",
    "InvalidEventCursor",
    "PublicEvent",
    "RunErrorEvent",
    "RunFinishedEvent",
    "RunFinishedResult",
    "RunStartedEvent",
    "StateSnapshot",
    "StateSnapshotEvent",
    "StepDegradedEvent",
    "StepFinishedEvent",
    "StepStartedEvent",
    "parse_event_cursor",
]
