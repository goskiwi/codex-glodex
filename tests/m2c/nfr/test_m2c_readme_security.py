"""README and repository-facing safety evidence for M2c operations."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.nfr,
    pytest.mark.spec(
        "GLO-M2C-P0-007",
        "GLO-M2C-NFR-001",
        "GLO-M2C-NFR-004",
        "GLO-M2C-NFR-005",
        "GLO-M2C-NFR-006",
    ),
]

_README = Path(__file__).parents[3] / "README.md"


def test_m2c_readme_documents_only_safe_operator_shapes_and_offline_boundary() -> None:
    text = _README.read_text(encoding="utf-8")

    for required in (
        "### M2c A100 BGE retrieval model service",
        "m2c-gpu-service --manifest <private-manifest>",
        "m2c-model-verify --live",
        "m2c-index --action build --snapshot m1d-demo-v1 --live",
        "m2c-profile --action set --live",
        "m2c-agent-demo --live",
        "m2c-eval-esci --live",
        "scripts/verify_m2c.py",
        "Ctrl-C",
    ):
        assert required in text

    assert "不记录 remote host、模型路径/hash、SSH command、credential" in text
