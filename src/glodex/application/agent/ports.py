"""Narrow framework-free ports used by the M1d application tools."""

from __future__ import annotations

from typing import Protocol

from glodex.application.agent.contracts import (
    ActionSelectionInput,
    AgentRunEvent,
    CallToolAction,
    CategoryInsightInput,
    CategoryInsightOutput,
    EmbeddingBatch,
    EmbeddingResult,
    ItemSearchInput,
    ItemSearchRuntimeResult,
    ToolFailureCode,
    WebSearchInput,
    WebSearchOutput,
)


class ToolPortError(RuntimeError):
    """A provider-neutral safe error carrying no dynamic third-party text."""

    def __init__(self, code: ToolFailureCode) -> None:
        if type(code) is not ToolFailureCode:
            raise TypeError("tool port error code must be an exact ToolFailureCode")
        self.code = code
        super().__init__(code.value)


class AgentEventObserver(Protocol):
    """Observe one immutable safe Agent event synchronously."""

    def on_event(self, event: AgentRunEvent) -> None: ...


class ActionSelector(Protocol):
    async def select(self, request: ActionSelectionInput) -> CallToolAction: ...


class WebSearchPort(Protocol):
    async def search(self, request: WebSearchInput) -> WebSearchOutput: ...


class EmbeddingPort(Protocol):
    async def embed(self, request: EmbeddingBatch) -> EmbeddingResult: ...


class CategoryInsightPort(Protocol):
    async def retrieve(self, request: CategoryInsightInput) -> CategoryInsightOutput: ...


class ItemSourcePort(Protocol):
    async def search(
        self,
        request: ItemSearchInput,
        *,
        query_vector: tuple[float, ...] | None,
    ) -> ItemSearchRuntimeResult: ...


__all__ = [
    "ActionSelector",
    "AgentEventObserver",
    "CategoryInsightPort",
    "EmbeddingPort",
    "ItemSourcePort",
    "ToolPortError",
    "WebSearchPort",
]
