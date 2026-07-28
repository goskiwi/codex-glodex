from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from glodex.adapters.local_snapshot import LocalSnapshotCatalog
from glodex.adapters.rule_intent import RuleIntentInterpreter
from glodex.application.search_service import SearchService
from glodex.config import GlodexConfig
from glodex.contracts import RunStatus, SearchRequest
from glodex.domain.catalog import CatalogBatch, EntityKind
from glodex.domain.eligibility import EligibleProduct
from glodex.domain.intent import InterpretedRequest

pytestmark = pytest.mark.acceptance

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_ROOT = PROJECT_ROOT / "data" / "snapshots"


@pytest.fixture(scope="module")
def m0_batch() -> CatalogBatch:
    batch = asyncio.run(
        LocalSnapshotCatalog(SNAPSHOT_ROOT).load(
            "m0-v1",
            display_currency="USD",
            budget_currency="USD",
        )
    )
    assert batch.fatal_issues == ()
    return batch


@dataclass
class _RunIds:
    calls: int = 0

    def next_run_id(self) -> str:
        self.calls += 1
        return f"run-ac-c07-{self.calls}"


@dataclass
class _Clock:
    monotonic_calls: int = 0

    def now_utc(self) -> datetime:
        return datetime(2026, 7, 23, tzinfo=UTC)

    def monotonic_ns(self) -> int:
        self.monotonic_calls += 1
        return self.monotonic_calls * 1_000_000


@dataclass
class _IntentSpy:
    delegate: RuleIntentInterpreter = field(default_factory=RuleIntentInterpreter)
    calls: int = 0

    async def interpret(self, request: SearchRequest) -> InterpretedRequest:
        self.calls += 1
        return await self.delegate.interpret(request)


@dataclass
class _CatalogSpy:
    batch: CatalogBatch
    calls: list[tuple[str, str | None, str | None]] = field(default_factory=list)

    async def load(
        self,
        snapshot_version: str,
        *,
        display_currency: str | None = None,
        budget_currency: str | None = None,
    ) -> CatalogBatch:
        self.calls.append((snapshot_version, display_currency, budget_currency))
        return self.batch


@dataclass
class _RankerSpy:
    calls: list[tuple[object, ...]] = field(default_factory=list)

    async def rank(
        self,
        query: str,
        preferred: tuple[object, ...],
        candidates: tuple[object, ...],
    ) -> tuple[object, ...]:
        self.calls.append(candidates)
        return candidates


@dataclass
class _MutatingRanker:
    raises: bool

    async def rank(
        self,
        query: str,
        preferred: tuple[object, ...],
        candidates: tuple[object, ...],
    ) -> tuple[object, ...]:
        del query
        del preferred
        candidate = cast(EligibleProduct, candidates[0])
        object.__setattr__(
            candidate.product,
            "entity_kind",
            EntityKind.ACCESSORY,
        )
        if self.raises:
            raise RuntimeError("mutated before failure")
        return candidates


def _service(
    batch: CatalogBatch,
) -> tuple[SearchService, _RunIds, _IntentSpy, _CatalogSpy, _RankerSpy]:
    run_ids = _RunIds()
    intent = _IntentSpy()
    catalog = _CatalogSpy(batch)
    ranker = _RankerSpy()
    service = SearchService(
        config=GlodexConfig(
            data_dir=SNAPSHOT_ROOT,
            default_snapshot="m0-v1",
            default_locale="zh-CN",
            default_currency="USD",
            default_top_k=3,
            fingerprint="a" * 64,
        ),
        run_id_provider=run_ids,
        clock=_Clock(),
        intent_interpreter=intent,
        catalog_gateway=catalog,
        query_ranker=ranker,
    )
    return service, run_ids, intent, catalog, ranker


def _product_ids(candidates: tuple[object, ...]) -> tuple[str, ...]:
    assert all(type(candidate) is EligibleProduct for candidate in candidates)
    return tuple(cast(EligibleProduct, candidate).product.product_id for candidate in candidates)


@pytest.mark.spec("AC-002")
def test_ac_002_over_budget_candidate_never_reaches_ranker_spy(
    m0_batch: CatalogBatch,
) -> None:
    service, _, _, _, ranker = _service(m0_batch)

    response = asyncio.run(service.search(SearchRequest(query="推荐 800 美元以内的笔记本")))

    assert len(ranker.calls) == 1
    ranked_ids = _product_ids(ranker.calls[0])
    assert "prod-budget-exact-800" in ranked_ids
    assert "prod-budget-over-800-01" not in ranked_ids
    assert "prod-budget-over-800-01" not in {result.product_id for result in response.results}


@pytest.mark.spec("AC-003")
def test_ac_003_zero_match_does_not_retry_or_relax_budget(
    m0_batch: CatalogBatch,
) -> None:
    service, run_ids, intent, catalog, ranker = _service(m0_batch)

    response = asyncio.run(service.search(SearchRequest(query="预算 1 美元以内的笔记本")))

    assert response.status is RunStatus.NO_MATCH
    assert response.results == ()
    assert response.interpreted_request.required[0].value == "1 USD"
    counts = {item.reason: item for item in response.filter_summary.reason_counts}
    assert "offer.over-budget" in counts
    assert counts["offer.over-budget"].offer_count > 0
    assert run_ids.calls == 1
    assert intent.calls == 1
    assert catalog.calls == [("m0-v1", "USD", "USD")]
    assert ranker.calls == []


@pytest.mark.spec("AC-005")
def test_ac_005_unknown_cost_and_out_of_stock_offers_are_rejected(
    m0_batch: CatalogBatch,
) -> None:
    service, _, _, _, ranker = _service(m0_batch)

    response = asyncio.run(service.search(SearchRequest(query="推荐 800 美元以内、有库存的笔记本")))

    assert len(ranker.calls) == 1
    ranked_ids = _product_ids(ranker.calls[0])
    assert "prod-budget-exact-800" in ranked_ids
    assert "prod-unknown-fee" not in ranked_ids
    assert "prod-out-of-stock" not in ranked_ids
    counts = {item.reason: item for item in response.filter_summary.reason_counts}
    assert counts["offer.cost-unknown"].offer_count > 0
    assert counts["offer.out-of-stock"].offer_count > 0


@pytest.mark.spec("AC-006")
def test_ac_006_highly_relevant_accessories_never_reach_ranker_spy(
    m0_batch: CatalogBatch,
) -> None:
    service, _, _, _, ranker = _service(m0_batch)

    response = asyncio.run(service.search(SearchRequest(query="推荐笔记本")))

    assert len(ranker.calls) == 1
    ranked_ids = set(_product_ids(ranker.calls[0]))
    assert "prod-cross-market-travelbook" in ranked_ids
    assert {
        "prod-accessory-stand",
        "prod-replacement-battery",
        "prod-decoration-sticker",
    }.isdisjoint(ranked_ids)
    counts = {item.reason: item for item in response.filter_summary.reason_counts}
    assert counts["product.not-primary-product"].product_count >= 3


@pytest.mark.parametrize("raises", [False, True])
@pytest.mark.spec("AC-002", "AC-006")
def test_ranker_mutation_discards_the_isolated_batch_and_degrades(
    m0_batch: CatalogBatch,
    raises: bool,
) -> None:
    service, _, _, _, _ = _service(m0_batch)
    service.query_ranker = _MutatingRanker(raises=raises)

    response = asyncio.run(service.search(SearchRequest(query="推荐笔记本")))

    assert response.status is RunStatus.COMPLETED
    assert response.results
    assert response.diagnostics.ranker_degraded is True
    assert response.diagnostics.issues[-1].code == "ranking.degraded"
