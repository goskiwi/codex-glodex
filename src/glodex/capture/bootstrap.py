"""Production composition for the operator-only Capture path."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime

from glodex.capture.config import CaptureRequest
from glodex.capture.contracts import CaptureId, CaptureReceipt
from glodex.capture.ebay_http import EbayHttpClient
from glodex.capture.ports import (
    CaptureClock,
    CaptureIdProvider,
    EbayPageSource,
    SnapshotPublisher,
)
from glodex.capture.service import CaptureService
from glodex.capture.snapshot_publisher import LocalSnapshotPublisher


class UuidCaptureIdProvider:
    """Allocate a fresh snapshot-safe identity for every established Capture."""

    def next_capture_id(self) -> CaptureId:
        return f"capture-{uuid.uuid4().hex}"


class SystemCaptureClock:
    """Provide the wall and monotonic clocks required by Capture."""

    def now_utc(self) -> datetime:
        return datetime.now(UTC)

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


def build_capture_service(
    *,
    id_provider: CaptureIdProvider | None = None,
    clock: CaptureClock | None = None,
    page_source: EbayPageSource | None = None,
    publisher: SnapshotPublisher | None = None,
) -> CaptureService:
    """Build the fixed production Capture composition with narrow test seams."""

    effective_clock = SystemCaptureClock() if clock is None else clock
    return CaptureService(
        page_source=(
            EbayHttpClient(monotonic_ns=effective_clock.monotonic_ns)
            if page_source is None
            else page_source
        ),
        publisher=LocalSnapshotPublisher() if publisher is None else publisher,
        id_provider=UuidCaptureIdProvider() if id_provider is None else id_provider,
        clock=effective_clock,
    )


def run_capture(request: CaptureRequest) -> CaptureReceipt:
    """Run one production Capture synchronously."""

    return build_capture_service().capture(request)


__all__ = [
    "SystemCaptureClock",
    "UuidCaptureIdProvider",
    "build_capture_service",
    "run_capture",
]
