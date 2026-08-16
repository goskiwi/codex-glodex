"""Forward-only, checksummed SQL migrations owned by the Durable adapter."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Final

from glodex.runtime.contracts import DurableStoreError


@dataclass(frozen=True, slots=True)
class DurableMigration:
    version: int
    sql: str

    @property
    def checksum(self) -> str:
        return sha256(self.sql.encode("utf-8")).hexdigest()


MIGRATIONS: Final = (
    DurableMigration(
        version=1,
        sql="""
        CREATE TABLE IF NOT EXISTS glodex_durable_schema_migrations (
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
    DurableMigration(
        version=2,
        sql="""
        CREATE TABLE IF NOT EXISTS local_users (
            user_id VARCHAR(64) PRIMARY KEY,
            username VARCHAR(32) NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            deleted_at TIMESTAMPTZ
        );

        CREATE TABLE IF NOT EXISTS local_sessions (
            token_hash CHAR(64) PRIMARY KEY,
            user_id VARCHAR(64) NOT NULL REFERENCES local_users(user_id) ON DELETE RESTRICT,
            expires_at TIMESTAMPTZ NOT NULL,
            revoked_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS local_sessions_active_user_ix
        ON local_sessions(user_id, expires_at) WHERE revoked_at IS NULL;

        CREATE TABLE IF NOT EXISTS user_profile_bindings (
            user_id VARCHAR(64) PRIMARY KEY REFERENCES local_users(user_id) ON DELETE RESTRICT,
            profile_id VARCHAR(64) NOT NULL UNIQUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            deleted_at TIMESTAMPTZ
        );

        CREATE TABLE IF NOT EXISTS user_thread_bindings (
            thread_id VARCHAR(64) PRIMARY KEY,
            user_id VARCHAR(64) NOT NULL REFERENCES local_users(user_id) ON DELETE RESTRICT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            deleted_at TIMESTAMPTZ
        );
        CREATE INDEX IF NOT EXISTS user_thread_bindings_owner_ix
        ON user_thread_bindings(user_id, created_at DESC) WHERE deleted_at IS NULL;

        CREATE TABLE IF NOT EXISTS conversation_turns (
            thread_id VARCHAR(64) NOT NULL
                REFERENCES user_thread_bindings(thread_id) ON DELETE RESTRICT,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
            role VARCHAR(16) NOT NULL CHECK (role IN ('user', 'assistant')),
            display_content TEXT NOT NULL CHECK (char_length(display_content) BETWEEN 1 AND 2000),
            terminal_run_id VARCHAR(64) REFERENCES durable_runs(run_id) ON DELETE RESTRICT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            deleted_at TIMESTAMPTZ,
            PRIMARY KEY(thread_id, ordinal)
        );

        CREATE TABLE IF NOT EXISTS thread_summaries (
            thread_id VARCHAR(64) PRIMARY KEY
                REFERENCES user_thread_bindings(thread_id) ON DELETE RESTRICT,
            revision INTEGER NOT NULL CHECK (revision >= 1),
            covered_through_ordinal INTEGER NOT NULL CHECK (covered_through_ordinal >= 1),
            summary_json JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            deleted_at TIMESTAMPTZ
        );

        CREATE TABLE IF NOT EXISTS user_memory_entries (
            user_id VARCHAR(64) NOT NULL REFERENCES local_users(user_id) ON DELETE RESTRICT,
            entry_id VARCHAR(64) NOT NULL,
            category VARCHAR(16) NOT NULL
                CHECK (category IN ('preference', 'blacklist', 'history')),
            canonical_key VARCHAR(128) NOT NULL,
            content TEXT NOT NULL CHECK (char_length(content) BETWEEN 1 AND 512),
            source_thread_id VARCHAR(64)
                REFERENCES user_thread_bindings(thread_id) ON DELETE RESTRICT,
            source_ordinal INTEGER CHECK (source_ordinal >= 1),
            confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
            revision INTEGER NOT NULL CHECK (revision >= 1),
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            deleted_at TIMESTAMPTZ,
            PRIMARY KEY(user_id, entry_id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS user_memory_entries_active_key_ux
        ON user_memory_entries(user_id, category, canonical_key) WHERE deleted_at IS NULL;
        """.strip(),
    ),
    DurableMigration(
        version=3,
        sql="""
        CREATE TABLE IF NOT EXISTS m6_run_traces (
            run_id VARCHAR(64) PRIMARY KEY REFERENCES durable_runs(run_id) ON DELETE RESTRICT,
            schema_version VARCHAR(64) NOT NULL,
            event_sequence INTEGER NOT NULL DEFAULT 0
                CHECK (event_sequence >= 0 AND event_sequence <= 96),
            terminal_state VARCHAR(96),
            export_attempted BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            terminal_at TIMESTAMPTZ,
            expires_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP + INTERVAL '14 days'
        );

        CREATE TABLE IF NOT EXISTS m6_trace_events (
            run_id VARCHAR(64) NOT NULL REFERENCES m6_run_traces(run_id) ON DELETE RESTRICT,
            sequence INTEGER NOT NULL CHECK (sequence >= 1 AND sequence <= 96),
            kind VARCHAR(32) NOT NULL,
            operation VARCHAR(32),
            outcome VARCHAR(16),
            safe_code VARCHAR(96),
            duration_ms INTEGER CHECK (duration_ms >= 0 AND duration_ms <= 120000),
            version VARCHAR(128),
            receipt_status VARCHAR(16),
            provider VARCHAR(32),
            model VARCHAR(128),
            input_tokens INTEGER CHECK (input_tokens >= 0),
            output_tokens INTEGER CHECK (output_tokens >= 0),
            total_tokens INTEGER CHECK (total_tokens >= 0),
            cost_status VARCHAR(16),
            price_table_version VARCHAR(128),
            currency CHAR(3),
            input_micro_units BIGINT CHECK (input_micro_units >= 0),
            output_micro_units BIGINT CHECK (output_micro_units >= 0),
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (run_id, sequence)
        );

        CREATE TABLE IF NOT EXISTS m6_breakers (
            operation VARCHAR(32) PRIMARY KEY,
            state VARCHAR(16) NOT NULL,
            failure_timestamps TIMESTAMPTZ[] NOT NULL DEFAULT '{}',
            cooldown_until TIMESTAMPTZ,
            half_open_probe BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS m6_metric_buckets (
            window_key VARCHAR(3) NOT NULL CHECK (window_key IN ('1h', '24h')),
            bucket_start TIMESTAMPTZ NOT NULL,
            completed_count INTEGER NOT NULL DEFAULT 0 CHECK (completed_count >= 0),
            no_match_count INTEGER NOT NULL DEFAULT 0 CHECK (no_match_count >= 0),
            failed_count INTEGER NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
            aborted_count INTEGER NOT NULL DEFAULT 0 CHECK (aborted_count >= 0),
            operation_failure_count INTEGER NOT NULL DEFAULT 0 CHECK (operation_failure_count >= 0),
            receipt_reported_count INTEGER NOT NULL DEFAULT 0 CHECK (receipt_reported_count >= 0),
            receipt_unavailable_count INTEGER NOT NULL DEFAULT 0
                CHECK (receipt_unavailable_count >= 0),
            cost_reported_micro_units BIGINT NOT NULL DEFAULT 0
                CHECK (cost_reported_micro_units >= 0),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP + INTERVAL '30 days',
            PRIMARY KEY (window_key, bucket_start)
        );

        CREATE TABLE IF NOT EXISTS m6_price_tables (
            model VARCHAR(128) NOT NULL,
            price_table_version VARCHAR(128) NOT NULL,
            currency CHAR(3) NOT NULL,
            input_micro_units_per_token BIGINT NOT NULL CHECK (input_micro_units_per_token >= 0),
            output_micro_units_per_token BIGINT NOT NULL CHECK (output_micro_units_per_token >= 0),
            source_label VARCHAR(128) NOT NULL,
            effective_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (model, price_table_version)
        );
        """.strip(),
    ),
    DurableMigration(
        version=4,
        sql="""
        CREATE TABLE IF NOT EXISTS m6_maintenance_alerts (
            code VARCHAR(32) PRIMARY KEY CHECK (code = 'RETENTION_FAILED'),
            active BOOLEAN NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """.strip(),
    ),
    DurableMigration(
        version=5,
        sql="""
        CREATE TABLE IF NOT EXISTS m7_evaluations (
            run_id VARCHAR(64) PRIMARY KEY
                REFERENCES durable_runs(run_id) ON DELETE RESTRICT,
            schema_version VARCHAR(128) NOT NULL,
            state VARCHAR(32) NOT NULL CHECK (state IN (
                'NOT_EVALUABLE', 'PENDING', 'RUBRIC_IN_FLIGHT', 'RUBRIC_READY',
                'JUDGE_IN_FLIGHT', 'REPORTED', 'P0_FAILED', 'UNAVAILABLE'
            )),
            rubric_schema_version VARCHAR(128) NOT NULL,
            model_version VARCHAR(128),
            rubric_fingerprint CHAR(64),
            p0_failures VARCHAR(64)[] NOT NULL DEFAULT '{}',
            p1_failures VARCHAR(64)[] NOT NULL DEFAULT '{}',
            need_coverage_score SMALLINT CHECK (need_coverage_score BETWEEN 1 AND 5),
            need_coverage_reason VARCHAR(32),
            scenario_fit_score SMALLINT CHECK (scenario_fit_score BETWEEN 1 AND 5),
            scenario_fit_reason VARCHAR(32),
            decision_value_score SMALLINT CHECK (decision_value_score BETWEEN 1 AND 5),
            decision_value_reason VARCHAR(32),
            reward SMALLINT CHECK (reward BETWEEN 0 AND 100),
            training_candidate BOOLEAN NOT NULL DEFAULT FALSE,
            safe_code VARCHAR(96),
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP + INTERVAL '14 days',
            CHECK (
                (state = 'REPORTED' AND model_version IS NOT NULL
                    AND rubric_fingerprint IS NOT NULL AND reward IS NOT NULL
                    AND need_coverage_score IS NOT NULL AND scenario_fit_score IS NOT NULL
                    AND decision_value_score IS NOT NULL)
                OR (state = 'P0_FAILED' AND reward = 0)
                OR (state NOT IN ('REPORTED', 'P0_FAILED'))
            )
        );
        CREATE INDEX IF NOT EXISTS m7_evaluations_expiry_ix
        ON m7_evaluations(expires_at);
        """.strip(),
    ),
    DurableMigration(
        version=6,
        sql="""
        ALTER TABLE user_memory_entries
        ADD COLUMN IF NOT EXISTS origin VARCHAR(32);

        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM user_memory_entries
                WHERE (source_thread_id IS NULL) <> (source_ordinal IS NULL)
            ) THEN
                RAISE EXCEPTION 'user memory provenance is inconsistent';
            END IF;
        END
        $$;

        DELETE FROM user_memory_entries
        WHERE origin IS NULL
          AND source_thread_id IS NOT NULL
          AND source_ordinal IS NOT NULL;

        UPDATE user_memory_entries
        SET origin = 'manual'
        WHERE origin IS NULL;

        ALTER TABLE user_memory_entries
        ALTER COLUMN origin SET NOT NULL;

        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'user_memory_entries_origin_ck'
                  AND conrelid = 'user_memory_entries'::regclass
            ) THEN
                ALTER TABLE user_memory_entries
                ADD CONSTRAINT user_memory_entries_origin_ck CHECK (
                    (origin = 'manual'
                        AND source_thread_id IS NULL AND source_ordinal IS NULL)
                    OR (origin = 'explicit_reflect'
                        AND source_thread_id IS NOT NULL AND source_ordinal IS NOT NULL)
                );
            END IF;
        END
        $$;

        DROP INDEX IF EXISTS user_memory_entries_active_key_ux;
        CREATE UNIQUE INDEX user_memory_entries_active_key_ux
        ON user_memory_entries(user_id, category, canonical_key)
        WHERE deleted_at IS NULL AND origin IN ('manual', 'explicit_reflect');

        CREATE INDEX IF NOT EXISTS user_memory_entries_owner_management_ix
        ON user_memory_entries(user_id, origin, category, canonical_key)
        WHERE deleted_at IS NULL;
        """.strip(),
    ),
    DurableMigration(
        version=7,
        sql="""
        CREATE TABLE IF NOT EXISTS product_title_translations (
            product_id VARCHAR(128) NOT NULL,
            source_hash CHAR(64) NOT NULL
                CHECK (source_hash ~ '^[0-9a-f]{64}$'),
            target_locale VARCHAR(16) NOT NULL CHECK (target_locale = 'zh-CN'),
            translated_title TEXT NOT NULL
                CHECK (char_length(translated_title) BETWEEN 1 AND 96),
            model_version VARCHAR(128) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(product_id, source_hash, target_locale)
        );
        """.strip(),
    ),
    DurableMigration(
        version=8,
        sql="""
        DROP TABLE IF EXISTS m7_evaluations;
        """.strip(),
    ),
    DurableMigration(
        version=9,
        sql="""
        CREATE TABLE m7_offline_summaries (
            run_id VARCHAR(64) PRIMARY KEY
                REFERENCES durable_runs(run_id) ON DELETE RESTRICT,
            schema_version VARCHAR(128) NOT NULL
                CHECK (schema_version = 'glodex.m7-offline-summary.v1'),
            judge_status VARCHAR(16) NOT NULL
                CHECK (judge_status IN ('SCORED', 'P0_FAILED', 'UNSCORED')),
            p0_passed BOOLEAN NOT NULL,
            p0_failure_count SMALLINT NOT NULL
                CHECK (p0_failure_count BETWEEN 0 AND 5),
            p1_failure_count SMALLINT NOT NULL
                CHECK (p1_failure_count BETWEEN 0 AND 4),
            need_coverage_score SMALLINT
                CHECK (need_coverage_score BETWEEN 1 AND 5),
            scenario_fit_score SMALLINT
                CHECK (scenario_fit_score BETWEEN 1 AND 5),
            decision_value_score SMALLINT
                CHECK (decision_value_score BETWEEN 1 AND 5),
            reward SMALLINT CHECK (reward BETWEEN 0 AND 100),
            training_candidate BOOLEAN NOT NULL DEFAULT FALSE,
            judge_model VARCHAR(128) NOT NULL,
            generated_at TIMESTAMPTZ NOT NULL,
            CHECK (p0_passed = (p0_failure_count = 0)),
            CHECK (
                (judge_status = 'SCORED'
                    AND need_coverage_score IS NOT NULL
                    AND scenario_fit_score IS NOT NULL
                    AND decision_value_score IS NOT NULL
                    AND reward IS NOT NULL)
                OR (judge_status = 'P0_FAILED'
                    AND need_coverage_score IS NULL
                    AND scenario_fit_score IS NULL
                    AND decision_value_score IS NULL
                    AND reward = 0)
                OR (judge_status = 'UNSCORED'
                    AND need_coverage_score IS NULL
                    AND scenario_fit_score IS NULL
                    AND decision_value_score IS NULL
                    AND reward IS NULL)
            ),
            CHECK (
                NOT training_candidate
                OR (
                    judge_status = 'SCORED'
                    AND p0_passed
                    AND p1_failure_count = 0
                    AND reward >= 80
                )
            )
        );
        """.strip(),
    ),
    DurableMigration(
        version=10,
        sql="""
        CREATE TABLE IF NOT EXISTS product_evidence (
            canonical_product_id VARCHAR(512) NOT NULL,
            variant_hash CHAR(64) NOT NULL
                CHECK (variant_hash ~ '^[0-9a-f]{64}$'),
            criterion_key VARCHAR(64) NOT NULL,
            source_url VARCHAR(2048) NOT NULL,
            source_content_hash CHAR(64) NOT NULL
                CHECK (source_content_hash ~ '^[0-9a-f]{64}$'),
            snippet TEXT NOT NULL,
            normalized_value TEXT,
            source VARCHAR(512) NOT NULL,
            captured_at TIMESTAMPTZ NOT NULL,
            status VARCHAR(16) NOT NULL
                CHECK (status IN ('KNOWN', 'MISSING', 'CONFLICTED')),
            applicable_model VARCHAR(256),
            applicable_variant VARCHAR(256),
            expires_at TIMESTAMPTZ,
            PRIMARY KEY (
                canonical_product_id,
                variant_hash,
                criterion_key,
                source_url,
                source_content_hash
            )
        );
        CREATE INDEX IF NOT EXISTS idx_product_evidence_lookup
            ON product_evidence (canonical_product_id, criterion_key);
        """.strip(),
    ),
    DurableMigration(
        version=11,
        sql="""
        ALTER TABLE durable_runs
            ADD COLUMN IF NOT EXISTS root_run_id VARCHAR(64),
            ADD COLUMN IF NOT EXISTS parent_run_id VARCHAR(64),
            ADD COLUMN IF NOT EXISTS child_id VARCHAR(64),
            ADD COLUMN IF NOT EXISTS depth SMALLINT,
            ADD COLUMN IF NOT EXISTS loop_kind VARCHAR(16),
            ADD COLUMN IF NOT EXISTS task_scope_digest CHAR(64);

        UPDATE durable_runs
        SET root_run_id = run_id,
            parent_run_id = NULL,
            child_id = NULL,
            depth = 0,
            loop_kind = 'root',
            task_scope_digest = NULL
        WHERE root_run_id IS NULL;

        ALTER TABLE durable_runs
            ALTER COLUMN root_run_id SET NOT NULL,
            ALTER COLUMN depth SET NOT NULL,
            ALTER COLUMN loop_kind SET NOT NULL;

        ALTER TABLE durable_runs
            DROP CONSTRAINT IF EXISTS durable_runs_root_run_fk,
            DROP CONSTRAINT IF EXISTS durable_runs_parent_run_fk,
            DROP CONSTRAINT IF EXISTS durable_runs_tree_shape_ck;

        ALTER TABLE durable_runs
            ADD CONSTRAINT durable_runs_root_run_fk
                FOREIGN KEY (root_run_id) REFERENCES durable_runs(run_id) ON DELETE RESTRICT,
            ADD CONSTRAINT durable_runs_parent_run_fk
                FOREIGN KEY (parent_run_id) REFERENCES durable_runs(run_id) ON DELETE RESTRICT,
            ADD CONSTRAINT durable_runs_tree_shape_ck CHECK (
                (loop_kind = 'root'
                    AND run_id = root_run_id
                    AND parent_run_id IS NULL
                    AND child_id IS NULL
                    AND depth = 0
                    AND task_scope_digest IS NULL)
                OR
                (loop_kind = 'child'
                    AND run_id <> root_run_id
                    AND parent_run_id IS NOT NULL
                    AND child_id IS NOT NULL
                    AND depth BETWEEN 1 AND 2
                    AND task_scope_digest ~ '^[0-9a-f]{64}$')
            );

        CREATE INDEX IF NOT EXISTS durable_runs_tree_root_ix
            ON durable_runs(root_run_id, depth, created_at ASC);
        CREATE UNIQUE INDEX IF NOT EXISTS durable_runs_child_identity_ux
            ON durable_runs(parent_run_id, child_id)
            WHERE loop_kind = 'child';
        """.strip(),
    ),
    DurableMigration(
        version=12,
        sql="""
        WITH classified AS (
            SELECT user_id, entry_id,
                CASE
                    WHEN position('预算' in lower(content)) > 0
                        OR position('budget' in lower(content)) > 0
                    THEN 'preference_budget_range'
                    WHEN lower(content) LIKE ANY (ARRAY[
                        '%平台%', '%amazon%', '%亚马逊%', '%ebay%', '%易贝%',
                        '%shopee%', '%虾皮%', '%aliexpress%', '%速卖通%',
                        '%alibaba%', '%阿里巴巴%', '%walmart%', '%沃尔玛%', '%shein%'
                    ]) AND lower(content) LIKE ANY (ARRAY[
                        '%偏好%', '%倾向%', '%喜欢%', '%习惯%', '%通常%', '%购买%',
                        '%下单%', '%购物%', '%prefer%', '%usually%', '%buy%', '%shop%'
                    ])
                    THEN 'preference_platform'
                    ELSE NULL
                END AS slot_key
            FROM user_memory_entries
            WHERE category = 'preference' AND deleted_at IS NULL
              AND origin IN ('manual', 'explicit_reflect')
        ), ranked AS (
            SELECT classified.user_id, classified.entry_id, classified.slot_key,
                ROW_NUMBER() OVER (
                    PARTITION BY classified.user_id, classified.slot_key
                    ORDER BY (entry.origin = 'manual') DESC,
                        entry.updated_at DESC, classified.entry_id DESC
                ) AS slot_position
            FROM classified
            JOIN user_memory_entries AS entry
              ON entry.user_id = classified.user_id
             AND entry.entry_id = classified.entry_id
            WHERE classified.slot_key IS NOT NULL
        )
        UPDATE user_memory_entries AS entry
        SET deleted_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
        FROM ranked
        WHERE entry.user_id = ranked.user_id AND entry.entry_id = ranked.entry_id
          AND ranked.slot_position > 1;

        UPDATE user_memory_entries
        SET canonical_key = CASE
                WHEN position('预算' in lower(content)) > 0
                    OR position('budget' in lower(content)) > 0
                THEN 'preference_budget_range'
                ELSE 'preference_platform'
            END,
            updated_at = CURRENT_TIMESTAMP
        WHERE category = 'preference' AND deleted_at IS NULL
          AND origin IN ('manual', 'explicit_reflect')
          AND (
              position('预算' in lower(content)) > 0
              OR position('budget' in lower(content)) > 0
              OR (
                  lower(content) LIKE ANY (ARRAY[
                      '%平台%', '%amazon%', '%亚马逊%', '%ebay%', '%易贝%',
                      '%shopee%', '%虾皮%', '%aliexpress%', '%速卖通%',
                      '%alibaba%', '%阿里巴巴%', '%walmart%', '%沃尔玛%', '%shein%'
                  ]) AND lower(content) LIKE ANY (ARRAY[
                      '%偏好%', '%倾向%', '%喜欢%', '%习惯%', '%通常%', '%购买%',
                      '%下单%', '%购物%', '%prefer%', '%usually%', '%buy%', '%shop%'
                  ])
              )
          );
        """.strip(),
    ),
    DurableMigration(
        version=13,
        sql="""
        CREATE TEMP TABLE glodex_obsolete_user_threads ON COMMIT DROP AS
        SELECT DISTINCT run.thread_id
        FROM durable_runs AS run
        WHERE run.loop_kind = 'root'
          AND COALESCE(run.request_payload->>'schema_version', '') <>
              'glodex.conversation-request.v2';

        CREATE TEMP TABLE glodex_obsolete_root_runs ON COMMIT DROP AS
        SELECT DISTINCT run.root_run_id AS run_id
        FROM durable_runs AS run
        WHERE COALESCE(run.request_payload->>'schema_version', '') <>
              'glodex.conversation-request.v2'
        UNION
        SELECT run.run_id
        FROM durable_runs AS run
        WHERE run.loop_kind = 'root'
          AND run.thread_id IN (SELECT thread_id FROM glodex_obsolete_user_threads);

        CREATE TEMP TABLE glodex_obsolete_runs ON COMMIT DROP AS
        SELECT run.run_id, run.depth
        FROM durable_runs AS run
        WHERE run.root_run_id IN (SELECT run_id FROM glodex_obsolete_root_runs);

        DELETE FROM user_memory_entries
        WHERE source_thread_id IN (SELECT thread_id FROM glodex_obsolete_user_threads);
        DELETE FROM conversation_turns
        WHERE thread_id IN (SELECT thread_id FROM glodex_obsolete_user_threads);
        DELETE FROM thread_summaries
        WHERE thread_id IN (SELECT thread_id FROM glodex_obsolete_user_threads);
        DELETE FROM user_thread_bindings
        WHERE thread_id IN (SELECT thread_id FROM glodex_obsolete_user_threads);

        DELETE FROM m7_offline_summaries
        WHERE run_id IN (SELECT run_id FROM glodex_obsolete_runs);
        DELETE FROM m6_trace_events
        WHERE run_id IN (SELECT run_id FROM glodex_obsolete_runs);
        DELETE FROM m6_run_traces
        WHERE run_id IN (SELECT run_id FROM glodex_obsolete_runs);
        DELETE FROM durable_events
        WHERE run_id IN (SELECT run_id FROM glodex_obsolete_runs);
        DELETE FROM durable_checkpoints
        WHERE run_id IN (SELECT run_id FROM glodex_obsolete_runs);

        DELETE FROM durable_runs
        WHERE run_id IN (SELECT run_id FROM glodex_obsolete_runs WHERE depth = 2);
        DELETE FROM durable_runs
        WHERE run_id IN (SELECT run_id FROM glodex_obsolete_runs WHERE depth = 1);
        DELETE FROM durable_runs
        WHERE run_id IN (SELECT run_id FROM glodex_obsolete_runs WHERE depth = 0);
        """.strip(),
    ),
)


