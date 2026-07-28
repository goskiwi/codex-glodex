"""Direct ASGI tests for actual-byte request body limiting."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence

import pytest
from starlette.types import Message, Receive, Scope, Send

from glodex.api.body_limit import RequestBodyLimitMiddleware

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec(
        "GLO-M1-P0-001",
        "GLO-M1-P0-007",
        "GLO-M1-NFR-006",
        "GLO-M1-NFR-007",
        "GLO-M1-NFR-009",
    ),
]


class BodyReadingApp:
    """Small downstream app that proves when and what it receives."""

    def __init__(self) -> None:
        self.calls = 0
        self.started = False
        self.scope: Scope | None = None
        self.messages: list[Message] = []
        self.body = b""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.calls += 1
        self.started = True
        self.scope = scope
        while True:
            message = await receive()
            self.messages.append(message)
            if message["type"] == "http.disconnect":
                break
            self.body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})


def _run_http(
    downstream: BodyReadingApp,
    *,
    max_bytes: int,
    incoming: Sequence[Message],
    headers: Sequence[tuple[bytes, bytes]] = (),
) -> tuple[list[Message], int, Scope]:
    async def exercise() -> tuple[list[Message], int, Scope]:
        source = list(incoming)
        receive_calls = 0
        sent: list[Message] = []
        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.5"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/runs",
            "raw_path": b"/api/v1/runs",
            "query_string": b"",
            "headers": list(headers),
            "client": ("test-client", 1),
            "server": ("test-server", 80),
        }

        async def receive() -> Message:
            nonlocal receive_calls
            assert not downstream.started, "downstream started before the complete body was checked"
            receive_calls += 1
            if not source:
                raise AssertionError("middleware read beyond the final request message")
            return source.pop(0)

        async def send(message: Message) -> None:
            sent.append(message)

        middleware = RequestBodyLimitMiddleware(downstream, max_bytes=max_bytes)
        await middleware(scope, receive, send)
        return sent, receive_calls, scope

    return asyncio.run(exercise())


def test_complete_body_is_checked_before_downstream_and_replayed_unchanged() -> None:
    downstream = BodyReadingApp()
    sent, receive_calls, scope = _run_http(
        downstream,
        max_bytes=5,
        incoming=(
            {"type": "http.request", "body": b"ab", "more_body": True},
            {"type": "http.request", "body": b"cde", "more_body": False},
        ),
        headers=((b"content-length", b"1"),),
    )

    assert receive_calls == 2
    assert downstream.calls == 1
    assert downstream.scope is scope
    assert downstream.body == b"abcde"
    assert downstream.messages == [{"type": "http.request", "body": b"abcde", "more_body": False}]
    assert [message["type"] for message in sent] == [
        "http.response.start",
        "http.response.body",
    ]
    assert sent[0]["status"] == 204


def test_large_declared_content_length_does_not_reject_a_small_actual_body() -> None:
    downstream = BodyReadingApp()

    sent, _, _ = _run_http(
        downstream,
        max_bytes=3,
        incoming=({"type": "http.request", "body": b"ok", "more_body": False},),
        headers=((b"content-length", b"999999"),),
    )

    assert downstream.calls == 1
    assert downstream.body == b"ok"
    assert sent[0]["status"] == 204


@pytest.mark.parametrize(
    ("headers", "incoming"),
    (
        (
            (),
            (
                {"type": "http.request", "body": b"123", "more_body": True},
                {"type": "http.request", "body": b"45", "more_body": False},
            ),
        ),
        (
            ((b"content-length", b"1"),),
            (
                {"type": "http.request", "body": b"123", "more_body": True},
                {"type": "http.request", "body": b"45", "more_body": False},
            ),
        ),
        (
            ((b"transfer-encoding", b"chunked"),),
            (
                {"type": "http.request", "body": b"12", "more_body": True},
                {"type": "http.request", "body": b"345", "more_body": False},
            ),
        ),
    ),
)
def test_actual_body_over_limit_returns_safe_413_without_calling_downstream(
    headers: Sequence[tuple[bytes, bytes]],
    incoming: Sequence[Message],
) -> None:
    downstream = BodyReadingApp()

    sent, receive_calls, _ = _run_http(
        downstream,
        max_bytes=4,
        incoming=incoming,
        headers=headers,
    )

    assert receive_calls == 2
    assert downstream.calls == 0
    assert len(sent) == 2
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 413
    assert sent[1]["type"] == "http.response.body"
    payload = json.loads(sent[1]["body"])
    assert payload == {
        "schema_version": "glodex.error.v1",
        "type": "api_error",
        "error": {
            "code": "REQUEST_BODY_TOO_LARGE",
            "message": "Request body is too large.",
            "field_errors": [],
        },
    }


def test_oversized_chunk_stops_reading_immediately_and_does_not_echo_body() -> None:
    downstream = BodyReadingApp()
    secret = b'{"secret":"do-not-echo"}'

    sent, receive_calls, _ = _run_http(
        downstream,
        max_bytes=4,
        incoming=(
            {"type": "http.request", "body": secret, "more_body": True},
            {"type": "http.request", "body": b"unread", "more_body": False},
        ),
    )

    assert receive_calls == 1
    assert downstream.calls == 0
    assert secret not in sent[1]["body"]
    assert b"do-not-echo" not in sent[1]["body"]


def test_limit_counts_raw_utf8_bytes_and_accepts_an_empty_body() -> None:
    unicode_downstream = BodyReadingApp()
    sent, _, _ = _run_http(
        unicode_downstream,
        max_bytes=2,
        incoming=({"type": "http.request", "body": "中".encode(), "more_body": False},),
    )
    assert unicode_downstream.calls == 0
    assert sent[0]["status"] == 413

    empty_downstream = BodyReadingApp()
    sent, _, _ = _run_http(
        empty_downstream,
        max_bytes=1,
        incoming=({"type": "http.request", "body": b"", "more_body": False},),
    )
    assert empty_downstream.calls == 1
    assert empty_downstream.body == b""
    assert sent[0]["status"] == 204


def test_non_http_scope_is_forwarded_with_original_callables() -> None:
    async def exercise() -> None:
        observed: list[object] = []
        scope: Scope = {"type": "lifespan", "asgi": {"version": "3.0"}}

        async def receive() -> Message:
            return {"type": "lifespan.startup"}

        async def send(message: Message) -> None:
            observed.append(message)

        async def downstream(
            received_scope: Scope,
            received_receive: Receive,
            received_send: Send,
        ) -> None:
            observed.extend((received_scope, received_receive, received_send))

        middleware = RequestBodyLimitMiddleware(downstream, max_bytes=4)
        await middleware(scope, receive, send)

        assert observed == [scope, receive, send]

    asyncio.run(exercise())


@pytest.mark.parametrize("max_bytes", (True, 0, -1, 1.5, "4"))
def test_limit_must_be_a_strict_positive_integer(max_bytes: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        RequestBodyLimitMiddleware(BodyReadingApp(), max_bytes=max_bytes)  # type: ignore[arg-type]
