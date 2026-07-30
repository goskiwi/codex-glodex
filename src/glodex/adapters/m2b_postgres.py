"""Fixed-loopback PostgreSQL truth store for the opt-in M2b runtime."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast
from urllib.parse import urlsplit

from glodex.adapters.m2b_migrations import apply_migrations
from glodex.application.durable.contracts import (
    TERMINAL_RUN_STATES,
    DurableCheckpoint,
    DurableCheckpointState,
    DurableEvent,
    DurableProfileSnapshot,
    DurableRun,
    DurableRunState,
    DurableStoreError,
    validate_identifier,
)
from glodex.application.m2a_profile import M2aProfileEntry

_DEFAULT_DSN: Final = "postgresql://glodex@127.0.0.1:5433/glodex"
_LEASE_SECONDS: Final = 300


def validate_postgres_dsn(value: object) -> str:
    """Accept only the one M2b local PostgreSQL service without credentials."""

    if type(value) is not str or not value or len(value) > 160:
        raise ValueError("M2b PostgreSQL DSN is invalid")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or parsed.hostname != "127.0.0.1"
        or parsed.port != 5433
        or parsed.username != "glodex"
        or parsed.password is not None
        or parsed.path != "/glodex"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("M2b PostgreSQL DSN must be the fixed loopback service")
    return _DEFAULT_DSN


class M2bPostgresStore:
    """One asyncpg owner with an intentionally finite M2b query surface."""

    def __init__(self, *, dsn: str = _DEFAULT_DSN, pool: Any | None = None) -> None:
        self._dsn = validate_postgres_dsn(dsn)
        self._pool = pool

    async def open(self) -> None:
        if self._pool is not None:
            return
        try:
            import asyncpg  # type: ignore[import-untyped]

            self._pool = await asyncpg.create_pool(
                dsn=self._dsn,
                min_size=1,
                max_size=4,
                command_timeout=5.0,
                max_inactive_connection_lifetime=30.0,
            )
        except Exception as error:
            raise DurableStoreError("M2B_STORE_UNAVAILABLE") from error

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is None:
            return
        try:
            await pool.close()
        except Exception:
            return

    async def health(self) -> None:
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection:
                value = await connection.fetchval("SELECT 1")
            if value != 1:
                raise ValueError("health response is invalid")
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("M2B_STORE_UNAVAILABLE") from error

    async def migrate(self) -> int:
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection, connection.transaction():
                return await apply_migrations(connection)
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("M2B_MIGRATION_FAILED") from error

    async def reserve_run(
        self,
        *,
        run_id: str,
        thread_id: str,
        request_payload: dict[str, object],
        asset_version: str,
        config_fingerprint: str,
        profile_id: str | None,
        profile_revision: int,
    ) -> DurableRun:
        validate_identifier(run_id, name="run ID")
        validate_identifier(thread_id, name="thread ID")
        if profile_id is not None:
            validate_identifier(profile_id, name="profile ID")
        if (
            type(request_payload) is not dict
            or type(asset_version) is not str
            or not asset_version
            or type(config_fingerprint) is not str
            or len(config_fingerprint) != 64
            or type(profile_revision) is not int
            or isinstance(profile_revision, bool)
            or profile_revision < 0
        ):
            raise ValueError("durable run reservation is invalid")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection, connection.transaction():
                row = await connection.fetchrow(
                    """
                        INSERT INTO durable_runs(
                            run_id, thread_id, state, asset_version, config_fingerprint,
                            profile_id, profile_revision, request_payload, lease_expires_at
                        ) VALUES($1, $2, 'ACCEPTED', $3, $4, $5, $6, $7::jsonb, $8)
                        RETURNING *
                        """,
                    run_id,
                    thread_id,
                    asset_version,
                    config_fingerprint,
                    profile_id,
                    profile_revision,
                    _json_text(request_payload),
                    _lease_deadline(),
                )
            return _run_from_row(row)
        except Exception as error:
            if _unique_violation(error):
                raise DurableStoreError("M2B_RUN_ALREADY_ACTIVE") from None
            raise DurableStoreError("M2B_STORE_UNAVAILABLE") from error

    async def load_run(self, *, run_id: str) -> DurableRun:
        validate_identifier(run_id, name="run ID")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection:
                row = await connection.fetchrow(
                    "SELECT * FROM durable_runs WHERE run_id = $1",
                    run_id,
                )
            if row is None:
                raise DurableStoreError("M2B_RUN_NOT_FOUND")
            return _run_from_row(row)
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("M2B_STORE_UNAVAILABLE") from error

    async def load_events(
        self,
        *,
        run_id: str,
        after_sequence: int = 0,
    ) -> tuple[DurableEvent, ...]:
        validate_identifier(run_id, name="run ID")
        if (
            type(after_sequence) is not int
            or isinstance(after_sequence, bool)
            or after_sequence < 0
        ):
            raise ValueError("event cursor is invalid")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection:
                rows = await connection.fetch(
                    """
                    SELECT run_id, sequence, payload, payload_hash
                    FROM durable_events WHERE run_id = $1 AND sequence > $2 ORDER BY sequence ASC
                    """,
                    run_id,
                    after_sequence,
                )
            return tuple(_event_from_row(row) for row in rows)
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("M2B_STORE_UNAVAILABLE") from error

    async def latest_checkpoint(self, *, run_id: str) -> DurableCheckpoint | None:
        validate_identifier(run_id, name="run ID")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection:
                row = await connection.fetchrow(
                    """
                    SELECT * FROM durable_checkpoints WHERE run_id = $1
                    ORDER BY checkpoint_number DESC LIMIT 1
                    """,
                    run_id,
                )
            return None if row is None else _checkpoint_from_row(row)
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("M2B_STORE_UNAVAILABLE") from error

    async def append_event_checkpoint(
        self,
        *,
        event: DurableEvent | None,
        checkpoint: DurableCheckpoint,
        next_state: DurableRunState,
    ) -> DurableRun:
        if type(checkpoint) is not DurableCheckpoint or type(next_state) is not DurableRunState:
            raise TypeError("durable append inputs are invalid")
        if event is not None and type(event) is not DurableEvent:
            raise TypeError("durable event is invalid")
        if event is not None and (
            event.run_id != checkpoint.run_id or event.sequence != checkpoint.event_sequence
        ):
            raise ValueError("event and checkpoint do not share a cursor")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection, connection.transaction():
                current = await connection.fetchrow(
                    "SELECT * FROM durable_runs WHERE run_id = $1 FOR UPDATE",
                    checkpoint.run_id,
                )
                if current is None:
                    raise DurableStoreError("M2B_RUN_NOT_FOUND")
                previous_sequence = int(current["event_sequence"])
                if event is None:
                    if checkpoint.event_sequence != previous_sequence:
                        raise DurableStoreError("M2B_CHECKPOINT_CURSOR_INVALID")
                elif event.sequence != previous_sequence + 1:
                    raise DurableStoreError("M2B_EVENT_SEQUENCE_INVALID")
                checkpoint_number = await _next_checkpoint_number(connection, checkpoint.run_id)
                if checkpoint.checkpoint_number != checkpoint_number:
                    raise DurableStoreError("M2B_CHECKPOINT_SEQUENCE_INVALID")
                if event is not None:
                    await connection.execute(
                        """
                            INSERT INTO durable_events(run_id, sequence, payload, payload_hash)
                            VALUES($1, $2, $3::jsonb, $4)
                            """,
                        event.run_id,
                        event.sequence,
                        _json_text(event.payload),
                        event.payload_hash,
                    )
                await _insert_checkpoint(connection, checkpoint)
                row = await connection.fetchrow(
                    """
                        UPDATE durable_runs SET state = $2, event_sequence = $3,
                            lease_expires_at = $4, updated_at = CURRENT_TIMESTAMP
                        WHERE run_id = $1 RETURNING *
                        """,
                    checkpoint.run_id,
                    next_state.value,
                    checkpoint.event_sequence,
                    _lease_deadline() if next_state not in TERMINAL_RUN_STATES else None,
                )
            return _run_from_row(row)
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("M2B_STORE_WRITE_FAILED") from error

    async def finish_run(
        self,
        *,
        event: DurableEvent,
        checkpoint: DurableCheckpoint,
        state: DurableRunState,
        response: dict[str, object] | None,
        error_code: str | None,
    ) -> DurableRun:
        if state not in TERMINAL_RUN_STATES or type(event) is not DurableEvent:
            raise ValueError("durable terminal transition is invalid")
        if state is DurableRunState.ABORTED and response is not None:
            raise ValueError("aborted run cannot store a response")
        if state is not DurableRunState.ABORTED and type(response) is not dict:
            raise ValueError("business terminal requires response")
        if (
            state in {DurableRunState.COMPLETED, DurableRunState.NO_MATCH}
            and error_code is not None
        ):
            raise ValueError("successful terminal cannot have an error code")
        if state is DurableRunState.ABORTED and (type(error_code) is not str or not error_code):
            raise ValueError("aborted terminal requires an error code")
        if state is DurableRunState.FAILED and (type(error_code) is not str or not error_code):
            raise ValueError("failed terminal requires an error code")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection, connection.transaction():
                current = await connection.fetchrow(
                    "SELECT event_sequence FROM durable_runs WHERE run_id = $1 FOR UPDATE",
                    event.run_id,
                )
                if current is None:
                    raise DurableStoreError("M2B_RUN_NOT_FOUND")
                if event.sequence != int(current["event_sequence"]) + 1:
                    raise DurableStoreError("M2B_EVENT_SEQUENCE_INVALID")
                if checkpoint.state not in {
                    DurableCheckpointState.TERMINAL,
                    DurableCheckpointState.CANCELLED,
                }:
                    raise DurableStoreError("M2B_CHECKPOINT_TERMINAL_INVALID")
                checkpoint_number = await _next_checkpoint_number(connection, event.run_id)
                if checkpoint.checkpoint_number != checkpoint_number:
                    raise DurableStoreError("M2B_CHECKPOINT_SEQUENCE_INVALID")
                await connection.execute(
                    """
                        INSERT INTO durable_events(run_id, sequence, payload, payload_hash)
                        VALUES($1, $2, $3::jsonb, $4)
                        """,
                    event.run_id,
                    event.sequence,
                    _json_text(event.payload),
                    event.payload_hash,
                )
                await _insert_checkpoint(connection, checkpoint)
                row = await connection.fetchrow(
                    """
                        UPDATE durable_runs SET state = $2, event_sequence = $3,
                            terminal_response = $4::jsonb, terminal_error_code = $5,
                            lease_expires_at = NULL, terminal_at = CURRENT_TIMESTAMP,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE run_id = $1 RETURNING *
                        """,
                    event.run_id,
                    state.value,
                    event.sequence,
                    None if response is None else _json_text(response),
                    error_code,
                )
            return _run_from_row(row)
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("M2B_STORE_WRITE_FAILED") from error

    async def request_cancel(self, *, run_id: str) -> DurableRun:
        validate_identifier(run_id, name="run ID")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection, connection.transaction():
                row = await connection.fetchrow(
                    "SELECT * FROM durable_runs WHERE run_id = $1 FOR UPDATE",
                    run_id,
                )
                if row is None:
                    raise DurableStoreError("M2B_RUN_NOT_FOUND")
                current = _run_from_row(row)
                if current.state in TERMINAL_RUN_STATES:
                    return current
                row = await connection.fetchrow(
                    """
                        UPDATE durable_runs SET state = 'CANCEL_REQUESTED', cancel_requested = TRUE,
                            updated_at = CURRENT_TIMESTAMP WHERE run_id = $1 RETURNING *
                        """,
                    run_id,
                )
            return _run_from_row(row)
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("M2B_STORE_WRITE_FAILED") from error

    async def set_recoverable(self, *, run_id: str) -> DurableRun:
        """Convert only a confirmed, non-cancelled active run to RECOVERABLE."""

        checkpoint = await self.latest_checkpoint(run_id=run_id)
        if checkpoint is None or checkpoint.state is not DurableCheckpointState.CONFIRMED:
            raise DurableStoreError("M2B_REMOTE_STEP_UNCERTAIN")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection:
                row = await connection.fetchrow(
                    """
                    UPDATE durable_runs SET state = 'RECOVERABLE', lease_expires_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE run_id = $1 AND cancel_requested = FALSE
                      AND state IN ('ACCEPTED', 'RUNNING', 'RECOVERABLE')
                    RETURNING *
                    """,
                    run_id,
                )
            if row is None:
                raise DurableStoreError("M2B_RUN_NOT_RECOVERABLE")
            return _run_from_row(row)
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("M2B_STORE_WRITE_FAILED") from error

    async def profile_snapshot(self, *, profile_id: str) -> DurableProfileSnapshot:
        validate_identifier(profile_id, name="profile ID")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection:
                revision_row = await connection.fetchrow(
                    "SELECT revision FROM profile_revisions WHERE profile_id = $1",
                    profile_id,
                )
                if revision_row is None:
                    return DurableProfileSnapshot(profile_id=profile_id, revision=0, entries=())
                rows = await connection.fetch(
                    """
                    SELECT entry_id, scope, kind, value, user_vector FROM profile_entries
                    WHERE profile_id = $1 AND deleted_at IS NULL ORDER BY entry_id ASC LIMIT 16
                    """,
                    profile_id,
                )
            return DurableProfileSnapshot(
                profile_id=profile_id,
                revision=int(revision_row["revision"]),
                entries=tuple(
                    M2aProfileEntry(
                        profile_id=profile_id,
                        entry_id=str(row["entry_id"]),
                        scope=str(row["scope"]),
                        kind=str(row["kind"]),
                        value=str(row["value"]),
                        user_vector=tuple(float(item) for item in row["user_vector"]),
                    )
                    for row in rows
                ),
            )
        except Exception as error:
            raise DurableStoreError("M2B_PROFILE_UNAVAILABLE") from error

    async def set_profile_entry(
        self,
        *,
        entry: M2aProfileEntry,
        embedding_model: str,
    ) -> DurableProfileSnapshot:
        if (
            type(entry) is not M2aProfileEntry
            or type(embedding_model) is not str
            or not embedding_model
        ):
            raise ValueError("durable profile entry is invalid")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection, connection.transaction():
                revision_row = await connection.fetchrow(
                    """
                        INSERT INTO profile_revisions(profile_id, revision) VALUES($1, 1)
                        ON CONFLICT(profile_id) DO UPDATE SET
                            revision = profile_revisions.revision + 1,
                            updated_at = CURRENT_TIMESTAMP
                        RETURNING revision
                        """,
                    entry.profile_id,
                )
                revision = int(revision_row["revision"])
                await connection.execute(
                    """
                        INSERT INTO profile_entries(
                            profile_id, entry_id, scope, kind, value, user_vector,
                            embedding_model, embedding_dimension, revision, deleted_at
                        ) VALUES($1, $2, $3, $4, $5, $6::real[], $7, 1024, $8, NULL)
                        ON CONFLICT(profile_id, entry_id) DO UPDATE SET
                            scope = EXCLUDED.scope, kind = EXCLUDED.kind, value = EXCLUDED.value,
                            user_vector = EXCLUDED.user_vector,
                            embedding_model = EXCLUDED.embedding_model,
                            embedding_dimension = EXCLUDED.embedding_dimension,
                            revision = EXCLUDED.revision,
                            deleted_at = NULL, updated_at = CURRENT_TIMESTAMP
                        """,
                    entry.profile_id,
                    entry.entry_id,
                    entry.scope,
                    entry.kind,
                    entry.value,
                    list(entry.user_vector),
                    embedding_model,
                    revision,
                )
            return await self.profile_snapshot(profile_id=entry.profile_id)
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("M2B_PROFILE_UNAVAILABLE") from error

    async def delete_profile_entry(
        self,
        *,
        profile_id: str,
        entry_id: str,
    ) -> DurableProfileSnapshot:
        validate_identifier(profile_id, name="profile ID")
        validate_identifier(entry_id, name="entry ID")
        pool = await self._require_pool()
        try:
            async with pool.acquire() as connection, connection.transaction():
                changed = await connection.fetchrow(
                    """
                        UPDATE profile_entries SET deleted_at = CURRENT_TIMESTAMP,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE profile_id = $1 AND entry_id = $2 AND deleted_at IS NULL
                        RETURNING entry_id
                        """,
                    profile_id,
                    entry_id,
                )
                if changed is not None:
                    await connection.execute(
                        """
                            INSERT INTO profile_revisions(profile_id, revision) VALUES($1, 1)
                            ON CONFLICT(profile_id) DO UPDATE SET
                                revision = profile_revisions.revision + 1,
                                updated_at = CURRENT_TIMESTAMP
                            """,
                        profile_id,
                    )
            return await self.profile_snapshot(profile_id=profile_id)
        except DurableStoreError:
            raise
        except Exception as error:
            raise DurableStoreError("M2B_PROFILE_UNAVAILABLE") from error

    async def _require_pool(self) -> Any:
        await self.open()
        if self._pool is None:
            raise DurableStoreError("M2B_STORE_UNAVAILABLE")
        return self._pool


async def _next_checkpoint_number(connection: Any, run_id: str) -> int:
    value = await connection.fetchval(
        "SELECT COALESCE(MAX(checkpoint_number), 0) + 1 FROM durable_checkpoints WHERE run_id = $1",
        run_id,
    )
    if type(value) is not int or value < 1:
        raise DurableStoreError("M2B_CHECKPOINT_SEQUENCE_INVALID")
    return value


async def _insert_checkpoint(connection: Any, checkpoint: DurableCheckpoint) -> None:
    await connection.execute(
        """
        INSERT INTO durable_checkpoints(
            run_id, checkpoint_number, state, event_sequence, runtime_payload, asset_version,
            config_fingerprint, profile_revision, payload_hash
        ) VALUES($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9)
        """,
        checkpoint.run_id,
        checkpoint.checkpoint_number,
        checkpoint.state.value,
        checkpoint.event_sequence,
        _json_text(checkpoint.runtime_payload),
        checkpoint.asset_version,
        checkpoint.config_fingerprint,
        checkpoint.profile_revision,
        checkpoint.payload_hash,
    )


def _run_from_row(row: Any) -> DurableRun:
    if row is None:
        raise DurableStoreError("M2B_RUN_NOT_FOUND")
    return DurableRun(
        run_id=str(row["run_id"]),
        thread_id=str(row["thread_id"]),
        state=DurableRunState(str(row["state"])),
        attempt=int(row["attempt"]),
        event_sequence=int(row["event_sequence"]),
        asset_version=str(row["asset_version"]),
        config_fingerprint=str(row["config_fingerprint"]),
        profile_id=None if row["profile_id"] is None else str(row["profile_id"]),
        profile_revision=int(row["profile_revision"]),
        request_payload=_json_object(row["request_payload"]),
        terminal_response=(
            None if row["terminal_response"] is None else _json_object(row["terminal_response"])
        ),
        terminal_error_code=(
            None if row["terminal_error_code"] is None else str(row["terminal_error_code"])
        ),
        cancel_requested=bool(row["cancel_requested"]),
    )


def _event_from_row(row: Any) -> DurableEvent:
    payload = _json_object(row["payload"])
    return DurableEvent(
        run_id=str(row["run_id"]),
        sequence=int(row["sequence"]),
        payload=payload,
        payload_hash=str(row["payload_hash"]),
    )


def _checkpoint_from_row(row: Any) -> DurableCheckpoint:
    checkpoint = DurableCheckpoint(
        run_id=str(row["run_id"]),
        checkpoint_number=int(row["checkpoint_number"]),
        state=DurableCheckpointState(str(row["state"])),
        event_sequence=int(row["event_sequence"]),
        runtime_payload=_json_object(row["runtime_payload"]),
        asset_version=str(row["asset_version"]),
        config_fingerprint=str(row["config_fingerprint"]),
        profile_revision=int(row["profile_revision"]),
        payload_hash=str(row["payload_hash"]),
    )
    return checkpoint


def _json_text(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_object(value: object) -> dict[str, object]:
    if type(value) is dict:
        return cast("dict[str, object]", value)
    if type(value) is str:
        decoded = json.loads(value)
        if type(decoded) is dict:
            return cast("dict[str, object]", decoded)
    raise DurableStoreError("M2B_STORE_DATA_INVALID")


def _lease_deadline() -> datetime:
    return datetime.now(UTC) + timedelta(seconds=_LEASE_SECONDS)


def _unique_violation(error: Exception) -> bool:
    return getattr(error, "sqlstate", None) == "23505"


__all__ = [
    "M2bPostgresStore",
    "validate_postgres_dsn",
]