async def apply_migrations(connection: object) -> int:
    """Apply only the exact known SQL sequence through one asyncpg connection."""

    if not hasattr(connection, "execute") or not hasattr(connection, "fetchrow"):
        raise TypeError("Durable migrations require an asyncpg-style connection")
    try:
        execute = connection.execute
        fetchrow = connection.fetchrow
        await execute(
            """
            CREATE TABLE IF NOT EXISTS glodex_durable_schema_migrations (
                version INTEGER PRIMARY KEY CHECK (version > 0),
                checksum CHAR(64) NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        applied = 0
        for migration in MIGRATIONS:
            row = await fetchrow(
                "SELECT checksum FROM glodex_durable_schema_migrations WHERE version = $1",
                migration.version,
            )
            if row is not None:
                if row["checksum"] != migration.checksum:
                    raise DurableStoreError("DURABLE_MIGRATION_MISMATCH")
                continue
            await execute(migration.sql)
            await execute(
                "INSERT INTO glodex_durable_schema_migrations(version, checksum) VALUES($1, $2)",
                migration.version,
                migration.checksum,
            )
            applied += 1
        return applied
    except DurableStoreError:
        raise
    except Exception as error:
        raise DurableStoreError("DURABLE_MIGRATION_FAILED") from error


__all__ = ["MIGRATIONS", "DurableMigration", "apply_migrations"]
