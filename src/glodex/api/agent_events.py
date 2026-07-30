"""Strict safe SSE projection for the independent M1d Agent API."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from glodex.application.agent.contracts import (
    AgentEventKind,
    AgentEventScope,
    AgentRunEvent,
    ToolName,
)
from glodex.application.ports import Clock
from glodex.contracts import Identifier

StrictSequence = Annotated[int, Field(strict=True, ge=1)]
EpochMilliseconds = Annotated[int, Field(strict=True, ge=0)]
ChildDepth = Annotated[int, Field(strict=True, ge=1, le=2)]
AgentTerminalStatus = Literal["COMPLETED", "NO_MATCH"]
AgentErrorStatus = Literal["FAILED", "ABORTED"]
ForkTerminalStatus = Literal["COMPLETED", "FAILED", "ABORTED"]

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_RUN_ABORTED_CODE = "RUN_ABORTED"


class AgentEventDTO(BaseModel):
    """Immutable outbound DTO serialized with camelCase aliases."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
        validate_default=True,
    )


class BaseAgentEvent(AgentEventDTO):
    schema_version: Literal["glodex.agent.event.v1"] = "glodex.agent.event.v1"
    thread_id: Identifier
    run_id: Identifier
    sequence: StrictSequence
    timestamp: EpochMilliseconds


class _ScopedStepEvent(BaseAgentEvent):
    scope: AgentEventScope
    child_id: Identifier | None = None
    depth: ChildDepth | None = None

    @model_validator(mode="after")
    def child_identity_matches_scope(self) -> Self:
        child_pair = (self.child_id is not None, self.depth is not None)
        if child_pair[0] is not child_pair[1]:
            raise ValueError("child ID and depth must appear together")
        if self.scope is AgentEventScope.CHILD and not child_pair[0]:
            raise ValueError("child-scoped event requires child identity")
        if self.scope is AgentEventScope.ROOT and child_pair[0]:
            raise ValueError("root-scoped event cannot expose child identity")
        return self


class AgentStartedEvent(BaseAgentEvent):
    type: Literal["AGENT_STARTED"] = "AGENT_STARTED"
    scope: Literal[AgentEventScope.ROOT] = AgentEventScope.ROOT


class ModelStartedEvent(_ScopedStepEvent):
    type: Literal["MODEL_STARTED"] = "MODEL_STARTED"
    round: Annotated[int, Field(strict=True, ge=1, le=14)]


class ModelFinishedEvent(_ScopedStepEvent):
    type: Literal["MODEL_FINISHED"] = "MODEL_FINISHED"
    round: Annotated[int, Field(strict=True, ge=1, le=14)]
    tool_name: ToolName


class ToolStartedEvent(_ScopedStepEvent):
    type: Literal["TOOL_STARTED"] = "TOOL_STARTED"
    tool_name: ToolName


class ToolFinishedEvent(_ScopedStepEvent):
    type: Literal["TOOL_FINISHED"] = "TOOL_FINISHED"
    tool_name: ToolName
    safe_code: Identifier


class ForkStartedEvent(BaseAgentEvent):
    type: Literal["FORK_STARTED"] = "FORK_STARTED"
    scope: Literal[AgentEventScope.ROOT] = AgentEventScope.ROOT
    child_id: Identifier
    depth: ChildDepth


class ForkFinishedEvent(BaseAgentEvent):
    type: Literal["FORK_FINISHED"] = "FORK_FINISHED"
    scope: Literal[AgentEventScope.ROOT] = AgentEventScope.ROOT
    child_id: Identifier
    depth: ChildDepth
    status: ForkTerminalStatus


class AgentResultEvent(BaseAgentEvent):
    type: Literal["AGENT_RESULT"] = "AGENT_RESULT"
    scope: Literal[AgentEventScope.ROOT] = AgentEventScope.ROOT
    status: AgentTerminalStatus


class AgentErrorEvent(BaseAgentEvent):
    type: Literal["AGENT_ERROR"] = "AGENT_ERROR"
    scope: Literal[AgentEventScope.ROOT] = AgentEventScope.ROOT
    status: AgentErrorStatus
    safe_code: Identifier


AgentPublicEvent = Annotated[
    AgentStartedEvent
    | ModelStartedEvent
    | ModelFinishedEvent
    | ToolStartedEvent
    | ToolFinishedEvent
    | ForkStartedEvent
    | ForkFinishedEvent
    | AgentResultEvent
    | AgentErrorEvent,
    Field(discriminator="type"),
]


