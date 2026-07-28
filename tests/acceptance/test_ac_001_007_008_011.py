# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from glodex.adapters.local_snapshot import LocalSnapshotCatalog
from glodex.application.ports import QueryRanker
from glodex.bootstrap import build_service
from glodex.config import GlodexConfig
from glodex.contracts import OfferSummary, RunStatus, SearchRequest, SearchResponse
from glodex.domain.catalog import CatalogBatch, Offer, StockStatus
from glodex.domain.eligibility import EligibleProduct
from glodex.domain.intent import PreferredCriterion
from glodex.domain.pricing import KnownCost, pricing_context

pytestmark = pytest.mark.acceptance

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_ROOT = PROJECT_ROOT / "data" / "snapshots"
QUERY = "推荐 800 美元以内、有库存、适合出差的轻薄本"


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
    assert batch.exchange_rates is not None
    return batch


def _config() -> GlodexConfig:
    return GlodexConfig(
        data_dir=SNAPSHOT_ROOT,
        default_snapshot="m0-v1",
        default_locale="zh-CN",
        default_currency="USD",
        default_top_k=3,
        fingerprint="a" * 64,
    )


def _search(*, ranker: QueryRanker | None = None) -> SearchResponse:
    service = build_service(_config(), query_ranker=ranker)
    return asyncio.run(
        service.search(
            SearchRequest(
                query=QUERY,
                display_currency="USD",
                top_k=3,
                snapshot_version="m0-v1",
            )
        )
    )


def _raw_offer(batch: CatalogBatch, summary: OfferSummary) -> Offer:
    matches = tuple(
        offer
        for offer in batch.offers
        if offer.provider_id == summary.provider_id and offer.offer_id == summary.offer_id
    )
    assert len(matches) == 1
    return matches[0]


@pytest.mark.spec("AC-001")
def test_ac_001_returns_three_unique_cross_market_budget_eligible_laptops(
    m0_batch: CatalogBatch,
) -> None:
    response = _search()

    assert response.status is RunStatus.COMPLETED
    assert len(response.results) == 3
    assert len({result.product_id for result in response.results}) == 3

    markets: set[str] = set()
    source_currencies: set[str] = set()
    evidence_index = {item.evidence_id: item for item in m0_batch.evidence}
    for result in response.results:
        assert result.category == "laptop"
        assert result.selected_offer in result.eligible_offers
        assert Decimal(result.selected_offer.landed_cost.exact) <= Decimal("800")
        assert result.evidence

        result_evidence_ids = {item.evidence_id for item in result.evidence}
        assert result_evidence_ids <= evidence_index.keys()
        assert all(
            evidence_index[evidence_id].snapshot_version == response.snapshot_version
            for evidence_id in result_evidence_ids
        )

        product_records = tuple(
            product for product in m0_batch.products if product.product_id == result.product_id
        )
        assert product_records
        product_fact_ids = {
            binding.evidence_id for product in product_records for binding in product.field_evidence
        }
        assert product_fact_ids <= result_evidence_ids

        for offer_summary in result.eligible_offers:
            raw_offer = _raw_offer(m0_batch, offer_summary)
            assert raw_offer.product_id == result.product_id
            assert raw_offer.stock_status is StockStatus.IN_STOCK
            assert raw_offer.cost_components.is_complete
            assert Decimal(offer_summary.landed_cost.exact) <= Decimal("800")
            assert {
                binding.evidence_id for binding in raw_offer.field_evidence
            } <= result_evidence_ids
            markets.add(raw_offer.market)
            source_currencies.add(raw_offer.cost_components.currency)

    assert len(markets) >= 3
    assert {"USD", "EUR", "GBP"} <= source_currencies


@pytest.mark.spec("AC-007")
def test_ac_007_reason_does_not_turn_unverified_travel_claims_into_facts() -> None:
    response = _search()

    assert response.status is RunStatus.COMPLETED
    expected_unknowns = tuple(
        f"未证实偏好：{criterion.source_span.text}"
        for criterion in response.interpreted_request.preferred
    )
    assert expected_unknowns == ("未证实偏好：适合出差", "未证实偏好：轻薄")

    for result in response.results:
        expected_reason = (
            "满足预算：到手价 "
            f"{result.landed_cost.exact} USD ≤ 800 USD；"
            f"有库存：{result.selected_offer.provider_id}/{result.selected_offer.market}"
        )
        assert result.reason == expected_reason
        assert result.unknowns == expected_unknowns
        assert all(
            unsupported not in result.reason
            for unsupported in ("适合出差", "轻薄", "耐用", "性能", "最佳")
        )


