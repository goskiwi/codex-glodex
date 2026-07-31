"""Pure M2b public-event to frozen AG-UI projection for the M2d console."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import TypeAdapter, ValidationError

from glodex.api.agent_events import (
    AgentErrorEvent,
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
from glodex.api.durable_agent_contracts import DurableRunStateDTO
from glodex.api.m2d_contracts import (
    AgUiCustom,
    AgUiPublicEvent,
    AgUiRunError,
    AgUiRunFinished,
    AgUiRunStarted,
    AgUiStateSnapshot,
    AgUiStepFinished,
    AgUiStepStarted,
    AgUiTextMessageContent,
    AgUiTextMessageEnd,
    AgUiTextMessageStart,
    AgUiToolCallEnd,
    AgUiToolCallStart,
    GlodexM2dState,
    M2dForkState,
    M2dForkView,
    M2dRelayCode,
    M2dRelayState,
    M2dRelayView,
    M2dStageState,
    M2dStageView,
    M2dTerminalView,
)
from glodex.application.agent.contracts import AgentDemoResponse

_M2D_SCHEMA = "glodex.m2d.event.v1"

type M2bSourceEvent = (
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

_AGENT_EVENT_ADAPTER: TypeAdapter[M2bSourceEvent] = TypeAdapter(AgentPublicEvent)


@dataclass(frozen=True, slots=True)
class ProjectionGroup:
    """The complete atomic projection of one M2b source event."""

    source_cursor: str | None
    state: GlodexM2dState
    events: tuple[AgUiPublicEvent, ...]


def parse_m2b_public_event(payload: str) -> M2bSourceEvent:
    """Parse exactly one existing M2b public event without a permissive fallback."""

    return _AGENT_EVENT_ADAPTER.validate_json(payload)


def initial_state(*, thread_id: str, run_id: str) -> GlodexM2dState:
    """Return the only safe state before the first durable source event."""

    return GlodexM2dState(
        thread_id=thread_id,
        run_id=run_id,
        state=DurableRunStateDTO.ACCEPTED,
    )


def project_payload(
    *,
    state: GlodexM2dState,
    payload: str,
    terminal_response: AgentDemoResponse | None = None,
) -> ProjectionGroup:
    """Safely parse and project one raw public SSE payload."""

    try:
        event = parse_m2b_public_event(payload)
    except (ValidationError, ValueError, TypeError):
        return project_relay_failure(
            state=state,
            code=M2dRelayCode.PROJECTION_INVALID,
            timestamp=0,
        )
    return project_event(state=state, event=event, terminal_response=terminal_response)


def project_event(
    *,
    state: GlodexM2dState,
    event: M2bSourceEvent,
    terminal_response: AgentDemoResponse | None = None,
) -> ProjectionGroup:
    """Project one validated M2b event; invalid source ordering fails closed."""

    try:
        _validate_source_event(state=state, event=event)
        return _project_valid_event(state=state, event=event, terminal_response=terminal_response)
    except (ValidationError, ValueError, TypeError):
        return project_relay_failure(
            state=state,
            code=M2dRelayCode.PROJECTION_INVALID,
            timestamp=event.timestamp,
        )


def project_relay_failure(
    *,
    state: GlodexM2dState,
    code: M2dRelayCode,
    timestamp: int,
) -> ProjectionGroup:
    """Expose a bounded M2d relay failure without changing durable truth."""

    degraded = state.model_copy(
        update={
            "relay": M2dRelayView(state=M2dRelayState.DEGRADED, safe_code=code.value),
        }
    )
    events: tuple[AgUiPublicEvent, ...] = (
        AgUiCustom(
            timestamp=timestamp,
            name="glodex.m2d.relay",
            value={
                "schemaVersion": _M2D_SCHEMA,
                "state": M2dRelayState.DEGRADED.value,
                "safeCode": code.value,
            },
        ),
        AgUiStateSnapshot(timestamp=timestamp, snapshot=degraded),
        AgUiRunError(timestamp=timestamp, code=code.value),
    )
    return ProjectionGroup(source_cursor=state.source_cursor, state=degraded, events=events)


def _validate_source_event(*, state: GlodexM2dState, event: M2bSourceEvent) -> None:
    if event.thread_id != state.thread_id or event.run_id != state.run_id:
        raise ValueError("source event identity does not match the projected run")
    previous_sequence = _sequence_from_cursor(state.source_cursor)
    if event.sequence != previous_sequence + 1:
        raise ValueError("source event sequence is not contiguous")
    if state.state in {
        DurableRunStateDTO.COMPLETED,
        DurableRunStateDTO.NO_MATCH,
        DurableRunStateDTO.FAILED,
        DurableRunStateDTO.ABORTED,
    }:
        raise ValueError("terminal run cannot receive another source event")
    if event.type == "AGENT_STARTED" and previous_sequence != 0:
        raise ValueError("agent started must be the first source event")
    if event.type != "AGENT_STARTED" and previous_sequence == 0:
        raise ValueError("agent started is required before work events")


def _project_valid_event(
    *,
    state: GlodexM2dState,
    event: M2bSourceEvent,
    terminal_response: AgentDemoResponse | None,
) -> ProjectionGroup:
    cursor = f"{event.run_id}:{event.sequence}"
    next_state = state.model_copy(
        update={
            "source_cursor": cursor,
            "state": DurableRunStateDTO.RUNNING,
            "relay": M2dRelayView(state=M2dRelayState.HEALTHY),
        }
    )
    events: list[AgUiPublicEvent] = []

    if isinstance(event, AgentStartedEvent):
        events.append(
            AgUiRunStarted(
                timestamp=event.timestamp,
                thread_id=event.thread_id,
                run_id=event.run_id,
            )
        )
    elif isinstance(event, ModelStartedEvent):
        next_state = _append_stage(
            next_state,
            name="model_round",
            stage_state=M2dStageState.RUNNING,
        )
        events.append(AgUiStepStarted(timestamp=event.timestamp, step_name="model_round"))
    elif isinstance(event, ModelFinishedEvent):
        next_state = _finish_latest_stage(next_state, name="model_round", safe_code=None)
        tool_stage = _tool_stage_name(event.tool_name.value)
        next_state = _append_stage(
            next_state,
            name=tool_stage,
            stage_state=M2dStageState.RUNNING,
        )
        events.extend(
            (
                AgUiStepFinished(timestamp=event.timestamp, step_name="model_round"),
                AgUiStepStarted(timestamp=event.timestamp, step_name=tool_stage),
            )
        )
    elif isinstance(event, ToolStartedEvent):
        tool_stage = _tool_stage_name(event.tool_name.value)
        call_id = _tool_call_id(event.sequence)
        next_state = _attach_tool_call_id(next_state, name=tool_stage, tool_call_id=call_id)
        events.append(
            AgUiToolCallStart(
                timestamp=event.timestamp,
                tool_call_id=call_id,
                tool_call_name=event.tool_name.value,
            )
        )
    elif isinstance(event, ToolFinishedEvent):
        tool_stage = _tool_stage_name(event.tool_name.value)
        call_id = _active_tool_call_id(next_state, name=tool_stage)
        next_state = _finish_latest_stage(
            next_state,
            name=tool_stage,
            safe_code=event.safe_code,
        )
        events.extend(
            (
                AgUiToolCallEnd(
                    timestamp=event.timestamp,
                    tool_call_id=call_id,
                ),
                AgUiStepFinished(timestamp=event.timestamp, step_name=tool_stage),
                _run_custom(event=event, cursor=cursor),
            )
        )
    elif isinstance(event, ForkStartedEvent):
        stage = _fork_stage_name(event.depth)
        next_state = _append_stage(
            next_state,
            name=stage,
            stage_state=M2dStageState.RUNNING,
        )
        next_state = next_state.model_copy(
            update={
                "forks": (
                    *next_state.forks,
                    M2dForkView(
                        child_id=event.child_id,
                        depth=event.depth,
                        state=M2dForkState.RUNNING,
                    ),
                ),
            }
        )
        events.extend(
            (
                AgUiStepStarted(timestamp=event.timestamp, step_name=stage),
                _run_custom(event=event, cursor=cursor),
            )
        )
    elif isinstance(event, ForkFinishedEvent):
        stage = _fork_stage_name(event.depth)
        next_state = _finish_latest_stage(next_state, name=stage, safe_code=None)
        next_state = _finish_fork(next_state, child_id=event.child_id, state=event.status)
        events.extend(
            (
                AgUiStepFinished(timestamp=event.timestamp, step_name=stage),
                _run_custom(event=event, cursor=cursor),
            )
        )
    elif isinstance(event, AgentResultEvent):
        next_state = _complete_success(
            state=next_state,
            status=event.status,
            terminal_response=terminal_response,
        )
    elif isinstance(event, AgentErrorEvent):
        next_state = next_state.model_copy(update={"state": DurableRunStateDTO(event.status)})
    else:
        raise ValueError("unsupported M2b source event")

    events.append(AgUiStateSnapshot(timestamp=event.timestamp, snapshot=next_state))
    if isinstance(event, AgentResultEvent):
        assert next_state.terminal is not None
        message_id = _answer_message_id(event.sequence)
        events.extend(
            (
                AgUiTextMessageStart(timestamp=event.timestamp, message_id=message_id),
                AgUiTextMessageContent(
                    timestamp=event.timestamp,
                    message_id=message_id,
                    delta=next_state.terminal.answer,
                ),
                AgUiTextMessageEnd(timestamp=event.timestamp, message_id=message_id),
                AgUiRunFinished(
                    timestamp=event.timestamp,
                    thread_id=event.thread_id,
                    run_id=event.run_id,
                ),
            )
        )
    elif isinstance(event, AgentErrorEvent):
        events.append(AgUiRunError(timestamp=event.timestamp, code=event.safe_code))
    return ProjectionGroup(source_cursor=cursor, state=next_state, events=tuple(events))


def _complete_success(
    *,
    state: GlodexM2dState,
    status: str,
    terminal_response: AgentDemoResponse | None,
) -> GlodexM2dState:
    if terminal_response is None or terminal_response.run_id != state.run_id:
        raise ValueError("successful terminal source event requires its matching response")
    terminal = M2dTerminalView.from_agent_response(terminal_response)
    if terminal.status != status:
        raise ValueError("source terminal status does not match terminal response")
    return state.model_copy(update={"state": DurableRunStateDTO(status), "terminal": terminal})


def _append_stage(
    source: GlodexM2dState,
    *,
    name: str,
    stage_state: M2dStageState,
) -> GlodexM2dState:
    return source.model_copy(
        update={"stages": (*source.stages, M2dStageView(name=name, state=stage_state))}
    )


def _finish_latest_stage(
    source: GlodexM2dState,
    *,
    name: str,
    safe_code: str | None,
) -> GlodexM2dState:
    stages = list(source.stages)
    for index in range(len(stages) - 1, -1, -1):
        stage = stages[index]
        if stage.name == name and stage.state is M2dStageState.RUNNING:
            stages[index] = stage.model_copy(
                update={"state": M2dStageState.FINISHED, "safe_code": safe_code}
            )
            return source.model_copy(update={"stages": tuple(stages)})
    raise ValueError("no matching running stage exists")


def _attach_tool_call_id(
    source: GlodexM2dState,
    *,
    name: str,
    tool_call_id: str,
) -> GlodexM2dState:
    stages = list(source.stages)
    for index in range(len(stages) - 1, -1, -1):
        stage = stages[index]
        if stage.name == name and stage.state is M2dStageState.RUNNING:
            if stage.tool_call_id is not None:
                raise ValueError("running tool stage already has a call ID")
            stages[index] = stage.model_copy(update={"tool_call_id": tool_call_id})
            return source.model_copy(update={"stages": tuple(stages)})
    raise ValueError("tool start has no matching planned stage")


def _active_tool_call_id(source: GlodexM2dState, *, name: str) -> str:
    for stage in reversed(source.stages):
        if stage.name == name and stage.state is M2dStageState.RUNNING:
            if stage.tool_call_id is None:
                raise ValueError("tool finish has no matching tool call")
            return stage.tool_call_id
    raise ValueError("tool finish has no matching running stage")


def _finish_fork(source: GlodexM2dState, *, child_id: str, state: str) -> GlodexM2dState:
    forks = list(source.forks)
    for index in range(len(forks) - 1, -1, -1):
        fork = forks[index]
        if fork.child_id == child_id and fork.state is M2dForkState.RUNNING:
            forks[index] = fork.model_copy(update={"state": M2dForkState(state)})
            return source.model_copy(update={"forks": tuple(forks)})
    raise ValueError("no matching running fork exists")


def _run_custom(*, event: M2bSourceEvent, cursor: str) -> AgUiCustom:
    value: dict[str, object] = {
        "schemaVersion": _M2D_SCHEMA,
        "sourceCursor": cursor,
        "scope": event.scope.value,
    }
    if isinstance(event, (ForkStartedEvent, ForkFinishedEvent)):
        value["childId"] = event.child_id
        value["depth"] = event.depth
    if isinstance(event, (ForkFinishedEvent, AgentResultEvent, AgentErrorEvent)):
        value["status"] = event.status
    if isinstance(event, (ToolFinishedEvent, AgentErrorEvent)):
        value["safeCode"] = event.safe_code
    return AgUiCustom(timestamp=event.timestamp, name="glodex.m2d.run", value=value)


def _sequence_from_cursor(cursor: str | None) -> int:
    return 0 if cursor is None else int(cursor.rsplit(":", 1)[1])


def _tool_stage_name(tool_name: str) -> str:
    return f"tool:{tool_name}"


def _fork_stage_name(depth: int) -> str:
    return f"fork:{depth}"


def _tool_call_id(sequence: int) -> str:
    return f"tool-{sequence}"


def _answer_message_id(sequence: int) -> str:
    return f"answer-{sequence}"


__all__ = [
    "ProjectionGroup",
    "initial_state",
    "parse_m2b_public_event",
    "project_event",
    "project_payload",
    "project_relay_failure",
]
