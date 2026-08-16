"""Offline M5 delivery-boundary evidence; live LLM acceptance is separate."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.architecture,
    pytest.mark.spec(
        "GLO-M5-P0-007",
        "GLO-M5-P0-008",
        "GLO-M5-NFR-005",
        "GLO-M5-NFR-006",
    ),
]

_ROOT = Path(__file__).parents[2]


def test_m5_public_path_has_no_legacy_profile_or_browser_storage_compatibility() -> None:
    active_paths = (
        _ROOT / "src/glodex/api/durable.py",
        _ROOT / "src/glodex/api/console_app.py",
        _ROOT / "src/glodex/api/durable_client.py",
        _ROOT / "src/glodex/agent/graph.py",
        _ROOT / "src/glodex/composition/agent_api.py",
        _ROOT / "src/glodex/cli.py",
    )
    active_source = "\n".join(path.read_text(encoding="utf-8") for path in active_paths)
    frontend_source = (_ROOT / "frontend/src/service/agent.ts").read_text(encoding="utf-8")
    package_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (_ROOT / "src/glodex").rglob("*.py")
        if path.name != "migrations.py"
    ).casefold()

    assert "profile_id" not in active_source
    assert "durable-profile" not in active_source and "retrieval_model-profile" not in active_source
    assert "localStorage" not in frontend_source and "sessionStorage" not in frontend_source
    assert "dashscope" not in package_source
    assert not (_ROOT / "src/glodex/memory/profile.py").exists()
    assert not (_ROOT / "src/glodex/memory/legacy_profile.py").exists()
