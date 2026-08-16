"""README and repository-facing safety evidence for RetrievalModel operations."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.nfr,
    pytest.mark.spec(
        "GLO-RETRIEVAL_MODEL-P0-004",
        "GLO-RETRIEVAL_MODEL-P0-007",
        "GLO-RETRIEVAL_MODEL-NFR-001",
        "GLO-RETRIEVAL_MODEL-NFR-004",
        "GLO-RETRIEVAL_MODEL-NFR-005",
        "GLO-RETRIEVAL_MODEL-NFR-006",
    ),
]

_README = Path(__file__).parents[3] / "README.md"


def test_retrieval_model_readme_documents_only_safe_operator_shapes_and_offline_boundary() -> None:
    text = _README.read_text(encoding="utf-8")

    for required in (
        "### Retrieval model service A100 BGE retrieval model service",
        "model-service serve --manifest <private-manifest>",
        "model-service verify --live",
        "product-index build --live",
        "agent-api serve --live",
        "scripts/verify_retrieval_model.py",
        "Ctrl-C",
    ):
        assert required in text

    assert "不记录 remote host、模型路径/hash、SSH command、credential" in text
    assert "retrieval_model-profile" not in text
