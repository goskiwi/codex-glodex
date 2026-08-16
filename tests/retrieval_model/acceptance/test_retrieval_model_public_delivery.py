"""Black-box delivery evidence for the explicit retrieval-model command."""

from __future__ import annotations

import json

import pytest

from glodex.cli import main

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.spec(
        "RETRIEVAL_MODEL-AC-001",
        "RETRIEVAL_MODEL-AC-002",
        "RETRIEVAL_MODEL-AC-003",
        "RETRIEVAL_MODEL-AC-004",
        "RETRIEVAL_MODEL-AC-005",
        "RETRIEVAL_MODEL-AC-006",
        "GLO-RETRIEVAL_MODEL-P0-007",
        "GLO-RETRIEVAL_MODEL-NFR-001",
        "GLO-RETRIEVAL_MODEL-NFR-005",
        "GLO-RETRIEVAL_MODEL-NFR-006",
    ),
]


def test_model_command_requires_explicit_live(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(("model-service", "verify")) == 2
    assert json.loads(capsys.readouterr().out) == {
        "code": "RETRIEVAL_MODEL_LIVE_REQUIRED",
        "status": "FAILED",
    }
