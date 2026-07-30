"""Focused metric and denominator contracts for M1e evaluation."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from glodex.esci_benchmark import RankedCandidate, evaluate_rankings, load_candidate_pool
from scripts.build_esci_benchmark import BENCHMARK_ID, build_benchmark
from tests.m1e.conftest import SyntheticEsciSource


def _example(query_id: str, product_id: str, label: str) -> dict[str, object]:
    return {
        "example_id": f"example-{query_id}-{product_id}",
        "query": query_id,
        "query_id": query_id,
        "product_id": product_id,
        "product_locale": "us",
        "esci_label": label,
        "small_version": 1,
        "large_version": 1,
        "split": "test",
    }


def _product(product_id: str) -> dict[str, object]:
    return {
        "product_id": product_id,
        "product_title": product_id,
        "product_description": "",
        "product_bullet_point": "",
        "product_brand": "",
        "product_color": "",
        "product_locale": "us",
    }


@pytest.mark.unit
@pytest.mark.spec("GLO-M1E-P0-003", "M1E-AC-003", "GLO-M1E-NFR-004")
def test_metrics_keep_exact_outside_top_ten_in_mrr_denominator(
    tmp_path: Path,
    valid_esci_source: Callable[..., SyntheticEsciSource],
) -> None:
    q1_product_ids = [f"p-{index:02d}" for index in range(1, 12)]
    examples = [
        _example("query-1", product_id, "Exact" if product_id == "p-11" else "Irrelevant")
        for product_id in q1_product_ids
    ] + [_example("query-2", "p-12", "Irrelevant")]
    source = valid_esci_source(
        examples=examples,
        products=[_product(product_id) for product_id in (*q1_product_ids, "p-12")],
        source_rows=[("query-1", "synthetic"), ("query-2", "synthetic")],
    )
    artifact_root = tmp_path / "metrics" / BENCHMARK_ID
    build_benchmark(source.root, source.revision, artifact_root)
    pool = load_candidate_pool(artifact_root)
    rankings = {
        "query-1": tuple(
            RankedCandidate(product_id=product_id, score=float(12 - index))
            for index, product_id in enumerate(q1_product_ids, start=1)
        ),
        "query-2": (RankedCandidate(product_id="p-12", score=0.0),),
    }

    result = evaluate_rankings(pool, rankings)

    assert result.metrics.exact_at_10.value == 0.0
    assert result.metrics.exact_at_10.denominator == 2
    assert result.metrics.exact_at_10.excluded == 0
    assert result.metrics.mrr_at_10.value == 0.0
    assert result.metrics.mrr_at_10.denominator == 1
    assert result.metrics.mrr_at_10.excluded == 1
    assert result.metrics.ndcg_at_10.value == 0.0
    assert result.metrics.ndcg_at_10.denominator == 1
    assert result.metrics.ndcg_at_10.excluded == 1
