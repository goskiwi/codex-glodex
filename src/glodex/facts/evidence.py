"""Runtime ``product_evidence-v1`` completion through bounded web search."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol, runtime_checkable

import httpx

PRODUCT_EVIDENCE_SCHEMA: Final = "glodex.product-evidence.v1"
EVIDENCE_GENERATOR_NAME: Final = "glodex-product-evidence"
EVIDENCE_GENERATOR_VERSION: Final = "v1"
_TAVILY_URL: Final = "https://api.tavily.com/search"
_TAVILY_CREDENTIAL_NAME: Final = "TAVILY_API_KEY"
_TAVILY_TIMEOUT_SECONDS: Final = 20.0
_MAX_SEARCH_RESULTS: Final = 5
_MAX_SNIPPET: Final = 800
_MAX_QUERY: Final = 200

# proof_fact_key -> search keywords.  This is not a requirement keyword map:
# the requirement is expressed structurally by ``criteria``; these terms only
# build the WebSearch query ``品牌 + 精确型号/ID + 版本/变体 + 所缺指标``.
_PROOF_SEARCH_TERMS: Final = {
    "battery_life_realworld": "续航测试 电池实测",
    "gaming_fps": "游戏 帧率 性能测试",
    "anc_review": "降噪 评测",
    "portability_review": "重量 便携 评测",
}

type SearchTransport = Callable[[str, int], Awaitable[tuple[EvidenceResult, ...]]]


@dataclass(frozen=True, slots=True)
class EvidenceResult:
    """One bounded WebSearch hit with source and snippet."""

    url: str
    title: str
    snippet: str
    published_date: str | None

    def __post_init__(self) -> None:
        if type(self.url) is not str or not self.url:
            raise ValueError("evidence URL is invalid")
        if type(self.title) is not str or not self.title:
            raise ValueError("evidence title is invalid")
        if type(self.snippet) is not str or not self.snippet:
            raise ValueError("evidence snippet is invalid")
        if self.published_date is not None and type(self.published_date) is not str:
            raise ValueError("evidence published date is invalid")


class EvidenceError(RuntimeError):
    """A stable failure while completing evidence for one product."""


def proof_search_terms(proof_fact_key: str) -> str | None:
    """Search keywords for one proof fact key, or None when not searchable."""
    return _PROOF_SEARCH_TERMS.get(proof_fact_key)


def build_search_query(*, brand: str, model: str, keyword: str) -> str:
    """Build the bounded ``品牌 + 精确型号 + 关键词`` query."""
    query = f"{brand} {model} {keyword}".strip()
    return query[:_MAX_QUERY]


def build_tavily_search(
    *,
    api_key: str | None = None,
    max_results: int = _MAX_SEARCH_RESULTS,
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> SearchTransport:
    """Build the one bounded Tavily search callable (async)."""

    if type(max_results) is not int or isinstance(max_results, bool) or max_results < 1:
        raise EvidenceError("max results is invalid")
    credential = api_key if api_key is not None else os.getenv(_TAVILY_CREDENTIAL_NAME)
    if credential is None or not isinstance(credential, str) or not credential.strip():
        raise EvidenceError("Tavily credentials are missing")
    if "\r" in credential or "\n" in credential:
        raise EvidenceError("Tavily credentials are invalid")

    async def search(query: str, limit: int) -> tuple[EvidenceResult, ...]:
        if type(query) is not str or not query.strip() or len(query) > _MAX_QUERY:
            raise EvidenceError("search query is invalid")
        if type(limit) is not int or isinstance(limit, bool) or limit < 1:
            raise EvidenceError("search limit is invalid")
        payload = {
            "api_key": credential,
            "query": query.strip(),
            "max_results": min(limit, max_results),
            "include_answer": False,
        }
        try:
            async with asyncio.timeout(_TAVILY_TIMEOUT_SECONDS):
                active_transport = http_transport
                if active_transport is None:
                    active_transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False)
                async with httpx.AsyncClient(
                    transport=active_transport,
                    timeout=httpx.Timeout(_TAVILY_TIMEOUT_SECONDS),
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    response = await client.post(
                        _TAVILY_URL,
                        json=payload,
                        headers={"Content-Type": "application/json"},
                    )
                    if not 200 <= response.status_code < 300:
                        raise EvidenceError("Tavily provider is unavailable")
                    return _parse_results(response.json())
        except EvidenceError:
            raise
        except (TimeoutError, httpx.HTTPError):
            raise EvidenceError("Tavily provider is unavailable") from None
        except ValueError:
            raise EvidenceError("Tavily response is invalid") from None

    return search


def _parse_results(payload: object) -> tuple[EvidenceResult, ...]:
    if type(payload) is not dict:
        raise EvidenceError("Tavily response is invalid")
    results = payload.get("results")
    if type(results) is not list:
        raise EvidenceError("Tavily response is invalid")
    parsed: list[EvidenceResult] = []
    for item in results:
        if type(item) is not dict:
            continue
        url = item.get("url")
        title = item.get("title")
        content = item.get("content")
        if type(url) is not str or type(title) is not str or type(content) is not str:
            continue
        published = item.get("published_date")
        parsed.append(
            EvidenceResult(
                url=url,
                title=title,
                snippet=content[:_MAX_SNIPPET],
                published_date=published if isinstance(published, str) else None,
            )
        )
    return tuple(parsed)


@runtime_checkable
class EvidenceStore(Protocol):
    """Durable evidence cache contract (PostgreSQL in production)."""

    async def get(
        self, canonical_product_id: str, fact_key: str
    ) -> tuple[dict[str, object], ...]: ...

    async def put(self, evidence: Mapping[str, object]) -> None: ...

    async def close(self) -> None: ...


class MemoryEvidenceStore:
    """In-memory evidence cache for tests and single-process runs.

    This is not the production persistence layer; PostgreSQL owns the durable
    ``product_evidence`` table via ``PostgresEvidenceStore``.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], list[dict[str, object]]] = {}

    async def get(self, canonical_product_id: str, fact_key: str) -> tuple[dict[str, object], ...]:
        return tuple(self._entries.get((canonical_product_id, fact_key), ()))

    async def put(self, evidence: Mapping[str, object]) -> None:
        canonical = _text(evidence.get("canonical_product_id"), "evidence canonical ID")
        fact_key = _text(evidence.get("fact_key"), "evidence fact key")
        self._entries.setdefault((canonical, fact_key), []).append(dict(evidence))

    async def close(self) -> None:
        return None

    @property
    def size(self) -> int:
        return sum(len(rows) for rows in self._entries.values())