class AgentEventProjector:
    """Project safe application facts without adding model or tool payload text."""

    def __init__(self, *, clock: Clock) -> None:
        self._clock = clock

    def project_event(
        self,
        *,
        thread_id: str,
        event: AgentRunEvent,
        sequence: int,
    ) -> (
        AgentStartedEvent
        | ModelStartedEvent
        | ModelFinishedEvent
        | ToolStartedEvent
        | ToolFinishedEvent
        | ForkStartedEvent
        | ForkFinishedEvent
        | AgentResultEvent
        | AgentErrorEvent
    ):
        if type(event) is not AgentRunEvent:
            raise TypeError("event must be an exact AgentRunEvent")
        timestamp = _epoch_milliseconds(self._clock.now_utc())

        projected: (
            AgentStartedEvent
            | ModelStartedEvent
            | ModelFinishedEvent
            | ToolStartedEvent
            | ToolFinishedEvent
            | ForkStartedEvent
            | ForkFinishedEvent
            | AgentResultEvent
            | AgentErrorEvent
        )
        if event.kind is AgentEventKind.AGENT_STARTED:
            projected = AgentStartedEvent(
                thread_id=thread_id,
                run_id=event.run_id,
                sequence=sequence,
                timestamp=timestamp,
            )
        elif event.kind is AgentEventKind.MODEL_STARTED:
            assert event.round is not None
            projected = ModelStartedEvent(
                thread_id=thread_id,
                run_id=event.run_id,
                sequence=sequence,
                timestamp=timestamp,
                scope=event.scope,
                child_id=event.child_id,
                depth=event.depth,
                round=event.round,
            )
        elif event.kind is AgentEventKind.MODEL_FINISHED:
            assert event.round is not None
            assert event.tool_name is not None
            projected = ModelFinishedEvent(
                thread_id=thread_id,
                run_id=event.run_id,
                sequence=sequence,
                timestamp=timestamp,
                scope=event.scope,
                child_id=event.child_id,
                depth=event.depth,
                round=event.round,
                tool_name=event.tool_name,
            )
        elif event.kind is AgentEventKind.TOOL_STARTED:
            assert event.tool_name is not None
            projected = ToolStartedEvent(
                thread_id=thread_id,
                run_id=event.run_id,
                sequence=sequence,
                timestamp=timestamp,
                scope=event.scope,
                child_id=event.child_id,
                depth=event.depth,
                tool_name=event.tool_name,
            )
        elif event.kind is AgentEventKind.TOOL_FINISHED:
            assert event.tool_name is not None
            assert event.safe_code is not None
            projected = ToolFinishedEvent(
                thread_id=thread_id,
                run_id=event.run_id,
                sequence=sequence,
                timestamp=timestamp,
                scope=event.scope,
                child_id=event.child_id,
                depth=event.depth,
                tool_name=event.tool_name,
                safe_code=event.safe_code,
            )
        elif event.kind is AgentEventKind.FORK_STARTED:
            assert event.child_id is not None
            assert event.depth is not None
            projected = ForkStartedEvent(
                thread_id=thread_id,
                run_id=event.run_id,
                sequence=sequence,
                timestamp=timestamp,
                child_id=event.child_id,
                depth=event.depth,
            )
        elif event.kind is AgentEventKind.FORK_FINISHED:
            assert event.child_id is not None
            assert event.depth is not None
            assert event.status is not None
            projected = ForkFinishedEvent(
                thread_id=thread_id,
                run_id=event.run_id,
                sequence=sequence,
                timestamp=timestamp,
                child_id=event.child_id,
                depth=event.depth,
                status=cast(ForkTerminalStatus, event.status),
            )
        elif event.kind is AgentEventKind.AGENT_RESULT:
            assert event.status is not None
            projected = AgentResultEvent(
                thread_id=thread_id,
                run_id=event.run_id,
                sequence=sequence,
                timestamp=timestamp,
                status=cast(AgentTerminalStatus, event.status),
            )
        elif event.kind is AgentEventKind.AGENT_ERROR:
            assert event.status is not None
            assert event.safe_code is not None
            projected = AgentErrorEvent(
                thread_id=thread_id,
                run_id=event.run_id,
                sequence=sequence,
                timestamp=timestamp,
                status=cast(AgentErrorStatus, event.status),
                safe_code=event.safe_code,
            )
        else:
            raise ValueError("unsupported Agent event kind")

        _validate_json_projection(projected)
        return projected

    def project_aborted(
        self,
        *,
        thread_id: str,
        run_id: str,
        sequence: int,
    ) -> AgentErrorEvent:
        projected = AgentErrorEvent(
            thread_id=thread_id,
            run_id=run_id,
            sequence=sequence,
            timestamp=_epoch_milliseconds(self._clock.now_utc()),
            status="ABORTED",
            safe_code=_RUN_ABORTED_CODE,
        )
        _validate_json_projection(projected)
        return projected


def _epoch_milliseconds(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("Agent event clock must return a UTC-aware datetime")
    delta = value - _EPOCH
    return ((delta.days * 86_400 + delta.seconds) * 1_000) + delta.microseconds // 1_000


def _validate_json_projection(event: AgentEventDTO) -> None:
    event.model_dump(mode="json", by_alias=True, exclude_none=True)


__all__ = [
    "AgentErrorEvent",
    "AgentEventDTO",
    "AgentEventProjector",
    "AgentPublicEvent",
    "AgentResultEvent",
    "AgentStartedEvent",
    "BaseAgentEvent",
    "ForkFinishedEvent",
    "ForkStartedEvent",
    "ModelFinishedEvent",
    "ModelStartedEvent",
    "ToolFinishedEvent",
    "ToolStartedEvent",
]
