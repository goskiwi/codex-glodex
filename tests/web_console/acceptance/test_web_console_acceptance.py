"""Black-box WebConsole acceptance against the public fake durable client only."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from glodex.api.console_app import create_web_console_app
from glodex.api.durable_client import DurablePublicEventFrame
from tests.web_console.contract.test_web_console_app import (
    _WS_HEADERS,
    _FakeDurableClient,
    _input,
    _receive_closed_run,
)

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.spec(
        "WEB_CONSOLE-AC-001",
        "WEB_CONSOLE-AC-002",
        "WEB_CONSOLE-AC-004",
        "WEB_CONSOLE-AC-005",
        "GLO-WEB_CONSOLE-P0-001",
        "GLO-WEB_CONSOLE-P0-002",
        "GLO-WEB_CONSOLE-P0-004",
        "GLO-WEB_CONSOLE-P0-005",
    ),
]


def test_fake_durable_run_reaches_a_safe_terminal_over_websocket() -> None:
    durable = _FakeDurableClient()
    with (
        TestClient(create_web_console_app(durable_client=durable)) as client,
        client.websocket_connect(
            "/api/v1/web-console/ws",
            headers=_WS_HEADERS,
        ) as websocket,
    ):
        websocket.send_json({"type": "START", "input": _input()})
        frames = _receive_closed_run(websocket)

    events = [frame["event"] for frame in frames if frame["type"] == "EVENT"]
    serialized = str(frames)
    assert frames[0]["type"] == "READY"
    assert frames[-1] == {"type": "CLOSED", "code": "COMPLETED"}
    assert sum(event["type"] == "RUN_STARTED" for event in events) == 1
    assert sum(event["type"] == "RUN_FINISHED" for event in events) == 1
    assert "Safe answer from Durable." in serialized
    assert "推荐轻薄本" not in serialized
    assert len(durable.create_payloads) == 1


def test_bad_source_is_reduced_to_a_safe_websocket_code() -> None:
    class BrokenDurable(_FakeDurableClient):
        async def _events(self, run_id: str) -> AsyncIterator[DurablePublicEventFrame]:
            yield DurablePublicEventFrame(event_id=f"{run_id}:1", data='{"type":"UNKNOWN"}')

    with (
        TestClient(create_web_console_app(durable_client=BrokenDurable())) as client,
        client.websocket_connect(
            "/api/v1/web-console/ws",
            headers=_WS_HEADERS,
        ) as websocket,
    ):
        websocket.send_json({"type": "START", "input": _input()})
        frames = _receive_closed_run(websocket)

    assert frames[-2] == {
        "type": "ERROR",
        "code": "WEB_CONSOLE_PROJECTION_INVALID",
    }
    assert frames[-1] == {
        "type": "CLOSED",
        "code": "WEB_CONSOLE_PROJECTION_INVALID",
    }
    assert "UNKNOWN" not in str(frames)


@pytest.mark.spec(
    "GLO-WEB_CONSOLE-P0-003",
    "GLO-WEB_CONSOLE-P0-006",
    "WEB_CONSOLE-AC-003",
    "WEB_CONSOLE-AC-006",
)
def test_console_source_uses_only_the_same_origin_websocket_transport() -> None:
    root = Path(__file__).resolve().parents[3]
    agent_source = (root / "frontend" / "src" / "service" / "agent.ts").read_text(encoding="utf-8")
    workspace_source = (
        root / "frontend" / "src" / "views" / "chat" / "modules" / "research-workspace.vue"
    ).read_text(encoding="utf-8")

    assert "new WebSocket(webSocketEndpoint())" in agent_source
    assert "type: 'START'" in agent_source
    assert "type: 'ATTACH'" in agent_source
    assert "projectionOrdinal" in agent_source
    assert "STATE_SNAPSHOT" in agent_source
    assert "TOOL_CALL_START" in agent_source
    assert "TEXT_MESSAGE_CONTENT" in agent_source
    assert "text/event-stream" not in agent_source
    assert "last-event-id" not in agent_source
    assert "/proxy-ws" not in agent_source
    assert "VITE_AGENT_BASE_URL" not in agent_source
    assert "8766" not in agent_source
    assert "18000" not in agent_source
    assert "applyAgentEvent" in workspace_source
    assert "cancelActiveResearch" in workspace_source
