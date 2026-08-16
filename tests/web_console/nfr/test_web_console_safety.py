"""Offline and privacy constraints for the WebConsole projection and local page."""

from __future__ import annotations

from pathlib import Path

import pytest

from glodex.api.agui import initial_state, project_relay_failure
from glodex.api.console_contracts import WebConsoleRelayCode

PROJECT_ROOT = Path(__file__).resolve().parents[3]

pytestmark = [
    pytest.mark.nfr,
    pytest.mark.spec(
        "GLO-WEB_CONSOLE-NFR-001",
        "GLO-WEB_CONSOLE-NFR-002",
        "GLO-WEB_CONSOLE-NFR-003",
        "GLO-WEB_CONSOLE-NFR-004",
        "GLO-WEB_CONSOLE-NFR-005",
        "WEB_CONSOLE-AC-005",
        "WEB_CONSOLE-AC-006",
    ),
]


def test_relay_failure_has_deterministic_safe_json_without_source_payload() -> None:
    state = initial_state(thread_id="thread-web_console-nfr", run_id="run-web_console-nfr")
    first = project_relay_failure(
        state=state,
        code=WebConsoleRelayCode.PROJECTION_INVALID,
        timestamp=7,
    )
    second = project_relay_failure(
        state=state,
        code=WebConsoleRelayCode.PROJECTION_INVALID,
        timestamp=7,
    )

    first_json = tuple(event.model_dump_json(by_alias=True) for event in first.events)
    second_json = tuple(event.model_dump_json(by_alias=True) for event in second.events)
    assert first_json == second_json
    assert all("rawEvent" not in value for value in first_json)
    assert all("source body" not in value for value in first_json)


def test_browser_source_uses_no_storage_console_or_external_http_endpoint() -> None:
    source_paths = (
        PROJECT_ROOT / "frontend" / "src" / "service" / "agent.ts",
        PROJECT_ROOT / "frontend" / "src" / "views" / "chat" / "modules" / "research-workspace.vue",
        PROJECT_ROOT / "frontend" / "src" / "store" / "modules" / "research-history" / "index.ts",
    )
    source = "\n".join(path.read_text(encoding="utf-8") for path in source_paths)

    assert "localStorage" not in source
    assert "sessionStorage" not in source
    assert "localStg.set('glodexResearchHistoryItems'" not in source
    assert "localStg.get('glodexResearchHistoryItems'" not in source
    assert "console." not in source
    assert "fetch('http://" not in source
    assert 'fetch("http://' not in source
    assert "fetch('https://" not in source
    assert 'fetch("https://' not in source
    assert "new WebSocket('ws://" not in source
    assert 'new WebSocket("ws://' not in source
    assert "new WebSocket('wss://" not in source
    assert 'new WebSocket("wss://' not in source
