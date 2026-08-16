"""M5b migration, Store surface, and sole current public memory DTO contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from glodex.api.memory_contracts import MemoryEntryView, MemoryManagementView
from glodex.infrastructure.migrations import MIGRATIONS
from glodex.infrastructure.postgres import DurablePostgresStore

pytestmark = [
    pytest.mark.contract,
    pytest.mark.spec(
        "GLO-M5B-P0-004",
        "GLO-M5B-P0-005",
        "GLO-M5B-P0-006",
        "GLO-M5B-NFR-003",
    ),
]

_ROOT = Path(__file__).parents[3]


def test_memory_migrations_delete_unknown_origins_and_keep_current_index() -> None:
    migration = next(item for item in MIGRATIONS if item.version == 6)
    sql = migration.sql
    assert tuple(item.version for item in MIGRATIONS) == tuple(range(1, 14))
    assert migration.version == 6
    assert "ADD COLUMN IF NOT EXISTS origin" in sql
    assert "provenance is inconsistent" in sql
    assert "SET origin = 'manual'" in sql
    assert "ALTER COLUMN origin SET NOT NULL" in sql
    assert "origin IN ('manual', 'explicit_reflect')" in sql
    assert "user_memory_entries_owner_management_ix" in sql
    assert "DELETE FROM user_memory_entries" in sql
    assert "SET content" not in sql and "SET canonical_key" not in sql


def test_single_value_preference_migration_rekeys_and_tombstones_duplicates() -> None:
    sql = next(item.sql for item in MIGRATIONS if item.version == 12)

    assert "preference_budget_range" in sql
    assert "preference_platform" in sql
    assert "ROW_NUMBER() OVER" in sql
    assert "entry.origin = 'manual'" in sql
    assert "slot_position > 1" in sql


def test_store_exposes_only_purpose_specific_m5b_memory_operations() -> None:
    required = {
        "create_manual_memory",
        "replace_memory_as_manual",
        "write_explicit_reflect_memory",
        "list_active_memory",
        "tombstone_memory",
    }
    removed = {
        "upsert_user_memory",
        "list_user_memory",
        "replace_user_memory",
        "delete_user_memory",
    }

    assert all(callable(getattr(DurablePostgresStore, name, None)) for name in required)
    assert all(not hasattr(DurablePostgresStore, name) for name in removed)


def test_memory_management_response_has_one_exact_origin_aware_v3_shape() -> None:
    active = MemoryEntryView(
        entry_id="mem-" + "1" * 24,
        category="preference",
        content="我一直喜欢小众设计",
        revision=1,
        origin="explicit_reflect",
        source_thread_id="thread-m5b",
    )
    payload = MemoryManagementView(
        active_entries=(active,),
    ).model_dump(mode="json", by_alias=True)

    assert payload == {
        "schemaVersion": "glodex.user-memory.memory-list.v3",
        "activeEntries": [
            {
                "entryId": "mem-" + "1" * 24,
                "category": "preference",
                "content": "我一直喜欢小众设计",
                "revision": 1,
                "origin": "explicit_reflect",
                "sourceThreadId": "thread-m5b",
            }
        ],
    }


def test_memory_contract_has_no_generic_frontend_parser() -> None:
    frontend_source = (_ROOT / "frontend/src/service/agent.ts").read_text(encoding="utf-8")

    assert "isMemoryList" not in frontend_source
    assert "value.entries" not in frontend_source
