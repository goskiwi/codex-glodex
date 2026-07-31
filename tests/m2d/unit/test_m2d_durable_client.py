"""Regression coverage for the fixed loopback M2b public client."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from glodex.api.m2d_durable_client import (
    M2B_PUBLIC_BASE_URL,
    LoopbackM2bPublicClient,
    _validate_response,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec("GLO-M2D-P0-001", "GLO-M2D-P0-006", "GLO-M2D-NFR-001", "GLO-M2D-NFR-002"),
]


class _UnreadStream(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"data: {}\n\n"


def test_loopback_client_rejects_any_alternate_upstream() -> None:
    with pytest.raises(ValueError, match="fixed loopback"):
        LoopbackM2bPublicClient(base_url="http://127.0.0.1:18000")

    assert M2B_PUBLIC_BASE_URL == "http://127.0.0.1:8766"


def test_stream_content_type_validation_never_reads_an_unread_sse_body() -> None:
    response = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        stream=_UnreadStream(),
    )

    _validate_response(response, expected_content_type="text/event-stream")
