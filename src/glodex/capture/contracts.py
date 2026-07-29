"""Strict public receipts for the operator-only Capture boundary."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Final, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

CaptureId = Annotated[
    str,
    StringConstraints(pattern=r"^capture-[0-9a-f]{32}$"),
]
_Count = Annotated[int, Field(ge=0)]
_BoundedCount = Annotated[int, Field(ge=0, le=10)]
_PositiveCount = Annotated[int, Field(gt=0)]
_SCHEMA_VERSION: Final = "glodex.capture-receipt.v1"


class CaptureIssueCode(StrEnum):
    """Closed, stable issue codes allowed by Capture receipt v1."""

    CAPTURE_CONFIG_INVALID = "CAPTURE_CONFIG_INVALID"
    CAPTURE_CREDENTIALS_MISSING = "CAPTURE_CREDENTIALS_MISSING"
    CAPTURE_INPUT_INVALID = "CAPTURE_INPUT_INVALID"
    PROVIDER_TARGET_REJECTED = "PROVIDER_TARGET_REJECTED"
    PROVIDER_AUTH_REJECTED = "PROVIDER_AUTH_REJECTED"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_RESPONSE_INVALID = "PROVIDER_RESPONSE_INVALID"
    PROVIDER_RESPONSE_INCOMPLETE = "PROVIDER_RESPONSE_INCOMPLETE"
    PROVIDER_RESPONSE_LIMIT = "PROVIDER_RESPONSE_LIMIT"
    SNAPSHOT_VALIDATION_FAILED = "SNAPSHOT_VALIDATION_FAILED"
    SNAPSHOT_PUBLISH_FAILED = "SNAPSHOT_PUBLISH_FAILED"
    RECORD_QUARANTINED = "RECORD_QUARANTINED"


REJECTED_ISSUE_CODES: Final = frozenset(
    {
        CaptureIssueCode.CAPTURE_CONFIG_INVALID,
        CaptureIssueCode.CAPTURE_CREDENTIALS_MISSING,
        CaptureIssueCode.CAPTURE_INPUT_INVALID,
    }
)
PROVIDER_FAILURE_CODES: Final = frozenset(
    {
        CaptureIssueCode.PROVIDER_TARGET_REJECTED,
        CaptureIssueCode.PROVIDER_AUTH_REJECTED,
        CaptureIssueCode.PROVIDER_TIMEOUT,
        CaptureIssueCode.PROVIDER_RATE_LIMITED,
        CaptureIssueCode.PROVIDER_UNAVAILABLE,
        CaptureIssueCode.PROVIDER_RESPONSE_INVALID,
        CaptureIssueCode.PROVIDER_RESPONSE_INCOMPLETE,
        CaptureIssueCode.PROVIDER_RESPONSE_LIMIT,
    }
)
SNAPSHOT_FAILURE_CODES: Final = frozenset(
    {
        CaptureIssueCode.SNAPSHOT_VALIDATION_FAILED,
        CaptureIssueCode.SNAPSHOT_PUBLISH_FAILED,
    }
)
FAILED_ISSUE_CODES: Final = PROVIDER_FAILURE_CODES | SNAPSHOT_FAILURE_CODES
PUBLISHED_ISSUE_CODES: Final = frozenset({CaptureIssueCode.RECORD_QUARANTINED})


class _FrozenCaptureDTO(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class CaptureIssue(_FrozenCaptureDTO):
    """One stable issue and its merged positive occurrence count."""

    code: CaptureIssueCode
    count: _PositiveCount = 1


def build_capture_issues(
    issues: Iterable[CaptureIssueCode | CaptureIssue],
) -> tuple[CaptureIssue, ...]:
    """Merge repeated codes and return the canonical code-sorted tuple."""

    counts: Counter[CaptureIssueCode] = Counter()
    for issue in issues:
        if type(issue) is CaptureIssueCode:
            counts[issue] += 1
        elif type(issue) is CaptureIssue:
            counts[issue.code] += issue.count
        else:
            raise TypeError("capture issues must be CaptureIssueCode or CaptureIssue")
    return tuple(
        CaptureIssue(code=code, count=counts[code])
        for code in sorted(counts, key=lambda item: item.value)
    )


def _require_canonical_issues(
    issues: tuple[CaptureIssue, ...],
    allowed_codes: frozenset[CaptureIssueCode],
) -> None:
    codes = tuple(issue.code for issue in issues)
    if any(code not in allowed_codes for code in codes):
        raise ValueError("receipt contains an issue code invalid for its status")
    if codes != tuple(sorted(codes, key=lambda item: item.value)):
        raise ValueError("receipt issues must be sorted by code")
    if len(codes) != len(set(codes)):
        raise ValueError("receipt issue codes must be unique")


class RejectedCaptureReceipt(_FrozenCaptureDTO):
    """A local preflight rejection with no allocated Capture identity."""

    schema_version: Literal["glodex.capture-receipt.v1"] = _SCHEMA_VERSION
    status: Literal["REJECTED"] = "REJECTED"
    request_count: Literal[0] = 0
    issues: Annotated[tuple[CaptureIssue, ...], Field(min_length=1)]

    @field_validator("request_count", mode="before")
    @classmethod
    def request_count_must_be_an_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("request_count must be an integer")
        return value

    @model_validator(mode="after")
    def validate_rejected_issues(self) -> Self:
        _require_canonical_issues(self.issues, REJECTED_ISSUE_CODES)
        return self


class FailedCaptureReceipt(_FrozenCaptureDTO):
    """An established Capture that failed before publishing a snapshot."""

    schema_version: Literal["glodex.capture-receipt.v1"] = _SCHEMA_VERSION
    status: Literal["FAILED"] = "FAILED"
    request_count: Annotated[int, Field(ge=1, le=2)]
    issues: Annotated[tuple[CaptureIssue, ...], Field(min_length=1)]
    provider_id: Literal["ebay-browse"] = "ebay-browse"
    capture_id: CaptureId
    marketplace: Literal["EBAY_US"] = "EBAY_US"
    currency: Literal["USD"] = "USD"
    category: Literal["phone"] = "phone"
    received_record_count: _Count = 0

    @model_validator(mode="after")
    def validate_failed_issues(self) -> Self:
        _require_canonical_issues(self.issues, FAILED_ISSUE_CODES)
        return self


class PublishedCaptureReceipt(_FrozenCaptureDTO):
    """A Capture whose validated immutable snapshot passed the rename commit point."""

    schema_version: Literal["glodex.capture-receipt.v1"] = _SCHEMA_VERSION
    status: Literal["PUBLISHED"] = "PUBLISHED"
    request_count: Literal[2] = 2
    issues: tuple[CaptureIssue, ...] = ()
    provider_id: Literal["ebay-browse"] = "ebay-browse"
    capture_id: CaptureId
    marketplace: Literal["EBAY_US"] = "EBAY_US"
    currency: Literal["USD"] = "USD"
    category: Literal["phone"] = "phone"
    received_record_count: _BoundedCount
    snapshot_version: CaptureId
    captured_at: datetime
    published_product_count: _BoundedCount
    published_offer_count: _BoundedCount
    quarantine_count: _BoundedCount

    @field_validator("request_count", mode="before")
    @classmethod
    def request_count_must_be_an_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("request_count must be an integer")
        return value

    @field_validator("captured_at")
    @classmethod
    def captured_at_must_be_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("captured_at must be a UTC datetime")
        return value

    @model_validator(mode="after")
    def validate_published_invariants(self) -> Self:
        _require_canonical_issues(self.issues, PUBLISHED_ISSUE_CODES)
        if self.snapshot_version != self.capture_id:
            raise ValueError("snapshot_version must equal capture_id")
        if self.published_product_count != self.published_offer_count:
            raise ValueError("published Product and Offer counts must match")
        if self.published_product_count + self.quarantine_count != self.received_record_count:
            raise ValueError("published plus quarantine counts must equal received count")
        if self.received_record_count > 0 and self.published_product_count == 0:
            raise ValueError("a non-empty fully quarantined page cannot be published")

        if self.quarantine_count == 0:
            if self.issues:
                raise ValueError("zero quarantine count requires no published issues")
        elif (
            len(self.issues) != 1
            or self.issues[0].code is not CaptureIssueCode.RECORD_QUARANTINED
            or self.issues[0].count != self.quarantine_count
        ):
            raise ValueError("quarantine issue count must equal quarantine_count")
        return self


type CaptureReceipt = RejectedCaptureReceipt | FailedCaptureReceipt | PublishedCaptureReceipt


def capture_exit_code_for(receipt: CaptureReceipt) -> Literal[0, 1, 2]:
    """Map every receipt variant to its approved process exit code."""

    if type(receipt) is PublishedCaptureReceipt:
        return 0
    if type(receipt) is FailedCaptureReceipt:
        return 1
    if type(receipt) is RejectedCaptureReceipt:
        return 2
    raise TypeError("unsupported Capture receipt type")


__all__ = [
    "CaptureId",
    "CaptureIssue",
    "CaptureIssueCode",
    "CaptureReceipt",
    "FailedCaptureReceipt",
    "PublishedCaptureReceipt",
    "RejectedCaptureReceipt",
    "build_capture_issues",
    "capture_exit_code_for",
]
