"""Pure Durable public-event to frozen AG-UI projection for the WebConsole console."""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import TypeAdapter, ValidationError

from glodex.agent.contracts import AgentDemoResponse, Platform, ToolName
from glodex.api.agent_events import (
    AgentErrorEvent,
    AgentPublicEvent,
    AgentResultEvent,
    AgentStartedEvent,
    ChildCheckpointConfirmedEvent,
    ChildFailedEvent,
    ChildHandoffReadyEvent,
    ChildRunStartedEvent,
    ForkJoinedEvent,
    ForkRequestedEvent,
    ModelFinishedEvent,
    ModelStartedEvent,
    ModelStreamingEvent,
    ToolFinishedEvent,
    ToolStartedEvent,
)
from glodex.api.console_contracts import (
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
    GlodexWebConsoleState,
    WebConsoleForkState,
    WebConsoleForkView,
    WebConsoleRelayCode,
    WebConsoleRelayState,
    WebConsoleRelayView,
    WebConsoleStageState,
    WebConsoleStageView,
    WebConsoleTerminalView,
    WebConsoleTraceBullet,
    WebConsoleTracePhase,
    WebConsoleTraceStep,
)
from glodex.api.durable_contracts import DurableRunStateDTO
from glodex.runtime.contracts import LoopKind

_RECOVERABLE_PROPOSAL_CODES = frozenset({"INVALID_ACTION", "MODEL_INVALID"})

_WEB_CONSOLE_SCHEMA = "glodex.web-console.event.v5"
_MAX_SAFE_ASSISTANT_DELTA_CHARS = 96

_TOOL_TRACE_COPY: dict[str, tuple[str, str, str]] = {
    "planner": ("拆解购物需求", "已锁定用户原文中的商品、预算与约束", "需求已拆解"),
    "chat_fallback": ("判断请求类型", "正在确认当前请求是否需要商品检索", "请求类型已确认"),
    "web_search": ("核验独立网页证据", "正在补充与当前需求有关的独立证据", "网页证据已核验"),
    "category_insight": ("查询类目知识", "正在取得商品组成、属性与价格区间", "类目知识已取得"),
    "item_search": ("检索商品", "正在检索并核验商品与原始需求的一致性", "商品检索已完成"),
    "price_compare": ("换算并比较价格", "正在统一换算为人民币并比较可信候选", "价格比较已完成"),
    "shipping_calc": ("核算到手成本", "正在核算配送、税费与到手成本", "到手成本已核算"),
    "item_picker": ("筛选最终候选", "正在按硬性条件和证据筛选商品", "最终候选已筛选"),
    "shopping_summary": ("生成研究结论", "正在把已验证事实整理为最终回复", "研究结论已生成"),
    "dispatch_tool": ("创建独立检索任务", "正在把检索范围交给独立任务", "独立检索任务已创建"),
    "parallel_dispatch_tool": (
        "创建并行检索任务",
        "正在并行分配多个平台检索",
        "并行检索任务已创建",
    ),
}

type DurableSourceEvent = (
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
    | AgentErrorEvent
)

_AGENT_EVENT_ADAPTER: TypeAdapter[DurableSourceEvent] = TypeAdapter(AgentPublicEvent)


@dataclass(frozen=True, slots=True)
class ProjectionGroup:
    """The complete atomic projection of one Durable source event."""

    source_cursor: str | None
    state: GlodexWebConsoleState
    events: tuple[AgUiPublicEvent, ...]


def parse_durable_public_event(payload: str) -> DurableSourceEvent:
    """Parse exactly one v4 camelCase Durable public event."""

    try:
        decoded = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("durable public event is invalid JSON") from error
    if not isinstance(decoded, dict) or decoded.get("schemaVersion") != "glodex.agent.event.v4":
        raise ValueError("durable public event schema is invalid")
    if any(not isinstance(key, str) or "_" in key for key in decoded):
        raise ValueError("durable public event must use camelCase fields")
    return _AGENT_EVENT_ADAPTER.validate_json(payload, by_alias=True, by_name=False)


