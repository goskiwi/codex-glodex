"""Fixed 20k/100 reference performance workload for GLO-NFR-005."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter_ns

import pytest

from glodex.adapters.local_snapshot import LocalSnapshotCatalog
from glodex.bootstrap import build_service
from glodex.config import GlodexConfig
from glodex.contracts import RunStatus, SearchRequest
from glodex.domain.catalog import CatalogBatch
from scripts.generate_perf_snapshot import (
    PRODUCT_COUNT,
    SNAPSHOT_VERSION,
    generate_snapshot,
)

pytestmark = [
    pytest.mark.performance,
    pytest.mark.spec("GLO-P0-012", "GLO-NFR-005"),
]

WARM_UP_REQUESTS = 10
MEASURED_REQUESTS = 100
REFERENCE_P95_LIMIT_NS = 2_000_000_000
QUERY = "推荐 800 美元以内、有库存的笔记本电脑"


@dataclass(frozen=True, slots=True)
class _LoadedCatalog:
    batch: CatalogBatch
    snapshot_hash: str

    async def load(
        self,
        snapshot_version: str,
        *,
        display_currency: str | None = None,
        budget_currency: str | None = None,
    ) -> CatalogBatch:
        del display_currency, budget_currency
        if snapshot_version != self.batch.snapshot_version:
            raise ValueError("unexpected performance snapshot version")
        return self.batch


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot_digests(snapshot_dir: Path) -> dict[str, str]:
    return {path.name: _digest(path) for path in sorted(snapshot_dir.iterdir()) if path.is_file()}


def _nearest_rank(samples: list[int], percentile: int) -> int:
    if not samples:
        raise ValueError("samples cannot be empty")
    if not 1 <= percentile <= 100:
        raise ValueError("percentile must be in [1, 100]")
    ordered = sorted(samples)
    rank = math.ceil(percentile * len(ordered) / 100)
    return ordered[rank - 1]


def test_nearest_rank_uses_fixed_one_based_ceiling_oracle() -> None:
    samples = list(range(1, 101))

    assert _nearest_rank(samples, 50) == 50
    assert _nearest_rank(samples, 95) == 95
    assert _nearest_rank(samples, 100) == 100
    assert _nearest_rank([4, 1, 3, 2], 25) == 1
    assert _nearest_rank([4, 1, 3, 2], 50) == 2
    assert _nearest_rank([10], 1) == 10


@pytest.mark.parametrize(
    ("samples", "percentile"),
    [
        ([], 50),
        ([1], 0),
        ([1], 101),
    ],
)
def test_nearest_rank_rejects_empty_samples_and_invalid_percentiles(
    samples: list[int],
    percentile: int,
) -> None:
    with pytest.raises(ValueError):
        _nearest_rank(samples, percentile)


@pytest.fixture(scope="module")
def loaded_catalog(tmp_path_factory: pytest.TempPathFactory) -> _LoadedCatalog:
    snapshot_dir = tmp_path_factory.mktemp("glodex-perf") / SNAPSHOT_VERSION
    first_hash = generate_snapshot(snapshot_dir)
    first_digests = _snapshot_digests(snapshot_dir)
    second_hash = generate_snapshot(snapshot_dir)
    second_digests = _snapshot_digests(snapshot_dir)

    assert first_hash == second_hash
    assert first_digests == second_digests
    assert first_hash == second_digests["manifest.json"]

    batch = asyncio.run(
        LocalSnapshotCatalog(snapshot_dir.parent).load(
            SNAPSHOT_VERSION,
            display_currency="USD",
            budget_currency="USD",
        )
    )
    assert batch.fatal_issues == ()
    assert batch.quarantine_issues == ()
    assert len(batch.products) == PRODUCT_COUNT
    assert len({product.product_id for product in batch.products}) == PRODUCT_COUNT
    assert len(batch.offers) == 3
    return _LoadedCatalog(batch=batch, snapshot_hash=first_hash)


def _config(data_dir: Path) -> GlodexConfig:
    return GlodexConfig(
        data_dir=data_dir,
        default_snapshot=SNAPSHOT_VERSION,
        default_locale="zh-CN",
        default_currency="USD",
        default_top_k=3,
        fingerprint="e" * 64,
    )


def _run_workload(loaded: _LoadedCatalog) -> tuple[list[int], str]:
    service = build_service(
        _config(Path("/unmeasured/preloaded-snapshot")),
        catalog_gateway=loaded,
    )
    request = SearchRequest(
        query=QUERY,
        display_currency="USD",
        top_k=3,
        snapshot_version=SNAPSHOT_VERSION,
    )

    async def run() -> tuple[list[int], str]:
        algorithm_version = ""
        for _ in range(WARM_UP_REQUESTS):
            response = await service.search(request)
            assert response.status is RunStatus.COMPLETED
            assert len(response.results) == 3
            algorithm_version = response.algorithm_version

        samples: list[int] = []
        for _ in range(MEASURED_REQUESTS):
            started_ns = perf_counter_ns()
            response = await service.search(request)
            elapsed_ns = perf_counter_ns() - started_ns
            assert response.status is RunStatus.COMPLETED
            assert len(response.results) == 3
            assert response.algorithm_version == algorithm_version
            samples.append(elapsed_ns)
        return samples, algorithm_version

    return asyncio.run(run())


def test_20k_snapshot_100_request_domain_pipeline_performance(
    loaded_catalog: _LoadedCatalog,
) -> None:
    samples, algorithm_version = _run_workload(loaded_catalog)

    assert len(samples) == MEASURED_REQUESTS
    p50_ns = _nearest_rank(samples, 50)
    p95_ns = _nearest_rank(samples, 95)
    max_ns = max(samples)
    report = {
        "algorithm_version": algorithm_version,
        "max_ms": round(max_ns / 1_000_000, 3),
        "os": f"{platform.system()} {platform.release()}",
        "p50_ms": round(p50_ns / 1_000_000, 3),
        "p95_ms": round(p95_ns / 1_000_000, 3),
        "python": sys.version.split()[0],
        "requests": MEASURED_REQUESTS,
        "snapshot_hash": loaded_catalog.snapshot_hash,
        "snapshot_products": PRODUCT_COUNT,
        "warm_up_requests": WARM_UP_REQUESTS,
    }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))

    if os.environ.get("GLODEX_REFERENCE_CI") == "1":
        assert p95_ns <= REFERENCE_P95_LIMIT_NS, (
            f"reference p95 {p95_ns / 1_000_000:.3f}ms exceeds 2000ms"
        )
