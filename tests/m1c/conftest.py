"""Deterministic Provider-shaped fakes for M1c tests."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

import pytest


@dataclass
class RecordingDeepSeekTransport:
    """Narrow async callable that records final request bytes."""

    response: bytes
    calls: list[bytes] = field(default_factory=list)

    async def __call__(self, payload: bytes) -> bytes:
        self.calls.append(payload)
        return self.response


class _DeepSeekCallable(Protocol):
    async def __call__(self, payload: bytes) -> bytes: ...


@pytest.fixture
def install_live_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[_DeepSeekCallable], list[object]]:
    """Install a narrow Fake below the production adapter and retain preflight."""

    def install(transport: _DeepSeekCallable) -> list[object]:
        from glodex import bootstrap
        from glodex.adapters import deepseek_http
        from glodex.api import live_app

        real_http_builder = deepseek_http.build_deepseek_transport
        real_service_builder = bootstrap.build_live_intent_service
        configs: list[object] = []

        def build_http() -> Callable[[bytes], Awaitable[bytes]]:
            real_http_builder()
            return transport

        def build_service(config: object, **kwargs: object) -> object:
            configs.append(config)
            return real_service_builder(config, **kwargs)

        monkeypatch.setattr(deepseek_http, "build_deepseek_transport", build_http)
        monkeypatch.setattr(bootstrap, "build_live_intent_service", build_service)
        monkeypatch.setattr(live_app, "build_live_intent_service", build_service)
        return configs

    return install


@pytest.fixture
def deepseek_response_bytes() -> Callable[[object], bytes]:
    """Build the one approved successful DeepSeek response envelope."""

    def build(content: object) -> bytes:
        envelope = {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "content": json.dumps(
                            content,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    },
                }
            ]
        }
        return json.dumps(
            envelope,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()

    return build
