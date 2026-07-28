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
    pytest.mark.spec("GLO-M1-P0-009", "GLO-M1-NFR-009"),
]

M1A_P0_IDS = tuple(f"GLO-M1-P0-{index:03d}" for index in range(1, 10))
M1A_AC_IDS = tuple(f"M1-AC-{index:03d}" for index in range(1, 11))
M1A_NFR_IDS = tuple(f"GLO-M1-NFR-{index:03d}" for index in range(1, 11))


def _write_m1a_spec(path: Path, *, p0_count: int = 9) -> Path:
    lines = [
        "# Approved M1a specification",
        "",
        "| ID | Requirement | Acceptance |",
        "|---|---|---|",
        *(
            f"| `GLO-M1-P0-{index:03d}` | requirement | accepted |"
            for index in range(1, p0_count + 1)
        ),
        "",
        *(f"### `M1-AC-{index:03d}` scenario" for index in range(1, 11)),
        "",
        "| ID | Category | Requirement |",
        "|---|---|---|",
        *(f"| `GLO-M1-NFR-{index:03d}` | quality | automated |" for index in range(1, 11)),
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _write_complete_m1a_test(path: Path) -> Path:
    all_ids = (*M1A_P0_IDS, *M1A_AC_IDS, *M1A_NFR_IDS)
    marker_arguments = ", ".join(repr(spec_id) for spec_id in all_ids)
    path.write_text(
        "\n".join(
            [
                "import pytest",
                "",
                "@pytest.mark.acceptance",
                f"@pytest.mark.spec({marker_arguments})",
                "def test_complete_m1a():",
                "    pass",
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_real_m1a_spec_has_the_exact_approved_inventory() -> None:
    inventory = load_spec_inventory(PROJECT_ROOT / "specs" / "001-glodex-m1-api" / "spec.md")

    assert inventory.p0_ids == M1A_P0_IDS
    assert inventory.ac_ids == M1A_AC_IDS
    assert inventory.nfr_ids == M1A_NFR_IDS


def test_m1a_coverage_profile_accepts_only_the_exact_approved_inventory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    spec_path = _write_m1a_spec(tmp_path / "spec.md")
    test_path = _write_complete_m1a_test(tmp_path / "test_complete_m1a.py")

    exit_code = main(
        [
            "--profile",
            "m1a",
            "--mode",
            "coverage",
            "--spec",
            str(spec_path),
            "--tests",
            str(test_path),
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["valid"] is True
    assert set(output["matrix"]) == {
        *M1A_P0_IDS,
        *M1A_AC_IDS,
        *M1A_NFR_IDS,
    }


def test_m1a_coverage_profile_rejects_a_shrunken_inventory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    spec_path = _write_m1a_spec(tmp_path / "spec.md", p0_count=8)
    test_path = _write_complete_m1a_test(tmp_path / "test_complete_m1a.py")

    exit_code = main(
        [
            "--profile",
            "m1a",
            "--mode",
            "coverage",
            "--spec",
            str(spec_path),
            "--tests",
            str(test_path),
        ]
    )

    captured = capsys.readouterr()
    output = json.loads(captured.err)
    assert not captured.out
    assert exit_code == 2
    assert output["valid"] is False
    assert "exact approved M1a inventory" in output["error"]
    assert "GLO-M1-P0-009" in output["error"]


def test_default_profiles_collect_disjoint_owned_test_roots(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    collected_paths: list[tuple[Path, ...]] = []

    def fake_collect(test_paths: tuple[Path, ...]) -> tuple[traceability.CollectedTest, ...]:
        paths = tuple(test_paths)
        collected_paths.append(paths)
        is_m1a = paths == (PROJECT_ROOT / "tests" / "m1a",)
        spec_id = "GLO-M1-P0-009" if is_m1a else "GLO-P0-012"
        nodeid = "tests/m1a/test_owned.py::test_owned" if is_m1a else "tests/test_owned.py"
        return (
            traceability.CollectedTest(
                nodeid=nodeid,
                spec_markers=(traceability.CollectedSpecMarker(args=(spec_id,), kwargs=()),),
            ),
        )

    monkeypatch.setattr(traceability, "collect_pytest_references", fake_collect)

    assert main(["--profile", "m0", "--mode", "references"]) == 0
    m0_output = json.loads(capsys.readouterr().out)
    assert main(["--profile", "m1a", "--mode", "references"]) == 0
    m1a_output = json.loads(capsys.readouterr().out)

    m0_paths, m1a_paths = collected_paths
    assert PROJECT_ROOT / "tests" not in m0_paths
    assert PROJECT_ROOT / "tests" / "m1a" not in m0_paths
    assert m1a_paths == (PROJECT_ROOT / "tests" / "m1a",)
    assert not any(nodeid.startswith("tests/m1a/") for nodeid in m0_output["forward"])
    assert all(nodeid.startswith("tests/m1a/") for nodeid in m1a_output["forward"])
