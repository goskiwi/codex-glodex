"""Small synchronous ports used only by the operator Capture service."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from glodex.capture.config import EbayCredentials
from glodex.capture.contracts import (
    PROVIDER_FAILURE_CODES,
    SNAPSHOT_FAILURE_CODES,
    CaptureId,
    CaptureIssueCode,
)
from glodex.domain.catalog import CatalogBatch


def _require_non_negative_integer(value: int, label: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class EbaySearchPage:
    """One complete decoded item summary page."""

    item_summaries: tuple[object, ...]
    request_count: Literal[2] = field(default=2, init=False)

    def __post_init__(self) -> None:
        if type(self.item_summaries) is not tuple:
            raise TypeError("item_summaries must be a tuple")
        if len(self.item_summaries) > 10:
            raise ValueError("item_summaries cannot exceed the fixed page limit")

    @property
    def received_record_count(self) -> int:
        return len(self.item_summaries)


@dataclass(frozen=True, slots=True)
class EbayProviderFailure:
    """A stable Provider failure without third-party text or secrets."""

    issue_code: CaptureIssueCode
    request_count: int
    received_record_count: int = 0

    def __post_init__(self) -> None:
        if self.issue_code not in PROVIDER_FAILURE_CODES:
            raise ValueError("issue_code must be a Provider failure code")
        if type(self.request_count) is not int or not 1 <= self.request_count <= 2:
            raise ValueError("request_count must be one or two")
        _require_non_negative_integer(
            self.received_record_count,
            "received_record_count",
        )


type EbayProviderResult = EbaySearchPage | EbayProviderFailure


@dataclass(frozen=True, slots=True)
class SnapshotPublishSuccess:
    """Counts verified by the existing loader and aggregation before rename."""

    product_count: int
    offer_count: int

    def __post_init__(self) -> None:
        _require_non_negative_integer(self.product_count, "product_count")
        _require_non_negative_integer(self.offer_count, "offer_count")


@dataclass(frozen=True, slots=True)
class SnapshotPublishFailure:
    """A stable pre-commit snapshot validation or publication failure."""

    issue_code: CaptureIssueCode

    def __post_init__(self) -> None:
        if self.issue_code not in SNAPSHOT_FAILURE_CODES:
            raise ValueError("issue_code must be a snapshot failure code")


type SnapshotPublishResult = SnapshotPublishSuccess | SnapshotPublishFailure


class CaptureIdProvider(Protocol):
    def next_capture_id(self) -> CaptureId: ...


class CaptureClock(Protocol):
    def now_utc(self) -> datetime: ...

    def monotonic_ns(self) -> int: ...


class EbayPageSource(Protocol):
    def fetch_page(
        self,
        *,
        query: str,
        credentials: EbayCredentials,
        deadline_ns: int,
    ) -> EbayProviderResult: ...


class SnapshotPublisher(Protocol):
    def publish(
        self,
        *,
        output_root: Path,
        batch: CatalogBatch,
        captured_at: datetime,
    ) -> SnapshotPublishResult: ...


__all__ = [
    "CaptureClock",
    "CaptureIdProvider",
    "EbayPageSource",
    "EbayProviderFailure",
    "EbayProviderResult",
    "EbaySearchPage",
    "SnapshotPublishFailure",
    "SnapshotPublishResult",
    "SnapshotPublishSuccess",
    "SnapshotPublisher",
]
