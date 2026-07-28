"""Strict, transport-only resource settings for the M1a API."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

PositiveStrictInt = Annotated[int, Field(strict=True, ge=1)]


class ApiSettings(BaseModel):
    """Bounded local defaults kept outside the M0 business fingerprint."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )

    max_active_runs: PositiveStrictInt = 16
    max_terminal_runs: PositiveStrictInt = 128
    terminal_ttl_seconds: PositiveStrictInt = 900
    max_events_per_run: PositiveStrictInt = 128
    max_subscribers_per_run: PositiveStrictInt = 4
    max_request_body_bytes: PositiveStrictInt = 16_384
    run_timeout_seconds: PositiveStrictInt = 30
    sse_heartbeat_seconds: PositiveStrictInt = 15


__all__ = ["ApiSettings"]