@pytest.mark.spec("AC-008")
def test_ac_008_every_public_landed_cost_is_recomputable_from_snapshot_evidence(
    m0_batch: CatalogBatch,
) -> None:
    first = _search()
    second = _search()

    assert first.status is second.status is RunStatus.COMPLETED
    assert first.snapshot_version == second.snapshot_version == "m0-v1"
    assert first.results == second.results

    rate_table = m0_batch.exchange_rates
    assert rate_table is not None
    rates = {rate.currency: rate for rate in rate_table.rates}
    evidence_index = {item.evidence_id: item for item in m0_batch.evidence}
    replayed_source_currencies: set[str] = set()

    for result in first.results:
        result_evidence_ids = {item.evidence_id for item in result.evidence}
        for offer_summary in result.eligible_offers:
            raw_offer = _raw_offer(m0_batch, offer_summary)
            source_rate = rates[raw_offer.cost_components.currency]
            display_rate = rates[offer_summary.landed_cost.currency]
            replayed_source_currencies.add(source_rate.currency)

            component_evidence_ids: set[str] = set()
            with localcontext(pricing_context()):
                replayed_exact = Decimal(0)
                for _, component in raw_offer.cost_components.items():
                    assert type(component) is KnownCost
                    component_evidence_ids.add(component.evidence_id)
                    replayed_exact += (
                        component.amount * source_rate.base_per_unit / display_rate.base_per_unit
                    )
                quantum = Decimal(1).scaleb(-display_rate.minor_units)
                replayed_display = replayed_exact.quantize(quantum)

            assert Decimal(offer_summary.landed_cost.exact) == replayed_exact
            assert Decimal(offer_summary.landed_cost.display) == replayed_display
            assert component_evidence_ids <= result_evidence_ids
            assert {
                source_rate.evidence_id,
                display_rate.evidence_id,
            } <= result_evidence_ids
            for evidence_id in (
                *component_evidence_ids,
                source_rate.evidence_id,
                display_rate.evidence_id,
            ):
                evidence = evidence_index[evidence_id]
                assert evidence.snapshot_version == first.snapshot_version
                assert evidence.source_uri.startswith("fixture://")

        assert result.landed_cost == result.selected_offer.landed_cost

    assert {"USD", "EUR", "GBP"} <= replayed_source_currencies


@dataclass
class _ExplodingRanker:
    calls: list[tuple[EligibleProduct, ...]] = field(default_factory=list)

    async def rank(
        self,
        query: str,
        preferred: tuple[PreferredCriterion, ...],
        candidates: tuple[EligibleProduct, ...],
    ) -> tuple[object, ...]:
        del query
        del preferred
        self.calls.append(candidates)
        raise RuntimeError("acceptance scorer outage")


@pytest.mark.spec("AC-011")
def test_ac_011_ranker_exception_uses_stable_eligible_prefix_without_resurrection() -> None:
    first_ranker = _ExplodingRanker()
    first = _search(ranker=first_ranker)

    assert first.status is RunStatus.COMPLETED
    assert first.diagnostics.ranker_degraded is True
    assert any(issue.code == "ranking.degraded" for issue in first.diagnostics.issues)
    assert len(first_ranker.calls) == 1

    eligible = first_ranker.calls[0]
    stable_eligible_ids = tuple(candidate.product.product_id for candidate in eligible)
    assert stable_eligible_ids == tuple(
        candidate.product.product_id
        for candidate in sorted(
            eligible,
            key=lambda candidate: (
                candidate.product.snapshot_ordinal,
                candidate.product.product_id,
            ),
        )
    )
    result_ids = tuple(result.product_id for result in first.results)
    assert result_ids == stable_eligible_ids[:3]
    assert set(result_ids) <= set(stable_eligible_ids)
    known_ineligible_ids = {
        "prod-budget-over-800-01",
        "prod-out-of-stock",
        "prod-unknown-fee",
        "prod-accessory-stand",
        "prod-replacement-battery",
        "prod-decoration-sticker",
    }
    assert known_ineligible_ids.isdisjoint(stable_eligible_ids)
    assert known_ineligible_ids.isdisjoint(result_ids)

    second_ranker = _ExplodingRanker()
    second = _search(ranker=second_ranker)
    assert tuple(result.product_id for result in second.results) == result_ids
    assert (
        tuple(candidate.product.product_id for candidate in second_ranker.calls[0])
        == stable_eligible_ids
    )
