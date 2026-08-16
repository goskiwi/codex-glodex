"""Offline M5 evidence for strict LLM reflection and BGE-backed context."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import httpx
import pytest

from glodex.agent.contracts import AgentAnswer, AgentAnswerKind, AgentDemoResponse
from glodex.contracts import RunStatus, SearchRequest
from glodex.infrastructure.redis import DurableRedisCache
from glodex.memory.blacklist import VerifiedBlacklistGuard
from glodex.memory.context import (
    UserContextBuilder,
    UserContextContinuityError,
    UserContextProjection,
    _encode_bounded_context,
    user_context_cache_key,
)
from glodex.memory.llm_reflection import LlmMemoryReflector
from glodex.memory.models import (
    ConversationRole,
    ConversationTurn,
    MemoryCandidate,
    ThreadSummary,
    UserMemoryCategory,
    UserMemoryEntry,
    UserMemoryOrigin,
    canonical_memory_key,
    explicit_memory_entry_id,
)
from glodex.memory.retrieval import BgeMemoryRetriever
from glodex.memory.terminal_writer import MemoryTerminalWriter
from glodex.retrieval.contracts import (
    RETRIEVAL_MODEL_EMBEDDING_MODEL,
    RETRIEVAL_MODEL_RERANKER_MODEL,
    RetrievalModelIdentity,
)
from glodex.retrieval.model_service import RetrievalModelClient
from glodex.runtime.contracts import DurableRun, DurableRunState, LoopKind
from glodex.runtime.request_payload import durable_request_payload
from tests.m1d.unit.test_agent_tools import _pool

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec("GLO-M5-P0-003", "GLO-M5-P0-005", "GLO-M5-P0-006", "GLO-M5-NFR-004"),
]


class _RecordingLlm:
    def __init__(self, response: bytes) -> None:
        self.response = response
        self.calls: list[bytes] = []

    async def __call__(self, payload: bytes) -> bytes:
        self.calls.append(payload)
        return self.response


def _provider(
    content: str,
    *,
    system_fingerprint: str | None = None,
    reasoning_content: str | None = None,
) -> bytes:
    message: dict[str, object] = {
        "role": "assistant",
        "content": content,
        "tool_calls": [],
    }
    if reasoning_content is not None:
        message["reasoning_content"] = reasoning_content
    envelope: dict[str, object] = {
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": message,
            }
        ]
    }
    if system_fingerprint is not None:
        envelope["system_fingerprint"] = system_fingerprint
    return json.dumps(
        envelope,
        separators=(",", ":"),
    ).encode()


def _entry(entry_id: str, content: str) -> UserMemoryEntry:
    category = UserMemoryCategory.PREFERENCE
    return UserMemoryEntry(
        user_id="user-" + "a" * 32,
        entry_id=entry_id,
        category=category,
        origin=UserMemoryOrigin.EXPLICIT_REFLECT,
        canonical_key=canonical_memory_key(category=category, content=content),
        content=content,
        source_thread_id="thread-m5-context",
        source_ordinal=1,
        confidence=1.0,
        revision=1,
    )


def _identity() -> RetrievalModelIdentity:
    return RetrievalModelIdentity(
        manifest_digest="a" * 64,
        embedding_model=RETRIEVAL_MODEL_EMBEDDING_MODEL,
        reranker_model=RETRIEVAL_MODEL_RERANKER_MODEL,
        dimension=1024,
        max_embedding_texts=8,
        max_text_characters=2000,
        max_rerank_documents=50,
        max_query_characters=512,
        device_class="cuda",
        gpu_model_class="a100",
    )


def _unit_vector(axis: int) -> list[float]:
    vector = [0.0] * 1024
    vector[axis] = 1.0
    return vector


@dataclass
class _MemoryStore:
    entries: tuple[UserMemoryEntry, ...]

    async def list_active_memory(
        self,
        *,
        user_id: str,
        limit: int = 64,
    ) -> tuple[UserMemoryEntry, ...]:
        assert user_id == "user-" + "a" * 32 and limit == 128
        return self.entries


def _bge_client() -> RetrievalModelClient:
    vectors = {
        "current query": _unit_vector(0),
        "first": _unit_vector(0),
        "second": _unit_vector(1),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/health":
            identity = _identity()
            return httpx.Response(
                200,
                json={
                    "schemaVersion": identity.schema_version,
                    "manifestDigest": identity.manifest_digest,
                    "embeddingModel": identity.embedding_model,
                    "rerankerModel": identity.reranker_model,
                    "dimension": identity.dimension,
                    "maxEmbeddingTexts": identity.max_embedding_texts,
                    "maxTextCharacters": identity.max_text_characters,
                    "maxRerankDocuments": identity.max_rerank_documents,
                    "maxQueryCharacters": identity.max_query_characters,
                    "deviceClass": identity.device_class,
                    "gpuModelClass": identity.gpu_model_class,
                },
            )
        values = json.loads(request.content)["texts"]
        return httpx.Response(
            200,
            json={"manifestDigest": "a" * 64, "embeddings": [vectors[value] for value in values]},
        )

    return RetrievalModelClient(http_transport=httpx.MockTransport(handler))


def test_reflect_accepts_exact_chinese_literal_and_backend_derives_its_span() -> None:
    source = "我平时喜欢 14 英寸以下,通常预算 800 USD,以后都不要 Apple"
    transport = _RecordingLlm(
        _provider(
            '{"preference_quotes":["平时喜欢 14 英寸以下","通常预算 800 USD"],'
            '"blacklist_quotes":["以后都不要 Apple"],"history_quotes":[]}',
            system_fingerprint="fp-llm-memory",
        )
    )
    reflector = LlmMemoryReflector(transport, model_name="test-model")

    candidates = asyncio.run(
        reflector.reflect(current_user_content=source, terminal_status="COMPLETED")
    )

    assert len(transport.calls) == 1
    assert [(candidate.content, candidate.start, candidate.end) for candidate in candidates] == [
        (
            "平时喜欢 14 英寸以下",
            source.index("平时喜欢 14 英寸以下"),
            source.index("平时喜欢 14 英寸以下") + len("平时喜欢 14 英寸以下"),
        ),
        (
            "通常预算 800 USD",
            source.index("通常预算 800 USD"),
            source.index("通常预算 800 USD") + len("通常预算 800 USD"),
        ),
        (
            "以后都不要 Apple",
            source.index("以后都不要 Apple"),
            source.index("以后都不要 Apple") + len("以后都不要 Apple"),
        ),
    ]
    payload = json.loads(transport.calls[0])
    assert '"preference_quotes"' in payload["messages"][0]["content"]
    assert '"blacklist_quotes"' in payload["messages"][0]["content"]
    assert '"history_quotes"' in payload["messages"][0]["content"]
    bad = _RecordingLlm(
        _provider(
            '{"preference_quotes":["预算不超过八百美元"],"blacklist_quotes":[],"history_quotes":[]}'
        )
    )
    assert (
        asyncio.run(
            LlmMemoryReflector(bad, model_name="test-model").reflect(
                current_user_content=source,
                terminal_status="COMPLETED",
            )
        )
        == ()
    )
    assert len(bad.calls) == 1


def test_reflect_rejects_ambiguous_literal_and_legacy_character_offsets() -> None:
    ambiguous = _RecordingLlm(
        _provider(
            '{"preference_quotes":[],"blacklist_quotes":["以后都不要 Apple"],"history_quotes":[]}'
        )
    )
    legacy = _RecordingLlm(_provider('{"entries":[{"category":"blacklist","text":"不要 Apple"}]}'))

    assert (
        asyncio.run(
            LlmMemoryReflector(ambiguous, model_name="test-model").reflect(
                current_user_content="以后都不要 Apple,以后都不要 Apple",
                terminal_status="COMPLETED",
            )
        )
        == ()
    )
    assert (
        asyncio.run(
            LlmMemoryReflector(legacy, model_name="test-model").reflect(
                current_user_content="以后都不要 Apple",
                terminal_status="COMPLETED",
            )
        )
        == ()
    )


def test_reflect_discards_bounded_provider_reasoning_and_uses_only_strict_json() -> None:
    source = "我长期偏好轻薄笔记本。"
    transport = _RecordingLlm(
        _provider(
            '{"preference_quotes":["我长期偏好轻薄笔记本。"],'
            '"blacklist_quotes":[],"history_quotes":[]}',
            reasoning_content="private provider reasoning that must be discarded",
        )
    )

    result = asyncio.run(
        LlmMemoryReflector(transport, model_name="test-model").reflect(
            current_user_content=source,
            terminal_status="COMPLETED",
        )
    )

    assert tuple(candidate.content for candidate in result) == (source,)


def test_current_bge_reencodes_memory_and_returns_stable_score_free_top_k() -> None:
    retriever = BgeMemoryRetriever(
        store=_MemoryStore(
            entries=(_entry("mem-" + "1" * 24, "first"), _entry("mem-" + "2" * 24, "second"))
        ),
        gpu=_bge_client(),
    )

    selected = asyncio.run(
        retriever.read_relevant(user_id="user-" + "a" * 32, query="current query", top_k=1)
    )

    assert tuple(entry.entry_id for entry in selected) == ("mem-" + "1" * 24,)


def test_current_bge_does_not_inject_unrelated_memory() -> None:
    retriever = BgeMemoryRetriever(
        store=_MemoryStore(
            entries=(_entry("mem-" + "1" * 24, "first"), _entry("mem-" + "2" * 24, "second"))
        ),
        gpu=_bge_client(),
    )

    selected = asyncio.run(
        retriever.read_relevant(user_id="user-" + "a" * 32, query="current query", top_k=2)
    )

    assert tuple(entry.entry_id for entry in selected) == ("mem-" + "1" * 24,)


@dataclass
class _ContextStore:
    turns: tuple[ConversationTurn, ...]
    summary: ThreadSummary | None = None
    entries: tuple[UserMemoryEntry, ...] = ()
    saved: list[ThreadSummary] = field(default_factory=list)
    turn_reads: int = 0

    async def list_conversation_turns(
        self,
        *,
        thread_id: str,
        limit: int = 64,
    ) -> tuple[ConversationTurn, ...]:
        assert thread_id == "thread-m5-context" and limit == 64
        self.turn_reads += 1
        return self.turns

    async def latest_thread_summary(self, *, thread_id: str) -> ThreadSummary | None:
        assert thread_id == "thread-m5-context"
        return self.summary

    async def latest_conversation_ordinal(self, *, thread_id: str) -> int:
        assert thread_id == "thread-m5-context"
        return 0 if not self.turns else self.turns[-1].ordinal

    async def list_active_memory(
        self,
        *,
        user_id: str,
        limit: int = 64,
    ) -> tuple[UserMemoryEntry, ...]:
        assert user_id == "user-" + "a" * 32 and limit == 64
        return self.entries

    async def save_thread_summary(
        self,
        *,
        summary: ThreadSummary,
        expected_revision: int,
    ) -> ThreadSummary:
        current_revision = 0 if self.summary is None else self.summary.revision
        if expected_revision != current_revision or summary.revision != expected_revision + 1:
            raise RuntimeError("summary revision conflict")
        self.summary = summary
        self.saved.append(summary)
        return summary


@dataclass
class _ConcurrentContextStore(_ContextStore):
    conflict_injected: bool = False

    async def save_thread_summary(
        self,
        *,
        summary: ThreadSummary,
        expected_revision: int,
    ) -> ThreadSummary:
        if not self.conflict_injected:
            assert expected_revision == 0
            self.conflict_injected = True
            self.summary = ThreadSummary(
                thread_id=summary.thread_id,
                revision=1,
                covered_through_ordinal=summary.covered_through_ordinal,
                summary="concurrent-summary-through-12",
            )
            raise RuntimeError("summary revision conflict")
        return await super().save_thread_summary(
            summary=summary,
            expected_revision=expected_revision,
        )


class _Relevance:
    async def read_relevant(
        self,
        *,
        user_id: str,
        query: str,
        top_k: int = 5,
    ) -> tuple[UserMemoryEntry, ...]:
        assert user_id == "user-" + "a" * 32 and query == "current input" and top_k == 5
        return (_entry("mem-" + "3" * 24, "first"),)


class _Summarizer:
    async def summarize(
        self,
        *,
        turns: tuple[ConversationTurn, ...],
        prior_summary: str | None = None,
    ) -> str:
        assert len(turns) == 2 and prior_summary is None
        return "prior constraints"


class _LongSummarizer:
    async def summarize(
        self,
        *,
        turns: tuple[ConversationTurn, ...],
        prior_summary: str | None = None,
    ) -> str:
        del turns, prior_summary
        return "概" * 2_000


@dataclass
class _ProjectionCache:
    values: dict[str, object] = field(default_factory=dict)
    reads: int = 0
    writes: int = 0

    async def get_user_context_projection(self, *, key: str) -> object | None:
        self.reads += 1
        return self.values.get(key)

    async def set_user_context_projection(
        self,
        *,
        key: str,
        value: object,
        ttl_seconds: int,
    ) -> None:
        assert ttl_seconds == 900
        self.writes += 1
        self.values[key] = value


@dataclass
class _Redis:
    values: dict[str, bytes] = field(default_factory=dict)

    async def get(self, key: str) -> bytes | None:
        return self.values.get(key)

    async def set(self, key: str, value: bytes, *, ex: int) -> None:
        assert ex == 900
        self.values[key] = value


def test_context_keeps_fixed_private_order_and_summarizes_only_after_overflow() -> None:
    turns = tuple(
        ConversationTurn(
            thread_id="thread-m5-context",
            ordinal=ordinal,
            role=ConversationRole.USER if ordinal % 2 else ConversationRole.ASSISTANT,
            display_content=f"turn-{ordinal}",
            terminal_run_id=None if ordinal % 2 else f"run-m5-{ordinal}",
        )
        for ordinal in range(1, 9)
    )
    store = _ContextStore(turns=turns)
    context = asyncio.run(
        UserContextBuilder(store=store, relevance=_Relevance(), summarizer=_Summarizer()).build(
            user_id="user-" + "a" * 32,
            thread_id="thread-m5-context",
            current_user_input="current input",
        )
    )
    payload = json.loads(context.encoded)

    assert list(payload) == [
        "policy",
        "verified_blacklist_guard",
        "relevant_memory",
        "thread_summary",
        "recent_turns",
    ]
    assert (
        context.memory_count == 1
        and context.summary_revision == 1
        and context.recent_turn_count == 6
    )
    assert context.preference_text == "first"
    assert len(store.saved) == 1


@pytest.mark.acceptance
@pytest.mark.spec("M5-AC-003")
def test_context_projection_cache_never_replaces_postgres_version_check() -> None:
    turns = (
        ConversationTurn(
            thread_id="thread-m5-context",
            ordinal=1,
            role=ConversationRole.USER,
            display_content="first turn",
        ),
        ConversationTurn(
            thread_id="thread-m5-context",
            ordinal=2,
            role=ConversationRole.ASSISTANT,
            display_content="first answer",
            terminal_run_id="run-m5-2",
        ),
    )
    store = _ContextStore(turns=turns)
    cache = _ProjectionCache()
    builder = UserContextBuilder(
        store=store,
        relevance=_Relevance(),
        summarizer=_Summarizer(),
        cache=cache,  # type: ignore[arg-type]
    )

    first = asyncio.run(
        builder.build(
            user_id="user-" + "a" * 32,
            thread_id="thread-m5-context",
            current_user_input="current input",
        )
    )
    second = asyncio.run(
        builder.build(
            user_id="user-" + "a" * 32,
            thread_id="thread-m5-context",
            current_user_input="current input",
        )
    )

    assert first.encoded == second.encoded
    assert store.turn_reads == 1
    assert cache.reads == 2 and cache.writes == 1


def test_redis_context_projection_is_bounded_and_opaque_by_hashed_key() -> None:
    key = user_context_cache_key(
        user_id="user-" + "a" * 32,
        thread_id="thread-m5-context",
        summary_revision=0,
    )
    projection = UserContextProjection(
        thread_id="thread-m5-context",
        summary=None,
        recent_turns=(
            ConversationTurn(
                thread_id="thread-m5-context",
                ordinal=1,
                role=ConversationRole.USER,
                display_content="private display turn",
            ),
            ConversationTurn(
                thread_id="thread-m5-context",
                ordinal=2,
                role=ConversationRole.ASSISTANT,
                display_content="private display answer",
                terminal_run_id="run-m5-2",
            ),
        ),
        latest_ordinal=2,
    )
    redis = _Redis()
    cache = DurableRedisCache(client=redis)

    asyncio.run(
        cache.set_user_context_projection(
            key=key,
            value=projection,
            ttl_seconds=900,
        )
    )
    restored = asyncio.run(cache.get_user_context_projection(key=key))

    assert restored == projection
    assert "private display turn" not in key
    assert b"glodex_local_session" not in redis.values[key]
    assert b'"vector"' not in redis.values[key]


def test_context_trims_only_optional_layers_and_keeps_required_input() -> None:
    def preference(index: int) -> UserMemoryEntry:
        content = ("忆" * 511) + str(index)
        return UserMemoryEntry(
            user_id="user-" + "a" * 32,
            entry_id=f"mem-{index:024d}",
            category=UserMemoryCategory.PREFERENCE,
            origin=UserMemoryOrigin.MANUAL,
            canonical_key=canonical_memory_key(
                category=UserMemoryCategory.PREFERENCE,
                content=content,
            ),
            content=content,
            source_thread_id=None,
            source_ordinal=None,
            confidence=1.0,
            revision=1,
        )

    class _ManyRelevance:
        async def read_relevant(
            self,
            *,
            user_id: str,
            query: str,
            top_k: int = 5,
        ) -> tuple[UserMemoryEntry, ...]:
            assert user_id == "user-" + "a" * 32 and query == "今" * 2_000 and top_k == 5
            return tuple(preference(index) for index in range(1, 6))

    turns = tuple(
        ConversationTurn(
            thread_id="thread-m5-context",
            ordinal=ordinal,
            role=ConversationRole.USER if ordinal % 2 else ConversationRole.ASSISTANT,
            display_content="史" * 2_000,
            terminal_run_id=None if ordinal % 2 else f"run-m5-{ordinal}",
        )
        for ordinal in range(1, 9)
    )
    context = asyncio.run(
        UserContextBuilder(
            store=_ContextStore(turns=turns),
            relevance=_ManyRelevance(),
            summarizer=_LongSummarizer(),
        ).build(
            user_id="user-" + "a" * 32,
            thread_id="thread-m5-context",
            current_user_input="今" * 2_000,
        )
    )
    payload = json.loads(context.encoded)

    assert len(context.encoded.encode("utf-8")) <= 8_192
    assert payload["policy"]
    assert "current_user_input" not in payload
    assert payload["verified_blacklist_guard"] == {
        "enforced_before_publication": False,
        "rule_count": 0,
    }
    assert context.memory_count == 0
    assert payload["thread_summary"] is None


def test_context_applies_a_separate_complete_entry_memory_budget() -> None:
    entries = tuple(
        UserMemoryEntry(
            user_id="user-" + "a" * 32,
            entry_id=f"mem-{index:024d}",
            category=UserMemoryCategory.PREFERENCE,
            origin=UserMemoryOrigin.MANUAL,
            canonical_key=canonical_memory_key(
                category=UserMemoryCategory.PREFERENCE,
                content=("偏" * 300) + str(index),
            ),
            content=("偏" * 300) + str(index),
            source_thread_id=None,
            source_ordinal=None,
            confidence=1.0,
            revision=1,
        )
        for index in range(1, 6)
    )

    encoded, retained, _summary, _recent = _encode_bounded_context(
        blacklist_guard=VerifiedBlacklistGuard.from_entries(()),
        selected=entries,
        summary=None,
        recent=(),
    )
    payload = json.loads(encoded)

    assert 0 < len(retained) < len(entries)
    assert payload["relevant_memory"] == [
        {"category": entry.category.value, "content": entry.content} for entry in retained
    ]
    assert (
        len(
            json.dumps(
                payload["relevant_memory"], ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        )
        <= 1_500
    )


def test_context_byte_trimming_removes_whole_turn_pairs() -> None:
    recent = tuple(
        ConversationTurn(
            thread_id="thread-m5-context",
            ordinal=ordinal,
            role=ConversationRole.USER if ordinal % 2 else ConversationRole.ASSISTANT,
            display_content="史" * 450,
            terminal_run_id=None if ordinal % 2 else f"run-m5-{ordinal}",
        )
        for ordinal in range(1, 7)
    )

    encoded, _memory, _summary, retained = _encode_bounded_context(
        blacklist_guard=VerifiedBlacklistGuard.from_entries(()),
        selected=(),
        summary=None,
        recent=recent,
    )

    assert len(encoded.encode("utf-8")) <= 8_192
    assert tuple(turn.role for turn in retained) == (
        ConversationRole.USER,
        ConversationRole.ASSISTANT,
        ConversationRole.USER,
        ConversationRole.ASSISTANT,
    )


def test_summary_batches_cover_every_turn_before_the_recent_window() -> None:
    class _CapturingSummarizer:
        def __init__(self) -> None:
            self.calls: list[tuple[tuple[int, ...], str | None]] = []

        async def summarize(
            self,
            *,
            turns: tuple[ConversationTurn, ...],
            prior_summary: str | None = None,
        ) -> str:
            ordinals = tuple(turn.ordinal for turn in turns)
            self.calls.append((ordinals, prior_summary))
            return f"summary-through-{ordinals[-1]}"

    turns = tuple(
        ConversationTurn(
            thread_id="thread-m5-context",
            ordinal=ordinal,
            role=ConversationRole.USER if ordinal % 2 else ConversationRole.ASSISTANT,
            display_content=f"turn-{ordinal}",
            terminal_run_id=None if ordinal % 2 else f"run-m5-{ordinal}",
        )
        for ordinal in range(1, 31)
    )
    summarizer = _CapturingSummarizer()
    store = _ContextStore(turns=turns)
    context = asyncio.run(
        UserContextBuilder(
            store=store,
            relevance=_Relevance(),
            summarizer=summarizer,
        ).build(
            user_id="user-" + "a" * 32,
            thread_id="thread-m5-context",
            current_user_input="current input",
        )
    )

    assert summarizer.calls == [
        (tuple(range(1, 13)), None),
        (tuple(range(13, 25)), "summary-through-12"),
    ]
    assert store.saved[0].covered_through_ordinal == 12
    assert store.saved[1].covered_through_ordinal == 24
    assert context.summary_revision == 2
    payload = json.loads(context.encoded)
    assert payload["thread_summary"] == {
        "revision": 2,
        "summary": "summary-through-24",
    }
    assert [turn["content"] for turn in payload["recent_turns"]] == [
        f"turn-{ordinal}" for ordinal in range(25, 31)
    ]


def test_summary_failure_fails_closed_instead_of_omitting_old_constraints() -> None:
    class _UnavailableSummarizer:
        async def summarize(
            self,
            *,
            turns: tuple[ConversationTurn, ...],
            prior_summary: str | None = None,
        ) -> str:
            del turns, prior_summary
            raise RuntimeError("private provider failure")

    turns = tuple(
        ConversationTurn(
            thread_id="thread-m5-context",
            ordinal=ordinal,
            role=ConversationRole.USER if ordinal % 2 else ConversationRole.ASSISTANT,
            display_content=f"turn-{ordinal}",
            terminal_run_id=None if ordinal % 2 else f"run-m5-{ordinal}",
        )
        for ordinal in range(1, 9)
    )

    with pytest.raises(UserContextContinuityError) as captured:
        asyncio.run(
            UserContextBuilder(
                store=_ContextStore(turns=turns),
                relevance=_Relevance(),
                summarizer=_UnavailableSummarizer(),
            ).build(
                user_id="user-" + "a" * 32,
                thread_id="thread-m5-context",
                current_user_input="current input",
            )
        )

    assert str(captured.value) == "USER_CONTEXT_SUMMARY_UNAVAILABLE"
    assert "private provider failure" not in str(captured.value)


def test_summary_revision_conflict_adopts_newer_contiguous_summary() -> None:
    class _CapturingSummarizer:
        def __init__(self) -> None:
            self.calls: list[tuple[tuple[int, ...], str | None]] = []

        async def summarize(
            self,
            *,
            turns: tuple[ConversationTurn, ...],
            prior_summary: str | None = None,
        ) -> str:
            ordinals = tuple(turn.ordinal for turn in turns)
            self.calls.append((ordinals, prior_summary))
            return f"local-summary-through-{ordinals[-1]}"

    turns = tuple(
        ConversationTurn(
            thread_id="thread-m5-context",
            ordinal=ordinal,
            role=ConversationRole.USER if ordinal % 2 else ConversationRole.ASSISTANT,
            display_content=f"turn-{ordinal}",
            terminal_run_id=None if ordinal % 2 else f"run-m5-{ordinal}",
        )
        for ordinal in range(1, 31)
    )
    store = _ConcurrentContextStore(turns=turns)
    summarizer = _CapturingSummarizer()

    context = asyncio.run(
        UserContextBuilder(
            store=store,
            relevance=_Relevance(),
            summarizer=summarizer,
        ).build(
            user_id="user-" + "a" * 32,
            thread_id="thread-m5-context",
            current_user_input="current input",
        )
    )

    assert summarizer.calls == [
        (tuple(range(1, 13)), None),
        (tuple(range(13, 25)), "concurrent-summary-through-12"),
    ]
    assert context.summary_revision == 2
    assert store.summary is not None
    assert store.summary.covered_through_ordinal == 24


@pytest.mark.acceptance
@pytest.mark.spec("M5-AC-004")
def test_verified_blacklist_filters_only_exact_canonical_candidate_facts() -> None:
    def blacklist(content: str, entry_id: str) -> UserMemoryEntry:
        return UserMemoryEntry(
            user_id="user-" + "a" * 32,
            entry_id=entry_id,
            category=UserMemoryCategory.BLACKLIST,
            origin=UserMemoryOrigin.MANUAL,
            canonical_key=canonical_memory_key(
                category=UserMemoryCategory.BLACKLIST,
                content=content,
            ),
            content=content,
            source_thread_id=None,
            source_ordinal=None,
            confidence=1.0,
            revision=1,
        )

    pool = _pool()
    guard = VerifiedBlacklistGuard.from_entries(
        (
            blacklist("item_id:item-1", "mem-" + "4" * 24),
            blacklist("avoid this seller", "mem-" + "5" * 24),
        )
    )

    assert guard.private_labels == ("item_id:item-1",)
    assert guard.excluded_candidate_ids(pool=pool) == ("amazon.product-1",)
    assert (
        VerifiedBlacklistGuard.from_entries(
            (blacklist("item_id:other-item", "mem-" + "6" * 24),)
        ).excluded_candidate_ids(pool=pool)
        == ()
    )


@dataclass
class _TerminalStore:
    turns: list[ConversationTurn]
    entries: list[UserMemoryEntry] = field(default_factory=list)

    async def thread_owner(self, *, thread_id: str) -> str | None:
        return "user-" + "a" * 32 if thread_id == "thread-m5-context" else None

    async def list_conversation_turns(
        self,
        *,
        thread_id: str,
        limit: int = 64,
    ) -> tuple[ConversationTurn, ...]:
        assert thread_id == "thread-m5-context" and limit == 64
        return tuple(self.turns)

    async def append_conversation_turn(
        self,
        *,
        thread_id: str,
        role: ConversationRole,
        display_content: str,
        terminal_run_id: str | None = None,
    ) -> ConversationTurn:
        turn = ConversationTurn(
            thread_id=thread_id,
            ordinal=len(self.turns) + 1,
            role=role,
            display_content=display_content,
            terminal_run_id=terminal_run_id,
        )
        self.turns.append(turn)
        return turn

    async def write_explicit_reflect_memory(
        self,
        *,
        user_id: str,
        candidates: tuple[MemoryCandidate, ...],
        source_thread_id: str,
        source_ordinal: int,
    ) -> tuple[UserMemoryEntry, ...]:
        entries = tuple(
            UserMemoryEntry(
                user_id=user_id,
                entry_id=explicit_memory_entry_id(
                    category=candidate.category,
                    content=candidate.content,
                    source_thread_id=source_thread_id,
                    source_ordinal=source_ordinal,
                ),
                category=candidate.category,
                origin=UserMemoryOrigin.EXPLICIT_REFLECT,
                canonical_key=candidate.canonical_key,
                content=candidate.content,
                source_thread_id=source_thread_id,
                source_ordinal=source_ordinal,
                confidence=1.0,
                revision=1,
            )
            for candidate in candidates
        )
        self.entries.extend(entries)
        return entries


@pytest.mark.acceptance
@pytest.mark.spec("M5-AC-002")
def test_trusted_terminal_writes_assistant_then_calls_real_reflector_once() -> None:
    transport = _RecordingLlm(
        _provider(
            '{"preference_quotes":["一直喜欢 brandA"],"blacklist_quotes":[],"history_quotes":[]}'
        )
    )
    store = _TerminalStore(
        turns=[
            ConversationTurn(
                thread_id="thread-m5-context",
                ordinal=1,
                role=ConversationRole.USER,
                display_content="我一直喜欢 brandA",
            )
        ]
    )
    run = DurableRun(
        run_id="run-m5-terminal",
        thread_id="thread-m5-context",
        root_run_id="run-m5-terminal",
        parent_run_id=None,
        child_id=None,
        depth=0,
        loop_kind=LoopKind.ROOT,
        task_scope_digest=None,
        state=DurableRunState.COMPLETED,
        attempt=1,
        event_sequence=1,
        asset_version="m5-test",
        config_fingerprint="a" * 64,
        request_payload=durable_request_payload(
            request=SearchRequest(query="我一直喜欢 brandA"),
            display_query="我一直喜欢 brandA",
            task_turns=("我一直喜欢 brandA",),
        ),
        terminal_response={},
        terminal_error_code=None,
        cancel_requested=False,
    )
    response = AgentDemoResponse(
        run_id=run.run_id,
        status=RunStatus.COMPLETED,
        answer=AgentAnswer(kind=AgentAnswerKind.CHAT_FALLBACK, text="Trusted result."),
    )

    asyncio.run(
        MemoryTerminalWriter(
            store=store, reflector=LlmMemoryReflector(transport, model_name="test-model")
        ).write_terminal(
            run=run,
            response=response,
        )
    )

    assert len(transport.calls) == 1
    assert [turn.role for turn in store.turns] == [
        ConversationRole.USER,
        ConversationRole.ASSISTANT,
    ]
    assert len(store.entries) == 1 and store.entries[0].content == "一直喜欢 brandA"
