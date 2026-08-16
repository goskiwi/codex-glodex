"""Strict v4 safe SSE projection for durable Agent runs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from glodex.agent.contracts import (
    AgentEventKind,
    AgentEventScope,
    AgentRunEvent,
    AgentTraceBullet,
    Platform,
    ToolName,
)
from glodex.application.ports import Clock
from glodex.contracts import ConfigFingerprint, Identifier
from glodex.runtime.contracts import DurableRun, LoopKind

StrictSequence = Annotated[int, Field(strict=True, ge=1)]
EpochMilliseconds = Annotated[int, Field(strict=True, ge=0)]
ChildDepth = Annotated[int, Field(strict=True, ge=1, le=2)]
AgentTerminalStatus = Literal["COMPLETED", "NO_MATCH"]
AgentErrorStatus = Literal["FAILED", "ABORTED"]
ForkTerminalStatus = Literal["COMPLETED", "FAILED", "ABORTED"]

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


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
    """Every v4 event names its exact owning node in the durable run tree."""

    schema_version: Literal["glodex.agent.event.v4"] = "glodex.agent.event.v4"
    thread_id: Identifier
    run_id: Identifier
    root_run_id: Identifier
    parent_run_id: Identifier | None = None
    loop_kind: Literal[LoopKind.ROOT, LoopKind.CHILD]
    run_depth: Annotated[int, Field(strict=True, ge=0, le=2)]
    sequence: StrictSequence
    timestamp: EpochMilliseconds

    @model_validator(mode="after")
    def tree_identity_is_consistent(self) -> Self:
        if self.loop_kind is LoopKind.ROOT:
            if (
                self.run_id != self.root_run_id
                or self.parent_run_id is not None
                or self.run_depth != 0
            ):
                raise ValueError("root event tree identity is invalid")
        elif self.run_id == self.root_run_id or self.parent_run_id is None or self.run_depth < 1:
            raise ValueError("child event tree identity is invalid")
        return self


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


class ModelStreamingEvent(_ScopedStepEvent):
    """One browser-safe proof that a model round is producing streamed output."""

    type: Literal["MODEL_STREAMING"] = "MODEL_STREAMING"
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
    platforms: Annotated[tuple[Platform, ...], Field(max_length=1)] = ()
    candidate_count: Annotated[int, Field(strict=True, ge=0, le=50)] | None = None
    trace_bullets: Annotated[tuple[AgentTraceBullet, ...], Field(max_length=6)] = ()

    @model_validator(mode="after")
    def search_progress_matches_tool(self) -> Self:
        search_succeeded = self.tool_name is ToolName.ITEM_SEARCH and self.safe_code == "SUCCESS"
        if search_succeeded is not (len(self.platforms) == 1 and self.candidate_count is not None):
            raise ValueError("successful item search requires safe progress facts")
        if not search_succeeded and (self.platforms or self.candidate_count is not None):
            raise ValueError("non-search outcome cannot expose search progress facts")
        return self


class _ForkTargetEvent(BaseAgentEvent):
    """A parent-side event that describes a child node without its transcript."""

    child_id: Identifier
    depth: ChildDepth
    child_parent_run_id: Identifier
    task_scope_digest: ConfigFingerprint

    @model_validator(mode="after")
    def target_is_direct_child(self) -> Self:
        if self.child_parent_run_id != self.run_id or self.depth != self.run_depth + 1:
            raise ValueError("fork event does not identify a direct child")
        return self


class ForkRequestedEvent(_ForkTargetEvent):
    type: Literal["FORK_REQUESTED"] = "FORK_REQUESTED"
    scope: Literal[AgentEventScope.ROOT] = AgentEventScope.ROOT
    platforms: Annotated[tuple[Platform, ...], Field(min_length=1, max_length=8)]


class ForkJoinedEvent(_ForkTargetEvent):
    type: Literal["FORK_JOINED"] = "FORK_JOINED"
    scope: Literal[AgentEventScope.ROOT] = AgentEventScope.ROOT
    status: ForkTerminalStatus


class _ChildLifecycleEvent(BaseAgentEvent):
    """A child-owned lifecycle fact with parent and scope attribution."""

    scope: Literal[AgentEventScope.CHILD] = AgentEventScope.CHILD
    child_id: Identifier
    depth: ChildDepth
    task_scope_digest: ConfigFingerprint

    @model_validator(mode="after")
    def child_lifecycle_matches_owner(self) -> Self:
        if self.loop_kind is not LoopKind.CHILD or self.depth != self.run_depth:
            raise ValueError("child lifecycle tree identity is invalid")
        return self


class ChildRunStartedEvent(_ChildLifecycleEvent):
    type: Literal["CHILD_RUN_STARTED"] = "CHILD_RUN_STARTED"


class ChildCheckpointConfirmedEvent(_ChildLifecycleEvent):
    type: Literal["CHILD_CHECKPOINT_CONFIRMED"] = "CHILD_CHECKPOINT_CONFIRMED"


class ChildHandoffReadyEvent(_ChildLifecycleEvent):
    type: Literal["CHILD_HANDOFF_READY"] = "CHILD_HANDOFF_READY"
    status: ForkTerminalStatus


class ChildFailedEvent(_ChildLifecycleEvent):
    type: Literal["CHILD_FAILED"] = "CHILD_FAILED"
    status: Literal["FAILED"]
    safe_code: Identifier


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
    | ModelStreamingEvent
    | ModelFinishedEvent
    | ToolStartedEvent
    | ToolFinishedEvent
    | ForkRequestedEvent
    | ChildRunStartedEvent
    | ChildCheckpointConfirmedEvent
    | ChildHandoffReadyEvent
    | ChildFailedEvent
    | ForkJoinedEvent
    | AgentResultEvent
    | AgentErrorEvent,
    Field(discriminator="type"),
]


class AgentEventProjector:
    """Project safe facts using the owning durable run, never raw agent state."""

    def __init__(self, *, clock: Clock) -> None:
        self._clock = clock

    def project_event(
        self,
        *,
        run: DurableRun,
        event: AgentRunEvent,
        sequence: int,
    ) -> AgentPublicEvent:
        if type(run) is not DurableRun:
            raise TypeError("event projection requires an exact DurableRun")
        if type(event) is not AgentRunEvent or event.run_id != run.run_id:
            raise TypeError("event must belong to the supplied durable run")
        timestamp = _epoch_milliseconds(self._clock.now_utc())
        base = _base_fields(run=run, sequence=sequence, timestamp=timestamp)

        if event.kind is AgentEventKind.AGENT_STARTED:
            projected: AgentPublicEvent = _validated(AgentStartedEvent, base)
        elif event.kind is AgentEventKind.MODEL_STARTED:
            projected = _validated(
                ModelStartedEvent,
                {
                    **base,
                    "scope": event.scope,
                    "child_id": event.child_id,
                    "depth": event.depth,
                    "round": _required(event.round, name="round"),
                },
            )
        elif event.kind is AgentEventKind.MODEL_STREAMING:
            projected = _validated(
                ModelStreamingEvent,
                {
                    **base,
                    "scope": event.scope,
                    "child_id": event.child_id,
                    "depth": event.depth,
                    "round": _required(event.round, name="round"),
                },
            )
        elif event.kind is AgentEventKind.MODEL_FINISHED:
            projected = _validated(
                ModelFinishedEvent,
                {
                    **base,
                    "scope": event.scope,
                    "child_id": event.child_id,
                    "depth": event.depth,
                    "round": _required(event.round, name="round"),
                    "tool_name": _required(event.tool_name, name="tool name"),
                },
            )
        elif event.kind is AgentEventKind.TOOL_STARTED:
            projected = _validated(
                ToolStartedEvent,
                {
                    **base,
                    "scope": event.scope,
                    "child_id": event.child_id,
                    "depth": event.depth,
                    "tool_name": _required(event.tool_name, name="tool name"),
                },
            )
        elif event.kind is AgentEventKind.TOOL_FINISHED:
            projected = _validated(
                ToolFinishedEvent,
                {
                    **base,
                    "scope": event.scope,
                    "child_id": event.child_id,
                    "depth": event.depth,
                    "tool_name": _required(event.tool_name, name="tool name"),
                    "safe_code": _required(event.safe_code, name="safe code"),
                    "platforms": event.platforms,
                    "candidate_count": event.candidate_count,
                    "trace_bullets": event.trace_bullets,
                },
            )
        elif event.kind is AgentEventKind.FORK_REQUESTED:
            projected = _validated(
                ForkRequestedEvent,
                {**base, **_fork_target_fields(event), "platforms": event.platforms},
            )
        elif event.kind is AgentEventKind.FORK_JOINED:
            projected = _validated(
                ForkJoinedEvent,
                {
                    **base,
                    **_fork_target_fields(event),
                    "status": cast(ForkTerminalStatus, _required(event.status, name="status")),
                },
            )
        elif event.kind is AgentEventKind.CHILD_RUN_STARTED:
            projected = _validated(
                ChildRunStartedEvent,
                {**base, **_child_lifecycle_fields(event)},
            )
        elif event.kind is AgentEventKind.CHILD_CHECKPOINT_CONFIRMED:
            projected = _validated(
                ChildCheckpointConfirmedEvent,
                {**base, **_child_lifecycle_fields(event)},
            )
        elif event.kind is AgentEventKind.CHILD_HANDOFF_READY:
            projected = _validated(
                ChildHandoffReadyEvent,
                {
                    **base,
                    **_child_lifecycle_fields(event),
                    "status": cast(ForkTerminalStatus, _required(event.status, name="status")),
                },
            )
        elif event.kind is AgentEventKind.CHILD_FAILED:
            projected = _validated(
                ChildFailedEvent,
                {
                    **base,
                    **_child_lifecycle_fields(event),
                    "status": "FAILED",
                    "safe_code": _required(event.safe_code, name="safe code"),
                },
            )
        elif event.kind is AgentEventKind.AGENT_RESULT:
            projected = _validated(
                AgentResultEvent,
                {
                    **base,
                    "status": cast(AgentTerminalStatus, _required(event.status, name="status")),
                },
            )
        elif event.kind is AgentEventKind.AGENT_ERROR:
            projected = _validated(
                AgentErrorEvent,
                {
                    **base,
                    "status": cast(AgentErrorStatus, _required(event.status, name="status")),
                    "safe_code": _required(event.safe_code, name="safe code"),
                },
            )
        else:
            raise ValueError("unsupported v4 Agent event kind")

        _validate_json_projection(projected)
        return projected

    def project_aborted(
        self,
        *,
        run: DurableRun,
        sequence: int,
        safe_code: Identifier,
    ) -> AgentErrorEvent:
        if type(run) is not DurableRun or run.loop_kind is not LoopKind.ROOT:
            raise TypeError("only a root run may receive a public abort event")
        projected = _validated(
            AgentErrorEvent,
            {
                **_base_fields(
                    run=run,
                    sequence=sequence,
                    timestamp=_epoch_milliseconds(self._clock.now_utc()),
                ),
                "status": "ABORTED",
                "safe_code": safe_code,
            },
        )
        _validate_json_projection(projected)
        return projected


def _base_fields(*, run: DurableRun, sequence: int, timestamp: int) -> dict[str, object]:
    return {
        "thread_id": run.thread_id,
        "run_id": run.run_id,
        "root_run_id": run.root_run_id,
        "parent_run_id": run.parent_run_id,
        "loop_kind": run.loop_kind,
        "run_depth": run.depth,
        "sequence": sequence,
        "timestamp": timestamp,
    }


def _fork_target_fields(event: AgentRunEvent) -> dict[str, object]:
    return {
        "child_id": _required(event.child_id, name="child ID"),
        "depth": _required(event.depth, name="depth"),
        "child_parent_run_id": _required(event.parent_run_id, name="parent run ID"),
        "task_scope_digest": _required(event.task_scope_digest, name="task scope digest"),
    }


def _child_lifecycle_fields(event: AgentRunEvent) -> dict[str, object]:
    return {
        "child_id": _required(event.child_id, name="child ID"),
        "depth": _required(event.depth, name="depth"),
        "task_scope_digest": _required(event.task_scope_digest, name="task scope digest"),
    }


def _required[T](value: T | None, *, name: str) -> T:
    if value is None:
        raise ValueError(f"{name} is required for this event")
    return value


def _validated[T: AgentEventDTO](model: type[T], payload: dict[str, object]) -> T:
    """Use Pydantic validation instead of unsafe untyped ``**dict`` construction."""

    return model.model_validate(payload)


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
    "ChildCheckpointConfirmedEvent",
    "ChildFailedEvent",
    "ChildHandoffReadyEvent",
    "ChildRunStartedEvent",
    "ForkJoinedEvent",
    "ForkRequestedEvent",
    "ModelFinishedEvent",
    "ModelStartedEvent",
    "ModelStreamingEvent",
    "ToolFinishedEvent",
    "ToolStartedEvent",
]
