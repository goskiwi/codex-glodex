"""M7 source boundaries: one LLM judge path and no training/control-plane expansion."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.architecture,
    pytest.mark.spec("GLO-M7-NFR-003", "GLO-M7-NFR-004", "GLO-M7-NFR-005"),
]

_ROOT = Path(__file__).resolve().parents[3]


def test_m7_has_no_compat_provider_fallback_training_or_realtime_runtime() -> None:
    sources = (
        _ROOT / "src/glodex/quality/runtime.py",
        _ROOT / "src/glodex/quality/llm_judge.py",
        _ROOT / "src/glodex/quality/offline.py",
        _ROOT / "src/glodex/cli.py",
        _ROOT / "src/glodex/api/durable_client.py",
    )
    rendered = "\n".join(path.read_text(encoding="utf-8").casefold() for path in sources)
    for forbidden in (
        "dashscope",
        "retry(",
        "websocket",
        "langchain",
        "langgraph",
        "trainer",
        "three-tower",
    ):
        assert forbidden not in rendered
