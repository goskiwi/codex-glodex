"""Offline M5 evidence for local sessions and owner-bound durable access."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from glodex.agent.contracts import AgentExecution
from glodex.api.agent_events import AgentEventProjector
from glodex.api.durable import create_durable_agent_app
from glodex.api.identity_contracts import LocalCredentials
from glodex.memory.identity import (
    LocalSession,
    LocalUser,
    hash_password,
    new_session_token,
    new_user_id,
    password_matches,
    session_expiry,
    session_token_hash,
    validate_username,
)
from glodex.runtime.contracts import DurableRun, DurableRunState, DurableStoreError, LoopKind

pytestmark = [
    pytest.mark.contract,
    pytest.mark.spec("GLO-M5-P0-001", "GLO-M5-NFR-001", "GLO-M5-NFR-002", "GLO-M5-NFR-003"),
]


@dataclass
class _IdentityStore:
    users: dict[str, tuple[LocalUser, str]] = field(default_factory=dict)
    sessions: dict[str, LocalSession] = field(default_factory=dict)
    revoked: set[str] = field(default_factory=set)
    deleted: set[str] = field(default_factory=set)
    owners: dict[str, str] = field(default_factory=dict)

    async def register_local_user(self, *, user: LocalUser, password_hash: str) -> LocalUser:
        if user.username in {item.username for item, _ in self.users.values()}:
            raise DurableStoreError("DURABLE_USERNAME_TAKEN")
        self.users[user.user_id] = (user, password_hash)
        return user

    async def password_hash_for_username(self, *, username: str) -> tuple[LocalUser, str] | None:
        for user, password_hash in self.users.values():
            if user.username == username and user.user_id not in self.deleted:
                return user, password_hash
        return None

    async def issue_local_session(
        self,
        *,
        token_hash: str,
        user_id: str,
        expires_at: datetime,
    ) -> LocalSession:
        user = self.users[user_id][0]
        session = LocalSession(user=user, expires_at=expires_at)
        self.sessions[token_hash] = session
        return session

    async def session_for_token_hash(self, *, token_hash: str) -> LocalSession | None:
        session = self.sessions.get(token_hash)
        if (
            session is None
            or token_hash in self.revoked
            or session.user.user_id in self.deleted
            or session.expires_at <= datetime.now(UTC)
        ):
            return None
        return session

    async def revoke_local_session(self, *, token_hash: str) -> None:
        self.revoked.add(token_hash)

    async def delete_local_user(self, *, user_id: str) -> None:
        self.deleted.add(user_id)
        self.revoked.update(
            token_hash
            for token_hash, session in self.sessions.items()
            if session.user.user_id == user_id
        )

    async def bind_user_thread(self, *, user_id: str, thread_id: str) -> None:
        owner = self.owners.get(thread_id)
        if owner is not None and owner != user_id:
            raise DurableStoreError("DURABLE_OWNER_FORBIDDEN")
        self.owners[thread_id] = user_id

    async def thread_owner(self, *, thread_id: str) -> str | None:
        return self.owners.get(thread_id)

    async def load_run(self, *, run_id: str) -> DurableRun:
        return DurableRun(
            run_id=run_id,
            thread_id="thread-m5-owned" if run_id == "run-m5" else "thread-m5-legacy",
            root_run_id=run_id,
            parent_run_id=None,
            child_id=None,
            depth=0,
            loop_kind=LoopKind.ROOT,
            task_scope_digest=None,
            state=DurableRunState.ACCEPTED,
            attempt=1,
            event_sequence=0,
            asset_version="m5-test",
            config_fingerprint="a" * 64,
            request_payload={},
            terminal_response=None,
            terminal_error_code=None,
            cancel_requested=False,
        )


class _UnusedExecutor:
    async def execute_run(self, *args: Any, **kwargs: Any) -> AgentExecution:
        del args, kwargs
        raise AssertionError("identity tests must not start a durable run")


class _Clock:
    def now_utc(self) -> datetime:
        return datetime(2026, 7, 31, tzinfo=UTC)

    def monotonic_ns(self) -> int:
        return 0


def _app(store: _IdentityStore) -> FastAPI:
    return create_durable_agent_app(
        executor=_UnusedExecutor(),
        store=cast(Any, store),
        projector=AgentEventProjector(clock=_Clock()),
        asset_version="m5-test",
        config_fingerprint="a" * 64,
    )


async def _request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    **kwargs: Any,
) -> httpx.Response:
    return await client.request(method, path, **kwargs)


def test_identity_primitives_are_opaque_and_argon2id() -> None:
    password = "m5-local-passphrase"
    password_hash = hash_password(password)
    token = new_session_token()

    assert password_hash.startswith("$argon2id$")
    assert password_matches(password=password, password_hash=password_hash)
    assert not password_matches(password="different-passphrase", password_hash=password_hash)
    short_password_hash = hash_password("1")
    assert password_matches(password="1", password_hash=short_password_hash)
    assert new_user_id().startswith("user-")
    assert len(session_token_hash(token)) == 64
    assert session_expiry(now=datetime(2026, 7, 31, tzinfo=UTC)) == datetime(2026, 8, 7, tzinfo=UTC)
    for invalid in ("ab", "name with space", "中文名称", "x" * 33):
        with pytest.raises(ValueError):
            validate_username(invalid)


def test_local_password_has_no_complexity_or_minimum_length_policy() -> None:
    assert LocalCredentials(username="kkqq", password="1").password == "1"
    with pytest.raises(ValidationError):
        LocalCredentials(username="kkqq", password="")
    with pytest.raises(ValidationError):
        LocalCredentials(username="kkqq", password="x" * 129)
    with pytest.raises(ValidationError):
        LocalCredentials(username="kkqq", password="bad\0password")


def test_register_login_logout_and_delete_do_not_expose_a_bearer_token() -> None:
    store = _IdentityStore()

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=_app(store), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://durable.test") as client:
            registered = await _request(
                client,
                "POST",
                "/api/v1/local-auth/register",
                json={"username": "alice.m5", "password": "m5-local-passphrase"},
            )
            assert registered.status_code == 201
            assert registered.json()["username"] == "alice.m5"
            cookie = registered.headers["set-cookie"].lower()
            assert "httponly" in cookie and "samesite=strict" in cookie and "secure" not in cookie
            assert "glodex_local_session" not in registered.text
            me = await _request(client, "GET", "/api/v1/local-auth/me")
            assert me.status_code == 200
            duplicate = await _request(
                client,
                "POST",
                "/api/v1/local-auth/register",
                json={"username": "alice.m5", "password": "m5-local-passphrase"},
            )
            assert duplicate.status_code == 409
            wrong = await _request(
                client,
                "POST",
                "/api/v1/local-auth/login",
                json={"username": "alice.m5", "password": "not-the-right-password"},
            )
            assert wrong.status_code == 401
            logged_out = await _request(client, "POST", "/api/v1/local-auth/logout")
            assert logged_out.status_code == 204
            after_logout = await _request(client, "GET", "/api/v1/local-auth/me")
            assert after_logout.status_code == 401
            login = await _request(
                client,
                "POST",
                "/api/v1/local-auth/login",
                json={"username": "alice.m5", "password": "m5-local-passphrase"},
            )
            assert login.status_code == 200
            deleted = await _request(client, "DELETE", "/api/v1/local-auth/me")
            assert deleted.status_code == 204
            assert (await _request(client, "GET", "/api/v1/local-auth/me")).status_code == 401

    asyncio.run(scenario())


@pytest.mark.acceptance
@pytest.mark.spec("M5-AC-001")
def test_every_durable_thread_needs_its_authenticated_owner() -> None:
    store = _IdentityStore()
    owner = LocalUser(user_id="user-" + "1" * 32, username="owner.m5")
    other = LocalUser(user_id="user-" + "2" * 32, username="other.m5")
    owner_hash = session_token_hash("a" * 32)
    other_hash = session_token_hash("b" * 32)
    store.users[owner.user_id] = (owner, hash_password("owner-m5-passphrase"))
    store.users[other.user_id] = (other, hash_password("other-m5-passphrase"))
    store.sessions[owner_hash] = LocalSession(
        user=owner,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    store.sessions[other_hash] = LocalSession(
        user=other,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    store.owners["thread-m5-owned"] = owner.user_id

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=_app(store), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://durable.test") as client:
            legacy = await _request(client, "GET", "/api/v1/durable-agent-runs/run-m5-legacy")
            assert legacy.status_code == 401
            assert (
                await _request(client, "GET", "/api/v1/durable-agent-runs/run-m5")
            ).status_code == 401
            client.cookies.set("glodex_local_session", "b" * 32)
            forbidden = await _request(client, "GET", "/api/v1/durable-agent-runs/run-m5")
            assert forbidden.status_code == 403
            client.cookies.set("glodex_local_session", "a" * 32)
            owned = await _request(client, "GET", "/api/v1/durable-agent-runs/run-m5")
            assert owned.status_code == 200
            assert (
                await _request(client, "GET", "/api/v1/durable-agent-runs/run-m5-legacy")
            ).status_code == 403

    asyncio.run(scenario())
