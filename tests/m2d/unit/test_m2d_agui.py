"""Offline unit evidence for the frozen M2d AG-UI projection."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from glodex.api.agent_events import (
    AgentResultEvent,
    AgentStartedEvent,
    ModelFinishedEvent,
    ModelStartedEvent,
    ToolFinishedEvent,
    ToolStartedEvent,
)
from glodex.api.m2d_agui import initial_state, project_event, project_payload
from glodex.api.m2d_contracts import AgUiRunInput, M2dRelayCode
from glodex.application.agent.contracts import (
    AgentAnswer,
    AgentAnswerKind,
    AgentDemoResponse,
    AgentEventScope,
    ToolName,
)
from glodex.contracts import RunStatus

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec(
        "GLO-M2D-P0-001",
        "GLO-M2D-P0-002",
        "GLO-M2D-P0-005",
        "GLO-M2D-NFR-002",
        "GLO-M2D-NFR-003",
    ),
]


def _input() -> dict[str, object]:
    return {
        "threadId": "thread-m2d-test",
        "runId": "client-run-m2d-test",
        "state": {},
        "messages": [{"id": "message-m2d-test", "role": "user", "content": " 推荐轻薄本 "}],
        "tools": [],
        "context": [],
        "forwardedProps": {
            "locale": "zh-CN",
            "displayCurrency": "USD",
            "topK": 3,
            "snapshotVersion": "m1d-demo-v1",
        },
    }


def _response() -> AgentDemoResponse:
    return AgentDemoResponse(
        run_id="run-m2d-test",
        status=RunStatus.COMPLETED,
        answer=AgentAnswer(kind=AgentAnswerKind.CHAT_FALLBACK, text="Safe terminal answer."),
    )


def test_run_input_is_exact_and_maps_only_to_search_request() -> None:
    received = AgUiRunInput.model_validate(_input())

    assert received.messages[0].content == "推荐轻薄本"
    assert received.search_request_payload() == {
        "query": "推荐轻薄本",
        "locale": "zh-CN",
        "display_currency": "USD",
        "top_k": 3,
        "snapshot_version": "m1d-demo-v1",
    }
    for mutation in (
        {"parentRunId": "forged"},
        {"tools": [{"name": "browser_tool"}]},
        {"context": [{"description": "private", "value": "private"}]},
        {"state": {"profile": "private"}},
        {"forwardedProps": {"locale": "zh-CN", "model": "forged"}},
    ):
        candidate = _input() | mutation
        with pytest.raises(ValidationError):
            AgUiRunInput.model_validate(candidate)


def test_projector_maps_lifecycle_tool_and_terminal_without_upstream_payload() -> None:
    state = initial_state(thread_id="thread-m2d-test", run_id="run-m2d-test")
    started = project_event(
        state=state,
        event=AgentStartedEvent(
            thread_id="thread-m2d-test",
            run_id="run-m2d-test",
            sequence=1,
            timestamp=1,
        ),
    )
    model_started = project_event(
        state=started.state,
        event=ModelStartedEvent(
            thread_id="thread-m2d-test",
            run_id="run-m2d-test",
            sequence=2,
            timestamp=2,
            scope=AgentEventScope.ROOT,
            round=1,
        ),
    )
    model_finished = project_event(
        state=model_started.state,
        event=ModelFinishedEvent(
            thread_id="thread-m2d-test",
            run_id="run-m2d-test",
            sequence=3,
            timestamp=3,
            scope=AgentEventScope.ROOT,
            round=1,
            tool_name=ToolName.CHAT_FALLBACK,
        ),
    )
    tool_started = project_event(
        state=model_finished.state,
        event=ToolStartedEvent(
            thread_id="thread-m2d-test",
            run_id="run-m2d-test",
            sequence=4,
            timestamp=4,
            scope=AgentEventScope.ROOT,
            tool_name=ToolName.CHAT_FALLBACK,
        ),
    )
    tool_finished = project_event(
        state=tool_started.state,
        event=ToolFinishedEvent(
            thread_id="thread-m2d-test",
            run_id="run-m2d-test",
            sequence=5,
            timestamp=5,
            scope=AgentEventScope.ROOT,
            tool_name=ToolName.CHAT_FALLBACK,
            safe_code="COMPLETED",
        ),
    )
    terminal = project_event(
        state=tool_finished.state,
        event=AgentResultEvent(
            thread_id="thread-m2d-test",
            run_id="run-m2d-test",
            sequence=6,
            timestamp=6,
            scope=AgentEventScope.ROOT,
            status="COMPLETED",
        ),
        terminal_response=_response(),
    )

    assert [event.type for event in started.events] == ["RUN_STARTED", "STATE_SNAPSHOT"]
    assert [event.type for event in model_finished.events] == [
        "STEP_FINISHED",
        "STEP_STARTED",
        "STATE_SNAPSHOT",
    ]
    assert [event.type for event in tool_finished.events] == [
        "TOOL_CALL_END",
        "STEP_FINISHED",
        "CUSTOM",
        "STATE_SNAPSHOT",
    ]
    assert [event.type for event in terminal.events] == [
        "STATE_SNAPSHOT",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]
    body = "\n".join(event.model_dump_json(by_alias=True) for event in terminal.events)
    assert "Safe terminal answer." in body
    assert "rawEvent" not in body
    assert "推荐轻薄本" not in body
    assert terminal.state.terminal is not None
    assert terminal.state.terminal.answer == "Safe terminal answer."


def test_invalid_payload_and_source_gap_fail_closed_without_echoing_payload() -> None:
    state = initial_state(thread_id="thread-m2d-test", run_id="run-m2d-test")
    invalid = project_payload(
        state=state,
        payload='{"type":"TOOL_STARTED","query":"private request"}',
    )

    assert invalid.events[-1].type == "RUN_ERROR"
    assert invalid.events[-1].code == M2dRelayCode.PROJECTION_INVALID.value
    rendered = "\n".join(event.model_dump_json(by_alias=True) for event in invalid.events)
    assert "private request" not in rendered
    assert "query" not in rendered
