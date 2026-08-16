"""Bounded HTTP transport for an explicitly configured OpenAI-compatible endpoint."""

from __future__ import annotations

import asyncio
import re
from typing import Final

import httpx

from glodex.domain.intent import IntentIssueCode
from glodex.llm.config import LlmConfiguration, load_llm_configuration
from glodex.llm.contracts import JsonCompletionTransport, LlmTransportError

LLM_HTTP_DEADLINE_SECONDS: Final = 60.0
_MAX_RESPONSE_BYTES: Final = 65_536
_MAX_RESPONSE_BYTES_TEXT: Final = str(_MAX_RESPONSE_BYTES)
_CONTENT_LENGTH = re.compile(r"[0-9]+\Z")


def build_json_completion_transport(
    *,
    configuration: LlmConfiguration | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> JsonCompletionTransport:
    """Build one bounded JSON-completion callable from the explicit LLM settings."""

    active_configuration = load_llm_configuration() if configuration is None else configuration
    if type(active_configuration) is not LlmConfiguration:
        raise TypeError("LLM HTTP transport configuration is invalid")
    credential = active_configuration.api_key
    endpoint = active_configuration.chat_completions_url

    async def send(payload: bytes) -> bytes:
        try:
            async with asyncio.timeout(LLM_HTTP_DEADLINE_SECONDS):
                active_transport = http_transport
                if active_transport is None:
                    active_transport = httpx.AsyncHTTPTransport(
                        retries=0,
                        trust_env=False,
                    )
                async with (
                    httpx.AsyncClient(
                        transport=active_transport,
                        timeout=httpx.Timeout(LLM_HTTP_DEADLINE_SECONDS),
                        follow_redirects=False,
                        trust_env=False,
                    ) as client,
                    client.stream(
                        "POST",
                        endpoint,
                        headers={
                            "Accept-Encoding": "identity",
                            "Authorization": f"Bearer {credential}",
                            "Content-Type": "application/json",
                        },
                        content=payload,
                    ) as response,
                ):
                    return await _read_response(response)
        except LlmTransportError:
            raise
        except (TimeoutError, httpx.HTTPError):
            raise LlmTransportError(IntentIssueCode.PROVIDER_UNAVAILABLE) from None

    return send


async def _read_response(response: httpx.Response) -> bytes:
    if not 200 <= response.status_code < 300:
        raise LlmTransportError(IntentIssueCode.PROVIDER_UNAVAILABLE)

    content_encoding = response.headers.get("Content-Encoding")
    if content_encoding is not None and content_encoding.lower() != "identity":
        raise LlmTransportError(IntentIssueCode.PROVIDER_RESPONSE_INVALID)

    raw_content_length = response.headers.get("Content-Length")
    declared_length: int | None = None
    if raw_content_length is not None:
        if _CONTENT_LENGTH.fullmatch(raw_content_length) is None:
            raise LlmTransportError(IntentIssueCode.PROVIDER_RESPONSE_INVALID)
        normalized_length = raw_content_length.lstrip("0") or "0"
        if len(normalized_length) > len(_MAX_RESPONSE_BYTES_TEXT) or (
            len(normalized_length) == len(_MAX_RESPONSE_BYTES_TEXT)
            and normalized_length > _MAX_RESPONSE_BYTES_TEXT
        ):
            raise LlmTransportError(IntentIssueCode.PROVIDER_RESPONSE_INVALID)
        declared_length = int(normalized_length)

    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(chunk) > _MAX_RESPONSE_BYTES - len(body):
            raise LlmTransportError(IntentIssueCode.PROVIDER_RESPONSE_INVALID)
        if declared_length is not None and len(chunk) > declared_length - len(body):
            raise LlmTransportError(IntentIssueCode.PROVIDER_RESPONSE_INVALID)
        body.extend(chunk)
    if declared_length is not None and len(body) != declared_length:
        raise LlmTransportError(IntentIssueCode.PROVIDER_RESPONSE_INVALID)
    return bytes(body)


__all__ = [
    "LLM_HTTP_DEADLINE_SECONDS",
    "build_json_completion_transport",
]
