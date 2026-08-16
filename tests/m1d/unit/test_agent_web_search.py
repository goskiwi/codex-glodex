"""Unit tests for the bounded Tavily ``WebSearchPort`` adapter."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from glodex.agent.contracts import (
    EvidenceKind,
    ToolFailureCode,
    WebEvidence,
    WebSearchInput,
    WebSearchOutput,
)
from glodex.agent.ports import ToolPortError
from glodex.agent.web_search import TavilyWebSearchPort
from glodex.facts.evidence import EvidenceError, EvidenceResult


def _search(results: tuple[EvidenceResult, ...] = ()) -> object:
    async def search(query: str, limit: int) -> tuple[EvidenceResult, ...]:
        del query, limit
        return results

    return search


def test_port_maps_evidence_results_to_bounded_web_evidence() -> None:
    port = TavilyWebSearchPort(_search(  # type: ignore[arg-type]
        (
            EvidenceResult(
                url="https://reviews.example.com/laptop/1",
                title="Laptop review with a very long title",
                snippet="A bounded review snippet that must be trimmed to fit.",
                published_date="2026-07-01T08:00:00Z",
            ),
        )
    ))

    async def run() -> None:
        result = await port.search(
            WebSearchInput(query="laptop review", evidence_kind=EvidenceKind.REVIEW)
        )
        assert type(result) is WebSearchOutput
        assert len(result.evidence) == 1
        evidence = result.evidence[0]
        assert type(evidence) is WebEvidence
        assert evidence.source_id.startswith("web-")
        assert evidence.source_id == evidence.source_id  # deterministic
        assert evidence.url_domain == "reviews.example.com"
        assert evidence.published_at == datetime(2026, 7, 1, 8, 0, tzinfo=UTC)
        assert evidence.source_type is EvidenceKind.REVIEW
        assert 0 < len(evidence.snippet) <= 280

    asyncio.run(run())


def test_port_caps_results_to_request_max_results() -> None:
    results = tuple(
        EvidenceResult(
            url=f"https://example.test/{index}",
            title=f"Title {index}",
            snippet=f"Snippet {index}",
            published_date=None,
        )
        for index in range(5)
    )
    port = TavilyWebSearchPort(_search(results))  # type: ignore[arg-type]

    async def run() -> None:
        result = await port.search(
            WebSearchInput(query="laptop", evidence_kind=EvidenceKind.GUIDE, max_results=2)
        )
        assert len(result.evidence) == 2

    asyncio.run(run())


def test_port_maps_unavailable_provider_to_safe_code() -> None:
    async def broken(query: str, limit: int) -> tuple[EvidenceResult, ...]:
        del query, limit
        raise EvidenceError("Tavily provider is unavailable")

    port = TavilyWebSearchPort(broken)  # type: ignore[arg-type]

    async def run() -> None:
        with pytest.raises(ToolPortError) as captured:
            await port.search(
                WebSearchInput(query="laptop", evidence_kind=EvidenceKind.TREND)
            )
        assert captured.value.code is ToolFailureCode.PROVIDER_UNAVAILABLE

    asyncio.run(run())


def test_port_rejects_non_exact_input() -> None:
    port = TavilyWebSearchPort(_search())  # type: ignore[arg-type]

    async def run() -> None:
        with pytest.raises(TypeError):
            await port.search(object())  # type: ignore[arg-type]

    asyncio.run(run())


def test_port_rejects_non_callable_transport() -> None:
    with pytest.raises(TypeError):
        TavilyWebSearchPort(object())  # type: ignore[arg-type]
