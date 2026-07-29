from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import glodex.capture.bootstrap as capture_bootstrap
import glodex.cli as cli
from glodex.capture.config import CaptureRequest
from glodex.capture.contracts import (
    CaptureIssue,
    CaptureIssueCode,
    FailedCaptureReceipt,
    PublishedCaptureReceipt,
    RejectedCaptureReceipt,
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.spec(
        "GLO-M1B-P0-001",
        "GLO-M1B-P0-003",
        "M1B-AC-002",
        "M1B-AC-004",
        "M1B-AC-005",
        "GLO-M1B-NFR-001",
        "GLO-M1B-NFR-004",
        "GLO-M1B-NFR-006",
    ),
]

_CAPTURE_ID = "capture-fedcba9876543210fedcba9876543210"


def _single_json_line(output: str) -> dict[str, Any]:
    lines = output.splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert isinstance(parsed, dict)
    return parsed


@pytest.mark.parametrize(
    "arguments",
    [
        ("capture-provider",),
        (
            "capture-provider",
            "--query",
            "sentinel-query",
            "--output-root",
            "/sentinel/output",
        ),
        (
            "capture-provider",
            "--live",
            "--output-root",
            "/sentinel/output",
        ),
        (
            "capture-provider",
            "--live",
            "--query",
            "sentinel-query",
        ),
        (
            "capture-provider",
            "--live",
            "--query",
            "sentinel-query",
            "--output-root",
            "/sentinel/output",
            "--provider",
            "sentinel-provider",
        ),
    ],
)
def test_capture_usage_errors_are_safe_capture_rejections(
    arguments: tuple[str, ...],
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(arguments)
    captured = capsys.readouterr()
    payload = _single_json_line(captured.out)

    assert exit_code == 2
    assert set(payload) == {"schema_version", "status", "request_count", "issues"}
    assert payload["status"] == "REJECTED"
    assert payload["request_count"] == 0
    assert payload["issues"] == [{"code": "CAPTURE_INPUT_INVALID", "count": 1}]
    assert "type" not in payload
    assert "errors" not in payload
    assert captured.err == "glodex: capture rejected (CAPTURE_INPUT_INVALID)\n"
    for sentinel in ("sentinel-query", "/sentinel/output", "sentinel-provider"):
        assert sentinel not in captured.out
        assert sentinel not in captured.err


def test_capture_dispatches_before_loading_normal_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env_file = tmp_path / "explicit.env"
    requests: list[CaptureRequest] = []
    receipt = PublishedCaptureReceipt(
        capture_id=_CAPTURE_ID,
        received_record_count=1,
        snapshot_version=_CAPTURE_ID,
        captured_at=datetime(2026, 7, 29, 9, 0, tzinfo=UTC),
        published_product_count=1,
        published_offer_count=1,
        quarantine_count=0,
    )

    def fake_run_capture(request: CaptureRequest) -> PublishedCaptureReceipt:
        requests.append(request)
        return receipt

    def forbidden_load_config(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("normal configuration must not load for Capture")

    monkeypatch.setattr(capture_bootstrap, "run_capture", fake_run_capture)
    monkeypatch.setattr(cli, "load_config", forbidden_load_config)

    exit_code = cli.main(
        (
            "capture-provider",
            "--live",
            "--query",
            "smartphone",
            "--env-file",
            str(env_file),
            "--output-root",
            str(tmp_path / "external"),
        )
    )
    captured = capsys.readouterr()
    payload = _single_json_line(captured.out)

    assert exit_code == 0
    assert captured.err == ""
    assert requests == [
        CaptureRequest(
            live=True,
            query="smartphone",
            env_file=env_file,
            output_root=tmp_path / "external",
        )
    ]
    assert set(payload) == {
        "schema_version",
        "status",
        "request_count",
        "issues",
        "provider_id",
        "capture_id",
        "marketplace",
        "currency",
        "category",
        "received_record_count",
        "snapshot_version",
        "captured_at",
        "published_product_count",
        "published_offer_count",
        "quarantine_count",
    }
    assert payload["status"] == "PUBLISHED"


@pytest.mark.parametrize(
    ("receipt", "expected_exit", "expected_stderr", "expected_keys"),
    [
        (
            RejectedCaptureReceipt(
                issues=(
                    CaptureIssue(
                        code=CaptureIssueCode.CAPTURE_CREDENTIALS_MISSING,
                    ),
                )
            ),
            2,
            "glodex: capture rejected (CAPTURE_CREDENTIALS_MISSING)\n",
            {"schema_version", "status", "request_count", "issues"},
        ),
        (
            FailedCaptureReceipt(
                request_count=2,
                issues=(
                    CaptureIssue(
                        code=CaptureIssueCode.PROVIDER_UNAVAILABLE,
                    ),
                ),
                capture_id=_CAPTURE_ID,
                received_record_count=0,
            ),
            1,
            "glodex: capture failed (PROVIDER_UNAVAILABLE)\n",
            {
                "schema_version",
                "status",
                "request_count",
                "issues",
                "provider_id",
                "capture_id",
                "marketplace",
                "currency",
                "category",
                "received_record_count",
            },
        ),
    ],
)
def test_capture_receipt_exit_stderr_and_keys_are_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    receipt: RejectedCaptureReceipt | FailedCaptureReceipt,
    expected_exit: int,
    expected_stderr: str,
    expected_keys: set[str],
) -> None:
    monkeypatch.setattr(capture_bootstrap, "run_capture", lambda _request: receipt)

    exit_code = cli.main(
        (
            "capture-provider",
            "--live",
            "--query",
            "smartphone",
            "--output-root",
            str(tmp_path / "external"),
        )
    )
    captured = capsys.readouterr()
    payload = _single_json_line(captured.out)

    assert exit_code == expected_exit
    assert set(payload) == expected_keys
    assert captured.err == expected_stderr


def test_capture_help_discloses_that_query_is_sent_to_ebay(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(("capture-provider", "--help"))

    captured = capsys.readouterr()
    assert raised.value.code == 0
    assert "sent to eBay" in captured.out
    assert captured.err == ""


def test_normal_cli_command_does_not_dispatch_capture(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def forbidden_capture(_request: CaptureRequest) -> PublishedCaptureReceipt:
        raise AssertionError("normal CLI path must not run Capture")

    monkeypatch.setattr(capture_bootstrap, "run_capture", forbidden_capture)

    exit_code = cli.main(("demo",))
    captured = capsys.readouterr()
    payload = _single_json_line(captured.out)

    assert exit_code == 0
    assert payload["status"] == "COMPLETED"
    assert captured.err == ""
