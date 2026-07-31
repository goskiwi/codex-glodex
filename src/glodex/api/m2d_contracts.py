"""Strict, browser-safe contracts for the M2d AG-UI projection."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Annotated, Literal, Self, cast

from pydantic import Field, StringConstraints, field_validator, model_validator
from pydantic.alias_generators import to_camel

from glodex.api.contracts import ApiDTO
from glodex.api.durable_agent_contracts import DurableRunStateDTO
from glodex.application.agent.contracts import AgentDemoResponse
from glodex.contracts import CurrencyCode, Identifier, SnapshotVersion

_MAX_BODY_BYTES = 65_536
_MAX_STATE_BYTES = 32_768

AgUiMessageText = Annotated[str, StringConstraints(min_length=1, max_length=2_000)]
AgUiName = Annotated[str, StringConstraints(min_length=1, max_length=128)]
AgUiCode = Identifier
SourceCursor = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*:[1-9][0-9]*$",
    ),
]


class M2dDTO(ApiDTO):
    """Immutable M2d DTO serialized with the AG-UI camelCase wire convention."""

    model_config = ApiDTO.model_config | {"alias_generator": to_camel, "populate_by_name": True}


class AgUiUserMessage(M2dDTO):
    """The only client message accepted by the bounded shopping console."""

    id: Identifier
    role: Literal["user"]
    content: AgUiMessageText

    @field_validator("content", mode="before")
    @classmethod
    def trim_content(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value


class AgUiForwardedProps(M2dDTO):
    """The only AG-UI forwarded values mapped into an existing SearchRequest."""

    locale: Literal["zh-CN"] = "zh-CN"
    display_currency: CurrencyCode = "USD"
    top_k: Annotated[int, Field(strict=True, ge=1, le=3)] = 3
    snapshot_version: SnapshotVersion | None = None


class AgUiRunInput(M2dDTO):
    """A deliberately narrow, standard-shaped AG-UI RunAgentInput subset."""

    thread_id: Identifier
    run_id: Identifier
    state: dict[str, object]
    messages: Annotated[tuple[AgUiUserMessage, ...], Field(min_length=1, max_length=1)]
    tools: tuple[object, ...]
    context: tuple[object, ...]
    forwarded_props: AgUiForwardedProps

    @field_validator("messages", "tools", "context", mode="before")
    @classmethod
    def json_arrays_are_normalized_once(cls, value: object) -> object:
        """HTTP JSON arrays are the canonical wire form; no other coercion is allowed."""

        return tuple(value) if type(value) is list else value

    @model_validator(mode="after")
    def input_is_bounded_and_browser_safe(self) -> Self:
        if self.state or self.tools or self.context:
            raise ValueError("state, tools, and context must be empty")
        serialized = self.model_dump_json(by_alias=True, exclude_none=False).encode("utf-8")
        if len(serialized) > _MAX_BODY_BYTES:
            raise ValueError("AG-UI request exceeds the body limit")
        return self

    def search_request_payload(self) -> dict[str, object]:
        """Return only the pre-existing SearchRequest fields, never browser metadata."""

        props = self.forwarded_props
        return {
            "query": self.messages[0].content,
            "locale": props.locale,
            "display_currency": props.display_currency,
            "top_k": props.top_k,
            "snapshot_version": props.snapshot_version,
        }


class M2dStageState(StrEnum):
    """Small lifecycle set for an independently renderable stage."""

    RUNNING = "RUNNING"
    FINISHED = "FINISHED"


class M2dForkState(StrEnum):
    """Fork UI state intentionally contains no child input or context."""

    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


class M2dRelayState(StrEnum):
    """Health of the browser relay, distinct from the durable run truth."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"


class M2dRelayCode(StrEnum):
    """The complete bounded M2d-owned failure vocabulary."""

    UPSTREAM_UNAVAILABLE = "M2D_UPSTREAM_UNAVAILABLE"
    PROJECTION_INVALID = "M2D_PROJECTION_INVALID"
    STREAM_INTERRUPTED = "M2D_STREAM_INTERRUPTED"
    REQUEST_REJECTED = "M2D_REQUEST_REJECTED"


class M2dStageView(M2dDTO):
    """One safe stage displayed in the React timeline."""

    name: AgUiName
    state: M2dStageState
    safe_code: AgUiCode | None = None
    tool_call_id: Identifier | None = None


class M2dForkView(M2dDTO):
    """A child identity/depth/status projection with no demand text."""

    child_id: Identifier
    depth: Annotated[int, Field(strict=True, ge=1, le=2)]
    state: M2dForkState


