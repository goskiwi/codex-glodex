"""M5b architectural evidence for the deliberate no-compatibility cutover."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.architecture,
    pytest.mark.spec("GLO-M5B-P0-006", "GLO-M5B-NFR-004"),
]

_ROOT = Path(__file__).parents[3]


def test_production_memory_path_has_no_old_schema_or_generic_store_compatibility() -> None:
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            _ROOT / "src/glodex/memory",
            _ROOT / "src/glodex/api",
            _ROOT / "src/glodex/infrastructure/postgres.py",
            _ROOT / "frontend/src/App.tsx",
            _ROOT / "frontend/src/protocol.ts",
        )
        if path.is_file()
    )
    memory_sources = "\n".join(
        path.read_text(encoding="utf-8") for path in (_ROOT / "src/glodex/memory").glob("*.py")
    )

    assert "list_user_memory" not in memory_sources
    assert "upsert_user_memory" not in sources
    assert "isMemoryList" not in sources


def test_m5b_does_not_add_an_unapproved_stack() -> None:
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            _ROOT / "src/glodex/memory",
            _ROOT / "src/glodex/api/memory_contracts.py",
            _ROOT / "src/glodex/infrastructure/migrations.py",
        )
        if path.is_file()
    ).casefold()

    for forbidden in ("dashscope", "websocket", "observation_ledger", "prompt_cache_hit"):
        assert forbidden not in sources
