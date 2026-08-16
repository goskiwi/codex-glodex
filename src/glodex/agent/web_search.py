"""Tavily-backed ``WebSearchPort`` adapter for the bounded agent web_search tool.

``build_tavily_search`` (``glodex.facts.evidence``) returns a bare
``SearchTransport`` (``(query, limit) -> EvidenceResult``).  The Agent tool
port instead speaks ``WebSearchInput -> WebSearchOutput``.  This module is the
thin bounded adapter between the two: it keeps the Tavily result as evidence
only (source ID / domain / type / date / short snippet), never as a product
candidate or price fact, and it maps provider failures to safe stable codes.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from urllib.parse import urlsplit

from glodex.agent.contracts import (
    ToolFailureCode,
    WebEvidence,
    WebSearchInput,
    WebSearchOutput,
)
from glodex.agent.ports import ToolPortError
from glodex.facts.evidence import EvidenceError, SearchTransport

_MAX_QUERY: int = 200
_MAX_SNIPPET: int = 280
_MAX_TITLE: int = 256
_MAX_DOMAIN: int = 128
_SOURCE_ID_PREFIX: str = "web-"
_SOURCE_ID_DIGEST_LENGTH: int = 32


class TavilyWebSearchPort:
    """Adapt one bounded Tavily ``SearchTransport`` to the Agent port."""

    def __init__(self, search: SearchTransport) -> None:
        if not callable(search):
            raise TypeError("web search transport must be callable")
        self._search = search

    async def search(self, request: WebSearchInput) -> WebSearchOutput:
        if type(request) is not WebSearchInput:
            raise TypeError("web search port requires an exact WebSearchInput")
        bounded_query = request.query.strip()[:_MAX_QUERY]
        try:
            results = await self._search(bounded_query, request.max_results)
        except EvidenceError:
            raise ToolPortError(ToolFailureCode.PROVIDER_UNAVAILABLE) from None
        evidence = tuple(
            WebEvidence(
                source_id=_source_id(item.url),
                title=item.title[:_MAX_TITLE],
                url_domain=_url_domain(item.url),
                published_at=_published_at(item.published_date),
                snippet=item.snippet[:_MAX_SNIPPET],
                source_type=request.evidence_kind,
            )
            for item in results[: request.max_results]
        )
        try:
            return WebSearchOutput(evidence=evidence)
        except Exception:
            raise ToolPortError(ToolFailureCode.PROVIDER_RESPONSE_INVALID) from None


def _source_id(url: str) -> str:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:_SOURCE_ID_DIGEST_LENGTH]
    return f"{_SOURCE_ID_PREFIX}{digest}"


def _url_domain(url: str) -> str:
    hostname = urlsplit(url).hostname
    if hostname is None or not hostname:
        return "unknown"
    return hostname[:_MAX_DOMAIN]


def _published_at(value: str | None) -> datetime | None:
    if value is None or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


__all__ = [
    "TavilyWebSearchPort",
]
