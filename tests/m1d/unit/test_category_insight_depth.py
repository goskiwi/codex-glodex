from decimal import Decimal

from glodex.agent.contracts import (
    AgentCapabilities,
    CategoryInsightOutput,
    DataMode,
    InsightDepth,
    InsightStatus,
    Platform,
)
from glodex.agent.tool_session import _category_insight_can_run, _SessionState
from glodex.contracts import SearchRequest


def _state(depth: InsightDepth) -> _SessionState:
    return _SessionState(
        request=SearchRequest(query="推荐轻薄本"),
        interpreted_request=None,
        required_baseline=None,
        capabilities=AgentCapabilities(
            data_mode=DataMode.SYNTHETIC_INTERVIEW,
            available_platforms=(Platform.AMAZON,),
            web_search_enabled=False,
            embedding_enabled=True,
        ),
        category_result=CategoryInsightOutput(
            status=InsightStatus.FOUND,
            category="轻薄本",
            components=("轻薄本", "游戏本"),
            confidence=Decimal("0.9"),
        ),
        category_depth=depth,
    )


def test_quick_may_upgrade_once_to_deep() -> None:
    state = _state(InsightDepth.QUICK)
    assert _category_insight_can_run(
        state,
        category="轻薄本",
        depth=InsightDepth.DEEP,
    )
    assert not _category_insight_can_run(
        state,
        category="轻薄本",
        depth=InsightDepth.QUICK,
    )


def test_deep_and_other_category_cannot_repeat() -> None:
    state = _state(InsightDepth.DEEP)
    assert not _category_insight_can_run(
        state,
        category="轻薄本",
        depth=InsightDepth.DEEP,
    )
    assert not _category_insight_can_run(
        state,
        category="游戏本",
        depth=InsightDepth.DEEP,
    )
