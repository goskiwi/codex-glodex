"""Shared system clock and run-ID providers."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime


class UuidRunIdProvider:
    """Local run IDs; their nondeterminism is excluded from semantic comparisons."""

    def next_run_id(self) -> str:
        return f"run-{uuid.uuid4().hex}"


class SystemClock:
    """System implementation of the injected application clock."""

    def now_utc(self) -> datetime:
        return datetime.now(UTC)

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


__all__ = ["SystemClock", "UuidRunIdProvider"]
