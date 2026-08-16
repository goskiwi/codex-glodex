from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from glodex.infrastructure.postgres import DurablePostgresStore
from glodex.memory.models import MemoryCandidate, UserMemoryCategory, UserMemoryOrigin

_USER_ID = "user-" + "a" * 32

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec(
        "GLO-M5B-P0-004",
        "GLO-M5B-P0-005",
        "GLO-M5B-P0-006",
        "GLO-M5B-NFR-003",
    ),
]


class _AsyncContext:
    def __init__(self, value: object) -> None:
        self.value = value

    async def __aenter__(self) -> object:
        return self.value

    async def __aexit__(self, *_args: object) -> None:
        return None


@dataclass
class _Connection:
    fetchrow_results: list[object]
    fetch_results: list[object] = field(default_factory=list)
    calls: list[tuple[str, tuple[object, ...]]] = field(default_factory=list)

    def transaction(self) -> _AsyncContext:
        return _AsyncContext(self)

    async def fetchrow(self, sql: str, *args: object) -> object:
        self.calls.append((sql, args))
        return self.fetchrow_results.pop(0)

    async def fetch(self, sql: str, *args: object) -> object:
        self.calls.append((sql, args))
        return self.fetch_results.pop(0)


@dataclass
class _Pool:
    connection: _Connection

    def acquire(self) -> _AsyncContext:
        return _AsyncContext(self.connection)


def _row(
    *,
    content: str,
    origin: str,
    revision: int,
    thread_id: str | None,
    ordinal: int | None,
) -> dict[str, object]:
    return {
        "user_id": _USER_ID,
        "entry_id": "mem-" + "1" * 24,
        "category": "preference",
        "origin": origin,
        "canonical_key": "preference_budget_range",
        "content": content,
        "source_thread_id": thread_id,
        "source_ordinal": ordinal,
        "confidence": 1.0,
        "revision": revision,
        "deleted_at": None,
    }


def test_manual_single_value_write_replaces_the_active_slot_in_place() -> None:
    previous = _row(
        content="我长期预算为 3000 元",
        origin="explicit_reflect",
        revision=1,
        thread_id="thread-old",
        ordinal=1,
    )
    updated = _row(
        content="我的预算偏好是 5000 元",
        origin="manual",
        revision=2,
        thread_id=None,
        ordinal=None,
    )
    connection = _Connection(fetchrow_results=[{"user_id": _USER_ID}, previous, updated])
    store = DurablePostgresStore(pool=_Pool(connection))

    entry = asyncio.run(
        store.create_manual_memory(
            user_id=_USER_ID,
            category=UserMemoryCategory.PREFERENCE,
            content="我的预算偏好是 5000 元",
        )
    )

    assert entry.origin is UserMemoryOrigin.MANUAL
    assert entry.entry_id == previous["entry_id"]
    assert entry.content == updated["content"] and entry.revision == 2
    assert any(
        "UPDATE user_memory_entries SET origin = 'manual'" in sql for sql, _ in connection.calls
    )


def test_explicit_single_value_write_replaces_an_older_explicit_slot() -> None:
    previous = _row(
        content="我长期预算为 3000 元",
        origin="explicit_reflect",
        revision=1,
        thread_id="thread-old",
        ordinal=1,
    )
    updated = _row(
        content="以后预算改为 5000 元以内",
        origin="explicit_reflect",
        revision=2,
        thread_id="thread-new",
        ordinal=3,
    )
    connection = _Connection(
        fetchrow_results=[{"thread_id": "thread-new"}, updated],
        fetch_results=[[previous]],
    )
    store = DurablePostgresStore(pool=_Pool(connection))
    content = "以后预算改为 5000 元以内"

    entries = asyncio.run(
        store.write_explicit_reflect_memory(
            user_id=_USER_ID,
            candidates=(
                MemoryCandidate(
                    category=UserMemoryCategory.PREFERENCE,
                    content=content,
                    start=0,
                    end=len(content),
                ),
            ),
            source_thread_id="thread-new",
            source_ordinal=3,
        )
    )

    assert len(entries) == 1
    assert entries[0].entry_id == previous["entry_id"]
    assert entries[0].content == content and entries[0].revision == 2
    assert any("origin = 'explicit_reflect'" in sql for sql, _ in connection.calls)


def test_explicit_write_never_overrides_a_manual_single_value_slot() -> None:
    manual = _row(
        content="我的预算偏好是 5000 元",
        origin="manual",
        revision=4,
        thread_id=None,
        ordinal=None,
    )
    connection = _Connection(
        fetchrow_results=[{"thread_id": "thread-new"}],
        fetch_results=[[manual]],
    )
    store = DurablePostgresStore(pool=_Pool(connection))
    content = "以后预算改为 3000 元以内"

    entries = asyncio.run(
        store.write_explicit_reflect_memory(
            user_id=_USER_ID,
            candidates=(
                MemoryCandidate(
                    category=UserMemoryCategory.PREFERENCE,
                    content=content,
                    start=0,
                    end=len(content),
                ),
            ),
            source_thread_id="thread-new",
            source_ordinal=3,
        )
    )

    assert entries == ()
    assert len(connection.calls) == 2