@dataclass(frozen=True, slots=True)
class EvidenceCandidate:
    """One ranked candidate that may need runtime evidence completion."""

    canonical_product_id: str
    brand: str
    model: str
    category: str
    facts: Mapping[str, str]
    identifiers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("canonical_product_id", "brand", "model", "category"):
            value = getattr(self, name)
            if type(value) is not str:
                raise EvidenceError(f"candidate {name} is invalid")
        if not isinstance(self.identifiers, Mapping):
            raise EvidenceError("candidate identifiers are invalid")


def bind_results_to_identity(
    results: Sequence[EvidenceResult],
    *,
    brand: str,
    model: str,
    identifiers: Mapping[str, str],
) -> tuple[EvidenceResult, ...]:
    """Keep only WebSearch hits that are bound to this exact product.

    A hit must mention the concrete model (or a searchable external ID such as
    an ISBN/ASIN/MPN) in its title or snippet; a similar-looking title alone
    is never enough.  Identity-unbound results are dropped before they can
    enter the evidence layer.
    """

    if type(results) is not tuple and type(results) is not list:
        raise EvidenceError("results must be a sequence")
    bound: list[EvidenceResult] = []
    identity_tokens = {_normalize_token(token) for token in (model, *identifiers.values()) if token}
    for result in results:
        if type(result) is not EvidenceResult:
            raise EvidenceError("evidence result is invalid")
        haystack = _normalize_token(result.title + " " + result.snippet)
        if any(token and token in haystack for token in identity_tokens):
            bound.append(result)
    return tuple(bound)


def _normalize_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.strip().lower())


