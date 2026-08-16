"""M6 source-level boundaries that are faster and safer to verify without services."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = [pytest.mark.architecture, pytest.mark.spec("GLO-M6-NFR-005")]

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_m6_has_no_compatibility_provider_or_realtime_runtime_dependency() -> None:
    sources = (
        _PROJECT_ROOT / "src/glodex/observability/runtime.py",
        _PROJECT_ROOT / "src/glodex/observability/exporter.py",
        _PROJECT_ROOT / "src/glodex/api/observability_contracts.py",
        _PROJECT_ROOT / "src/glodex/api/durable_client.py",
    )
    rendered = "\n".join(path.read_text(encoding="utf-8").casefold() for path in sources)
    for forbidden in ("dashscope", "websocket", "langchain", "langgraph", "retry("):
        assert forbidden not in rendered