class M2dRelayView(M2dDTO):
    """The only relay status visible to the browser."""

    state: M2dRelayState
    safe_code: AgUiCode | None = None

    @model_validator(mode="after")
    def relay_code_matches_state(self) -> Self:
        if (self.state is M2dRelayState.HEALTHY) is (self.safe_code is not None):
            raise ValueError("healthy relay has no code and degraded relay requires one")
        return self


class M2dOfferView(M2dDTO):
    """The selected offer facts allowed on a trusted result card."""

    market: Identifier
    currency: CurrencyCode
    landed_cost: Annotated[str, StringConstraints(min_length=1, max_length=64)]


class M2dResultView(M2dDTO):
    """A limited terminal product card, derived only from AgentDemoResponse."""

    product_id: Identifier
    title: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    category: Identifier
    selected_offer: M2dOfferView
    matched_requirements: Annotated[tuple[str, ...], Field(max_length=32)] = ()
    unknowns: Annotated[tuple[str, ...], Field(max_length=32)] = ()
    reason: Annotated[str, StringConstraints(min_length=1, max_length=2_000)]
    evidence_ids: Annotated[tuple[Identifier, ...], Field(max_length=64)] = ()


class M2dEvidenceView(M2dDTO):
    """Safe evidence metadata; it deliberately does not make an external request."""

    title: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    url_domain: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    published_at: str | None = None
    snippet: Annotated[str, StringConstraints(min_length=1, max_length=280)]
    source_type: Identifier


class M2dToolSummaryView(M2dDTO):
    """A terminal count/outcome projection without call arguments or tool output."""

    tool_name: Identifier
    call_count: Annotated[int, Field(strict=True, ge=1, le=4)]
    safe_outcome: Identifier


class M2dTerminalView(M2dDTO):
    """Whitelisted trusted response facts used in a completed M2d UI state."""

    status: Literal["COMPLETED", "NO_MATCH"]
    answer_kind: Identifier
    answer: Annotated[str, StringConstraints(min_length=1, max_length=2_000)]
    results: Annotated[tuple[M2dResultView, ...], Field(max_length=3)] = ()
    evidence: Annotated[tuple[M2dEvidenceView, ...], Field(max_length=8)] = ()
    tool_summary: Annotated[tuple[M2dToolSummaryView, ...], Field(max_length=10)] = ()

    @classmethod
    def from_agent_response(cls, response: AgentDemoResponse) -> M2dTerminalView:
        """Produce the only terminal business payload M2d permits into the browser."""

        if response.status.value not in {"COMPLETED", "NO_MATCH"} or response.answer is None:
            raise ValueError("only successful Agent terminal responses can be rendered")
        search_response = response.search_response
        results: tuple[M2dResultView, ...] = ()
        if search_response is not None:
            results = tuple(
                M2dResultView(
                    product_id=result.product_id,
                    title=result.title,
                    category=result.category,
                    selected_offer=M2dOfferView(
                        market=result.selected_offer.market,
                        currency=result.landed_cost.currency,
                        landed_cost=result.landed_cost.display,
                    ),
                    matched_requirements=result.matched_requirements,
                    unknowns=result.unknowns,
                    reason=result.reason,
                    evidence_ids=tuple(item.evidence_id for item in result.evidence),
                )
                for result in search_response.results
            )
        evidence = tuple(
            M2dEvidenceView(
                title=item.title,
                url_domain=item.url_domain,
                published_at=None if item.published_at is None else item.published_at.isoformat(),
                snippet=item.snippet,
                source_type=item.source_type.value,
            )
            for item in response.web_evidence
        )
        tool_summary = tuple(
            M2dToolSummaryView(
                tool_name=item.tool_name.value,
                call_count=item.call_count,
                safe_outcome=item.safe_outcome,
            )
            for item in response.tool_summary
        )
        return cls(
            status=cast(Literal["COMPLETED", "NO_MATCH"], response.status.value),
            answer_kind=response.answer.kind.value,
            answer=response.answer.text,
            results=results,
            evidence=evidence,
            tool_summary=tool_summary,
        )


class GlodexM2dState(M2dDTO):
    """Bounded state snapshot rendered by the React console."""

    schema_version: Literal["glodex.m2d.ui-state.v1"] = "glodex.m2d.ui-state.v1"
    thread_id: Identifier
    run_id: Identifier
    state: DurableRunStateDTO
    source_cursor: SourceCursor | None = None
    stages: Annotated[tuple[M2dStageView, ...], Field(max_length=64)] = ()
    forks: Annotated[tuple[M2dForkView, ...], Field(max_length=20)] = ()
    terminal: M2dTerminalView | None = None
    relay: M2dRelayView = M2dRelayView(state=M2dRelayState.HEALTHY)

    @model_validator(mode="after")
    def state_is_bounded_and_terminal_consistent(self) -> Self:
        terminal_states = {DurableRunStateDTO.COMPLETED, DurableRunStateDTO.NO_MATCH}
        if (self.state in terminal_states) is not (self.terminal is not None):
            raise ValueError("business terminal state requires exactly one terminal view")
        serialized = self.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8")
        if len(serialized) > _MAX_STATE_BYTES:
            raise ValueError("M2d state snapshot exceeds the size limit")
        return self


