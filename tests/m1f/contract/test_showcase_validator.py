"""Public contract tests for the explicit M1f asset validator."""

from __future__ import annotations

import json

import pytest

import scripts.validate_m1f_showcase as validator

pytestmark = pytest.mark.contract


@pytest.mark.spec("GLO-M1F-P0-003", "M1F-AC-003", "GLO-M1F-NFR-001")
def test_validator_cli_emits_one_safe_completion_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        validator,
        "validate_assets",
        lambda: frozenset({"planner", "shopping_summary"}),
    )

    assert validator.main() == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.splitlines() == ['{"executed_tool_count":2,"status":"COMPLETED"}']


@pytest.mark.spec("GLO-M1F-P0-003", "M1F-AC-003", "GLO-M1F-NFR-002")
def test_validator_cli_hides_internal_failure_details(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret_path = "/private/m1f/showcase/assets.json"

    def broken_validator() -> frozenset[str]:
        raise validator.ShowcaseValidationError(secret_path)

    monkeypatch.setattr(validator, "validate_assets", broken_validator)
    assert validator.main() == 1

    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {"code": "M1F_SHOWCASE_INVALID", "status": "FAILED"}
    assert secret_path not in captured.out
