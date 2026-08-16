"""Shared strict contracts for the OpenAI-compatible JSON completion boundary."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from glodex._json import compact_bytes, compact_dumps
from glodex.domain.intent import IntentIssueCode

type JsonCompletionTransport = Callable[[bytes], Awaitable[bytes]]


def json_completion_payload(
    *, system: str, user: dict[str, object], model_name: str, max_tokens: int
) -> bytes:
    """Build the shared deterministic OpenAI-compatible JSON request envelope."""

    return compact_bytes(
        {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": compact_dumps(user)},
            ],
            "stream": False,
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": max_tokens,
            "tool_choice": "none",
        }
    )


def is_model_name(value: object) -> bool:
    return (
        type(value) is str
        and bool(value.strip())
        and len(value) <= 128
        and "\0" not in value
        and "\n" not in value
        and "\r" not in value
    )


class LlmTransportError(RuntimeError):
    """A stable provider failure that never retains response content."""

    def __init__(self, code: IntentIssueCode) -> None:
        if code is IntentIssueCode.PROVIDER_UNAVAILABLE:
            message = "LLM provider is unavailable."
        elif code is IntentIssueCode.PROVIDER_RESPONSE_INVALID:
            message = "LLM provider response is invalid."
        else:
            raise ValueError("unsupported LLM provider error code")
        self.code = code
        super().__init__(f"{code.value}: {message}")


__all__ = [
    "JsonCompletionTransport",
    "LlmTransportError",
    "is_model_name",
    "json_completion_payload",
]
