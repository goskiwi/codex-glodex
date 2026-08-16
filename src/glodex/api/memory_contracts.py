"""Safe owner-scoped HTTP contracts for user-memory conversation and long-term memory."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from glodex.api.contracts import ApiDTO

MemoryContent = Annotated[str, StringConstraints(min_length=1, max_length=512)]
ThreadIdentifier = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$",
    ),
]
MemoryIdentifier = Annotated[
    str,
    StringConstraints(min_length=28, max_length=28, pattern=r"^mem-[0-9a-f]{24}$"),
]


class ConversationThreadView(ApiDTO):
    """Safe summary for one owner-visible thread, without private context."""

    thread_id: ThreadIdentifier = Field(serialization_alias="threadId")
    turn_count: int = Field(ge=0, serialization_alias="turnCount")


class ConversationThreadList(ApiDTO):
    """Bounded list of the requesting user's threads."""

    schema_version: Literal["glodex.user-memory.thread-list.v1"] = Field(
        default="glodex.user-memory.thread-list.v1", serialization_alias="schemaVersion"
    )
    threads: tuple[ConversationThreadView, ...]


class ConversationTurnView(ApiDTO):
    """One display turn that belongs to the current user only."""

    ordinal: int = Field(ge=1)
    role: Literal["user", "assistant"]
    content: Annotated[str, StringConstraints(min_length=1, max_length=2_000)]
    terminal_run_id: ThreadIdentifier | None = Field(
        default=None, serialization_alias="terminalRunId"
    )


class ConversationTurnPage(ApiDTO):
    """A reverse-paginated, chronological page of display turns."""

    schema_version: Literal["glodex.user-memory.turn-page.v1"] = Field(
        default="glodex.user-memory.turn-page.v1", serialization_alias="schemaVersion"
    )
    thread_id: ThreadIdentifier = Field(serialization_alias="threadId")
    turns: tuple[ConversationTurnView, ...]
    next_before_ordinal: int | None = Field(default=None, serialization_alias="nextBeforeOrdinal")


class MemoryEntryInput(ApiDTO):
    """Manual memory is explicitly user-authored; no source is fabricated."""

    category: Literal["preference", "blacklist", "history"]
    content: MemoryContent

    @model_validator(mode="after")
    def blacklist_is_a_structured_hard_rule(self) -> Self:
        if self.category == "blacklist":
            from glodex.memory.normalization import normalize_blacklist_rule

            normalize_blacklist_rule(self.content)
        return self


class MemoryEntryView(ApiDTO):
    """Owner-visible entry without vectors, scores, prompts, or provider material."""

    entry_id: MemoryIdentifier = Field(serialization_alias="entryId")
    category: Literal["preference", "blacklist", "history"]
    content: MemoryContent
    revision: int = Field(ge=1)
    origin: Literal["manual", "explicit_reflect"]
    source_thread_id: ThreadIdentifier | None = Field(
        default=None, serialization_alias="sourceThreadId"
    )


class MemoryManagementView(ApiDTO):
    """The sole owner-memory response."""

    schema_version: Literal["glodex.user-memory.memory-list.v3"] = Field(
        default="glodex.user-memory.memory-list.v3", serialization_alias="schemaVersion"
    )
    active_entries: tuple[MemoryEntryView, ...] = Field(serialization_alias="activeEntries")


__all__ = [
    "ConversationThreadList",
    "ConversationThreadView",
    "ConversationTurnPage",
    "ConversationTurnView",
    "MemoryEntryInput",
    "MemoryEntryView",
    "MemoryManagementView",
]