def initial_state(*, thread_id: str, run_id: str) -> GlodexWebConsoleState:
    """Return the only safe state before the first durable source event."""

    return GlodexWebConsoleState(
        thread_id=thread_id,
        run_id=run_id,
        state=DurableRunStateDTO.ACCEPTED,
    )


def project_payload(
    *,
    state: GlodexWebConsoleState,
    payload: str,
    terminal_response: AgentDemoResponse | None = None,
) -> ProjectionGroup:
    """Safely parse and project one raw public SSE payload."""

    try:
        event = parse_durable_public_event(payload)
    except (ValidationError, ValueError, TypeError):
        return project_relay_failure(
            state=state,
            code=WebConsoleRelayCode.PROJECTION_INVALID,
            timestamp=0,
        )
    return project_event(state=state, event=event, terminal_response=terminal_response)


def project_event(
    *,
    state: GlodexWebConsoleState,
    event: DurableSourceEvent,
    terminal_response: AgentDemoResponse | None = None,
) -> ProjectionGroup:
    """Project one validated Durable event; invalid source ordering fails closed."""

    try:
        _validate_source_event(state=state, event=event)
        return _project_valid_event(state=state, event=event, terminal_response=terminal_response)
    except (ValidationError, ValueError, TypeError):
        return project_relay_failure(
            state=state,
            code=WebConsoleRelayCode.PROJECTION_INVALID,
            timestamp=event.timestamp,
        )


def project_relay_failure(
    *,
    state: GlodexWebConsoleState,
    code: WebConsoleRelayCode,
    timestamp: int,
) -> ProjectionGroup:
    """Expose a bounded WebConsole relay failure without changing durable truth."""

    degraded = state.model_copy(
        update={
            "relay": WebConsoleRelayView(state=WebConsoleRelayState.DEGRADED, safe_code=code.value),
        }
    )
    events: tuple[AgUiPublicEvent, ...] = (
        AgUiCustom(
            timestamp=timestamp,
            name="glodex.web-console.relay",
            value={
                "schemaVersion": _WEB_CONSOLE_SCHEMA,
                "state": WebConsoleRelayState.DEGRADED.value,
                "safeCode": code.value,
            },
        ),
        AgUiStateSnapshot(timestamp=timestamp, snapshot=degraded),
        AgUiRunError(timestamp=timestamp, code=code.value),
    )
    return ProjectionGroup(source_cursor=state.source_cursor, state=degraded, events=events)


