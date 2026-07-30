"""Unit contracts for the operator-only ESCI artifact builder."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as parquet  # type: ignore[import-untyped]
import pytest

import scripts.build_esci_benchmark as builder
from scripts.build_esci_benchmark import (
    ARTIFACT_FILE_NAMES,
    BENCHMARK_ID,
    EXAMPLE_COLUMNS,
    BuildError,
    build_benchmark,
)
from tests.m1e.conftest import SyntheticEsciSource

PROJECT_ROOT = Path(__file__).parents[3]


def _example(example_id: str, query_id: str, product_id: str, label: str) -> dict[str, object]:
    return {
        "example_id": example_id,
        "query": "query text",
        "query_id": query_id,
        "product_id": product_id,
        "product_locale": "us",
        "esci_label": label,
        "small_version": 1,
        "large_version": 1,
        "split": "test",
    }


@pytest.mark.unit
@pytest.mark.spec("GLO-M1E-P0-001", "GLO-M1E-P0-002", "M1E-AC-001")
def test_builds_bounded_canonical_artifact_from_real_format_fixture(
    tmp_path: Path,
    valid_esci_source: Callable[..., SyntheticEsciSource],
) -> None:
    source = valid_esci_source()
    output_root = tmp_path / "artifact" / BENCHMARK_ID

    result = build_benchmark(source.root, source.revision, output_root)

    assert result.benchmark_id == BENCHMARK_ID
    assert (result.queries, result.products, result.judgements) == (2, 3, 3)
    assert {path.name for path in output_root.iterdir()} == set(ARTIFACT_FILE_NAMES) | {
        "manifest.json"
    }
    manifest = json.loads((output_root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["counts"] == {"judgements": 3, "products": 3, "queries": 2}
    assert manifest["label_distribution"] == {
        "Complement": 1,
        "Exact": 1,
        "Irrelevant": 0,
        "Substitute": 1,
    }
    assert manifest["source"]["revision"] == source.revision
    assert str(source.root) not in (output_root / "manifest.json").read_text(encoding="utf-8")

    products = _jsonl(output_root / "products.jsonl")
    assert products[1]["product_id"] == "p-2"
    assert products[1]["product_brand"] == ""
    assert products[1]["product_description"] == ""


@pytest.mark.unit
@pytest.mark.spec("GLO-M1E-P0-002", "M1E-AC-002", "GLO-M1E-NFR-004")
def test_same_input_rebuilds_byte_for_byte(
    tmp_path: Path,
    valid_esci_source: Callable[..., SyntheticEsciSource],
) -> None:
    source = valid_esci_source()
    first = tmp_path / "first" / BENCHMARK_ID
    second = tmp_path / "second" / BENCHMARK_ID

    build_benchmark(source.root, source.revision, first)
    build_benchmark(source.root, source.revision, second)

    assert _tree_bytes(first) == _tree_bytes(second)


@pytest.mark.unit
@pytest.mark.spec("GLO-M1E-P0-001", "GLO-M1E-P0-002")
def test_rejects_reordered_parquet_columns(
    tmp_path: Path,
    valid_esci_source: Callable[..., SyntheticEsciSource],
) -> None:
    source = valid_esci_source(example_columns=tuple(reversed(EXAMPLE_COLUMNS)))

    with pytest.raises(BuildError, match="PARQUET_SCHEMA_INVALID"):
        build_benchmark(source.root, source.revision, tmp_path / BENCHMARK_ID)


@pytest.mark.unit
@pytest.mark.spec("GLO-M1E-P0-001", "M1E-AC-001")
def test_accepts_official_numeric_ids_and_known_trailing_index_column(
    tmp_path: Path,
    valid_esci_source: Callable[..., SyntheticEsciSource],
) -> None:
    source = valid_esci_source(source_rows=[("101", "synthetic")])
    official_record = {
        "example_id": 1,
        "query": "wireless keyboard",
        "query_id": 101,
        "product_id": "p-1",
        "product_locale": "us",
        "esci_label": "E",
        "small_version": 1,
        "large_version": 1,
        "split": "test",
        "__index_level_0__": 0,
    }
    official_schema = pa.schema(
        [
            pa.field("example_id", pa.int64()),
            pa.field("query", pa.string()),
            pa.field("query_id", pa.int64()),
            pa.field("product_id", pa.string()),
            pa.field("product_locale", pa.string()),
            pa.field("esci_label", pa.string()),
            pa.field("small_version", pa.int64()),
            pa.field("large_version", pa.int64()),
            pa.field("split", pa.string()),
            pa.field("__index_level_0__", pa.int64()),
        ]
    )
    parquet.write_table(
        pa.Table.from_pylist([official_record], schema=official_schema),
        source.root / "shopping_queries_dataset" / "shopping_queries_dataset_examples.parquet",
    )

    result = build_benchmark(source.root, source.revision, tmp_path / BENCHMARK_ID)

    assert (result.queries, result.products, result.judgements) == (1, 1, 1)


@pytest.mark.unit
@pytest.mark.spec("GLO-M1E-P0-002", "M1E-AC-002")
@pytest.mark.parametrize(
    ("examples", "expected_code"),
    [
        (
            [_example("e-1", "q-1", "p-1", "NotALabel")],
            "LABEL_UNKNOWN",
        ),
        (
            [
                _example("e-1", "q-1", "p-1", "Exact"),
                _example("e-2", "q-1", "p-1", "Substitute"),
            ],
            "QUERY_PRODUCT_DUPLICATE",
        ),
        (
            [_example("e-1", "q-1", "p-missing", "Exact")],
            "PRODUCT_RELATION_MISSING",
        ),
    ],
)
def test_rejects_bad_selected_identities_and_relations(
    tmp_path: Path,
    valid_esci_source: Callable[..., SyntheticEsciSource],
    examples: list[dict[str, object]],
    expected_code: str,
) -> None:
    source = valid_esci_source(examples=examples, source_rows=[("q-1", "synthetic")])

    with pytest.raises(BuildError, match=expected_code):
        build_benchmark(source.root, source.revision, tmp_path / BENCHMARK_ID)


@pytest.mark.unit
@pytest.mark.spec("GLO-M1E-P0-001", "GLO-M1E-P0-002", "GLO-M1E-NFR-002")
def test_rejects_unsafe_source_and_existing_output(
    tmp_path: Path,
    valid_esci_source: Callable[..., SyntheticEsciSource],
) -> None:
    source = valid_esci_source()
    output_root = tmp_path / BENCHMARK_ID
    output_root.mkdir()

    with pytest.raises(BuildError, match="OUTPUT_ALREADY_EXISTS"):
        build_benchmark(source.root, source.revision, output_root)
    with pytest.raises(BuildError, match="SOURCE_ROOT_INSIDE_REPOSITORY"):
        build_benchmark(PROJECT_ROOT, source.revision, tmp_path / "other" / BENCHMARK_ID)


@pytest.mark.unit
@pytest.mark.spec("GLO-M1E-P0-002", "GLO-M1E-NFR-002")
def test_size_failure_does_not_publish_partial_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    valid_esci_source: Callable[..., SyntheticEsciSource],
) -> None:
    source = valid_esci_source()
    output_root = tmp_path / BENCHMARK_ID
    monkeypatch.setattr(builder, "MAX_ARTIFACT_BYTES", 1)

    with pytest.raises(BuildError, match="ARTIFACT_SIZE_EXCEEDED"):
        build_benchmark(source.root, source.revision, output_root)

    assert not output_root.exists()


def _jsonl(path: Path) -> list[dict[str, object]]:
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    return [json.loads(line) for line in text[:-1].split("\n")]


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.iterdir(), key=lambda item: item.name)
    }
