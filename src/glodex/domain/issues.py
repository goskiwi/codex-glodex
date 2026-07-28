"""Stable, immutable catalog issue values."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class IssueDisposition(StrEnum):
    FATAL = "fatal"
    QUARANTINE = "quarantine"


class IssueStage(StrEnum):
    MANIFEST = "manifest"
    PRODUCTS = "products"
    OFFERS = "offers"
    EVIDENCE = "evidence"
    EXCHANGE_RATES = "exchange_rates"
    AGGREGATION = "aggregation"


class IssueCode(StrEnum):
    MANIFEST_MISSING = "catalog.manifest-missing"
    MANIFEST_INVALID = "catalog.manifest-invalid"
    SNAPSHOT_VERSION_MISMATCH = "catalog.snapshot-version-mismatch"
    PATH_INVALID = "catalog.path-invalid"
    FILE_TOO_LARGE = "catalog.file-too-large"
    RECORD_LIMIT_EXCEEDED = "catalog.record-limit-exceeded"
    HASH_MISMATCH = "catalog.hash-mismatch"
    RECORD_COUNT_MISMATCH = "catalog.record-count-mismatch"
    CORE_JSON_INVALID = "catalog.core-json-invalid"
    CURRENCY_UNSUPPORTED = "catalog.currency-unsupported"
    INVALID_RECORD = "catalog.invalid-record"
    MISSING_IDENTITY = "catalog.missing-identity"
    MISSING_SOURCE = "catalog.missing-source"
    DUPLICATE_EVIDENCE = "catalog.duplicate-evidence"
    EVIDENCE_NOT_FOUND = "catalog.evidence-not-found"
    EVIDENCE_ENTITY_MISMATCH = "catalog.evidence-entity-mismatch"
    ORPHAN_OFFER = "catalog.orphan-offer"
    IDENTITY_CONFLICT = "catalog.identity-conflict"
    DUPLICATE_OFFER = "catalog.duplicate-offer"


IssueDetailValue = str | int | bool | None


@dataclass(frozen=True, slots=True)
class IssueDetail:
    key: str
    value: IssueDetailValue

    def __post_init__(self) -> None:
        _require_text(self.key, "issue detail key", maximum=128)
        if self.value is not None and type(self.value) not in {str, int, bool}:
            raise TypeError("issue detail value must be a scalar")


@dataclass(frozen=True, slots=True)
class CatalogIssue:
    code: IssueCode
    stage: IssueStage
    disposition: IssueDisposition
    message: str
    entity_ref: str | None = None
    details: tuple[IssueDetail, ...] = ()

    def __post_init__(self) -> None:
        if type(self.code) is not IssueCode:
            raise TypeError("catalog issue code must be an IssueCode")
        if type(self.stage) is not IssueStage:
            raise TypeError("catalog issue stage must be an IssueStage")
        if type(self.disposition) is not IssueDisposition:
            raise TypeError("catalog issue disposition must be an IssueDisposition")
        _require_text(self.message, "catalog issue message", maximum=16_384)
        if self.entity_ref is not None:
            _require_text(self.entity_ref, "catalog issue entity_ref", maximum=512)
        if type(self.details) is not tuple or any(
            type(detail) is not IssueDetail for detail in self.details
        ):
            raise TypeError("catalog issue details must be a tuple of IssueDetail")
        keys = tuple(detail.key for detail in self.details)
        if len(set(keys)) != len(keys):
            raise ValueError("catalog issue detail keys must be unique")

    @property
    def is_fatal(self) -> bool:
        return self.disposition is IssueDisposition.FATAL


def _require_text(value: object, name: str, *, maximum: int) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if len(value) > maximum:
        raise ValueError(f"{name} exceeds {maximum} code points")
    return value


__all__ = [
    "CatalogIssue",
    "IssueCode",
    "IssueDetail",
    "IssueDetailValue",
    "IssueDisposition",
    "IssueStage",
]
