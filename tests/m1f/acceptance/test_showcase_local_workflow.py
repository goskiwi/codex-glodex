"""Black-box acceptance evidence for the static local M1f showcase."""

from __future__ import annotations

from pathlib import Path

import pytest

import scripts.serve_m1f_showcase as server
from scripts.validate_m1f_showcase import validate_assets

pytestmark = pytest.mark.acceptance

PROJECT_ROOT = Path(__file__).parents[3]


class _OneShotServer:
    def __init__(self) -> None:
        self.closed = False
        self.served = False

    def serve_forever(self) -> None:
        self.served = True

    def server_close(self) -> None:
        self.closed = True


@pytest.mark.spec("GLO-M1F-P0-001", "M1F-AC-001", "GLO-M1F-NFR-001")
def test_operator_can_start_the_credential_free_local_showcase(
    capsys: pytest.CaptureFixture[str],
) -> None:
    one_shot = _OneShotServer()

    assert server.main((), server_factory=lambda _address, _handler: one_shot) == 0

    assert one_shot.served is True
    assert one_shot.closed is True
    assert capsys.readouterr().out == "Showcase: http://127.0.0.1:8765/\n"


@pytest.mark.spec("GLO-M1F-P0-002", "M1F-AC-002", "GLO-M1F-NFR-003")
def test_static_entry_exposes_replay_controls_and_the_fixed_tool_inventory() -> None:
    index = (PROJECT_ROOT / "showcase" / "index.html").read_text(encoding="utf-8")
    script = (PROJECT_ROOT / "showcase" / "app.js").read_text(encoding="utf-8")

    for control in ("play-button", "pause-button", "reset-button"):
        assert f'id="{control}"' in index
    states = (
        'status: "idle"',
        'status = "running"',
        'status = "paused"',
        'status = "completed"',
    )
    for state in states:
        assert state in script
    assert "本次已执行" in script
    assert "本次未执行" in script
    assert script.count('"dispatch_tool"') == 1


@pytest.mark.spec("GLO-M1F-P0-003", "M1F-AC-003", "GLO-M1F-NFR-004")
def test_operator_can_verify_the_committed_offline_evidence_before_opening_the_page() -> None:
    assert validate_assets() == {
        "planner",
        "dispatch_tool",
        "item_search",
        "shopping_summary",
    }
