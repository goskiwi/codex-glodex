"""Deterministic M5b origin and long-term-expression semantics."""

from __future__ import annotations

import pytest

from glodex.memory.models import (
    MemoryCandidate,
    UserMemoryCategory,
    UserMemoryEntry,
    UserMemoryOrigin,
    canonical_memory_key,
    explicit_memory_entry_id,
    memory_entry_id,
)
from glodex.memory.semantics import (
    ExplicitMemorySemanticsError,
    may_contain_explicit_long_term_memory,
    validate_explicit_memory_candidates,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec(
        "GLO-M5B-P0-001",
        "GLO-M5B-P0-002",
        "GLO-M5B-P0-003",
        "GLO-M5B-P0-005",
        "GLO-M5B-NFR-002",
        "GLO-M5B-NFR-003",
    ),
]


@pytest.mark.parametrize(
    "current_input",
    (
        "预算 800 元以内",
        "这次买耳机,预算 800 元以内",
        "预算 6000 元以内",
        "3000 左右",
        "想买耳机",
        "这次不要塑料",
    ),
)
def test_current_run_constraints_do_not_pass_the_reflect_prefilter(current_input: str) -> None:
    assert may_contain_explicit_long_term_memory(current_input) is False


@pytest.mark.parametrize(
    "current_input",
    (
        "我平时预算通常在 3000 元左右",
        "我一直喜欢小众设计",
        "以后都不要推荐塑料材质",
        "我不接受翻新商品",
    ),
)
def test_explicit_long_term_cues_pass_the_bounded_prefilter(current_input: str) -> None:
    assert may_contain_explicit_long_term_memory(current_input) is True


def test_durable_gate_accepts_only_self_contained_verbatim_long_term_quotes() -> None:
    source = "我平时预算通常在 3000 元左右;以后都不要推荐塑料材质"
    preference = _candidate(
        source,
        "平时预算通常在 3000 元左右",
        UserMemoryCategory.PREFERENCE,
    )
    blacklist = _candidate(
        source,
        "以后都不要推荐塑料材质",
        UserMemoryCategory.BLACKLIST,
    )

    assert validate_explicit_memory_candidates(source, (preference, blacklist)) == (
        preference,
        blacklist,
    )


@pytest.mark.parametrize(
    ("source", "quote", "category"),
    (
        ("这次我喜欢红色", "这次我喜欢红色", UserMemoryCategory.PREFERENCE),
        ("我一直预算在 3000 元左右", "预算在 3000 元左右", UserMemoryCategory.PREFERENCE),
        ("以后都不要塑料", "以后都不要塑料", UserMemoryCategory.PREFERENCE),
        ("我一直买这个品牌", "我一直买这个品牌", UserMemoryCategory.HISTORY),
    ),
)
def test_one_invalid_candidate_rejects_the_complete_batch(
    source: str,
    quote: str,
    category: UserMemoryCategory,
) -> None:
    candidate = _candidate(source, quote, category)
    valid_source = f"{source};我平时喜欢轻量设计"
    valid = _candidate(valid_source, "我平时喜欢轻量设计", UserMemoryCategory.PREFERENCE)
    invalid = MemoryCandidate(
        category=candidate.category,
        content=candidate.content,
        start=candidate.start,
        end=candidate.end,
    )
    with pytest.raises(ExplicitMemorySemanticsError):
        validate_explicit_memory_candidates(valid_source, (valid, invalid))


def test_current_origin_is_required() -> None:
    manual = _entry(origin=UserMemoryOrigin.MANUAL, entry_id="mem-" + "1" * 24)
    explicit = _entry(
        origin=UserMemoryOrigin.EXPLICIT_REFLECT,
        entry_id="mem-" + "2" * 24,
    )
    assert manual.origin is UserMemoryOrigin.MANUAL
    assert explicit.origin is UserMemoryOrigin.EXPLICIT_REFLECT


def test_explicit_identity_does_not_collide_with_a_same_content_manual_identity() -> None:
    manual_id = memory_entry_id(
        category=UserMemoryCategory.PREFERENCE,
        content="我一直喜欢小众设计",
    )
    explicit_id = explicit_memory_entry_id(
        category=UserMemoryCategory.PREFERENCE,
        content="我一直喜欢小众设计",
        source_thread_id="thread-m5b",
        source_ordinal=1,
    )

    assert explicit_id != manual_id


def test_budget_and_platform_preferences_use_single_value_slots() -> None:
    assert (
        canonical_memory_key(
            category=UserMemoryCategory.PREFERENCE,
            content="我平时预算通常在 3000 元左右",
        )
        == canonical_memory_key(
            category=UserMemoryCategory.PREFERENCE,
            content="以后预算改为 5000 元以内",
        )
        == "preference_budget_range"
    )
    assert (
        canonical_memory_key(
            category=UserMemoryCategory.PREFERENCE,
            content="我通常在 Shopee 购物",
        )
        == canonical_memory_key(
            category=UserMemoryCategory.PREFERENCE,
            content="以后倾向在 Amazon 购买",
        )
        == "preference_platform"
    )


def test_unclassified_preferences_remain_content_addressed_and_can_coexist() -> None:
    first = canonical_memory_key(
        category=UserMemoryCategory.PREFERENCE,
        content="我一直喜欢小众设计",
    )
    second = canonical_memory_key(
        category=UserMemoryCategory.PREFERENCE,
        content="我一直喜欢轻量设计",
    )

    assert first != second


def _candidate(
    source: str,
    quote: str,
    category: UserMemoryCategory,
) -> MemoryCandidate:
    start = source.index(quote)
    return MemoryCandidate(
        category=category,
        content=quote,
        start=start,
        end=start + len(quote),
    )


def _entry(*, origin: UserMemoryOrigin, entry_id: str) -> UserMemoryEntry:
    content = "我一直喜欢小众设计"
    manual = origin is UserMemoryOrigin.MANUAL
    return UserMemoryEntry(
        user_id="user-" + "a" * 32,
        entry_id=entry_id,
        category=UserMemoryCategory.PREFERENCE,
        origin=origin,
        canonical_key=canonical_memory_key(
            category=UserMemoryCategory.PREFERENCE,
            content=content,
        ),
        content=content,
        source_thread_id=None if manual else "thread-m5b",
        source_ordinal=None if manual else 1,
        confidence=1.0,
        revision=1,
    )
