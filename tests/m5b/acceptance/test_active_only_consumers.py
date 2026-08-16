"""M5b evidence that current explicit memory has bounded publication effects."""

from __future__ import annotations

import pytest

from glodex.memory.blacklist import VerifiedBlacklistGuard
from glodex.memory.models import (
    UserMemoryCategory,
    UserMemoryEntry,
    UserMemoryOrigin,
    canonical_memory_key,
)
from tests.m1d.unit.test_agent_tools import _pool

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.spec(
        "M5B-AC-003",
        "M5B-AC-005",
        "GLO-M5B-P0-004",
        "GLO-M5B-P0-005",
    ),
]


def test_unstructured_explicit_blacklist_remains_soft_context_only() -> None:
    guard = VerifiedBlacklistGuard.from_entries(
        (
            _blacklist(
                content="以后都不要推荐塑料材质",
                origin=UserMemoryOrigin.EXPLICIT_REFLECT,
                entry_id="mem-" + "3" * 24,
            ),
        )
    )

    assert guard.rules == ()
    assert guard.excluded_candidate_ids(pool=_pool()) == ()


def _blacklist(
    *,
    content: str,
    origin: UserMemoryOrigin,
    entry_id: str,
) -> UserMemoryEntry:
    manual = origin is UserMemoryOrigin.MANUAL
    return UserMemoryEntry(
        user_id="user-" + "a" * 32,
        entry_id=entry_id,
        category=UserMemoryCategory.BLACKLIST,
        origin=origin,
        canonical_key=canonical_memory_key(
            category=UserMemoryCategory.BLACKLIST,
            content=content,
        ),
        content=content,
        source_thread_id=None if manual else "thread-m5b",
        source_ordinal=None if manual else 1,
        confidence=1.0,
        revision=1,
    )
