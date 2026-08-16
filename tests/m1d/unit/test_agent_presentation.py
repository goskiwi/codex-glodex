"""Unit evidence for the shared bounded Agent display summary."""

from __future__ import annotations

import pytest

from glodex.agent.contracts import AgentAnswer, AgentAnswerKind, AgentDemoResponse
from glodex.agent.presentation import AgentDisplayContentKind, agent_display_summary
from glodex.contracts import Diagnostics, FilterSummary, RunStatus, SearchResponse

pytestmark = pytest.mark.unit


def test_no_match_uses_fixed_copy_without_replacing_the_complete_agent_answer() -> None:
    search = SearchResponse(
        run_id="run-display-no-match",
        status=RunStatus.NO_MATCH,
        snapshot_version="test-v1",
        config_fingerprint="a" * 64,
        algorithm_version="test-v1",
        filter_summary=FilterSummary(),
        diagnostics=Diagnostics(),
    )
    response = AgentDemoResponse(
        run_id=search.run_id,
        status=RunStatus.NO_MATCH,
        answer=AgentAnswer(
            kind=AgentAnswerKind.SHOPPING_SUMMARY,
            text="完整 Agent NO_MATCH 回答仍供内部消费者使用。",
        ),
        search_response=search,
    )

    display = agent_display_summary(response)

    assert display.content_kind is AgentDisplayContentKind.SHOPPING_RESULTS
    assert display.text == "没有找到满足已验证硬性条件的商品。"
    assert response.answer.text == "完整 Agent NO_MATCH 回答仍供内部消费者使用。"


def test_chat_fallback_must_already_fit_the_display_bound() -> None:
    response = AgentDemoResponse(
        run_id="run-display-chat",
        status=RunStatus.COMPLETED,
        answer=AgentAnswer(
            kind=AgentAnswerKind.CHAT_FALLBACK,
            text="x" * 281,
        ),
    )

    with pytest.raises(ValueError, match="display summary text is invalid"):
        agent_display_summary(response)
