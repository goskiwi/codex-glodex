"""Fixed OpenAI-compatible configuration shared by every live LLM feature."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final

DEEPSEEK_API_KEY_ENV: Final = "DEEPSEEK_API_KEY"
DEEPSEEK_BASE_URL: Final = "https://2btocken.xyz/v1"
DEEPSEEK_MODEL_NAME: Final = "gpt-5.6-luna"


class LlmConfigurationError(RuntimeError):
    """A stable, secret-free activation failure."""

    def __init__(self, code: str) -> None:
        if code not in {"LLM_CREDENTIALS_MISSING", "LLM_CONFIG_INVALID"}:
            raise ValueError("unsupported LLM configuration error")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class LlmConfiguration:
    """The immutable settings snapshot used for one live composition."""

    api_key: str
    base_url: str
    model_name: str

    @property
    def chat_completions_url(self) -> str:
        """Return the standard endpoint below an OpenAI-compatible base URL."""

        return f"{self.base_url.rstrip('/')}/chat/completions"


def load_llm_configuration() -> LlmConfiguration:
    """Read only the model credential; provider and model are immutable."""

    api_key: object = os.getenv(DEEPSEEK_API_KEY_ENV)
    if api_key is None or (isinstance(api_key, str) and not api_key.strip()):
        raise LlmConfigurationError("LLM_CREDENTIALS_MISSING")
    if not isinstance(api_key, str):
        raise LlmConfigurationError("LLM_CONFIG_INVALID")
    if any(character in api_key for character in ("\n", "\r", "\0")):
        raise LlmConfigurationError("LLM_CONFIG_INVALID")
    return LlmConfiguration(
        api_key=api_key,
        base_url=DEEPSEEK_BASE_URL,
        model_name=DEEPSEEK_MODEL_NAME,
    )


__all__ = [
    "DEEPSEEK_API_KEY_ENV",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL_NAME",
    "LlmConfiguration",
    "LlmConfigurationError",
    "load_llm_configuration",
]
