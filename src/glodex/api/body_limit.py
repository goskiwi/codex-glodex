"""Pure ASGI middleware that limits actual request body bytes before parsing."""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from glodex.api.contracts import ApiError, ApiErrorEnvelope

_TOO_LARGE_BODY = (
    ApiErrorEnvelope(
        error=ApiError(
            code="REQUEST_BODY_TOO_LARGE",
            message="Request body is too large.",
        )
    )
    .model_dump_json()
    .encode("utf-8")
)
_TOO_LARGE_HEADERS = (
    (b"content-length", str(len(_TOO_LARGE_BODY)).encode("ascii")),
    (b"content-type", b"application/json"),
)


class RequestBodyLimitMiddleware:
    """Buffer at most ``max_bytes`` from each HTTP request before downstream use."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
            raise TypeError("max_bytes must be an integer")
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        body_buffer = bytearray()
        disconnected = False
        while True:
            message = await receive()
            message_type = message["type"]
            if message_type == "http.disconnect":
                disconnected = True
                break
            if message_type != "http.request":
                raise RuntimeError("unexpected ASGI message while reading request body")

            body = message.get("body", b"")
            if not isinstance(body, bytes):
                raise TypeError("ASGI request body must be bytes")
            if len(body) > self.max_bytes - len(body_buffer):
                del body, message
                await _send_too_large(send)
                return
            body_buffer.extend(body)
            if not message.get("more_body", False):
                break

        buffered_body = bytes(body_buffer)
        body_buffer.clear()
        if disconnected:
            replay_messages: tuple[Message, ...]
            if buffered_body:
                replay_messages = (
                    {
                        "type": "http.request",
                        "body": buffered_body,
                        "more_body": True,
                    },
                    {"type": "http.disconnect"},
                )
            else:
                replay_messages = ({"type": "http.disconnect"},)
        else:
            replay_messages = (
                {
                    "type": "http.request",
                    "body": buffered_body,
                    "more_body": False,
                },
            )

        replay_index = 0

        async def replay_receive() -> Message:
            nonlocal replay_index
            if replay_index < len(replay_messages):
                replayed = replay_messages[replay_index]
                replay_index += 1
                return replayed
            return await receive()

        await self.app(scope, replay_receive, send)


async def _send_too_large(send: Send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": list(_TOO_LARGE_HEADERS),
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": _TOO_LARGE_BODY,
            "more_body": False,
        }
    )


__all__ = ["RequestBodyLimitMiddleware"]
