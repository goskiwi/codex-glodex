from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from glodex.cli import exit_code_for
from glodex.contracts import RequestFieldError, RequestRejected, RunStatus, SearchResponse

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.spec("GLO-P0-001", "GLO-P0-010", "GLO-P0-012"),
]

PROJECT_ROOT = Path(__file__).parents[2]


def run_module(cwd: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "glodex", *arguments],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )


def parse_single_json_line(output: str) -> dict[str, Any]:
    lines = output.splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert isinstance(parsed, dict)
    return parsed


def test_demo_runs_from_an_unrelated_working_directory(tmp_path: Path) -> None:
    completed = run_module(tmp_path, "demo")
    payload = parse_single_json_line(completed.stdout)

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert payload["status"] == "COMPLETED"
    assert payload["snapshot_version"] == "m0-v1"
    assert payload["algorithm_version"] == "phase-d-v1"
    assert payload["interpreted_request"]["required"]
    assert payload["results"]
    stages = {stage["gate"]: stage for stage in payload["filter_summary"]["stages"]}
    assert stages["snapshot"] == {
        "gate": "snapshot",
        "before": 39,
        "after": 36,
    }
    assert stages["aggregation"] == {
        "gate": "aggregation",
        "before": 36,
        "after": 34,
    }
    assert "product.has_eligible_offer" in stages
    assert payload["run_id"].startswith("run-")


def test_search_uses_explicit_config_and_request_overrides_from_other_cwd(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "custom.toml"
    config_path.write_text(
        "\n".join(
            [
                "[app]",
                'data_dir = "snapshots"',
                'default_snapshot = "configured-v1"',
                "",
                "[search]",
                'default_locale = "zh-CN"',
                'default_currency = "USD"',
                "default_top_k = 2",
            ]
        ),
        encoding="utf-8",
    )
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()

    completed = run_module(
        unrelated,
        "search",
        "--config",
        str(config_path),
        "--query",
        "推荐轻薄本",
        "--snapshot",
        "request-v2",
        "--currency",
        "EUR",
        "--top-k",
        "1",
    )
    payload = parse_single_json_line(completed.stdout)

    assert completed.returncode == 1
    assert completed.stderr == ""
    assert payload["status"] == "FAILED"
    assert payload["snapshot_version"] == "request-v2"
    assert payload["diagnostics"]["issues"][0]["code"] == "catalog.manifest-missing"
    assert payload["run_id"].startswith("run-")


@pytest.mark.parametrize(
    "arguments",
    [
        ("search", "--query", ""),
        ("search", "--query", "推荐轻薄本", "--top-k", "4"),
        ("search", "--query", "推荐轻薄本", "--currency", "usd"),
    ],
)
def test_invalid_search_input_is_json_without_a_run_id(
    tmp_path: Path,
    arguments: tuple[str, ...],
) -> None:
    completed = run_module(tmp_path, *arguments)
    payload = parse_single_json_line(completed.stdout)

    assert completed.returncode == 2
    assert completed.stderr
    assert payload["type"] == "request_rejected"
    assert "run_id" not in payload
    assert payload["errors"]


def test_missing_config_is_a_safe_structured_rejection(tmp_path: Path) -> None:
    completed = run_module(
        tmp_path,
        "demo",
        "--config",
        str(tmp_path / "missing.toml"),
    )
    payload = parse_single_json_line(completed.stdout)

    assert completed.returncode == 2
    assert payload["type"] == "request_rejected"
    assert payload["errors"][0]["code"] == "CONFIG_NOT_FOUND"
    assert str(tmp_path) not in completed.stdout
    assert str(tmp_path) not in completed.stderr


def test_validate_snapshot_runs_the_real_phase_b_validator(tmp_path: Path) -> None:
    completed = run_module(tmp_path, "validate-snapshot", "m0-v1")
    payload = parse_single_json_line(completed.stdout)

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert payload["type"] == "snapshot_validation"
    assert payload["valid"] is True
    assert payload["product_count"] == 34
    assert payload["offer_count"] == 37
    assert payload["quarantine_count"] == 7


def test_validate_snapshot_rejects_overlong_version_as_json(tmp_path: Path) -> None:
    completed = run_module(tmp_path, "validate-snapshot", "a" * 65)
    payload = parse_single_json_line(completed.stdout)

    assert completed.returncode == 2
    assert "Traceback" not in completed.stderr
    assert payload["type"] == "request_rejected"
    assert payload["errors"][0]["field"] == "snapshot_version"


def test_console_script_entrypoint_is_installed(tmp_path: Path) -> None:
    executable = Path(sys.executable).with_name("glodex")

    completed = subprocess.run(
        [str(executable), "demo"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert parse_single_json_line(completed.stdout)["status"] == "COMPLETED"


def test_exit_code_mapping_is_exhaustive() -> None:
    rejected = RequestRejected(
        errors=(
            RequestFieldError(
                field="query",
                code="invalid",
                message="invalid query",
            ),
        )
    )
    failed = SearchResponse(
        run_id="run-1",
        status=RunStatus.FAILED,
        snapshot_version="m0-v1",
        config_fingerprint="a" * 64,
        algorithm_version="walking-skeleton-v1",
    )
    no_match = failed.model_copy(update={"status": RunStatus.NO_MATCH})

    assert exit_code_for(rejected) == 2
    assert exit_code_for(failed) == 1
    assert exit_code_for(no_match) == 0
