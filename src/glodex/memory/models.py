"""Owner-scoped durable conversation and long-term memory contracts for user-memory."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from glodex.memory.identity import validate_user_id

_ENTRY_ID = re.compile(r"mem-[0-9a-f]{24}\Z")
_THREAD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
_CANONICAL_KEY = re.compile(r"[a-z][a-z0-9_-]{2,127}\Z")
_MAX_TURN_LENGTH: Final = 2_000
_MAX_MEMORY_CONTENT_LENGTH: Final = 512
_MAX_SUMMARY_LENGTH: Final = 2_000
_BUDGET_CUES: Final = ("预算", "budget")
_PLATFORM_CUES: Final = (
    "平台",
    "amazon",
    "亚马逊",
    "ebay",
    "易贝",
    "shopee",
    "虾皮",
    "aliexpress",
    "速卖通",
    "alibaba",
    "阿里巴巴",
    "walmart",
    "沃尔玛",
    "shein",
)
_PLATFORM_PREFERENCE_CUES: Final = (
    "偏好",
    "倾向",
    "喜欢",
    "习惯",
    "通常",
    "购买",
    "下单",
    "购物",
    "prefer",
    "usually",
    "buy",
    "shop",
)


class UserMemoryCategory(StrEnum):
    """The only durable memory categories accepted from user-memory reflection or UI CRUD."""

    PREFERENCE = "preference"
    BLACKLIST = "blacklist"
    HISTORY = "history"


class UserMemoryOrigin(StrEnum):
    """The only durable origins accepted by the current memory contract."""

    MANUAL = "manual"
    EXPLICIT_REFLECT = "explicit_reflect"


class ConversationRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class UserMemoryError(ValueError):
    """Stable validation failure that carries no private text."""


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    """One owner-bound display turn; full history remains PostgreSQL truth."""

    thread_id: str
    ordinal: int
    role: ConversationRole
    display_content: str
    terminal_run_id: str | None = None

    def __post_init__(self) -> None:
        if (
            _THREAD_ID.fullmatch(self.thread_id) is None
            or type(self.ordinal) is not int
            or self.ordinal < 1
            or type(self.role) is not ConversationRole
            or not _valid_text(self.display_content, maximum=_MAX_TURN_LENGTH)
            or (
                self.terminal_run_id is not None
                and _THREAD_ID.fullmatch(self.terminal_run_id) is None
            )
            or (self.role is ConversationRole.USER) is (self.terminal_run_id is not None)
        ):
            raise UserMemoryError("conversation turn is invalid")


@dataclass(frozen=True, slots=True)
class ConversationThreadInfo:
    """One owner-bound thread summary for the history management surface."""

    thread_id: str
    turn_count: int

    def __post_init__(self) -> None:
        if (
            _THREAD_ID.fullmatch(self.thread_id) is None
            or type(self.turn_count) is not int
            or self.turn_count < 0
        ):
            raise UserMemoryError("conversation thread is invalid")


@dataclass(frozen=True, slots=True)
class ConversationTurnPage:
    """A chronological page with an opaque-by-convention ordinal cursor."""

    thread_id: str
    turns: tuple[ConversationTurn, ...]
    next_before_ordinal: int | None

    def __post_init__(self) -> None:
        if (
            _THREAD_ID.fullmatch(self.thread_id) is None
            or type(self.turns) is not tuple
            or any(
                type(turn) is not ConversationTurn or turn.thread_id != self.thread_id
                for turn in self.turns
            )
            or (
                self.next_before_ordinal is not None
                and (type(self.next_before_ordinal) is not int or self.next_before_ordinal < 1)
            )
        ):
            raise UserMemoryError("conversation turn page is invalid")


@dataclass(frozen=True, slots=True)
class ThreadSummary:
    """Strict-model summary of a prefix; it never replaces retained turn history."""

    thread_id: str
    revision: int
    covered_through_ordinal: int
    summary: str

    def __post_init__(self) -> None:
        if (
            _THREAD_ID.fullmatch(self.thread_id) is None
            or type(self.revision) is not int
            or self.revision < 1
            or type(self.covered_through_ordinal) is not int
            or self.covered_through_ordinal < 1
            or not _valid_text(self.summary, maximum=_MAX_SUMMARY_LENGTH)
        ):
            raise UserMemoryError("thread summary is invalid")


@dataclass(frozen=True, slots=True)
class UserMemoryEntry:
    """One owner-scoped entry whose current-BGE vector is never persisted."""

    user_id: str
    entry_id: str
    category: UserMemoryCategory
    origin: UserMemoryOrigin
    canonical_key: str
    content: str
    source_thread_id: str | None
    source_ordinal: int | None
    confidence: float
    revision: int

    def __post_init__(self) -> None:
        validate_user_id(self.user_id)
        if (
            _ENTRY_ID.fullmatch(self.entry_id) is None
            or type(self.category) is not UserMemoryCategory
            or type(self.origin) is not UserMemoryOrigin
            or _CANONICAL_KEY.fullmatch(self.canonical_key) is None
            or not _valid_text(self.content, maximum=_MAX_MEMORY_CONTENT_LENGTH)
            or (self.source_thread_id is None) is not (self.source_ordinal is None)
            or (
                self.source_thread_id is not None
                and _THREAD_ID.fullmatch(self.source_thread_id) is None
            )
            or (
                self.source_ordinal is not None
                and (type(self.source_ordinal) is not int or self.source_ordinal < 1)
            )
            or type(self.confidence) not in {int, float}
            or not 0.0 <= float(self.confidence) <= 1.0
            or type(self.revision) is not int
            or self.revision < 1
            or (
                self.origin is UserMemoryOrigin.MANUAL
                and (self.source_thread_id is not None or self.source_ordinal is not None)
            )
            or (
                self.origin is not UserMemoryOrigin.MANUAL
                and (self.source_thread_id is None or self.source_ordinal is None)
            )
        ):
            raise UserMemoryError("user memory entry is invalid")


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    """One strict-LLM candidate whose text must exactly originate in current input."""

    category: UserMemoryCategory
    content: str
    start: int
    end: int

    def __post_init__(self) -> None:
        if (
            type(self.category) is not UserMemoryCategory
            or not _valid_text(self.content, maximum=_MAX_MEMORY_CONTENT_LENGTH)
            or type(self.start) is not int
            or type(self.end) is not int
            or self.start < 0
            or self.end <= self.start
        ):
            raise UserMemoryError("memory candidate is invalid")

    @property
    def canonical_key(self) -> str:
        return canonical_memory_key(category=self.category, content=self.content)

    @property
    def entry_id(self) -> str:
        return memory_entry_id(category=self.category, content=self.content)


def canonical_memory_key(*, category: UserMemoryCategory, content: str) -> str:
    """Return one deterministic logical slot or a content-addressed multi-value key."""

    if type(category) is not UserMemoryCategory or not _valid_text(
        content,
        maximum=_MAX_MEMORY_CONTENT_LENGTH,
    ):
        raise UserMemoryError("memory canonical input is invalid")
    normalized = content.casefold()
    if category is UserMemoryCategory.PREFERENCE:
        if any(cue in normalized for cue in _BUDGET_CUES):
            return "preference_budget_range"
        if any(cue in normalized for cue in _PLATFORM_CUES) and any(
            cue in normalized for cue in _PLATFORM_PREFERENCE_CUES
        ):
            return "preference_platform"
    digest = hashlib.sha256(f"{category.value}:{normalized}".encode()).hexdigest()[:24]
    return f"m_{category.value}_{digest}"


def memory_entry_id(*, category: UserMemoryCategory, content: str) -> str:
    """Derive an opaque deterministic ID for one manually authored value."""

    if type(category) is not UserMemoryCategory or not _valid_text(
        content,
        maximum=_MAX_MEMORY_CONTENT_LENGTH,
    ):
        raise UserMemoryError("memory entry identity input is invalid")
    material = f"{category.value}:{content}".encode()
    return f"mem-{hashlib.sha256(material).hexdigest()[:24]}"


def explicit_memory_entry_id(
    *,
    category: UserMemoryCategory,
    content: str,
    source_thread_id: str,
    source_ordinal: int,
) -> str:
    """Derive a replay-stable ID for an explicitly confirmed memory."""

    if (
        type(category) is not UserMemoryCategory
        or not _valid_text(content, maximum=_MAX_MEMORY_CONTENT_LENGTH)
        or type(source_thread_id) is not str
        or _THREAD_ID.fullmatch(source_thread_id) is None
        or type(source_ordinal) is not int
        or isinstance(source_ordinal, bool)
        or source_ordinal < 1
    ):
        raise UserMemoryError("explicit memory identity input is invalid")
    material = (
        f"explicit_reflect:{category.value}:{content}:{source_thread_id}:{source_ordinal}"
    ).encode()
    return f"mem-{hashlib.sha256(material).hexdigest()[:24]}"


def _valid_text(value: object, *, maximum: int) -> bool:
    return (
        type(value) is str
        and bool(value)
        and value == value.strip()
        and len(value) <= maximum
        and "\0" not in value
    )


__all__ = [
    "ConversationRole",
    "ConversationThreadInfo",
    "ConversationTurn",
    "ConversationTurnPage",
    "MemoryCandidate",
    "ThreadSummary",
    "UserMemoryCategory",
    "UserMemoryEntry",
    "UserMemoryError",
    "UserMemoryOrigin",
    "canonical_memory_key",
    "explicit_memory_entry_id",
    "memory_entry_id",
]
