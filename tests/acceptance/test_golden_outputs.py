from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

import scripts.update_goldens as updater
from tests.golden_support import (
    GOLDEN_ROOT,
    GoldenProjection,
    build_all_golden_projections,
)

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.spec(
        "GLO-P0-003",
        "GLO-P0-005",
        "GLO-P0-009",
        "GLO-P0-011",
        "GLO-P0-012",
        "GLO-NFR-002",
        "GLO-NFR-004",
    ),
]


@pytest.fixture(scope="module")
def projections() -> dict[str, GoldenProjection]:
    return asyncio.run(build_all_golden_projections())


def test_committed_golden_files_equal_current_semantic_projections(
    projections: dict[str, GoldenProjection],
) -> None:
    rendered = {
        name: updater.render_projection(projection) for name, projection in projections.items()
    }

    assert updater.collect_changes(rendered, golden_root=GOLDEN_ROOT) == ()


def test_golden_projection_is_deterministic_and_excludes_only_volatile_run_data(
    projections: dict[str, GoldenProjection],
) -> None:
    repeated = asyncio.run(build_all_golden_projections())

    assert repeated == projections
    keys = set(_nested_keys(projections))
    assert {
        "run_id",
        "duration_ms",
        "occurred_at",
        "timestamp",
    }.isdisjoint(keys)


def test_golden_scenarios_capture_pricing_evidence_no_match_and_degradation(
    projections: dict[str, GoldenProjection],
) -> None:
    completed = projections["completed"]
    no_match = projections["no-match"]
    degraded = projections["ranker-degraded"]

    assert completed["status"] == "COMPLETED"
    completed_results = cast(list[dict[str, object]], completed["results"])
    assert completed_results
    replayed_currencies = {
        cast(str, replay["source_currency"])
        for result in completed_results
        for replay in cast(list[dict[str, object]], result["pricing_replay"])
    }
    assert {"USD", "EUR", "GBP"} <= replayed_currencies
    for result in completed_results:
        assert result["evidence"]
        for replay in cast(list[dict[str, object]], result["pricing_replay"]):
            assert len(cast(list[object], replay["components"])) == 4
            assert replay["source_rate"]
            assert replay["display_rate"]
            assert replay["algorithm_version"] == "pricing-v1"

    assert no_match["status"] == "NO_MATCH"
    assert no_match["results"] == []

    assert degraded["status"] == "COMPLETED"
    degraded_diagnostics = cast(dict[str, object], degraded["diagnostics"])
    assert degraded_diagnostics["ranker_degraded"] is True
    degraded_issues = cast(list[dict[str, object]], degraded_diagnostics["issues"])
    assert any(issue["code"] == "ranking.degraded" for issue in degraded_issues)


def test_updater_check_never_writes_and_write_prints_diff_before_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    projection: GoldenProjection = {"scenario": "case", "status": "COMPLETED"}

    async def fake_build() -> dict[str, GoldenProjection]:
        return {"case": projection}

    monkeypatch.setattr(updater, "build_all_golden_projections", fake_build)
    golden_root = tmp_path / "goldens"

    assert updater.run_update("check", golden_root=golden_root) == 1
    assert not golden_root.exists()
    capsys.readouterr()

    assert updater.run_update("write", golden_root=golden_root) == 0
    output = capsys.readouterr().out
    assert output.index("--- /dev/null") < output.index("Golden files updated")
    assert (golden_root / "case.json").is_file()

    assert updater.run_update("check", golden_root=golden_root) == 0
    assert "Golden files are current" in capsys.readouterr().out


def _nested_keys(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for key, nested in value.items():
            if isinstance(key, str):
                yield key
            yield from _nested_keys(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _nested_keys(nested)
