"""Conversation task continuity from the last trusted shopping terminal."""

from __future__ import annotations

import asyncio

import pytest

from glodex.agent.contracts import AgentAnswer, AgentAnswerKind, AgentDemoResponse
from glodex.contracts import (
    Diagnostics,
    FilterSummary,
    InterpretedCriterionSummary,
    InterpretedRequestSummary,
    RunStatus,
    SearchRequest,
    SearchResponse,
    SourceSpanSummary,
)
from glodex.domain.intent import budget_source_spans
from glodex.memory.conversation import ConversationRequestResolver, merge_current_shopping_task
from glodex.memory.models import ConversationRole, ConversationTurn
from glodex.runtime.contracts import DurableRun, DurableRunState, LoopKind
from glodex.runtime.request_payload import (
    display_query_from_durable_payload,
    durable_request_payload,
    search_request_from_durable_payload,
)

pytestmark = pytest.mark.unit


def _criterion(kind: str, text: str, start: int) -> InterpretedCriterionSummary:
    return InterpretedCriterionSummary(
        kind=kind,
        value=text,
        source_span=SourceSpanSummary(start=start, end=start + len(text), text=text),
    )


def _previous_request() -> InterpretedRequestSummary:
    return InterpretedRequestSummary(
        required=(
            _criterion("target_category", "笔记本", 0),
            _criterion("budget_max", "8000元以内", 4),
        ),
        preferred=(
            _criterion("preference", "旅行", 12),
            _criterion("preference", "剪视频", 15),
            _criterion("weight_max", "重量最好不超过1.5kg", 19),
        ),
        parser_version="planner-v1",
    )


def test_budget_adjustment_overlays_prior_verified_conditions() -> None:
    original = "帮我找一台8000元以内、适合旅行剪视频、重量最好不超过1.5kg的笔记本。"
    current = SearchRequest(
        query="预算改成7000元以内",
        display_currency="CNY",
        top_k=2,
        snapshot_version="snapshot-v1",
    )

    resolved, task_turns = merge_current_shopping_task(
        current=current,
        previous_turns=(original,),
    )

    assert resolved.query == (
        "ORIGINAL_USER_REQUEST: 帮我找一台[SUPERSEDED_BUDGET]、"
        "适合旅行剪视频、重量最好不超过1.5kg的笔记本。\n"
        "USER_ADJUSTMENT_1: 预算改成7000元以内"
    )
    assert "8000元以内" not in resolved.query
    assert "7000元以内" in resolved.query
    assert "笔记本" in resolved.query
    assert resolved.display_currency == "CNY"
    assert resolved.top_k == 2
    assert resolved.snapshot_version == "snapshot-v1"
    assert task_turns == (original, "预算改成7000元以内")
    assert tuple(span.text for span in budget_source_spans(resolved.query)) == (
        "预算改成7000元以内",
    )


def test_resolver_reads_only_last_trusted_shopping_terminal() -> None:
    search = SearchResponse(
        run_id="run-conversation-1",
        status=RunStatus.NO_MATCH,
        snapshot_version="snapshot-v1",
        config_fingerprint="a" * 64,
        algorithm_version="agent-v1",
        interpreted_request=_previous_request(),
        filter_summary=FilterSummary(),
        diagnostics=Diagnostics(),
    )
    response = AgentDemoResponse(
        run_id=search.run_id,
        status=RunStatus.NO_MATCH,
        answer=AgentAnswer(kind=AgentAnswerKind.SHOPPING_SUMMARY, text="没有匹配商品。"),
        search_response=search,
    )
    run = DurableRun(
        run_id=search.run_id,
        thread_id="thread-conversation-1",
        root_run_id=search.run_id,
        parent_run_id=None,
        child_id=None,
        depth=0,
        loop_kind=LoopKind.ROOT,
        task_scope_digest=None,
        state=DurableRunState.COMPLETED,
        attempt=1,
        event_sequence=1,
        asset_version="test-v1",
        config_fingerprint="a" * 64,
        request_payload=durable_request_payload(
            request=SearchRequest(query="8000元以内的旅行剪视频笔记本"),
            display_query="8000元以内的旅行剪视频笔记本",
            task_turns=("8000元以内的旅行剪视频笔记本",),
        ),
        terminal_response=response.model_dump(mode="json"),
        terminal_error_code=None,
        cancel_requested=False,
    )

    class Store:
        async def list_conversation_turns(
            self, *, thread_id: str, limit: int = 64
        ) -> tuple[ConversationTurn, ...]:
            assert thread_id == run.thread_id and limit == 64
            return (
                ConversationTurn(
                    thread_id=run.thread_id,
                    ordinal=1,
                    role=ConversationRole.USER,
                    display_content="8000元以内的旅行剪视频笔记本",
                ),
                ConversationTurn(
                    thread_id=run.thread_id,
                    ordinal=2,
                    role=ConversationRole.ASSISTANT,
                    display_content="没有匹配商品。",
                    terminal_run_id=run.run_id,
                ),
            )

        async def load_run(self, *, run_id: str) -> DurableRun:
            assert run_id == run.run_id
            return run

    resolved = asyncio.run(
        ConversationRequestResolver(store=Store()).resolve(
            thread_id=run.thread_id,
            current=SearchRequest(query="预算改成7000元以内"),
        )
    )

    assert "旅行" in resolved.request.query
    assert "笔记本" in resolved.request.query
    assert resolved.request.query.endswith("预算改成7000元以内")
    assert resolved.task_turns == (
        "8000元以内的旅行剪视频笔记本",
        "预算改成7000元以内",
    )


def test_durable_envelope_separates_display_turn_and_effective_request() -> None:
    effective = SearchRequest(
        query="CURRENT_SHOPPING_CONDITIONS: 旧条件\nCURRENT_USER_ADJUSTMENT: 预算7000元以内",
        display_currency="CNY",
    )
    payload = durable_request_payload(
        request=effective,
        display_query="预算7000元以内",
        task_turns=("旧条件", "预算7000元以内"),
    )

    assert display_query_from_durable_payload(payload) == "预算7000元以内"
    assert search_request_from_durable_payload(payload) == effective
