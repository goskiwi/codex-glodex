"""Public isolation acceptance coverage for the M1f showcase."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.validate_m1f_showcase import validate_assets

pytestmark = pytest.mark.acceptance

PROJECT_ROOT = Path(__file__).parents[3]


@pytest.mark.spec("GLO-M1F-P0-004", "M1F-AC-004", "GLO-M1F-NFR-001", "GLO-M1F-NFR-004")
def test_public_showcase_workflow_remains_separate_from_agent_and_benchmark_runtime() -> None:
    static_runtime = "\n".join(
        (PROJECT_ROOT / "showcase" / name).read_text(encoding="utf-8")
        for name in ("index.html", "styles.css", "app.js")
    )

    assert validate_assets() == {
        "planner",
        "dispatch_tool",
        "item_search",
        "shopping_summary",
    }
    for forbidden in ("agent_bootstrap", "agent-runs", "benchmark_summary", "http://", "https://"):
        assert forbidden not in static_runtime