def _validate_source_event(*, state: GlodexWebConsoleState, event: DurableSourceEvent) -> None:
    # The browser consumes the root aggregate stream.  A child owns a distinct
    # durable run and thread; root_run_id is the aggregate membership key.
    if event.root_run_id != state.run_id or (
        event.loop_kind is LoopKind.ROOT and event.thread_id != state.thread_id
    ):
        raise ValueError("source event does not belong to the projected run tree")
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
    state: GlodexWebConsoleState,
    event: DurableSourceEvent,
    terminal_response: AgentDemoResponse | None,
) -> ProjectionGroup:
    # Event ``run_id`` is a child ID for child-owned lifecycle events.  The
    # resume cursor must always remain rooted at the browser-visible stream.
    cursor = f"{state.run_id}:{event.sequence}"
    next_state = state.model_copy(
        update={
            "source_cursor": cursor,
            "state": DurableRunStateDTO.RUNNING,
            "relay": WebConsoleRelayView(state=WebConsoleRelayState.HEALTHY),
        }
    )
    events: list[AgUiPublicEvent] = []

    if isinstance(event, AgentStartedEvent):
        next_state = _append_trace(
            next_state,
            trace_id=_trace_id(event.sequence),
            owner_run_id=event.run_id,
            phase=WebConsoleTracePhase.THINK,
            title="任务已创建",
            detail="开始理解你的购物需求",
            trace_state=WebConsoleStageState.FINISHED,
        )
        events.append(
            AgUiRunStarted(
                timestamp=event.timestamp,
                thread_id=event.thread_id,
                run_id=event.run_id,
            )
        )
    elif isinstance(event, ModelStartedEvent):
        model_stage = _model_stage_name(event)
        next_state = _append_stage(
            next_state,
            name=model_stage,
            stage_state=WebConsoleStageState.RUNNING,
        )
        if event.loop_kind is LoopKind.ROOT:
            next_state = _append_trace(
                next_state,
                trace_id=_trace_id(event.sequence),
                owner_run_id=event.run_id,
                phase=(
                    WebConsoleTracePhase.THINK if event.round == 1 else WebConsoleTracePhase.REFLECT
                ),
                title=("正在理解购物需求" if event.round == 1 else "Agent 正在判断下一步"),
                detail=(
                    "从用户原文中识别商品、预算、硬性限制与偏好"
                    if event.round == 1
                    else "根据最新可信结果选择下一项操作"
                ),
                trace_state=WebConsoleStageState.RUNNING,
            )
        events.append(AgUiStepStarted(timestamp=event.timestamp, step_name=model_stage))
    elif isinstance(event, ModelStreamingEvent):
        if event.loop_kind is LoopKind.ROOT:
            next_state = _mark_latest_model_trace_streaming(
                next_state,
                owner_run_id=event.run_id,
                round_number=event.round,
            )
        events.append(_run_custom(event=event, cursor=cursor))
    elif isinstance(event, ModelFinishedEvent):
        model_stage = _model_stage_name(event)
        next_state = _finish_latest_stage(next_state, name=model_stage, safe_code=None)
        if event.loop_kind is LoopKind.ROOT:
            action_title, _action_detail, _finished_title = _tool_trace_copy(event.tool_name.value)
            next_state = _finish_latest_trace(
                next_state,
                owner_run_id=event.run_id,
                phase=None,
                safe_code=None,
                title=f"下一步: {action_title}",
                detail=f"已根据当前验证状态决定执行 {event.tool_name.value}",
            )
        tool_stage = _tool_stage_name(event)
        next_state = _append_stage(
            next_state,
            name=tool_stage,
            stage_state=WebConsoleStageState.RUNNING,
        )
        events.extend(
            (
                AgUiStepFinished(timestamp=event.timestamp, step_name=model_stage),
                AgUiStepStarted(timestamp=event.timestamp, step_name=tool_stage),
            )
        )
    elif isinstance(event, ToolStartedEvent):
        tool_stage = _tool_stage_name(event)
        call_id = _tool_call_id(event.sequence)
        next_state = _attach_tool_call_id(next_state, name=tool_stage, tool_call_id=call_id)
        if event.loop_kind is LoopKind.ROOT:
            title, detail, _finished_title = _tool_trace_copy(event.tool_name.value)
            next_state = _promote_latest_decision_trace(
                next_state,
                owner_run_id=event.run_id,
                title=title,
                detail=detail,
                tool_name=event.tool_name.value,
            )
        events.append(
            AgUiToolCallStart(
                timestamp=event.timestamp,
                tool_call_id=call_id,
                tool_call_name=event.tool_name.value,
            )
        )
    elif isinstance(event, ToolFinishedEvent):
        tool_stage = _tool_stage_name(event)
        call_id = _active_tool_call_id(next_state, name=tool_stage)
        recovered_proposal = event.safe_code in _RECOVERABLE_PROPOSAL_CODES
        next_state = _finish_latest_stage(
            next_state,
            name=tool_stage,
            safe_code=None if recovered_proposal else event.safe_code,
            platforms=event.platforms,
            candidate_count=event.candidate_count,
        )
        _title, _detail, finished_title = _tool_trace_copy(event.tool_name.value)
        succeeded = event.safe_code == "SUCCESS"
        trace_bullets = tuple(
            WebConsoleTraceBullet(
                label=bullet.label,
                value=bullet.value,
                source=bullet.source.value,
            )
            for bullet in event.trace_bullets
        )
        observation_detail = (
            _tool_observation_detail(event)
            if succeeded
            else f"安全边界拒绝了本次动作: {event.safe_code}"
        )
        if event.loop_kind is LoopKind.ROOT:
            if recovered_proposal:
                next_state = _finish_latest_trace(
                    next_state,
                    owner_run_id=event.run_id,
                    phase=WebConsoleTracePhase.ACT,
                    safe_code=None,
                    title="Agent 正在调整下一步",
                    detail="当前操作未改变研究数据，已继续根据可信结果重新决策",  # noqa: RUF001
                    trace_phase=WebConsoleTracePhase.REFLECT,
                    bullets=(),
                    tool_name=None,
                    platforms=(),
                    candidate_count=None,
                )
            else:
                next_state = _finish_latest_trace(
                    next_state,
                    owner_run_id=event.run_id,
                    phase=WebConsoleTracePhase.ACT,
                    safe_code=event.safe_code,
                    title=finished_title if succeeded else "动作未完成",
                    detail=observation_detail,
                    trace_phase=WebConsoleTracePhase.OBSERVE,
                    bullets=trace_bullets,
                    tool_name=event.tool_name.value,
                    platforms=event.platforms,
                    candidate_count=event.candidate_count,
                )
        elif succeeded and event.tool_name is ToolName.ITEM_SEARCH:
            next_state = _upsert_child_search_trace(
                next_state,
                owner_run_id=event.run_id,
                title=finished_title,
                detail=observation_detail,
                bullets=trace_bullets,
                safe_code=event.safe_code,
                platforms=event.platforms,
                candidate_count=event.candidate_count,
                trace_id=_trace_id(event.sequence),
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
    elif isinstance(event, ForkRequestedEvent):
        stage = _fork_stage_name(event.child_id)
        next_state = _append_stage(
            next_state,
            name=stage,
            stage_state=WebConsoleStageState.RUNNING,
        )
        next_state = _append_fork(
            next_state,
            child_id=event.child_id,
            depth=event.depth,
            platforms=event.platforms,
        )
        events.extend(
            (
                AgUiStepStarted(timestamp=event.timestamp, step_name=stage),
                _run_custom(event=event, cursor=cursor),
            )
        )
    elif isinstance(event, (ChildRunStartedEvent, ChildCheckpointConfirmedEvent)):
        _require_running_fork(next_state, child_id=event.child_id)
        events.append(_run_custom(event=event, cursor=cursor))
    elif isinstance(event, ChildHandoffReadyEvent):
        stage = _fork_stage_name(event.child_id)
        next_state = _finish_latest_stage(next_state, name=stage, safe_code=None)
        next_state = _finish_fork(next_state, child_id=event.child_id, state=event.status)
        events.extend(
            (
                AgUiStepFinished(timestamp=event.timestamp, step_name=stage),
                _run_custom(event=event, cursor=cursor),
            )
        )
    elif isinstance(event, ChildFailedEvent):
        stage = _fork_stage_name(event.child_id)
        next_state = _finish_latest_stage(next_state, name=stage, safe_code=event.safe_code)
        next_state = _finish_fork(next_state, child_id=event.child_id, state=event.status)
        events.extend(
            (
                AgUiStepFinished(timestamp=event.timestamp, step_name=stage),
                _run_custom(event=event, cursor=cursor),
            )
        )
    elif isinstance(event, ForkJoinedEvent):
        _require_finished_fork(next_state, child_id=event.child_id, state=event.status)
        events.append(_run_custom(event=event, cursor=cursor))
    elif isinstance(event, AgentResultEvent):
        next_state = _complete_success(
            state=next_state,
            status=event.status,
            terminal_response=terminal_response,
        )
        assert next_state.terminal is not None
        next_state = _append_trace(
            next_state,
            trace_id=_trace_id(event.sequence),
            owner_run_id=event.run_id,
            phase=WebConsoleTracePhase.OBSERVE,
            title="研究已完成",
            detail=f"已通过终态校验, 返回 {len(next_state.terminal.results)} 个商品",
            trace_state=WebConsoleStageState.FINISHED,
            candidate_count=len(next_state.terminal.results),
        )
    elif isinstance(event, AgentErrorEvent):
        next_state = _finish_all_running_traces(next_state, safe_code=event.safe_code)
        next_state = _append_trace(
            next_state,
            trace_id=_trace_id(event.sequence),
            owner_run_id=event.run_id,
            phase=WebConsoleTracePhase.OBSERVE,
            title="研究未完成",
            detail=f"运行已安全终止: {event.safe_code}",
            trace_state=WebConsoleStageState.FINISHED,
            safe_code=event.safe_code,
        ).model_copy(update={"state": DurableRunStateDTO(event.status)})
    else:
        raise ValueError("unsupported Durable source event")

    events.append(AgUiStateSnapshot(timestamp=event.timestamp, snapshot=next_state))
    if isinstance(event, AgentResultEvent):
        assert next_state.terminal is not None
        message_id = _answer_message_id(event.sequence)
        assistant_deltas = _safe_assistant_deltas(next_state.terminal.summary)
        events.extend(
            (
                AgUiTextMessageStart(timestamp=event.timestamp, message_id=message_id),
                *(
                    AgUiTextMessageContent(
                        timestamp=event.timestamp,
                        message_id=message_id,
                        delta=delta,
                    )
                    for delta in assistant_deltas
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
    state: GlodexWebConsoleState,
    status: str,
    terminal_response: AgentDemoResponse | None,
) -> GlodexWebConsoleState:
    if terminal_response is None or terminal_response.run_id != state.run_id:
        raise ValueError("successful terminal source event requires its matching response")
    terminal = WebConsoleTerminalView.from_agent_response(terminal_response)
    if terminal.status != status:
        raise ValueError("source terminal status does not match terminal response")
    return state.model_copy(update={"state": DurableRunStateDTO(status), "terminal": terminal})


def _safe_assistant_deltas(summary: str) -> tuple[str, ...]:
    """Chunk only the already whitelisted terminal display summary.

    The console never forwards model reasoning or raw tool payloads.  Splitting
    this bounded terminal text gives the browser a real incremental text-event
    lifecycle while preserving the exact safe projection contract.
    """

    if type(summary) is not str or not summary:
        raise ValueError("terminal summary must be non-empty")
    return tuple(
        summary[index : index + _MAX_SAFE_ASSISTANT_DELTA_CHARS]
        for index in range(0, len(summary), _MAX_SAFE_ASSISTANT_DELTA_CHARS)
    )


def _append_stage(
    source: GlodexWebConsoleState,
    *,
    name: str,
    stage_state: WebConsoleStageState,
) -> GlodexWebConsoleState:
    return source.model_copy(
        update={"stages": (*source.stages, WebConsoleStageView(name=name, state=stage_state))}
    )


def _append_trace(
    source: GlodexWebConsoleState,
    *,
    trace_id: str,
    owner_run_id: str,
    phase: WebConsoleTracePhase,
    title: str,
    detail: str,
    trace_state: WebConsoleStageState,
    bullets: tuple[WebConsoleTraceBullet, ...] = (),
    tool_name: str | None = None,
    safe_code: str | None = None,
    platforms: tuple[Platform, ...] = (),
    candidate_count: int | None = None,
) -> GlodexWebConsoleState:
    return source.model_copy(
        update={
            "trace": (
                *source.trace,
                WebConsoleTraceStep(
                    id=trace_id,
                    owner_run_id=owner_run_id,
                    phase=phase,
                    title=title,
                    detail=detail,
                    state=trace_state,
                    bullets=bullets,
                    tool_name=tool_name,
                    safe_code=safe_code,
                    platforms=platforms,
                    candidate_count=candidate_count,
                ),
            )
        }
    )


def _finish_latest_trace(
    source: GlodexWebConsoleState,
    *,
    owner_run_id: str,
    phase: WebConsoleTracePhase | None,
    safe_code: str | None,
    title: str | None = None,
    detail: str | None = None,
    trace_phase: WebConsoleTracePhase | None = None,
    bullets: tuple[WebConsoleTraceBullet, ...] | None = None,
    tool_name: str | None = None,
    platforms: tuple[Platform, ...] | None = None,
    candidate_count: int | None = None,
) -> GlodexWebConsoleState:
    trace = list(source.trace)
    for index in range(len(trace) - 1, -1, -1):
        step = trace[index]
        if (
            step.owner_run_id == owner_run_id
            and step.state is WebConsoleStageState.RUNNING
            and (phase is None or step.phase is phase)
        ):
            trace[index] = step.model_copy(
                update={
                    "state": WebConsoleStageState.FINISHED,
                    "safe_code": safe_code,
                    **({"title": title} if title is not None else {}),
                    **({"detail": detail} if detail is not None else {}),
                    **({"phase": trace_phase} if trace_phase is not None else {}),
                    **({"bullets": bullets} if bullets is not None else {}),
                    **({"tool_name": tool_name} if tool_name is not None else {}),
                    **({"platforms": platforms} if platforms is not None else {}),
                    **({"candidate_count": candidate_count} if candidate_count is not None else {}),
                }
            )
            return source.model_copy(update={"trace": tuple(trace)})
    raise ValueError("no matching running research trace exists")


def _mark_latest_model_trace_streaming(
    source: GlodexWebConsoleState,
    *,
    owner_run_id: str,
    round_number: int,
) -> GlodexWebConsoleState:
    """Make the live provider stream visible without exposing raw chunks."""

    trace = list(source.trace)
    for index in range(len(trace) - 1, -1, -1):
        step = trace[index]
        if (
            step.owner_run_id == owner_run_id
            and step.state is WebConsoleStageState.RUNNING
            and step.phase in {WebConsoleTracePhase.THINK, WebConsoleTracePhase.REFLECT}
        ):
            trace[index] = step.model_copy(
                update={
                    "title": (
                        "模型正在流式理解购物需求"
                        if round_number == 1
                        else "模型正在流式生成下一步"
                    ),
                    "detail": (
                        "正在接收模型输出并形成安全的结构化决策"
                        if round_number == 1
                        else "正在结合最新可信观察生成下一项工具决策"
                    ),
                }
            )
            return source.model_copy(update={"trace": tuple(trace)})
    raise ValueError("model stream has no matching running research trace")


def _promote_latest_decision_trace(
    source: GlodexWebConsoleState,
    *,
    owner_run_id: str,
    title: str,
    detail: str,
    tool_name: str,
) -> GlodexWebConsoleState:
    trace = list(source.trace)
    for index in range(len(trace) - 1, -1, -1):
        step = trace[index]
        if step.owner_run_id == owner_run_id and step.phase in {
            WebConsoleTracePhase.THINK,
            WebConsoleTracePhase.REFLECT,
        }:
            trace[index] = step.model_copy(
                update={
                    "phase": WebConsoleTracePhase.ACT,
                    "title": title,
                    "detail": detail,
                    "state": WebConsoleStageState.RUNNING,
                    "tool_name": tool_name,
                }
            )
            return source.model_copy(update={"trace": tuple(trace)})
    raise ValueError("tool start has no matching research decision")


def _upsert_child_search_trace(
    source: GlodexWebConsoleState,
    *,
    owner_run_id: str,
    title: str,
    detail: str,
    bullets: tuple[WebConsoleTraceBullet, ...],
    safe_code: str,
    platforms: tuple[Platform, ...],
    candidate_count: int | None,
    trace_id: str,
) -> GlodexWebConsoleState:
    trace = list(source.trace)
    for index in range(len(trace) - 1, -1, -1):
        step = trace[index]
        if step.tool_name == ToolName.ITEM_SEARCH.value and step.platforms == platforms:
            trace[index] = step.model_copy(
                update={
                    "owner_run_id": owner_run_id,
                    "title": title,
                    "detail": detail,
                    "bullets": bullets,
                    "safe_code": safe_code,
                    "candidate_count": candidate_count,
                }
            )
            return source.model_copy(update={"trace": tuple(trace)})
    return _append_trace(
        source,
        trace_id=trace_id,
        owner_run_id=owner_run_id,
        phase=WebConsoleTracePhase.OBSERVE,
        title=title,
        detail=detail,
        trace_state=WebConsoleStageState.FINISHED,
        bullets=bullets,
        tool_name=ToolName.ITEM_SEARCH.value,
        safe_code=safe_code,
        platforms=platforms,
        candidate_count=candidate_count,
    )


def _finish_all_running_traces(
    source: GlodexWebConsoleState,
    *,
    safe_code: str,
) -> GlodexWebConsoleState:
    return source.model_copy(
        update={
            "trace": tuple(
                step.model_copy(
                    update={"state": WebConsoleStageState.FINISHED, "safe_code": safe_code}
                )
                if step.state is WebConsoleStageState.RUNNING
                else step
                for step in source.trace
            )
        }
    )


def _finish_latest_stage(
    source: GlodexWebConsoleState,
    *,
    name: str,
    safe_code: str | None,
    platforms: tuple[Platform, ...] = (),
    candidate_count: int | None = None,
) -> GlodexWebConsoleState:
    stages = list(source.stages)
    for index in range(len(stages) - 1, -1, -1):
        stage = stages[index]
        if stage.name == name and stage.state is WebConsoleStageState.RUNNING:
            stages[index] = stage.model_copy(
                update={
                    "state": WebConsoleStageState.FINISHED,
                    "safe_code": safe_code,
                    "platforms": platforms,
                    "candidate_count": candidate_count,
                }
            )
            return source.model_copy(update={"stages": tuple(stages)})
    raise ValueError("no matching running stage exists")


def _attach_tool_call_id(
    source: GlodexWebConsoleState,
    *,
    name: str,
    tool_call_id: str,
) -> GlodexWebConsoleState:
    stages = list(source.stages)
    for index in range(len(stages) - 1, -1, -1):
        stage = stages[index]
        if stage.name == name and stage.state is WebConsoleStageState.RUNNING:
            if stage.tool_call_id is not None:
                raise ValueError("running tool stage already has a call ID")
            stages[index] = stage.model_copy(update={"tool_call_id": tool_call_id})
            return source.model_copy(update={"stages": tuple(stages)})
    raise ValueError("tool start has no matching planned stage")


def _active_tool_call_id(source: GlodexWebConsoleState, *, name: str) -> str:
    for stage in reversed(source.stages):
        if stage.name == name and stage.state is WebConsoleStageState.RUNNING:
            if stage.tool_call_id is None:
                raise ValueError("tool finish has no matching tool call")
            return stage.tool_call_id
    raise ValueError("tool finish has no matching running stage")


def _append_fork(
    source: GlodexWebConsoleState,
    *,
    child_id: str,
    depth: int,
    platforms: tuple[Platform, ...],
) -> GlodexWebConsoleState:
    if any(fork.child_id == child_id for fork in source.forks):
        raise ValueError("fork child ID is already present")
    return source.model_copy(
        update={
            "forks": (
                *source.forks,
                WebConsoleForkView(
                    child_id=child_id,
                    depth=depth,
                    state=WebConsoleForkState.RUNNING,
                    platforms=platforms,
                ),
            )
        }
    )


def _require_running_fork(source: GlodexWebConsoleState, *, child_id: str) -> None:
    for fork in reversed(source.forks):
        if fork.child_id == child_id:
            if fork.state is not WebConsoleForkState.RUNNING:
                raise ValueError("fork is no longer running")
            return
    raise ValueError("no matching running fork exists")


def _finish_fork(
    source: GlodexWebConsoleState,
    *,
    child_id: str,
    state: str,
) -> GlodexWebConsoleState:
    forks = list(source.forks)
    for index in range(len(forks) - 1, -1, -1):
        fork = forks[index]
        if fork.child_id == child_id and fork.state is WebConsoleForkState.RUNNING:
            forks[index] = fork.model_copy(update={"state": WebConsoleForkState(state)})
            return source.model_copy(update={"forks": tuple(forks)})
    raise ValueError("no matching running fork exists")


def _require_finished_fork(source: GlodexWebConsoleState, *, child_id: str, state: str) -> None:
    for fork in reversed(source.forks):
        if fork.child_id == child_id:
            if fork.state.value != state:
                raise ValueError("fork join status does not match child terminal state")
            return
    raise ValueError("no matching finished fork exists")


def _run_custom(*, event: DurableSourceEvent, cursor: str) -> AgUiCustom:
    value: dict[str, object] = {
        "schemaVersion": _WEB_CONSOLE_SCHEMA,
        "sourceCursor": cursor,
        "kind": event.type,
        "scope": event.scope.value,
        "runId": event.run_id,
        "rootRunId": event.root_run_id,
        "loopKind": event.loop_kind.value,
        "runDepth": event.run_depth,
    }
    if event.parent_run_id is not None:
        value["parentRunId"] = event.parent_run_id
    if isinstance(
        event,
        (
            ForkRequestedEvent,
            ChildRunStartedEvent,
            ChildCheckpointConfirmedEvent,
            ChildHandoffReadyEvent,
            ChildFailedEvent,
            ForkJoinedEvent,
        ),
    ):
        value["childId"] = event.child_id
        value["depth"] = event.depth
    if isinstance(event, ForkRequestedEvent):
        value["platforms"] = [platform.value for platform in event.platforms]
    if isinstance(event, ToolFinishedEvent) and event.platforms:
        value["platforms"] = [platform.value for platform in event.platforms]
        value["candidateCount"] = event.candidate_count
    if isinstance(
        event,
        (
            ChildHandoffReadyEvent,
            ChildFailedEvent,
            ForkJoinedEvent,
            AgentResultEvent,
            AgentErrorEvent,
        ),
    ):
        value["status"] = event.status
    if isinstance(event, (ModelStartedEvent, ModelStreamingEvent, ModelFinishedEvent)):
        value["round"] = event.round
    if isinstance(event, (ToolFinishedEvent, ChildFailedEvent, AgentErrorEvent)):
        value["safeCode"] = event.safe_code
    return AgUiCustom(timestamp=event.timestamp, name="glodex.web-console.run", value=value)


def _sequence_from_cursor(cursor: str | None) -> int:
    return 0 if cursor is None else int(cursor.rsplit(":", 1)[1])


def _model_stage_name(
    event: ModelStartedEvent | ModelStreamingEvent | ModelFinishedEvent,
) -> str:
    return _scoped_stage_name("model_round", event)


def _tool_stage_name(
    event: ModelFinishedEvent | ToolStartedEvent | ToolFinishedEvent,
) -> str:
    return _scoped_stage_name(f"tool:{event.tool_name.value}", event)


def _scoped_stage_name(name: str, event: DurableSourceEvent) -> str:
    """Keep concurrent child stages distinct without exposing task content."""

    return name if event.run_id == event.root_run_id else f"{name}:{event.run_id}"


def _fork_stage_name(child_id: str) -> str:
    return f"fork:{child_id}"


def _tool_call_id(sequence: int) -> str:
    return f"tool-{sequence}"


def _answer_message_id(sequence: int) -> str:
    return f"answer-{sequence}"


def _trace_id(sequence: int) -> str:
    return f"trace-{sequence}"


def _tool_trace_copy(tool_name: str) -> tuple[str, str, str]:
    try:
        return _TOOL_TRACE_COPY[tool_name]
    except KeyError:
        raise ValueError("tool has no public trace copy") from None


def _tool_observation_detail(event: ToolFinishedEvent) -> str:
    if event.tool_name.value == "item_search":
        platform = event.platforms[0].value
        return f"{platform} 返回 {event.candidate_count} 个通过语义核验的候选"
    if event.trace_bullets:
        return event.trace_bullets[0].value
    return "可信工具已完成, 结果已写入验证状态"


__all__ = [
    "ProjectionGroup",
    "initial_state",
    "parse_durable_public_event",
    "project_event",
    "project_payload",
    "project_relay_failure",
]
