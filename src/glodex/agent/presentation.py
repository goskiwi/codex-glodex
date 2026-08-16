"""Deterministic user-facing summaries derived from trusted Agent terminal results."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from glodex.agent.contracts import AgentAnswerKind, AgentDemoResponse
from glodex.contracts import RunStatus

_MAX_DISPLAY_SUMMARY_CHARS = 280


class AgentDisplayContentKind(StrEnum):
    """The two terminal presentation modes understood by user-facing consumers."""

    SHOPPING_RESULTS = "SHOPPING_RESULTS"
    CHAT_FALLBACK = "CHAT_FALLBACK"


@dataclass(frozen=True, slots=True)
class AgentDisplaySummary:
    """One bounded display projection that never replaces the complete Agent answer."""

    content_kind: AgentDisplayContentKind
    text: str

    def __post_init__(self) -> None:
        if type(self.content_kind) is not AgentDisplayContentKind:
            raise TypeError("display summary content kind is invalid")
        if (
            type(self.text) is not str
            or not self.text
            or self.text != self.text.strip()
            or len(self.text) > _MAX_DISPLAY_SUMMARY_CHARS
            or "\0" in self.text
        ):
            raise ValueError("display summary text is invalid")


def agent_display_summary(response: AgentDemoResponse) -> AgentDisplaySummary:
    """Project a short user summary while retaining the full answer on ``response``."""

    if type(response) is not AgentDemoResponse:
        raise TypeError("display summary requires an exact AgentDemoResponse")
    if response.status not in {RunStatus.COMPLETED, RunStatus.NO_MATCH} or response.answer is None:
        raise ValueError("display summary requires a successful terminal response")

    if response.answer.kind is AgentAnswerKind.CHAT_FALLBACK:
        return AgentDisplaySummary(
            content_kind=AgentDisplayContentKind.CHAT_FALLBACK,
            text=response.answer.text,
        )

    search_response = response.search_response
    if search_response is None or search_response.status is not response.status:
        raise ValueError("shopping display summary requires its terminal SearchResponse")
    if response.status is RunStatus.NO_MATCH:
        text = "没有找到满足已验证硬性条件的商品。"
    else:
        text = (
            f"找到 {len(search_response.results)} 款满足已验证硬性条件的候选; "
            "未验证的偏好请查看需求覆盖状态。"
        )
    return AgentDisplaySummary(
        content_kind=AgentDisplayContentKind.SHOPPING_RESULTS,
        text=text,
    )


__all__ = ["AgentDisplayContentKind", "AgentDisplaySummary", "agent_display_summary"]
