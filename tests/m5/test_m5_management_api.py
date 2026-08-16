"""Contract evidence for M5 owner-scoped history and memory management routes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI

from glodex.agent.contracts import AgentExecution
from glodex.api.agent_events import AgentEventProjector
from glodex.api.durable import create_durable_agent_app
from glodex.memory.identity import LocalSession, LocalUser, session_token_hash
from glodex.memory.models import (
    ConversationRole,
    ConversationThreadInfo,
    ConversationTurn,
    ConversationTurnPage,
    UserMemoryCategory,
    UserMemoryEntry,
    UserMemoryOrigin,
    canonical_memory_key,
    memory_entry_id,
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.spec("GLO-M5-P0-001", "GLO-M5-P0-002", "GLO-M5-P0-004", "GLO-M5-NFR-002"),
]


@dataclass
class _M5Store:
    owner: LocalUser
    other: LocalUser
    sessions: dict[str, LocalSession]
    owners: dict[str, str] = field(default_factory=dict)
    turns: dict[str, list[ConversationTurn]] = field(default_factory=dict)
    entries: list[UserMemoryEntry] = field(default_factory=list)

    async def session_for_token_hash(self, *, token_hash: str) -> LocalSession | None:
        return self.sessions.get(token_hash)

    async def thread_owner(self, *, thread_id: str) -> str | None:
        return self.owners.get(thread_id)

    async def list_user_threads(
        self, *, user_id: str, limit: int = 32
    ) -> tuple[ConversationThreadInfo, ...]:
        return tuple(
            ConversationThreadInfo(
                thread_id=thread_id,
                turn_count=len(self.turns.get(thread_id, [])),
            )
            for thread_id, owner_id in self.owners.items()
            if owner_id == user_id
        )[:limit]

    async def list_conversation_turn_page(
        self,
        *,
        thread_id: str,
        limit: int = 24,
        before_ordinal: int | None = None,
    ) -> ConversationTurnPage:
        turns = tuple(self.turns.get(thread_id, []))
        if before_ordinal is not None:
            turns = tuple(turn for turn in turns if turn.ordinal < before_ordinal)
        return ConversationTurnPage(
            thread_id=thread_id,
            turns=turns[-limit:],
            next_before_ordinal=None,
        )

    async def delete_user_thread(self, *, user_id: str, thread_id: str) -> bool:
        if self.owners.get(thread_id) != user_id:
            return False
        del self.owners[thread_id]
        self.entries = [entry for entry in self.entries if entry.source_thread_id != thread_id]
        return True

    async def list_active_memory(
        self, *, user_id: str, limit: int = 64
    ) -> tuple[UserMemoryEntry, ...]:
        return tuple(entry for entry in self.entries if entry.user_id == user_id)[:limit]

    async def create_manual_memory(
        self,
        *,
        user_id: str,
        category: UserMemoryCategory,
        content: str,
    ) -> UserMemoryEntry:
        entry = UserMemoryEntry(
            user_id=user_id,
            entry_id=memory_entry_id(category=category, content=content),
            category=category,
            origin=UserMemoryOrigin.MANUAL,
            canonical_key=canonical_memory_key(category=category, content=content),
            content=content,
            source_thread_id=None,
            source_ordinal=None,
            confidence=1.0,
            revision=1,
        )
        self.entries.append(entry)
        return entry

    async def replace_memory_as_manual(
        self,
        *,
        user_id: str,
        entry_id: str,
        category: UserMemoryCategory,
        content: str,
    ) -> UserMemoryEntry | None:
        for index, existing in enumerate(self.entries):
            if existing.user_id == user_id and existing.entry_id == entry_id:
                updated = UserMemoryEntry(
                    user_id=user_id,
                    entry_id=entry_id,
                    category=category,
                    origin=UserMemoryOrigin.MANUAL,
                    canonical_key=canonical_memory_key(category=category, content=content),
                    content=content,
                    source_thread_id=None,
                    source_ordinal=None,
                    confidence=1.0,
                    revision=existing.revision + 1,
                )
                self.entries[index] = updated
                return updated
        return None

    async def tombstone_memory(self, *, user_id: str, entry_id: str) -> bool:
        for index, entry in enumerate(self.entries):
            if entry.user_id == user_id and entry.entry_id == entry_id:
                del self.entries[index]
                return True
        return False


class _UnusedExecutor:
    async def execute_run(self, *args: Any, **kwargs: Any) -> AgentExecution:
        del args, kwargs
        raise AssertionError("management API tests do not execute an agent")


class _Clock:
    def now_utc(self) -> datetime:
        return datetime(2026, 7, 31, tzinfo=UTC)

    def monotonic_ns(self) -> int:
        return 0


def _store() -> tuple[_M5Store, str, str]:
    owner = LocalUser(user_id="user-" + "a" * 32, username="owner.m5")
    other = LocalUser(user_id="user-" + "b" * 32, username="other.m5")
    owner_token, other_token = "a" * 32, "b" * 32
    expiry = datetime.now(UTC) + timedelta(minutes=5)
    store = _M5Store(
        owner=owner,
        other=other,
        sessions={
            session_token_hash(owner_token): LocalSession(user=owner, expires_at=expiry),
            session_token_hash(other_token): LocalSession(user=other, expires_at=expiry),
        },
        owners={"thread-m5-owner": owner.user_id, "thread-m5-other": other.user_id},
        turns={
            "thread-m5-owner": [
                ConversationTurn(
                    thread_id="thread-m5-owner",
                    ordinal=1,
                    role=ConversationRole.USER,
                    display_content="只推荐轻薄本",
                )
            ]
        },
    )
    return store, owner_token, other_token


def _app(store: _M5Store) -> FastAPI:
    return create_durable_agent_app(
        executor=_UnusedExecutor(),
        store=cast(Any, store),
        projector=AgentEventProjector(clock=_Clock()),
        asset_version="m5-test",
        config_fingerprint="a" * 64,
    )


def test_history_and_memory_routes_are_owner_scoped_and_never_return_vectors() -> None:
    store, owner_token, other_token = _store()

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=_app(store), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://m5.test") as client:
            owner_headers = {"cookie": f"glodex_local_session={owner_token}"}
            other_headers = {"cookie": f"glodex_local_session={other_token}"}
            threads = await client.get("/api/v1/conversation-threads", headers=owner_headers)
            assert threads.status_code == 200
            assert threads.json()["threads"] == [{"threadId": "thread-m5-owner", "turnCount": 1}]
            own_turns = await client.get(
                "/api/v1/conversation-threads/thread-m5-owner/turns", headers=owner_headers
            )
            assert own_turns.status_code == 200
            assert own_turns.json()["turns"][0]["content"] == "只推荐轻薄本"
            assert own_turns.json()["turns"][0]["terminalRunId"] is None
            cross_turns = await client.get(
                "/api/v1/conversation-threads/thread-m5-owner/turns", headers=other_headers
            )
            assert cross_turns.status_code == 403

            rejected = await client.post(
                "/api/v1/memory",
                headers=owner_headers,
                json={"category": "blacklist", "content": "不要塑料"},
            )
            assert rejected.status_code == 422

            created = await client.post(
                "/api/v1/memory",
                headers=owner_headers,
                json={"category": "blacklist", "content": "brand:brand-a"},
            )
            assert created.status_code == 201
            entry_id = created.json()["entryId"]
            assert entry_id == memory_entry_id(
                category=UserMemoryCategory.BLACKLIST, content="brand:brand-a"
            )
            assert created.json()["origin"] == "manual"
            assert "vector" not in created.text and "score" not in created.text
            listed = await client.get("/api/v1/memory", headers=owner_headers)
            assert listed.json()["schemaVersion"] == "glodex.user-memory.memory-list.v3"
            assert [entry["entryId"] for entry in listed.json()["activeEntries"]] == [entry_id]
            updated = await client.put(
                f"/api/v1/memory/{entry_id}",
                headers=owner_headers,
                json={"category": "preference", "content": "brand-a"},
            )
            assert updated.status_code == 200 and updated.json()["revision"] == 2
            assert (
                await client.delete(f"/api/v1/memory/{entry_id}", headers=other_headers)
            ).status_code == 403
            assert (
                await client.delete(f"/api/v1/memory/{entry_id}", headers=owner_headers)
            ).status_code == 204

    asyncio.run(scenario())


def test_deleting_a_thread_removes_only_current_memory_derived_from_that_thread() -> None:
    store, owner_token, _other_token = _store()
    derived = UserMemoryEntry(
        user_id=store.owner.user_id,
        entry_id="mem-" + "1" * 24,
        category=UserMemoryCategory.PREFERENCE,
        origin=UserMemoryOrigin.EXPLICIT_REFLECT,
        canonical_key=canonical_memory_key(
            category=UserMemoryCategory.PREFERENCE,
            content="travel",
        ),
        content="travel",
        source_thread_id="thread-m5-owner",
        source_ordinal=1,
        confidence=1.0,
        revision=1,
    )
    manual = UserMemoryEntry(
        user_id=store.owner.user_id,
        entry_id="mem-" + "2" * 24,
        category=UserMemoryCategory.PREFERENCE,
        origin=UserMemoryOrigin.MANUAL,
        canonical_key=canonical_memory_key(category=UserMemoryCategory.PREFERENCE, content="quiet"),
        content="quiet",
        source_thread_id=None,
        source_ordinal=None,
        confidence=1.0,
        revision=1,
    )
    store.entries = [derived, manual]

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=_app(store), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://m5.test") as client:
            before = await client.get(
                "/api/v1/memory", headers={"cookie": f"glodex_local_session={owner_token}"}
            )
            assert [entry["content"] for entry in before.json()["activeEntries"]] == [
                "travel",
                "quiet",
            ]
            response = await client.delete(
                "/api/v1/conversation-threads/thread-m5-owner",
                headers={"cookie": f"glodex_local_session={owner_token}"},
            )
            assert response.status_code == 204
            memory = await client.get(
                "/api/v1/memory", headers={"cookie": f"glodex_local_session={owner_token}"}
            )
            assert memory.status_code == 200
            assert [entry["content"] for entry in memory.json()["activeEntries"]] == ["quiet"]

    asyncio.run(scenario())
