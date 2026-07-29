"""Explicit live-Intent FastAPI factory with local-only preflight."""

from __future__ import annotations

from fastapi import FastAPI

from glodex.adapters.deepseek_http import (
    DEEPSEEK_TOTAL_DEADLINE_SECONDS,
    DeepSeekPreflightError,
)
from glodex.api.app import create_app
from glodex.api.settings import ApiSettings
from glodex.application.ports import Clock, RunIdProvider
from glodex.bootstrap import (
    SystemClock,
    UuidRunIdProvider,
    build_live_intent_service,
)
from glodex.config import GlodexConfig, load_config


def create_live_app(
    *,
    settings: ApiSettings | None = None,
    config: GlodexConfig | None = None,
    clock: Clock | None = None,
    run_id_provider: RunIdProvider | None = None,
) -> FastAPI:
    """Preflight one live composition, then delegate every route to M1a."""

    effective_settings = ApiSettings() if settings is None else settings
    if effective_settings.run_timeout_seconds <= DEEPSEEK_TOTAL_DEADLINE_SECONDS:
        raise DeepSeekPreflightError("INTENT_LIVE_CONFIG_INVALID")

    effective_clock = SystemClock() if clock is None else clock
    effective_run_id_provider = UuidRunIdProvider() if run_id_provider is None else run_id_provider
    service = build_live_intent_service(
        load_config() if config is None else config,
        clock=effective_clock,
        run_id_provider=effective_run_id_provider,
    )
    return create_app(
        settings=effective_settings,
        service=service,
        clock=effective_clock,
        run_id_provider=effective_run_id_provider,
    )


__all__ = ["create_live_app"]
