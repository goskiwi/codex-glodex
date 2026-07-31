"""Forward-only, checksummed SQL migrations owned by the M2b adapter."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Final

from glodex.application.durable.contracts import DurableStoreError


@dataclass(frozen=True, slots=True)
class M2bMigration:
    version: int
    sql: str

    @property
    def checksum(self) -> str:
        return sha256(self.sql.encode("utf-8")).hexdigest()


MIGRATIONS: Final = (
    M2bMigration(
        version=1,
        sql="""
        CREATE TABLE IF NOT EXISTS glodex_m2b_schema_migrations (
            version INTEGER PRIMARY KEY CHECK (version > 0),
            checksum CHAR(64) NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS durable_runs (
            run_id VARCHAR(64) PRIMARY KEY,
            thread_id VARCHAR(64) NOT NULL,
            state VARCHAR(32) NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
            event_sequence INTEGER NOT NULL DEFAULT 0 CHECK (event_sequence >= 0),
            asset_version VARCHAR(128) NOT NULL,
            config_fingerprint CHAR(64) NOT NULL,
            profile_id VARCHAR(64),
            profile_revision INTEGER NOT NULL DEFAULT 0 CHECK (profile_revision >= 0),
            request_payload JSONB NOT NULL,
            terminal_response JSONB,
            terminal_error_code VARCHAR(96),
            cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
            lease_expires_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            terminal_at TIMESTAMPTZ
        );

        CREATE UNIQUE INDEX IF NOT EXISTS durable_runs_active_thread_ux
        ON durable_runs(thread_id)
        WHERE state IN ('ACCEPTED', 'RUNNING', 'RECOVERABLE', 'CANCEL_REQUESTED');

        CREATE TABLE IF NOT EXISTS durable_events (
            run_id VARCHAR(64) NOT NULL REFERENCES durable_runs(run_id) ON DELETE RESTRICT,
            sequence INTEGER NOT NULL CHECK (sequence >= 1),
            payload JSONB NOT NULL,
            payload_hash CHAR(64) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (run_id, sequence)
        );

        CREATE TABLE IF NOT EXISTS durable_checkpoints (
            run_id VARCHAR(64) NOT NULL REFERENCES durable_runs(run_id) ON DELETE RESTRICT,
            checkpoint_number INTEGER NOT NULL CHECK (checkpoint_number >= 1),
            state VARCHAR(32) NOT NULL,
            event_sequence INTEGER NOT NULL CHECK (event_sequence >= 0),
            runtime_payload JSONB NOT NULL,
            asset_version VARCHAR(128) NOT NULL,
            config_fingerprint CHAR(64) NOT NULL,
            profile_revision INTEGER NOT NULL CHECK (profile_revision >= 0),
            payload_hash CHAR(64) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (run_id, checkpoint_number)
        );

        CREATE TABLE IF NOT EXISTS profile_revisions (
            profile_id VARCHAR(64) PRIMARY KEY,
            revision INTEGER NOT NULL CHECK (revision >= 0),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS profile_entries (
            profile_id VARCHAR(64) NOT NULL REFERENCES profile_revisions(profile_id)
                ON DELETE RESTRICT,
            entry_id VARCHAR(64) NOT NULL,
            scope VARCHAR(16) NOT NULL CHECK (scope = 'soft'),
            kind VARCHAR(32) NOT NULL CHECK (kind = 'preference'),
            value TEXT NOT NULL CHECK (char_length(value) BETWEEN 1 AND 512),
            user_vector REAL[] NOT NULL CHECK (array_length(user_vector, 1) = 1024),
            embedding_model VARCHAR(128) NOT NULL,
            embedding_dimension INTEGER NOT NULL CHECK (embedding_dimension = 1024),
            revision INTEGER NOT NULL CHECK (revision >= 1),
            deleted_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (profile_id, entry_id)
        );
        """.strip(),
    ),
)


async def apply_migrations(connection: object) -> int:
    """Apply only the exact known SQL sequence through one asyncpg connection."""

    if not hasattr(connection, "execute") or not hasattr(connection, "fetchrow"):
        raise TypeError("M2b migrations require an asyncpg-style connection")
    try:
        execute = connection.execute
        fetchrow = connection.fetchrow
        await execute(
            """
            CREATE TABLE IF NOT EXISTS glodex_m2b_schema_migrations (
                version INTEGER PRIMARY KEY CHECK (version > 0),
                checksum CHAR(64) NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        applied = 0
        for migration in MIGRATIONS:
            row = await fetchrow(
                "SELECT checksum FROM glodex_m2b_schema_migrations WHERE version = $1",
                migration.version,
            )
            if row is not None:
                if row["checksum"] != migration.checksum:
                    raise DurableStoreError("M2B_MIGRATION_MISMATCH")
                continue
            await execute(migration.sql)
            await execute(
                "INSERT INTO glodex_m2b_schema_migrations(version, checksum) VALUES($1, $2)",
                migration.version,
                migration.checksum,
            )
            applied += 1
        return applied
    except DurableStoreError:
        raise
    except Exception as error:
        raise DurableStoreError("M2B_MIGRATION_FAILED") from error


__all__ = ["MIGRATIONS", "M2bMigration", "apply_migrations"]
