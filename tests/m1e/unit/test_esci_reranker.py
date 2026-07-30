"""Focused unit evidence for the label-isolated M1e lexical reranker."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

import glodex.esci_benchmark as esci_benchmark
from glodex.esci_benchmark import load_candidate_pool, rank_candidate_pool
from scripts.build_esci_benchmark import BENCHMARK_ID, build_benchmark
from tests.m1e.conftest import SyntheticEsciSource


def _example(product_id: str, label: str) -> dict[str, object]:
    return {
        "example_id": f"example-{product_id}",
        "query": "alpha",
        "query_id": "query-1",
        "product_id": product_id,
        "product_locale": "us",
        "esci_label": label,
        "small_version": 1,
        "large_version": 1,
        "split": "test",
    }


def _product(product_id: str, *, title: str, description: str = "") -> dict[str, object]:
    return {
        "product_id": product_id,
        "product_title": title,
        "product_description": description,
        "product_bullet_point": "",
        "product_brand": "",
        "product_color": "",
        "product_locale": "us",
    }


def _artifact_root(
    tmp_path: Path,
    source: SyntheticEsciSource,
    name: str,
) -> Path:
    root = tmp_path / name / BENCHMARK_ID
    build_benchmark(source.root, source.revision, root)
    return root


@pytest.mark.unit
@pytest.mark.spec("GLO-M1E-P0-003", "M1E-AC-003", "GLO-M1E-NFR-004")
def test_ranking_is_label_opaque_and_ties_break_by_product_id(
    tmp_path: Path,
    valid_esci_source: Callable[..., SyntheticEsciSource],
) -> None:
    products = [
        _product("p-1", title="Unrelated product"),
        _product("p-2", title="Another unrelated product"),
    ]
    first_source = valid_esci_source(
        examples=[_example("p-1", "Exact"), _example("p-2", "Irrelevant")],
        products=products,
        source_rows=[("query-1", "synthetic")],
    )
    changed_labels_source = valid_esci_source(
        examples=[_example("p-1", "Irrelevant"), _example("p-2", "Exact")],
        products=products,
        source_rows=[("query-1", "synthetic")],
    )

    first_pool = load_candidate_pool(_artifact_root(tmp_path, first_source, "first"))
    changed_pool = load_candidate_pool(_artifact_root(tmp_path, changed_labels_source, "second"))
    first_ranking = rank_candidate_pool(first_pool)["query-1"]
    changed_ranking = rank_candidate_pool(changed_pool)["query-1"]

    assert first_ranking == changed_ranking
    assert [candidate.product_id for candidate in first_ranking] == ["p-1", "p-2"]
    assert not hasattr(first_ranking[0], "label")


@pytest.mark.unit
@pytest.mark.spec("GLO-M1E-P0-003", "M1E-AC-003", "GLO-M1E-NFR-004")
def test_candidate_pool_and_reranker_do_not_load_judgements(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    valid_esci_source: Callable[..., SyntheticEsciSource],
) -> None:
    source = valid_esci_source()
    artifact_root = _artifact_root(tmp_path, source, "candidate-only")

    def unexpected_judgement_read(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("candidate-only ranking must not load ESCI labels")

    monkeypatch.setattr(esci_benchmark, "_load_judgement_records", unexpected_judgement_read)
    pool = load_candidate_pool(artifact_root)

    assert set(rank_candidate_pool(pool)) == {"q-1", "q-2"}


@pytest.mark.unit
@pytest.mark.spec("GLO-M1E-P0-003", "M1E-AC-003", "GLO-M1E-NFR-004")
def test_fixed_field_weights_make_title_match_beat_longer_description_match(
    tmp_path: Path,
    valid_esci_source: Callable[..., SyntheticEsciSource],
) -> None:
    source = valid_esci_source(
        examples=[_example("p-1", "Exact"), _example("p-2", "Substitute")],
        products=[
            _product("p-1", title="alpha"),
            _product("p-2", title="other", description="alpha alpha alpha alpha"),
        ],
        source_rows=[("query-1", "synthetic")],
    )
    pool = load_candidate_pool(_artifact_root(tmp_path, source, "weighted"))

    ranking = rank_candidate_pool(pool)["query-1"]

    assert [candidate.product_id for candidate in ranking] == ["p-1", "p-2"]
    assert ranking[0].score > ranking[1].score
