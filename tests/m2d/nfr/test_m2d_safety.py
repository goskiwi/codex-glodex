"""Offline and privacy constraints for the M2d projection and local page."""

from __future__ import annotations

from pathlib import Path

import pytest

from glodex.api.m2d_agui import initial_state, project_relay_failure
from glodex.api.m2d_contracts import M2dRelayCode

PROJECT_ROOT = Path(__file__).resolve().parents[3]

pytestmark = [
    pytest.mark.nfr,
    pytest.mark.spec(
        "GLO-M2D-NFR-001",
        "GLO-M2D-NFR-002",
        "GLO-M2D-NFR-003",
        "GLO-M2D-NFR-004",
        "GLO-M2D-NFR-005",
        "M2D-AC-005",
        "M2D-AC-006",
    ),
]


def test_relay_failure_has_deterministic_safe_json_without_source_payload() -> None:
    state = initial_state(thread_id="thread-m2d-nfr", run_id="run-m2d-nfr")
    first = project_relay_failure(
        state=state,
        code=M2dRelayCode.PROJECTION_INVALID,
        timestamp=7,
    )
    second = project_relay_failure(
        state=state,
        code=M2dRelayCode.PROJECTION_INVALID,
        timestamp=7,
    )

    first_json = tuple(event.model_dump_json(by_alias=True) for event in first.events)
    second_json = tuple(event.model_dump_json(by_alias=True) for event in second.events)
    assert first_json == second_json
    assert all("rawEvent" not in value for value in first_json)
    assert all("source body" not in value for value in first_json)


def test_react_source_uses_no_storage_console_or_external_http_endpoint() -> None:
    source_root = PROJECT_ROOT / "frontend" / "src"
    source = "\n".join(path.read_text(encoding="utf-8") for path in source_root.glob("*.ts*"))

    assert "localStorage" not in source
    assert "sessionStorage" not in source
    assert "console." not in source
    assert "http://" not in source
    assert "https://" not in source
