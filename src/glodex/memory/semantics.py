"""Deterministic M5b eligibility checks around one optional LLM reflection call."""

from __future__ import annotations

from typing import Final

from glodex.memory.models import MemoryCandidate, UserMemoryCategory

_MAX_CURRENT_CONTENT: Final = 2_000
LONG_TERM_CUES: Final = (
    "平时",
    "通常",
    "一贯",
    "一直",
    "总是",
    "以后",
    "长期",
    "习惯",
    "偏好",
    "喜欢",
    "不接受",
    "绝不",
    "永远",
    "从不",
)
IMMEDIATE_CUES: Final = (
    "这次",
    "本次",
    "当前",
    "今天",
    "现在",
    "这回",
    "这单",
)
_BLACKLIST_CUES: Final = (
    "不要",
    "不接受",
    "绝不",
    "从不",
    "禁止",
    "排除",
)


class ExplicitMemorySemanticsError(ValueError):
    """A safe, content-free failure from the deterministic durable gate."""


def may_contain_explicit_long_term_memory(current_input: str) -> bool:
    """Return whether current input is worth one Reflect call; never inspect history."""

    _validate_current_input(current_input)
    normalized = current_input.casefold()
    return any(cue in normalized for cue in LONG_TERM_CUES)


def validate_explicit_memory_candidates(
    current_input: str,
    candidates: tuple[MemoryCandidate, ...],
) -> tuple[MemoryCandidate, ...]:
    """Validate a complete response atomically; one bad candidate rejects the batch."""

    _validate_current_input(current_input)
    if (
        type(candidates) is not tuple
        or len(candidates) > 3
        or any(type(candidate) is not MemoryCandidate for candidate in candidates)
    ):
        raise ExplicitMemorySemanticsError("EXPLICIT_MEMORY_BATCH_INVALID")
    keys: set[tuple[str, str]] = set()
    for candidate in candidates:
        content = candidate.content
        normalized = content.casefold()
        key = (candidate.category.value, normalized)
        if (
            candidate.category is UserMemoryCategory.HISTORY
            or current_input[candidate.start : candidate.end] != content
            or current_input.count(content) != 1
            or not any(cue in normalized for cue in LONG_TERM_CUES)
            or any(cue in normalized for cue in IMMEDIATE_CUES)
            or key in keys
        ):
            raise ExplicitMemorySemanticsError("EXPLICIT_MEMORY_CANDIDATE_INVALID")
        is_blacklist = any(cue in normalized for cue in _BLACKLIST_CUES)
        if (candidate.category is UserMemoryCategory.BLACKLIST and not is_blacklist) or (
            candidate.category is UserMemoryCategory.PREFERENCE and is_blacklist
        ):
            raise ExplicitMemorySemanticsError("EXPLICIT_MEMORY_CATEGORY_INVALID")
        keys.add(key)
    return candidates


def _validate_current_input(current_input: object) -> None:
    if (
        type(current_input) is not str
        or not current_input
        or current_input != current_input.strip()
        or len(current_input) > _MAX_CURRENT_CONTENT
        or "\0" in current_input
    ):
        raise ExplicitMemorySemanticsError("EXPLICIT_MEMORY_INPUT_INVALID")


__all__ = [
    "IMMEDIATE_CUES",
    "LONG_TERM_CUES",
    "ExplicitMemorySemanticsError",
    "may_contain_explicit_long_term_memory",
    "validate_explicit_memory_candidates",
]
