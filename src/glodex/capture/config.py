"""Fixed eBay profile, credentials, and zero-I/O-before-approval preflight."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from glodex.capture.contracts import (
    CaptureIssue,
    CaptureIssueCode,
    RejectedCaptureReceipt,
)
from glodex.domain.catalog import EntityKind

_APPROVED_CREDENTIAL_NAMES: Final = ("EBAY_APP_ID", "EBAY_CERT_ID")
_DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True, slots=True)
class EbayCaptureProfile:
    """The one production Provider operation approved for M1b."""

    provider_id: str = field(default="ebay-browse", init=False)
    https_host: str = field(default="api.ebay.com", init=False)
    https_port: int = field(default=443, init=False)
    auth_path: str = field(default="/identity/v1/oauth2/token", init=False)
    search_path: str = field(
        default="/buy/browse/v1/item_summary/search",
        init=False,
    )
    oauth_scope: str = field(
        default="https://api.ebay.com/oauth/api_scope",
        init=False,
    )
    marketplace: str = field(default="EBAY_US", init=False)
    currency: str = field(default="USD", init=False)
    category_id: str = field(default="9355", init=False)
    canonical_category: str = field(default="phone", init=False)
    entity_kind: EntityKind = field(default=EntityKind.PRIMARY_PRODUCT, init=False)
    buying_option_filter: str = field(
        default="buyingOptions:{FIXED_PRICE}",
        init=False,
    )
    offset: int = field(default=0, init=False)
    limit: int = field(default=10, init=False)
    query_max_code_points: int = field(default=100, init=False)
    oauth_body_limit_bytes: int = field(default=64 * 1024, init=False)
    search_body_limit_bytes: int = field(default=256 * 1024, init=False)
    oauth_timeout_seconds: int = field(default=10, init=False)
    search_timeout_seconds: int = field(default=15, init=False)
    capture_deadline_seconds: int = field(default=30, init=False)


EBAY_CAPTURE_PROFILE: Final = EbayCaptureProfile()


@dataclass(frozen=True, slots=True)
class EbayCredentials:
    """The only two runtime secrets accepted by the eBay adapter."""

    app_id: str = field(repr=False)
    cert_id: str = field(repr=False)

    def __post_init__(self) -> None:
        for value in (self.app_id, self.cert_id):
            if (
                type(value) is not str
                or not value
                or value != value.strip()
                or any(character in value for character in ("\0", "\r", "\n"))
                or any(0xD800 <= ord(character) <= 0xDFFF for character in value)
            ):
                raise ValueError("eBay credentials must be non-empty single-line strings")


@dataclass(frozen=True, slots=True)
class CaptureRequest:
    """Untrusted local inputs supplied by the operator CLI."""

    live: bool
    query: str
    output_root: Path
    env_file: Path | None = None


@dataclass(frozen=True, slots=True)
class PreparedCapture:
    """Validated local inputs ready for Capture identity allocation and outbound I/O."""

    query: str
    output_root: Path
    credentials: EbayCredentials = field(repr=False)
    profile: EbayCaptureProfile = EBAY_CAPTURE_PROFILE


type CapturePreflightResult = PreparedCapture | RejectedCaptureReceipt


def _rejected(code: CaptureIssueCode) -> RejectedCaptureReceipt:
    return RejectedCaptureReceipt(issues=(CaptureIssue(code=code),))


def _validated_query(request: CaptureRequest) -> str | None:
    if request.live is not True or type(request.query) is not str:
        return None
    query = request.query.strip()
    if (
        not query
        or len(query) > EBAY_CAPTURE_PROFILE.query_max_code_points
        or any(character in query for character in ("\0", "\r", "\n", "*"))
        or any(0xD800 <= ord(character) <= 0xDFFF for character in query)
    ):
        return None
    return query


def _validated_output_root(path: object, project_root: Path) -> Path | None:
    if not isinstance(path, Path) or not path.is_absolute() or path.is_symlink():
        return None
    try:
        resolved = path.resolve(strict=True)
        checkout = project_root.resolve(strict=True)
        mode = stat.S_IMODE(resolved.stat().st_mode)
    except OSError:
        return None
    if (
        not resolved.is_dir()
        or mode != 0o700
        or not os.access(resolved, os.W_OK | os.X_OK)
        or resolved == checkout
        or checkout in resolved.parents
    ):
        return None
    return resolved


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ValueError("credential file is not a regular file")
    values: dict[str, str] = {}
    with path.open(encoding="utf-8") as stream:
        for raw_line in stream:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, raw_value = line.partition("=")
            if not separator:
                raise ValueError("credential file must contain KEY=VALUE lines")
            normalized_key = key.strip()
            if normalized_key in _APPROVED_CREDENTIAL_NAMES:
                values[normalized_key] = raw_value.strip()
    return values


def _load_credentials(
    *,
    environ: Mapping[str, str],
    env_file: object,
) -> EbayCredentials | CaptureIssueCode:
    values: dict[str, str] = {}
    if env_file is not None:
        if not isinstance(env_file, Path):
            return CaptureIssueCode.CAPTURE_CONFIG_INVALID
        try:
            values.update(_read_env_file(env_file))
        except (OSError, UnicodeError, ValueError):
            return CaptureIssueCode.CAPTURE_CONFIG_INVALID

    for name in _APPROVED_CREDENTIAL_NAMES:
        if name in environ:
            values[name] = environ[name].strip()
    try:
        return EbayCredentials(
            app_id=values.get("EBAY_APP_ID", ""),
            cert_id=values.get("EBAY_CERT_ID", ""),
        )
    except ValueError:
        return CaptureIssueCode.CAPTURE_CREDENTIALS_MISSING


def preflight_capture(
    request: CaptureRequest,
    *,
    environ: Mapping[str, str] | None = None,
    project_root: Path | None = None,
) -> CapturePreflightResult:
    """Validate all local inputs without allocating an ID or performing outbound I/O."""

    if type(request) is not CaptureRequest:
        return _rejected(CaptureIssueCode.CAPTURE_INPUT_INVALID)
    query = _validated_query(request)
    if query is None:
        return _rejected(CaptureIssueCode.CAPTURE_INPUT_INVALID)

    root = _validated_output_root(
        request.output_root,
        _DEFAULT_PROJECT_ROOT if project_root is None else project_root,
    )
    if root is None:
        return _rejected(CaptureIssueCode.CAPTURE_CONFIG_INVALID)

    credentials = _load_credentials(
        environ=os.environ if environ is None else environ,
        env_file=request.env_file,
    )
    if isinstance(credentials, CaptureIssueCode):
        return _rejected(credentials)
    return PreparedCapture(
        query=query,
        output_root=root,
        credentials=credentials,
    )


__all__ = [
    "EBAY_CAPTURE_PROFILE",
    "CapturePreflightResult",
    "CaptureRequest",
    "EbayCaptureProfile",
    "EbayCredentials",
    "PreparedCapture",
    "preflight_capture",
]
