"""Admission rules for the reviewed digital-product catalog."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any


class CatalogAdmissionReason(StrEnum):
    ADMITTED = "ADMITTED"
    INVALID_RELEASE_DATE = "INVALID_RELEASE_DATE"
    BEFORE_WINDOW = "BEFORE_WINDOW"
    AFTER_WINDOW = "AFTER_WINDOW"
    MISSING_IDENTITY = "MISSING_IDENTITY"
    INVALID_SPECIFICATION_SOURCE = "INVALID_SPECIFICATION_SOURCE"


@dataclass(frozen=True, slots=True)
class CatalogPolicy:
    market: str
    window_start: date
    window_end: date
    require_exact_release_date: bool
    require_manufacturer_specification: bool

    @classmethod
    def from_payload(cls, payload: object) -> CatalogPolicy | None:
        if not isinstance(payload, dict):
            return None
        try:
            require_exact_release_date = payload["require_exact_release_date"]
            require_manufacturer_specification = payload[
                "require_manufacturer_specification"
            ]
            if not isinstance(require_exact_release_date, bool) or not isinstance(
                require_manufacturer_specification, bool
            ):
                raise TypeError
            return cls(
                market=str(payload["market"]),
                window_start=date.fromisoformat(str(payload["window_start"])),
                window_end=date.fromisoformat(str(payload["window_end"])),
                require_exact_release_date=require_exact_release_date,
                require_manufacturer_specification=require_manufacturer_specification,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("digital catalog policy is invalid") from error

    def __post_init__(self) -> None:
        if (
            self.market != "CN"
            or self.window_start > self.window_end
            or not self.require_exact_release_date
        ):
            raise ValueError("digital catalog policy is invalid")


@dataclass(frozen=True, slots=True)
class CatalogQualityReport:
    total: int
    admitted: int
    rejected: int
    reasons: dict[str, int]


def admission_reason(raw: object, policy: CatalogPolicy) -> CatalogAdmissionReason:
    if not isinstance(raw, dict):
        return CatalogAdmissionReason.MISSING_IDENTITY
    attributes = raw.get("attributes")
    if not isinstance(attributes, dict) or not all(
        str(attributes.get(field, "")).strip() for field in ("brand", "model")
    ):
        return CatalogAdmissionReason.MISSING_IDENTITY

    release_date = _exact_date(attributes.get("release_date"))
    if release_date is None:
        return CatalogAdmissionReason.INVALID_RELEASE_DATE
    if release_date < policy.window_start:
        return CatalogAdmissionReason.BEFORE_WINDOW
    if release_date > policy.window_end:
        return CatalogAdmissionReason.AFTER_WINDOW

    source = raw.get("specification_source")
    if not isinstance(source, dict) or not str(source.get("url", "")).startswith("https://"):
        return CatalogAdmissionReason.INVALID_SPECIFICATION_SOURCE
    if (
        policy.require_manufacturer_specification
        and source.get("source_type") != "MANUFACTURER_SPECIFICATION"
    ):
        return CatalogAdmissionReason.INVALID_SPECIFICATION_SOURCE
    return CatalogAdmissionReason.ADMITTED


def audit_catalog(
    products: list[object], policy: CatalogPolicy
) -> tuple[list[dict[str, Any]], CatalogQualityReport]:
    admitted: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for raw in products:
        reason = admission_reason(raw, policy)
        counts[reason.value] += 1
        if reason is CatalogAdmissionReason.ADMITTED and isinstance(raw, dict):
            admitted.append(raw)
    rejected = len(products) - len(admitted)
    return admitted, CatalogQualityReport(
        total=len(products),
        admitted=len(admitted),
        rejected=rejected,
        reasons=dict(sorted(counts.items())),
    )


def _exact_date(value: object) -> date | None:
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None
