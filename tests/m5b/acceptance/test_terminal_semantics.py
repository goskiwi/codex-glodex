"""M5b terminal evidence for zero/one Reflect and atomic explicit writes."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field

import pytest

from glodex.agent.contracts import AgentAnswer, AgentAnswerKind, AgentDemoResponse
from glodex.contracts import (
    Diagnostics,
    EvidenceSummary,
    FilterSummary,
    MoneySummary,
    OfferSummary,
    RunStatus,
    SearchRequest,
    SearchResponse,
    SearchResult,
)
from glodex.memory.llm_reflection import LlmMemoryReflector
from glodex.memory.models import (
    ConversationRole,
    ConversationTurn,
    MemoryCandidate,
    UserMemoryEntry,
)
from glodex.memory.terminal_writer import MemoryTerminalWriter
from glodex.runtime.contracts import DurableRun, DurableRunState, LoopKind
from glodex.runtime.request_payload import durable_request_payload

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.spec(
        "M5B-AC-001",
        "M5B-AC-002",
        "M5B-AC-004",
        "GLO-M5B-NFR-002",
    ),
]


class _Transport:
    def __init__(self, response: bytes | None) -> None:
        self.response = response
        self.calls: list[bytes] = []

    async def __call__(self, payload: bytes) -> bytes:
        self.calls.append(payload)
        if self.response is None:
            raise AssertionError("Reflect must not be called")
        return self.response


@dataclass
class _Store:
    turns: list[ConversationTurn]
    writes: list[tuple[MemoryCandidate, ...]] = field(default_factory=list)

    async def thread_owner(self, *, thread_id: str) -> str | None:
        return "user-" + "a" * 32 if thread_id == "thread-m5b" else None

    async def list_conversation_turns(
        self,
        *,
        thread_id: str,
        limit: int = 64,
    ) -> tuple[ConversationTurn, ...]:
        assert thread_id == "thread-m5b" and limit == 64
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
        assert user_id == "user-" + "a" * 32
        assert source_thread_id == "thread-m5b" and source_ordinal == 1
        self.writes.append(candidates)
        return ()


@pytest.mark.parametrize(
    "query",
    (
        "这次买耳机,预算 800 元以内",
        "预算 6000 元以内",
        "3000 左右",
    ),
)
def test_current_constraints_write_history_but_make_zero_reflect_calls(query: str) -> None:
    transport = _Transport(None)
    store = _store(query)

    asyncio.run(_writer(store, transport).write_terminal(run=_run(query), response=_response()))

    assert transport.calls == [] and store.writes == []
    assert [turn.role for turn in store.turns] == [
        ConversationRole.USER,
        ConversationRole.ASSISTANT,
    ]


def test_shopping_history_stores_the_short_display_summary_not_the_full_comparison() -> None:
    query = "预算 6000 元以内"
    transport = _Transport(None)
    store = _store(query)
    response = _shopping_response()

    asyncio.run(_writer(store, transport).write_terminal(run=_run(query), response=response))

    assert response.answer is not None and "FULL_COMPARISON_SENTINEL" in response.answer.text
    assert store.turns[-1].display_content == (
        "找到 1 款满足已验证硬性条件的候选; 未验证的偏好请查看需求覆盖状态。"
    )
    assert "FULL_COMPARISON_SENTINEL" not in store.turns[-1].display_content


def test_explicit_long_term_budget_makes_one_call_and_one_atomic_batch_write() -> None:
    query = "我平时预算通常在 3000 元左右"
    transport = _Transport(
        _provider(
            '{"preference_quotes":["我平时预算通常在 3000 元左右"],'
            '"blacklist_quotes":[],"history_quotes":[]}'
        )
    )
    store = _store(query)

    asyncio.run(_writer(store, transport).write_terminal(run=_run(query), response=_response()))

    assert len(transport.calls) == 1
    assert len(store.writes) == 1
    assert tuple(candidate.content for candidate in store.writes[0]) == (query,)


def test_prefilter_hit_with_an_immediate_quote_calls_once_but_writes_nothing() -> None:
    query = "这次我喜欢红色"
    transport = _Transport(
        _provider(
            '{"preference_quotes":["这次我喜欢红色"],"blacklist_quotes":[],"history_quotes":[]}'
        )
    )
    store = _store(query)

    asyncio.run(_writer(store, transport).write_terminal(run=_run(query), response=_response()))

    assert len(transport.calls) == 1 and store.writes == []


def test_one_invalid_candidate_discards_the_whole_reflect_response() -> None:
    query = "我一直喜欢小众设计,以后都不要塑料材质"
    transport = _Transport(
        _provider(
            '{"preference_quotes":["我一直喜欢小众设计","塑料材质"],'
            '"blacklist_quotes":[],"history_quotes":[]}'
        )
    )
    store = _store(query)

    asyncio.run(_writer(store, transport).write_terminal(run=_run(query), response=_response()))

    assert len(transport.calls) == 1 and store.writes == []


def test_terminal_memory_write_failure_is_safe_and_observable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    query = "我平时一直喜欢小众设计"
    transport = _Transport(
        _provider(
            '{"preference_quotes":["我平时一直喜欢小众设计"],'
            '"blacklist_quotes":[],"history_quotes":[]}'
        )
    )

    class FailingStore(_Store):
        async def write_explicit_reflect_memory(
            self,
            *,
            user_id: str,
            candidates: tuple[MemoryCandidate, ...],
            source_thread_id: str,
            source_ordinal: int,
        ) -> tuple[UserMemoryEntry, ...]:
            del user_id, candidates, source_thread_id, source_ordinal
            raise RuntimeError("PRIVATE_MEMORY_SENTINEL")

    source = _store(query)
    store = FailingStore(turns=source.turns)
    caplog.set_level(logging.WARNING, logger="glodex.memory.terminal_writer")

    asyncio.run(_writer(store, transport).write_terminal(run=_run(query), response=_response()))

    assert len(transport.calls) == 1
    assert store.turns[-1].role is ConversationRole.ASSISTANT
    records = [
        record for record in caplog.records if record.name == "glodex.memory.terminal_writer"
    ]
    assert len(records) == 1
    assert records[0].message == "terminal memory write failed"
    assert records[0].safe_code == "MEMORY_TERMINAL_WRITE_FAILED"  # type: ignore[attr-defined]
    assert query not in caplog.text
    assert "PRIVATE_MEMORY_SENTINEL" not in caplog.text


def _store(query: str) -> _Store:
    return _Store(
        turns=[
            ConversationTurn(
                thread_id="thread-m5b",
                ordinal=1,
                role=ConversationRole.USER,
                display_content=query,
            )
        ]
    )


def _writer(store: _Store, transport: _Transport) -> MemoryTerminalWriter:
    return MemoryTerminalWriter(
        store=store,
        reflector=LlmMemoryReflector(transport, model_name="test-model"),
    )


def _run(query: str) -> DurableRun:
    return DurableRun(
        run_id="run-m5b",
        thread_id="thread-m5b",
        root_run_id="run-m5b",
        parent_run_id=None,
        child_id=None,
        depth=0,
        loop_kind=LoopKind.ROOT,
        task_scope_digest=None,
        state=DurableRunState.COMPLETED,
        attempt=1,
        event_sequence=1,
        asset_version="m5b-test",
        config_fingerprint="a" * 64,
        request_payload=durable_request_payload(
            request=SearchRequest(query=query),
            display_query=query,
            task_turns=(query,),
        ),
        terminal_response={},
        terminal_error_code=None,
        cancel_requested=False,
    )


def _response() -> AgentDemoResponse:
    return AgentDemoResponse(
        run_id="run-m5b",
        status=RunStatus.COMPLETED,
        answer=AgentAnswer(kind=AgentAnswerKind.CHAT_FALLBACK, text="可信结果"),
    )


def _shopping_response() -> AgentDemoResponse:
    cost = MoneySummary(currency="CNY", exact="5799", display="5799.00")
    offer = OfferSummary(
        offer_id="offer-m5b",
        provider_id="provider-m5b",
        market="CN",
        landed_cost=cost,
    )
    result = SearchResult(
        product_id="product-m5b",
        title="Travel Notebook",
        category="laptop",
        selected_offer=offer,
        eligible_offers=(offer,),
        landed_cost=cost,
        reason="Verified fixture",
        evidence=(
            EvidenceSummary(
                evidence_id="evidence-m5b",
                provider_id="manufacturer-test",
                source_uri="https://example.com/specification",
                field_path="product.title",
                captured_at="2026-08-11T00:00:00+00:00",
            ),
        ),
    )
    search = SearchResponse(
        run_id="run-m5b",
        status=RunStatus.COMPLETED,
        snapshot_version="test-v1",
        config_fingerprint="a" * 64,
        algorithm_version="test-v1",
        results=(result,),
        filter_summary=FilterSummary(),
        diagnostics=Diagnostics(),
    )
    return AgentDemoResponse(
        run_id=search.run_id,
        status=RunStatus.COMPLETED,
        answer=AgentAnswer(
            kind=AgentAnswerKind.SHOPPING_SUMMARY,
            text="FULL_COMPARISON_SENTINEL: 完整逐项比较仍保留在 Agent answer。",
        ),
        search_response=search,
        selected_product_ids=(result.product_id,),
        evidence_ids=("evidence-m5b",),
    )


def _provider(content: str) -> bytes:
    return json.dumps(
        {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "tool_calls": [],
                    },
                }
            ]
        },
        separators=(",", ":"),
    ).encode()
