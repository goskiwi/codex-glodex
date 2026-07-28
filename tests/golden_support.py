"""Stable semantic projections used by the committed M0 golden files."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from glodex.adapters.local_snapshot import LocalSnapshotCatalog
from glodex.bootstrap import build_service
from glodex.config import GlodexConfig
from glodex.contracts import OfferSummary, SearchRequest, SearchResponse, SearchResult
from glodex.domain.catalog import CatalogBatch, ExchangeRate, Offer
from glodex.domain.eligibility import EligibleProduct
from glodex.domain.intent import PreferredCriterion
from glodex.domain.pricing import (
    PRICING_ALGORITHM_VERSION,
    KnownCost,
    canonical_exact_amount,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_ROOT = PROJECT_ROOT / "data" / "snapshots"
GOLDEN_ROOT = PROJECT_ROOT / "tests" / "golden" / "m0-v1"
DEFAULT_QUERY = "推荐 800 美元以内、有库存、适合出差的轻薄本"

type GoldenProjection = dict[str, object]


@dataclass(frozen=True, slots=True)
class GoldenScenario:
    """One stable public-boundary behavior captured by a golden file."""

    name: str
    query: str
    ranker_degraded: bool = False


GOLDEN_SCENARIOS = (
    GoldenScenario(name="completed", query=DEFAULT_QUERY),
    GoldenScenario(name="no-match", query="预算 1 美元以内的笔记本"),
    GoldenScenario(
        name="ranker-degraded",
        query=DEFAULT_QUERY,
        ranker_degraded=True,
    ),
)


@dataclass(slots=True)
class _RunIds:
    calls: int = 0

    def next_run_id(self) -> str:
        self.calls += 1
        return f"run-golden-{self.calls}"


@dataclass(slots=True)
class _Clock:
    ticks: int = 0

    def now_utc(self) -> datetime:
        return datetime(2026, 7, 28, tzinfo=UTC)

    def monotonic_ns(self) -> int:
        self.ticks += 1
        return self.ticks * 1_000_000


class _ExplodingRanker:
    async def rank(
        self,
        query: str,
        preferred: tuple[PreferredCriterion, ...],
        candidates: tuple[EligibleProduct, ...],
    ) -> tuple[object, ...]:
        del query
        del preferred
        del candidates
        raise RuntimeError("golden scorer outage")


def _config() -> GlodexConfig:
    return GlodexConfig(
        data_dir=SNAPSHOT_ROOT,
        default_snapshot="m0-v1",
        default_locale="zh-CN",
        default_currency="USD",
        default_top_k=3,
        fingerprint="a" * 64,
    )


async def build_golden_projection(scenario: GoldenScenario) -> GoldenProjection:
    """Execute one scenario and remove only explicitly volatile fields."""

    ranker = _ExplodingRanker() if scenario.ranker_degraded else None
    service = build_service(
        _config(),
        run_id_provider=_RunIds(),
        clock=_Clock(),
        query_ranker=ranker,
    )
    response = await service.search(
        SearchRequest(
            query=scenario.query,
            display_currency="USD",
            snapshot_version="m0-v1",
            top_k=3,
        )
    )
    batch = await LocalSnapshotCatalog(SNAPSHOT_ROOT).load(
        "m0-v1",
        display_currency="USD",
        budget_currency="USD",
    )
    if batch.fatal_issues or batch.exchange_rates is None:
        raise ValueError("golden snapshot must load with a complete exchange-rate table")
    return project_response(response, batch, scenario_name=scenario.name)


async def build_all_golden_projections() -> dict[str, GoldenProjection]:
    """Build every approved golden scenario in declared order."""

    projections: dict[str, GoldenProjection] = {}
    for scenario in GOLDEN_SCENARIOS:
        projections[scenario.name] = await build_golden_projection(scenario)
    return projections


def project_response(
    response: SearchResponse,
    batch: CatalogBatch,
    *,
    scenario_name: str,
) -> GoldenProjection:
    """Return the stable business projection of a public response."""

    if response.snapshot_version != batch.snapshot_version:
        raise ValueError("golden response and replay batch must use the same snapshot")
    return {
        "scenario": scenario_name,
        "status": response.status.value,
        "snapshot_version": response.snapshot_version,
        "config_fingerprint": response.config_fingerprint,
        "algorithm_version": response.algorithm_version,
        "interpreted_request": cast(
            dict[str, object],
            response.interpreted_request.model_dump(mode="json"),
        ),
        "results": [_project_result(result, batch) for result in response.results],
        "filter_summary": cast(
            dict[str, object],
            response.filter_summary.model_dump(mode="json"),
        ),
        "warnings": [
            cast(dict[str, object], warning.model_dump(mode="json"))
            for warning in response.warnings
        ],
        "diagnostics": {
            "ranker_degraded": response.diagnostics.ranker_degraded,
            "stages": [stage.stage for stage in response.diagnostics.stages],
            "issues": [
                cast(dict[str, object], issue.model_dump(mode="json"))
                for issue in response.diagnostics.issues
            ],
        },
    }


def _project_result(result: SearchResult, batch: CatalogBatch) -> dict[str, object]:
    public = cast(dict[str, object], result.model_dump(mode="json"))
    public["pricing_replay"] = [
        _project_pricing_input(result, summary, batch) for summary in result.eligible_offers
    ]
    return public


def _project_pricing_input(
    result: SearchResult,
    summary: OfferSummary,
    batch: CatalogBatch,
) -> dict[str, object]:
    raw_offer = _find_offer(batch, summary)
    rates = batch.exchange_rates
    if rates is None:
        raise ValueError("pricing replay requires exchange rates")
    source_rate = _find_rate(rates.rates, raw_offer.cost_components.currency)
    display_rate = _find_rate(rates.rates, summary.landed_cost.currency)

    components: list[dict[str, object]] = []
    component_evidence_ids: set[str] = set()
    for name, component in raw_offer.cost_components.items():
        if type(component) is not KnownCost:
            raise ValueError("public eligible offer cannot have an unknown cost component")
        component_evidence_ids.add(component.evidence_id)
        components.append(
            {
                "name": name,
                "source_amount": format(component.amount, "f"),
                "evidence_id": component.evidence_id,
            }
        )

    result_evidence_ids = {item.evidence_id for item in result.evidence}
    required_evidence_ids = {
        *component_evidence_ids,
        source_rate.evidence_id,
        display_rate.evidence_id,
    }
    if not required_evidence_ids.issubset(result_evidence_ids):
        raise ValueError("public result does not expose the complete pricing evidence closure")

    return {
        "provider_id": raw_offer.provider_id,
        "offer_id": raw_offer.offer_id,
        "market": raw_offer.market,
        "source_currency": raw_offer.cost_components.currency,
        "components": components,
        "source_rate": _project_rate(source_rate),
        "display_rate": _project_rate(display_rate),
        "display_exact": summary.landed_cost.exact,
        "display": summary.landed_cost.display,
        "algorithm_version": PRICING_ALGORITHM_VERSION,
    }


def _find_offer(batch: CatalogBatch, summary: OfferSummary) -> Offer:
    matches = tuple(
        offer
        for offer in batch.offers
        if offer.provider_id == summary.provider_id and offer.offer_id == summary.offer_id
    )
    if len(matches) != 1:
        raise ValueError("public offer identity must resolve exactly once in its snapshot")
    return matches[0]


def _find_rate(rates: tuple[ExchangeRate, ...], currency: str) -> ExchangeRate:
    matches = tuple(rate for rate in rates if rate.currency == currency)
    if len(matches) != 1:
        raise ValueError("pricing currency must resolve exactly once in its snapshot")
    return matches[0]


def _project_rate(rate: ExchangeRate) -> dict[str, object]:
    return {
        "currency": rate.currency,
        "base_per_unit": canonical_exact_amount(rate.base_per_unit),
        "minor_units": rate.minor_units,
        "evidence_id": rate.evidence_id,
    }


__all__ = [
    "GOLDEN_ROOT",
    "GOLDEN_SCENARIOS",
    "GoldenProjection",
    "GoldenScenario",
    "build_all_golden_projections",
    "build_golden_projection",
    "project_response",
]