class AgUiEvent(M2dDTO):
    """One serializable event in the M2d frozen AG-UI subset."""

    type: AgUiName
    timestamp: Annotated[int, Field(strict=True, ge=0)]


class AgUiRunStarted(AgUiEvent):
    type: Literal["RUN_STARTED"] = "RUN_STARTED"
    thread_id: Identifier
    run_id: Identifier


class AgUiRunFinished(AgUiEvent):
    type: Literal["RUN_FINISHED"] = "RUN_FINISHED"
    thread_id: Identifier
    run_id: Identifier


class AgUiRunError(AgUiEvent):
    type: Literal["RUN_ERROR"] = "RUN_ERROR"
    message: Literal["Run could not be completed."] = "Run could not be completed."
    code: AgUiCode


class AgUiStepStarted(AgUiEvent):
    type: Literal["STEP_STARTED"] = "STEP_STARTED"
    step_name: AgUiName


class AgUiStepFinished(AgUiEvent):
    type: Literal["STEP_FINISHED"] = "STEP_FINISHED"
    step_name: AgUiName


class AgUiToolCallStart(AgUiEvent):
    type: Literal["TOOL_CALL_START"] = "TOOL_CALL_START"
    tool_call_id: Identifier
    tool_call_name: AgUiName


class AgUiToolCallEnd(AgUiEvent):
    type: Literal["TOOL_CALL_END"] = "TOOL_CALL_END"
    tool_call_id: Identifier


class AgUiStateSnapshot(AgUiEvent):
    type: Literal["STATE_SNAPSHOT"] = "STATE_SNAPSHOT"
    snapshot: GlodexM2dState


class AgUiCustom(AgUiEvent):
    type: Literal["CUSTOM"] = "CUSTOM"
    name: Literal["glodex.m2d.run", "glodex.m2d.relay"]
    value: dict[str, object]


class AgUiTextMessageStart(AgUiEvent):
    type: Literal["TEXT_MESSAGE_START"] = "TEXT_MESSAGE_START"
    message_id: Identifier
    role: Literal["assistant"] = "assistant"


class AgUiTextMessageContent(AgUiEvent):
    type: Literal["TEXT_MESSAGE_CONTENT"] = "TEXT_MESSAGE_CONTENT"
    message_id: Identifier
    delta: AgUiMessageText


class AgUiTextMessageEnd(AgUiEvent):
    type: Literal["TEXT_MESSAGE_END"] = "TEXT_MESSAGE_END"
    message_id: Identifier


AgUiPublicEvent = (
    AgUiRunStarted
    | AgUiRunFinished
    | AgUiRunError
    | AgUiStepStarted
    | AgUiStepFinished
    | AgUiToolCallStart
    | AgUiToolCallEnd
    | AgUiStateSnapshot
    | AgUiCustom
    | AgUiTextMessageStart
    | AgUiTextMessageContent
    | AgUiTextMessageEnd
)


def encode_agui_event(event: AgUiPublicEvent) -> str:
    """Encode exactly one browser-safe JSON event for an SSE data frame."""

    return json.dumps(
        event.model_dump(mode="json", by_alias=True, exclude_none=True),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


__all__ = [
    "AgUiCustom",
    "AgUiEvent",
    "AgUiForwardedProps",
    "AgUiPublicEvent",
    "AgUiRunError",
    "AgUiRunFinished",
    "AgUiRunInput",
    "AgUiRunStarted",
    "AgUiStateSnapshot",
    "AgUiStepFinished",
    "AgUiStepStarted",
    "AgUiTextMessageContent",
    "AgUiTextMessageEnd",
    "AgUiTextMessageStart",
    "AgUiToolCallEnd",
    "AgUiToolCallStart",
    "GlodexM2dState",
    "M2dEvidenceView",
    "M2dForkState",
    "M2dForkView",
    "M2dRelayCode",
    "M2dRelayState",
    "M2dRelayView",
    "M2dResultView",
    "M2dStageState",
    "M2dStageView",
    "M2dTerminalView",
    "encode_agui_event",
]
