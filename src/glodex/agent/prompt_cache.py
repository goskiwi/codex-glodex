"""Strict provider prompt-cache capability and safe boolean telemetry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class PromptCacheTelemetry:
    """Provider-independent booleans safe for local callback metadata."""

    cache_supported: bool
    cache_attempted: bool
    cache_hit: bool

    def __post_init__(self) -> None:
        if (
            type(self.cache_supported) is not bool
            or type(self.cache_attempted) is not bool
            or type(self.cache_hit) is not bool
            or (self.cache_attempted and not self.cache_supported)
            or (self.cache_hit and not self.cache_attempted)
        ):
            raise ValueError("prompt-cache telemetry is invalid")

    def as_metadata(self) -> dict[str, bool]:
        return {
            "cache_supported": self.cache_supported,
            "cache_attempted": self.cache_attempted,
            "cache_hit": self.cache_hit,
        }


@dataclass(frozen=True, slots=True)
class PromptCacheCapability:
    """The exact current provider contract; unsupported means no payload mutation."""

    provider: str
    cache_supported: bool

    def __post_init__(self) -> None:
        if type(self.provider) is not str or not self.provider or self.cache_supported is not False:
            raise ValueError("an unverified prompt-cache provider contract cannot be enabled")

    def telemetry(self) -> PromptCacheTelemetry:
        return PromptCacheTelemetry(
            cache_supported=False,
            cache_attempted=False,
            cache_hit=False,
        )


DEEPSEEK_PROMPT_CACHE: Final = PromptCacheCapability(
    provider="deepseek",
    cache_supported=False,
)


__all__ = [
    "DEEPSEEK_PROMPT_CACHE",
    "PromptCacheCapability",
    "PromptCacheTelemetry",
]
