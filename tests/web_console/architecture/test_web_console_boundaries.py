"""Source-level architecture boundaries for the intentionally narrow WebConsole seam."""

from __future__ import annotations

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
WEB_CONSOLE_SOURCE = PROJECT_ROOT / "src" / "glodex" / "api"
_CONSOLE_SOURCES = ("agui.py", "console_app.py", "console_contracts.py", "durable_client.py")

pytestmark = [
    pytest.mark.architecture,
    pytest.mark.spec("GLO-WEB_CONSOLE-P0-006", "GLO-WEB_CONSOLE-NFR-006", "WEB_CONSOLE-AC-006"),
]


def test_web_console_never_imports_storage_gpu_provider_or_retrieval_adapters() -> None:
    forbidden = (
        "durable_postgres",
        "durable_redis",
        "opensearch",
        "openai_compatible_http",
        "retrieval_model_",
        "gpu_service",
    )
    source = "\n".join(
        (WEB_CONSOLE_SOURCE / name).read_text(encoding="utf-8") for name in _CONSOLE_SOURCES
    ).lower()

    assert all(name not in source for name in forbidden)


def test_web_console_has_one_fixed_public_upstream_and_no_gpu_or_browser_private_route() -> None:
    client_source = (WEB_CONSOLE_SOURCE / "durable_client.py").read_text(encoding="utf-8")
    app_source = (WEB_CONSOLE_SOURCE / "console_app.py").read_text(encoding="utf-8")
    frontend_source = (PROJECT_ROOT / "frontend" / "src" / "service" / "agent.ts").read_text(
        encoding="utf-8"
    )

    assert "http://127.0.0.1:8766" in client_source
    assert "18000" not in client_source
    assert "18000" not in app_source
    assert "8766" not in frontend_source
    assert "18000" not in frontend_source


def test_web_console_uses_only_the_v4_agent_event_protocol() -> None:
    agent_events = (WEB_CONSOLE_SOURCE / "agent_events.py").read_text(encoding="utf-8")
    relay_source = (WEB_CONSOLE_SOURCE / "agui.py").read_text(encoding="utf-8")

    assert "glodex.agent.event.v4" in agent_events
    assert "glodex.web-console.event.v5" in relay_source
    assert not (WEB_CONSOLE_SOURCE / "events.py").exists()
