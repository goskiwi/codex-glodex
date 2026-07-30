"""Public CLI and reader contracts for the standalone ESCI benchmark."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as parquet  # type: ignore[import-untyped]
import pytest

import glodex.cli as cli
from glodex.esci_benchmark import BenchmarkArtifactError, load_candidate_pool
from scripts.build_esci_benchmark import BENCHMARK_ID, build_benchmark
from tests.m1e.conftest import SyntheticEsciSource


def _build_artifact(
    tmp_path: Path,
    valid_esci_source: Callable[..., SyntheticEsciSource],
) -> Path:
    source = valid_esci_source()
    artifact_root = tmp_path / "artifact" / BENCHMARK_ID
    build_benchmark(source.root, source.revision, artifact_root)
    return artifact_root


def _single_json(capsys: pytest.CaptureFixture[str]) -> tuple[dict[str, object], str]:
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert isinstance(payload, dict)
    return payload, captured.err


@pytest.mark.contract
@pytest.mark.spec("GLO-M1E-P0-002", "M1E-AC-004", "GLO-M1E-NFR-003")
def test_reader_fails_closed_when_a_hashed_jsonl_file_changes(
    tmp_path: Path,
    valid_esci_source: Callable[..., SyntheticEsciSource],
) -> None:
    artifact_root = _build_artifact(tmp_path, valid_esci_source)
    queries_path = artifact_root / "queries.jsonl"
    queries_path.write_text(queries_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(BenchmarkArtifactError, match="BENCHMARK_ARTIFACT_INVALID"):
        load_candidate_pool(artifact_root)


@pytest.mark.contract
@pytest.mark.spec("GLO-M1E-P0-002", "M1E-AC-003")
def test_reader_preserves_unicode_line_separators_inside_product_text(
    tmp_path: Path,
    valid_esci_source: Callable[..., SyntheticEsciSource],
) -> None:
    source = valid_esci_source()
    products_path = (
        source.root / "shopping_queries_dataset" / "shopping_queries_dataset_products.parquet"
    )
    products = parquet.read_table(products_path)
    descriptions = products["product_description"].to_pylist()
    descriptions[0] = "first\u2028second"
    description_index = products.schema.get_field_index("product_description")
    products = products.set_column(
        description_index,
        "product_description",
        pa.array(descriptions, type=pa.string()),
    )
    parquet.write_table(products, products_path)
    artifact_root = tmp_path / "artifact" / BENCHMARK_ID

    build_benchmark(source.root, source.revision, artifact_root)

    pool = load_candidate_pool(artifact_root)
    assert pool.products["p-1"].product_description == "first\u2028second"


@pytest.mark.contract
@pytest.mark.spec("GLO-M1E-P0-003", "M1E-AC-004", "GLO-M1E-NFR-001", "GLO-M1E-NFR-003")
def test_cli_dispatches_before_config_and_emits_only_aggregate_summary(
    tmp_path: Path,
    valid_esci_source: Callable[..., SyntheticEsciSource],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifact_root = _build_artifact(tmp_path, valid_esci_source)

    def forbidden_load_config(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("benchmark command must not load normal configuration")

    monkeypatch.setattr(cli, "load_config", forbidden_load_config)
    exit_code = cli.main(("benchmark-esci", "--artifact-root", str(artifact_root)))
    payload, stderr = _single_json(capsys)

    assert exit_code == 0
    assert stderr == ""
    assert set(payload) == {
        "artifact_manifest_sha256",
        "benchmark_id",
        "counts",
        "label_distribution",
        "metrics",
        "scorer_version",
        "source",
        "status",
    }
    assert payload["status"] == "COMPLETED"
    assert payload["benchmark_id"] == BENCHMARK_ID
    assert str(artifact_root) not in json.dumps(payload)
    assert "wireless keyboard" not in json.dumps(payload)


@pytest.mark.contract
@pytest.mark.spec("M1E-AC-004", "GLO-M1E-NFR-001", "GLO-M1E-NFR-003")
def test_cli_rejects_missing_or_malformed_artifacts_without_path_leakage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing_root = tmp_path / "missing-artifact"

    def forbidden_load_config(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("benchmark command must not load normal configuration")

    monkeypatch.setattr(cli, "load_config", forbidden_load_config)
    exit_code = cli.main(("benchmark-esci", "--artifact-root", str(missing_root)))
    payload, stderr = _single_json(capsys)

    assert exit_code == 1
    assert stderr == ""
    assert payload == {"code": "BENCHMARK_ARTIFACT_INVALID", "status": "FAILED"}
    assert str(missing_root) not in json.dumps(payload)
