"""Public artifact-shape contracts independent from the evaluator."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts.build_esci_benchmark import (
    ARTIFACT_FILE_NAMES,
    BENCHMARK_ID,
    SCHEMA_VERSION,
    build_benchmark,
    main,
)
from tests.m1e.conftest import SyntheticEsciSource


@pytest.mark.contract
@pytest.mark.spec("GLO-M1E-P0-001", "M1E-AC-001", "GLO-M1E-NFR-003")
def test_artifact_has_exact_hashed_canonical_public_shape(
    tmp_path: Path,
    valid_esci_source: Callable[..., SyntheticEsciSource],
) -> None:
    source = valid_esci_source()
    output_root = tmp_path / "artifact" / BENCHMARK_ID
    build_benchmark(source.root, source.revision, output_root)

    assert {path.name for path in output_root.iterdir()} == set(ARTIFACT_FILE_NAMES) | {
        "manifest.json"
    }
    manifest_bytes = (output_root / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    assert manifest_bytes == _canonical_json(manifest).encode("utf-8") + b"\n"
    assert manifest["schema_version"] == SCHEMA_VERSION
    assert manifest["benchmark_id"] == BENCHMARK_ID
    assert manifest["license"] == "Apache-2.0"
    assert str(source.root).encode("utf-8") not in manifest_bytes

    assert set(manifest["artifact_files"]) == set(ARTIFACT_FILE_NAMES)
    for name, record in manifest["artifact_files"].items():
        content = (output_root / name).read_bytes()
        assert record == {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}

    for name in ("queries.jsonl", "products.jsonl", "judgements.jsonl"):
        text = (output_root / name).read_text(encoding="utf-8")
        assert text.endswith("\n")
        lines = text[:-1].split("\n")
        assert lines
        assert all(line == _canonical_json(json.loads(line)) for line in lines)


@pytest.mark.contract
@pytest.mark.spec("GLO-M1E-P0-001", "GLO-M1E-NFR-003")
def test_builder_cli_reports_only_aggregate_safe_result(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    valid_esci_source: Callable[..., SyntheticEsciSource],
) -> None:
    source = valid_esci_source()
    output_root = tmp_path / BENCHMARK_ID

    exit_code = main(
        [
            "--source-root",
            str(source.root),
            "--source-revision",
            source.revision,
            "--output-root",
            str(output_root),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    assert str(source.root) not in captured.out
    assert "wireless keyboard" not in captured.out
    assert json.loads(captured.out)["counts"] == {
        "judgements": 3,
        "products": 3,
        "queries": 2,
    }


def _canonical_json(record: object) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
