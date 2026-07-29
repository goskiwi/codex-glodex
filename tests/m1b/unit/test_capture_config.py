from __future__ import annotations

from pathlib import Path

import pytest

from glodex.capture.config import (
    EBAY_CAPTURE_PROFILE,
    CaptureRequest,
    EbayCaptureProfile,
    PreparedCapture,
    preflight_capture,
)
from glodex.capture.contracts import CaptureIssueCode, RejectedCaptureReceipt

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec(
        "GLO-M1B-P0-001",
        "GLO-M1B-P0-002",
        "M1B-AC-001",
        "M1B-AC-005",
        "GLO-M1B-NFR-004",
        "GLO-M1B-NFR-005",
    ),
]


def _safe_root(tmp_path: Path) -> tuple[Path, Path]:
    project_root = tmp_path / "checkout"
    output_root = tmp_path / "snapshots"
    project_root.mkdir(mode=0o700)
    output_root.mkdir(mode=0o700)
    return project_root, output_root


def _request(output_root: Path, *, env_file: Path | None = None) -> CaptureRequest:
    return CaptureRequest(
        live=True,
        query="  smartphone  ",
        output_root=output_root,
        env_file=env_file,
    )


def test_preflight_uses_trimmed_query_and_environment_credentials(tmp_path: Path) -> None:
    project_root, output_root = _safe_root(tmp_path)

    result = preflight_capture(
        _request(output_root),
        environ={"EBAY_APP_ID": "app-sentinel", "EBAY_CERT_ID": "cert-sentinel"},
        project_root=project_root,
    )

    assert isinstance(result, PreparedCapture)
    assert result.query == "smartphone"
    assert result.output_root == output_root.resolve()
    assert result.profile is EBAY_CAPTURE_PROFILE
    assert result.credentials.app_id == "app-sentinel"
    assert result.credentials.cert_id == "cert-sentinel"
    assert "app-sentinel" not in repr(result.credentials)
    assert "cert-sentinel" not in repr(result.credentials)


def test_explicit_env_file_is_simple_and_process_environment_wins(tmp_path: Path) -> None:
    project_root, output_root = _safe_root(tmp_path)
    env_file = tmp_path / "provider.env"
    env_file.write_text(
        "# local credentials\nUNRELATED=value\nEBAY_APP_ID=file-app\nEBAY_CERT_ID=file-cert\n",
        encoding="utf-8",
    )

    result = preflight_capture(
        _request(output_root, env_file=env_file),
        environ={"EBAY_APP_ID": "process-app"},
        project_root=project_root,
    )

    assert isinstance(result, PreparedCapture)
    assert result.credentials.app_id == "process-app"
    assert result.credentials.cert_id == "file-cert"


@pytest.mark.parametrize(
    "request_factory",
    [
        lambda root: CaptureRequest(live=False, query="phone", output_root=root),
        lambda root: CaptureRequest(live=True, query=" ", output_root=root),
        lambda root: CaptureRequest(live=True, query="bad\nquery", output_root=root),
        lambda root: CaptureRequest(live=True, query="*", output_root=root),
        lambda root: CaptureRequest(live=True, query="x" * 101, output_root=root),
        lambda root: CaptureRequest(live=True, query="\ud800", output_root=root),
    ],
)
def test_input_preflight_rejects_before_credentials(
    tmp_path: Path,
    request_factory: object,
) -> None:
    project_root, output_root = _safe_root(tmp_path)
    request = request_factory(output_root)  # type: ignore[operator]

    result = preflight_capture(request, environ={}, project_root=project_root)

    assert isinstance(result, RejectedCaptureReceipt)
    assert result.issues[0].code is CaptureIssueCode.CAPTURE_INPUT_INVALID


def test_output_root_must_be_absolute_external_real_and_private(tmp_path: Path) -> None:
    project_root, _output_root = _safe_root(tmp_path)
    inside_checkout = project_root / "snapshots"
    inside_checkout.mkdir(mode=0o700)
    wrong_mode = tmp_path / "wide"
    wrong_mode.mkdir(mode=0o755)
    wrong_mode.chmod(0o755)

    for candidate in (Path("relative"), inside_checkout, wrong_mode):
        result = preflight_capture(
            _request(candidate),
            environ={"EBAY_APP_ID": "app", "EBAY_CERT_ID": "cert"},
            project_root=project_root,
        )
        assert isinstance(result, RejectedCaptureReceipt)
        assert result.issues[0].code is CaptureIssueCode.CAPTURE_CONFIG_INVALID


def test_missing_credentials_and_invalid_env_file_have_stable_codes(tmp_path: Path) -> None:
    project_root, output_root = _safe_root(tmp_path)
    missing_file = tmp_path / "missing.env"

    missing_credentials = preflight_capture(
        _request(output_root),
        environ={},
        project_root=project_root,
    )
    invalid_file = preflight_capture(
        _request(output_root, env_file=missing_file),
        environ={},
        project_root=project_root,
    )
    invalid_credentials = preflight_capture(
        _request(output_root),
        environ={"EBAY_APP_ID": "\ud800", "EBAY_CERT_ID": "cert"},
        project_root=project_root,
    )

    assert isinstance(missing_credentials, RejectedCaptureReceipt)
    assert missing_credentials.issues[0].code is CaptureIssueCode.CAPTURE_CREDENTIALS_MISSING
    assert isinstance(invalid_file, RejectedCaptureReceipt)
    assert invalid_file.issues[0].code is CaptureIssueCode.CAPTURE_CONFIG_INVALID
    assert str(missing_file) not in repr(invalid_file)
    assert isinstance(invalid_credentials, RejectedCaptureReceipt)
    assert invalid_credentials.issues[0].code is CaptureIssueCode.CAPTURE_CREDENTIALS_MISSING


def test_ebay_profile_is_fixed_and_not_user_configurable() -> None:
    profile = EBAY_CAPTURE_PROFILE

    assert profile.provider_id == "ebay-browse"
    assert profile.https_host == "api.ebay.com"
    assert profile.marketplace == "EBAY_US"
    assert profile.currency == "USD"
    assert profile.category_id == "9355"
    assert profile.canonical_category == "phone"
    assert profile.limit == 10
    assert profile.oauth_body_limit_bytes == 64 * 1024
    assert profile.search_body_limit_bytes == 256 * 1024

    with pytest.raises(TypeError):
        EbayCaptureProfile(https_host="example.invalid")  # type: ignore[call-arg]
