"""Fixed, bounded HTTP transport for the approved DeepSeek intent call."""

from __future__ import annotations

import asyncio
import os
import re
from typing import Final

import httpx

from glodex.adapters.deepseek_intent import (
    DeepSeekIntentError,
    DeepSeekTransport,
)
from glodex.domain.intent import IntentIssueCode

_DEEPSEEK_URL: Final = "https://api.deepseek.com/chat/completions"
_DEEPSEEK_CREDENTIAL_NAME: Final = "DEEPSEEK_API_KEY"
DEEPSEEK_TOTAL_DEADLINE_SECONDS: Final = 15.0
_MAX_RESPONSE_BYTES: Final = 65_536
_MAX_RESPONSE_BYTES_TEXT: Final = str(_MAX_RESPONSE_BYTES)
_CONTENT_LENGTH = re.compile(r"[0-9]+\Z")

_MISSING_CREDENTIALS = "INTENT_LIVE_CREDENTIALS_MISSING"
_INVALID_CONFIG = "INTENT_LIVE_CONFIG_INVALID"


class DeepSeekPreflightError(RuntimeError):
    """A stable, secret-free live activation error raised before a Run exists."""

    def __init__(self, code: str) -> None:
        if code == _MISSING_CREDENTIALS:
            message = "DeepSeek live credentials are missing."
        elif code == _INVALID_CONFIG:
            message = "DeepSeek live configuration is invalid."
        else:
            raise ValueError("unsupported DeepSeek preflight error code")
        self.code = code
        super().__init__(f"{code}: {message}")


def build_deepseek_transport(
    *,
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> DeepSeekTransport:
    """Validate process credentials and build the one bounded async callable."""

    credential: object = os.getenv(_DEEPSEEK_CREDENTIAL_NAME)
    if credential is None:
        raise DeepSeekPreflightError(_MISSING_CREDENTIALS)
    if not isinstance(credential, str):
        raise DeepSeekPreflightError(_INVALID_CONFIG)
    if "\r" in credential or "\n" in credential:
        raise DeepSeekPreflightError(_INVALID_CONFIG)
    if not credential.strip():
        raise DeepSeekPreflightError(_MISSING_CREDENTIALS)

    async def send(payload: bytes) -> bytes:
        try:
            async with asyncio.timeout(DEEPSEEK_TOTAL_DEADLINE_SECONDS):
                active_transport = http_transport
                if active_transport is None:
                    active_transport = httpx.AsyncHTTPTransport(
                        retries=0,
                        trust_env=False,
                    )
                async with (
                    httpx.AsyncClient(
                        transport=active_transport,
                        timeout=httpx.Timeout(DEEPSEEK_TOTAL_DEADLINE_SECONDS),
                        follow_redirects=False,
                        trust_env=False,
                    ) as client,
                    client.stream(
                        "POST",
                        _DEEPSEEK_URL,
                        headers={
                            "Accept-Encoding": "identity",
                            "Authorization": f"Bearer {credential}",
                            "Content-Type": "application/json",
                        },
                        content=payload,
                    ) as response,
                ):
                    return await _read_response(response)
        except DeepSeekIntentError:
            raise
        except (TimeoutError, httpx.HTTPError):
            raise DeepSeekIntentError(IntentIssueCode.PROVIDER_UNAVAILABLE) from None

    return send


async def _read_response(response: httpx.Response) -> bytes:
    if not 200 <= response.status_code < 300:
        raise DeepSeekIntentError(IntentIssueCode.PROVIDER_UNAVAILABLE)

    content_encoding = response.headers.get("Content-Encoding")
    if content_encoding is not None and content_encoding.lower() != "identity":
        raise DeepSeekIntentError(IntentIssueCode.PROVIDER_RESPONSE_INVALID)

    raw_content_length = response.headers.get("Content-Length")
    declared_length: int | None = None
    if raw_content_length is not None:
        if _CONTENT_LENGTH.fullmatch(raw_content_length) is None:
            raise DeepSeekIntentError(IntentIssueCode.PROVIDER_RESPONSE_INVALID)
        normalized_length = raw_content_length.lstrip("0") or "0"
        if len(normalized_length) > len(_MAX_RESPONSE_BYTES_TEXT) or (
            len(normalized_length) == len(_MAX_RESPONSE_BYTES_TEXT)
            and normalized_length > _MAX_RESPONSE_BYTES_TEXT
        ):
            raise DeepSeekIntentError(IntentIssueCode.PROVIDER_RESPONSE_INVALID)
        declared_length = int(normalized_length)

    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(chunk) > _MAX_RESPONSE_BYTES - len(body):
            raise DeepSeekIntentError(IntentIssueCode.PROVIDER_RESPONSE_INVALID)
        if declared_length is not None and len(chunk) > declared_length - len(body):
            raise DeepSeekIntentError(IntentIssueCode.PROVIDER_RESPONSE_INVALID)
        body.extend(chunk)
    if declared_length is not None and len(body) != declared_length:
        raise DeepSeekIntentError(IntentIssueCode.PROVIDER_RESPONSE_INVALID)
    return bytes(body)


__all__ = [
    "DEEPSEEK_TOTAL_DEADLINE_SECONDS",
    "DeepSeekPreflightError",
    "build_deepseek_transport",
]
