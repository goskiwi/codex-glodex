"""Public, operator-style acceptance coverage for the M1e ESCI slice."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from glodex import cli
from scripts.build_esci_benchmark import BENCHMARK_ID
from scripts.build_esci_benchmark import main as build_main
from tests.m1e.conftest import SyntheticEsciSource

pytestmark = pytest.mark.acceptance

PROJECT_ROOT = Path(__file__).parents[3]

_DEFAULT_CLI_PROBE = r"""
import contextlib
import io
import json
import socket
import sys

network_calls = []

def blocked_network(*args, **kwargs):
    del args, kwargs
    network_calls.append("network")
    raise AssertionError("default CLI attempted network access")

socket.socket.connect = blocked_network
socket.create_connection = blocked_network

from glodex import cli

output = io.StringIO()
with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
    exit_code = cli.main(("demo",))
lines = output.getvalue().splitlines()
response = json.loads(lines[0]) if len(lines) == 1 else None
print(
    json.dumps(
        {
            "benchmark_module_loaded": "glodex.esci_benchmark" in sys.modules,
            "exit_code": exit_code,
            "network_calls": network_calls,
            "response_is_object": type(response) is dict,
        },
        sort_keys=True,
    )
)
"""


def _build_fixture_artifact(
    *,
    capsys: pytest.CaptureFixture[str],
    output_root: Path,
    valid_esci_source: Callable[..., SyntheticEsciSource],
) -> tuple[SyntheticEsciSource, dict[str, object]]:
    source = valid_esci_source()

    exit_code = build_main(
        (
            "--source-root",
            str(source.root),
            "--source-revision",
            source.revision,
            "--output-root",
            str(output_root),
        )
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert type(payload) is dict
    assert str(source.root) not in captured.out
    assert "wireless keyboard" not in captured.out
    assert output_root.is_dir()
    return source, payload


@pytest.mark.spec(
    "GLO-M1E-P0-001",
    "GLO-M1E-P0-002",
    "M1E-AC-001",
    "GLO-M1E-NFR-002",
    "GLO-M1E-NFR-003",
)
def test_operator_can_build_a_closed_esci_artifact_from_a_local_source(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    valid_esci_source: Callable[..., SyntheticEsciSource],
) -> None:
    output_root = tmp_path / BENCHMARK_ID
    source, payload = _build_fixture_artifact(
        capsys=capsys,
        output_root=output_root,
        valid_esci_source=valid_esci_source,
    )

    assert payload == {
        "artifact_bytes": sum(path.stat().st_size for path in output_root.iterdir()),
        "benchmark_id": BENCHMARK_ID,
        "counts": {"judgements": 3, "products": 3, "queries": 2},
        "status": "built",
    }
    assert (output_root / "LICENSE").read_bytes() == (source.root / "LICENSE").read_bytes()
    assert (output_root / "NOTICE").read_bytes() == (source.root / "NOTICE").read_bytes()
    assert (
        json.loads((output_root / "manifest.json").read_text(encoding="utf-8"))["source"][
            "revision"
        ]
        == source.revision
    )


@pytest.mark.spec(
    "GLO-M1E-P0-002",
    "M1E-AC-002",
    "GLO-M1E-NFR-004",
)
def test_operator_rebuilds_the_same_local_source_to_identical_artifact_bytes(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    valid_esci_source: Callable[..., SyntheticEsciSource],
) -> None:
    source = valid_esci_source()
    first_root = tmp_path / "first" / BENCHMARK_ID
    second_root = tmp_path / "second" / BENCHMARK_ID

    for output_root in (first_root, second_root):
        exit_code = build_main(
            (
                "--source-root",
                str(source.root),
                "--source-revision",
                source.revision,
                "--output-root",
                str(output_root),
            )
        )
        captured = capsys.readouterr()
        assert exit_code == 0
        assert captured.err == ""
        assert len(captured.out.splitlines()) == 1
        assert str(source.root) not in captured.out

    assert _tree_bytes(first_root) == _tree_bytes(second_root)


@pytest.mark.spec(
    "GLO-M1E-P0-003",
    "M1E-AC-003",
    "GLO-M1E-NFR-001",
    "GLO-M1E-NFR-003",
    "GLO-M1E-NFR-004",
)
def test_public_cli_evaluates_only_aggregate_metrics_from_the_local_artifact(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    valid_esci_source: Callable[..., SyntheticEsciSource],
) -> None:
    output_root = tmp_path / BENCHMARK_ID
    _source, _build_payload = _build_fixture_artifact(
        capsys=capsys,
        output_root=output_root,
        valid_esci_source=valid_esci_source,
    )

    exit_code = cli.main(("benchmark-esci", "--artifact-root", str(output_root)))
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["status"] == "COMPLETED"
    assert payload["benchmark_id"] == BENCHMARK_ID
    assert payload["counts"] == {"judgements": 3, "products": 3, "queries": 2}
    assert set(payload["metrics"]) == {"exact_at_10", "mrr_at_10", "ndcg_at_10"}
    assert payload["metrics"]["exact_at_10"] == {
        "denominator": 2,
        "excluded": 0,
        "value": 0.5,
    }
    assert payload["metrics"]["mrr_at_10"] == {
        "denominator": 1,
        "excluded": 1,
        "value": 1.0,
    }
    assert payload["metrics"]["ndcg_at_10"] == {
        "denominator": 2,
        "excluded": 0,
        "value": 1.0,
    }
    assert "wireless keyboard" not in captured.out
    assert "Keyboard One" not in captured.out
    assert str(output_root) not in captured.out


@pytest.mark.spec(
    "GLO-M1E-P0-004",
    "M1E-AC-004",
    "GLO-M1E-NFR-001",
    "GLO-M1E-NFR-004",
)
def test_explicit_benchmark_cli_is_isolated_from_the_default_offline_cli(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    valid_esci_source: Callable[..., SyntheticEsciSource],
) -> None:
    output_root = tmp_path / BENCHMARK_ID
    _source, _build_payload = _build_fixture_artifact(
        capsys=capsys,
        output_root=output_root,
        valid_esci_source=valid_esci_source,
    )

    benchmark_exit_code = cli.main(("benchmark-esci", "--artifact-root", str(output_root)))
    benchmark_output = capsys.readouterr()

    assert benchmark_exit_code == 0
    assert benchmark_output.err == ""
    assert len(benchmark_output.out.splitlines()) == 1
    assert json.loads(benchmark_output.out)["status"] == "COMPLETED"
    assert "wireless keyboard" not in benchmark_output.out
    assert str(output_root) not in benchmark_output.out

    completed = subprocess.run(
        [sys.executable, "-c", _DEFAULT_CLI_PROBE],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert json.loads(completed.stdout) == {
        "benchmark_module_loaded": False,
        "exit_code": 0,
        "network_calls": [],
        "response_is_object": True,
    }


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.iterdir(), key=lambda item: item.name)
    }
