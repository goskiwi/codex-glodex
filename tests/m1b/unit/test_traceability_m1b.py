from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

import scripts.check_traceability as traceability  # noqa: E402
from scripts.check_traceability import load_spec_inventory, main  # noqa: E402

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec("GLO-M1B-P0-006", "GLO-M1B-NFR-006"),
]

M1B_P0_IDS = tuple(f"GLO-M1B-P0-{index:03d}" for index in range(1, 7))
M1B_AC_IDS = tuple(f"M1B-AC-{index:03d}" for index in range(1, 7))
M1B_NFR_IDS = tuple(f"GLO-M1B-NFR-{index:03d}" for index in range(1, 7))


def _write_spec(path: Path, *, p0_count: int = 6) -> Path:
    path.write_text(
        "\n".join(
            [
                "# Approved M1b specification",
                "",
                "| ID | Requirement | Acceptance |",
                "|---|---|---|",
                *(
                    f"| `GLO-M1B-P0-{index:03d}` | requirement | accepted |"
                    for index in range(1, p0_count + 1)
                ),
                "",
                *(f"### `M1B-AC-{index:03d}` scenario" for index in range(1, 7)),
                "",
                "| ID | Category | Requirement |",
                "|---|---|---|",
                *(f"| `GLO-M1B-NFR-{index:03d}` | quality | automated |" for index in range(1, 7)),
            ]
        ),
        encoding="utf-8",
    )
    return path


def _write_complete_test(path: Path) -> Path:
    ids = (*M1B_P0_IDS, *M1B_AC_IDS, *M1B_NFR_IDS)
    path.write_text(
        "import pytest\n\n"
        "@pytest.mark.acceptance\n"
        f"@pytest.mark.spec({', '.join(repr(spec_id) for spec_id in ids)})\n"
        "def test_complete_m1b():\n"
        "    pass\n",
        encoding="utf-8",
    )
    return path


def test_real_m1b_spec_has_exact_approved_inventory() -> None:
    inventory = load_spec_inventory(PROJECT_ROOT / "specs" / "002-glodex-m1b-provider" / "spec.md")

    assert inventory.p0_ids == M1B_P0_IDS
    assert inventory.ac_ids == M1B_AC_IDS
    assert inventory.nfr_ids == M1B_NFR_IDS


def test_m1b_coverage_accepts_only_exact_inventory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "--profile",
            "m1b",
            "--mode",
            "coverage",
            "--spec",
            str(_write_spec(tmp_path / "spec.md")),
            "--tests",
            str(_write_complete_test(tmp_path / "test_complete.py")),
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["valid"] is True
    assert set(output["matrix"]) == {*M1B_P0_IDS, *M1B_AC_IDS, *M1B_NFR_IDS}


def test_m1b_coverage_rejects_shrunken_inventory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "--profile",
            "m1b",
            "--mode",
            "coverage",
            "--spec",
            str(_write_spec(tmp_path / "spec.md", p0_count=5)),
            "--tests",
            str(_write_complete_test(tmp_path / "test_complete.py")),
        ]
    )

    captured = capsys.readouterr()
    output = json.loads(captured.err)
    assert exit_code == 2
    assert not captured.out
    assert output["valid"] is False
    assert "exact approved M1b inventory" in output["error"]
    assert "GLO-M1B-P0-006" in output["error"]


def test_default_profiles_keep_disjoint_owned_roots() -> None:
    profiles = traceability._TRACEABILITY_PROFILES

    assert profiles["m1b"].test_paths == (PROJECT_ROOT / "tests" / "m1b",)
    assert profiles["m1a"].test_paths == (PROJECT_ROOT / "tests" / "m1a",)
    assert PROJECT_ROOT / "tests" / "m1b" not in profiles["m0"].test_paths
    assert PROJECT_ROOT / "tests" / "m1a" not in profiles["m0"].test_paths
