from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from glodex.adapters.local_snapshot import LocalSnapshotCatalog
from glodex.application.search_service import SearchService
from glodex.config import GlodexConfig
from glodex.contracts import RunStatus, SearchRequest
from glodex.domain.catalog import CatalogBatch, aggregate_catalog_batch
from glodex.domain.intent import BudgetMax, InterpretedRequest, SourceSpan
from glodex.domain.issues import IssueCode

pytestmark = pytest.mark.acceptance

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_ROOT = PROJECT_ROOT / "data" / "snapshots"


def _load_m0() -> CatalogBatch:
    return asyncio.run(
        LocalSnapshotCatalog(SNAPSHOT_ROOT).load(
            "m0-v1",
            display_currency="USD",
        )
    )


def _config(data_dir: Path) -> GlodexConfig:
    return GlodexConfig(
        data_dir=data_dir,
        default_snapshot="m0-v1",
        default_locale="zh-CN",
        default_currency="USD",
        default_top_k=3,
        fingerprint="a" * 64,
    )


@pytest.mark.spec("AC-004")
def test_ac_004_cross_source_product_is_unique_and_every_legal_offer_survives() -> None:
    aggregation = aggregate_catalog_batch(_load_m0())

    products = [
        product
        for product in aggregation.products
        if product.product_id == "prod-cross-market-travelbook"
    ]
    offers = [
        offer for offer in aggregation.offers if offer.product_id == "prod-cross-market-travelbook"
    ]

    assert len(products) == 1
    assert len(products[0].sources) == 3
    assert {source.provider_id for source in products[0].sources} == {
        "shop-us",
        "shop-eu",
        "shop-cn",
    }
    assert len({source.source_uri for source in products[0].sources}) == 3
    assert len(offers) == 4
    assert {(offer.provider_id, offer.market) for offer in offers} == {
        ("shop-us", "US"),
        ("shop-eu", "DE"),
        ("shop-cn", "CN"),
        ("shop-eu", "GB"),
    }
    assert len({offer.source_uri for offer in offers}) == 4
    assert aggregation.offers_conserved


@pytest.mark.spec("AC-010")
def test_ac_010_dirty_records_are_counted_while_legal_records_continue() -> None:
    batch = _load_m0()
    aggregation = aggregate_catalog_batch(batch)

    assert batch.fatal_issues == ()
    assert len(batch.products) == 36
    assert len(batch.offers) == 37
    assert len(batch.quarantine_issues) == 7
    assert {issue.code for issue in batch.quarantine_issues} == {
        IssueCode.MISSING_IDENTITY,
        IssueCode.MISSING_SOURCE,
        IssueCode.ORPHAN_OFFER,
        IssueCode.EVIDENCE_NOT_FOUND,
    }
    assert len(aggregation.products) == 34
    assert len(aggregation.offers) == 37
    assert aggregation.offers_conserved


@dataclass
class _RunIds:
    run_id: str
    calls: int = 0

    def next_run_id(self) -> str:
        self.calls += 1
        return self.run_id


@dataclass
class _Clock:
    ticks: int = 0

    def now_utc(self) -> datetime:
        return datetime(2026, 7, 23, tzinfo=UTC)

    def monotonic_ns(self) -> int:
        self.ticks += 1
        return self.ticks * 1_000_000


@dataclass
class _ForgedIntent:
    calls: int = 0

    async def interpret(self, request: SearchRequest) -> InterpretedRequest:
        self.calls += 1
        return InterpretedRequest(
            required=(
                BudgetMax(
                    amount=Decimal("1"),
                    currency="CNY",
                    source_span=SourceSpan(start=0, end=7, text="预算1000元"),
                ),
            ),
            parser_version="malicious-v1",
        )


@dataclass
class _FixedIntent:
    calls: int = 0

    async def interpret(self, request: SearchRequest) -> InterpretedRequest:
        self.calls += 1
        return InterpretedRequest(parser_version="rules-zh-cn-v1")


@dataclass
class _CatalogSpy:
    calls: int = 0

    async def load(
        self,
        snapshot_version: str,
        *,
        display_currency: str | None = None,
        budget_currency: str | None = None,
    ) -> CatalogBatch:
        self.calls += 1
        raise AssertionError("forged intent must fail before catalog I/O")


@dataclass
class _RankerSpy:
    calls: int = 0

    async def rank(
        self,
        query: str,
        preferred: tuple[object, ...],
        candidates: tuple[object, ...],
    ) -> tuple[object, ...]:
        self.calls += 1
        raise AssertionError("Phase B cannot rank forged intent")


@pytest.mark.spec("AC-009")
def test_ac_009_forged_required_reference_fails_closed_before_catalog() -> None:
    run_ids = _RunIds(run_id="run-ac-009")
    intent = _ForgedIntent()
    catalog = _CatalogSpy()
    ranker = _RankerSpy()
    service = SearchService(
        config=_config(SNAPSHOT_ROOT),
        run_id_provider=run_ids,
        clock=_Clock(),
        intent_interpreter=intent,
        catalog_gateway=catalog,
        query_ranker=ranker,
    )

    response = asyncio.run(service.search(SearchRequest(query="预算1000元的笔记本")))

    assert response.status is RunStatus.FAILED
    assert response.run_id == "run-ac-009"
    assert response.results == ()
    assert response.diagnostics.issues[0].code == ("intent.budget-amount-span-mismatch")
    assert run_ids.calls == 1
    assert intent.calls == 1
    assert catalog.calls == 0
    assert ranker.calls == 0


@pytest.mark.spec("AC-010")
def test_ac_010_missing_core_manifest_fails_the_run_without_partial_results(
    tmp_path: Path,
) -> None:
    snapshot_root = tmp_path / "snapshots"
    (snapshot_root / "m0-v1").mkdir(parents=True)
    run_ids = _RunIds(run_id="run-ac-010")
    intent = _FixedIntent()
    ranker = _RankerSpy()
    service = SearchService(
        config=_config(snapshot_root),
        run_id_provider=run_ids,
        clock=_Clock(),
        intent_interpreter=intent,
        catalog_gateway=LocalSnapshotCatalog(snapshot_root),
        query_ranker=ranker,
    )

    execution = asyncio.run(service.execute(SearchRequest(query="推荐笔记本")))

    response = execution.response
    assert response.status is RunStatus.FAILED
    assert response.run_id == "run-ac-010"
    assert response.snapshot_version == "m0-v1"
    assert response.results == ()
    assert tuple(issue.code for issue in response.diagnostics.issues) == (
        IssueCode.MANIFEST_MISSING.value,
    )
    assert execution.journal.terminal_status is RunStatus.FAILED
    assert run_ids.calls == 1
    assert intent.calls == 1
    assert ranker.calls == 0
