"""Shared deterministic fixtures for M1a tests."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest


@pytest.fixture
def valid_create_run_payload() -> Callable[..., dict[str, Any]]:
    """Build one valid strict create-run request with optional top-level overrides."""

    def build(**updates: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "thread_id": "thread-test-001",
            "request": {
                "query": "推荐 800 美元以内、有库存、适合出差的轻薄本",
                "locale": "zh-CN",
                "display_currency": "USD",
                "top_k": 3,
                "snapshot_version": "m0-v1",
            },
        }
        payload.update(updates)
        return payload

    return build