class EvidenceCompleter:
    """Complete key evidence gaps for identity-clear, ranked candidates.

    ``complete`` checks the durable cache first; only missing evidence triggers
    a bounded WebSearch, and every result is written back to the cache so later
    queries reuse it.  Candidates without a concrete model are never searched.
    """

    def __init__(
        self,
        *,
        store: EvidenceStore,
        search: SearchTransport,
    ) -> None:
        if not isinstance(store, EvidenceStore):
            raise EvidenceError("evidence store is invalid")
        if not callable(search):
            raise EvidenceError("evidence search is invalid")
        self._store = store
        self._search = search

    async def complete(
        self,
        candidates: Sequence[EvidenceCandidate],
        proof_keys: Sequence[str],
        *,
        max_per_product: int = 2,
    ) -> dict[str, dict[str, tuple[dict[str, object], ...]]]:
        """Return canonical_product_id -> fact_key -> evidence rows.

        ``proof_keys`` are the criteria-derived proof fact keys that a stable
        spec cannot prove on its own (e.g. ``gaming_fps``).
        """
        if type(candidates) is not tuple and type(candidates) is not list:
            raise EvidenceError("candidates must be a sequence")
        if type(proof_keys) is not tuple and type(proof_keys) is not list:
            raise EvidenceError("proof keys must be a sequence")
        if (
            type(max_per_product) is not int
            or isinstance(max_per_product, bool)
            or max_per_product < 1
        ):
            raise EvidenceError("max per product is invalid")
        needs: list[tuple[str, str]] = []
        for proof_fact_key in proof_keys:
            keyword = proof_search_terms(proof_fact_key)
            if keyword is not None:
                needs.append((proof_fact_key, keyword))
        result: dict[str, dict[str, tuple[dict[str, object], ...]]] = {}
        for candidate in candidates:
            if not candidate.brand or not candidate.model:
                continue  # identity not clear enough for WebSearch
            per_product: dict[str, list[dict[str, object]]] = {}
            for fact_key, keyword in needs:
                cached = await self._store.get(candidate.canonical_product_id, fact_key)
                if cached:
                    per_product[fact_key] = list(cached)
                    continue
                query = build_search_query(
                    brand=candidate.brand, model=candidate.model, keyword=keyword
                )
                try:
                    results = await self._search(query, max_per_product)
                except EvidenceError:
                    continue
                if not results:
                    continue
                bound = bind_results_to_identity(
                    results,
                    brand=candidate.brand,
                    model=candidate.model,
                    identifiers=candidate.identifiers,
                )
                if not bound:
                    continue
                row = build_evidence_row(
                    canonical_product_id=candidate.canonical_product_id,
                    brand=candidate.brand,
                    model=candidate.model,
                    fact_key=fact_key,
                    query=query,
                    results=bound,
                )
                await self._store.put(row)
                per_product[fact_key] = [row]
            if per_product:
                result[candidate.canonical_product_id] = {
                    key: tuple(rows) for key, rows in per_product.items()
                }
        return result


def build_evidence_row(
    *,
    canonical_product_id: str,
    brand: str,
    model: str,
    fact_key: str,
    query: str,
    results: Sequence[EvidenceResult],
    captured_at: datetime | None = None,
) -> dict[str, object]:
    """Assemble one durable product-evidence row from bounded search hits."""
    if not results:
        raise EvidenceError("evidence requires at least one result")
    captured = captured_at if captured_at is not None else datetime.now(UTC)
    if captured.tzinfo is None or captured.utcoffset() != timedelta(0):
        raise EvidenceError("evidence capture time must be UTC")
    claims = [
        {
            "source_url": result.url,
            "title": result.title,
            "snippet": result.snippet,
            "published_date": result.published_date,
        }
        for result in results
    ]
    return {
        "schema_version": PRODUCT_EVIDENCE_SCHEMA,
        "evidence_id": _evidence_id(canonical_product_id, fact_key, query, captured),
        "canonical_product_id": canonical_product_id,
        "brand": brand,
        "model": model,
        "fact_key": fact_key,
        "query": query,
        "claims": claims,
        "captured_at": captured.isoformat(),
        "status": "EVIDENCED",
    }


def _evidence_id(canonical: str, fact_key: str, query: str, captured: datetime) -> str:
    return hashlib.sha256(
        f"{canonical}\0{fact_key}\0{query}\0{captured.isoformat()}".encode()
    ).hexdigest()[:32]


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise EvidenceError(f"{name} is invalid")
    return value


__all__ = [
    "EVIDENCE_GENERATOR_NAME",
    "EVIDENCE_GENERATOR_VERSION",
    "PRODUCT_EVIDENCE_SCHEMA",
    "EvidenceCandidate",
    "EvidenceCompleter",
    "EvidenceError",
    "EvidenceResult",
    "EvidenceStore",
    "MemoryEvidenceStore",
    "SearchTransport",
    "bind_results_to_identity",
    "build_evidence_row",
    "build_search_query",
    "build_tavily_search",
    "proof_search_terms",
]
