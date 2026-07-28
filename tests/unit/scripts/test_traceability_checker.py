from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.check_traceability import (  # noqa: E402
    CollectedSpecMarker,
    CollectedTest,
    SpecFormatError,
    analyze_coverage,
    analyze_references,
    collect_pytest_references,
    load_spec_inventory,
    main,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec("GLO-P0-012"),
]


def _write_spec(path: Path, *, duplicate_p0: bool = False) -> Path:
    duplicate = "| `GLO-P0-001` | duplicate definition | duplicate |\n" if duplicate_p0 else ""
    path.write_text(
        "\n".join(
            [
                "# Test specification",
                "",
                "### 7.1 P0 requirements",
                "",
                "| ID | Requirement | Acceptance |",
                "|---|---|---|",
                "| `GLO-P0-001` | first requirement | accepted |",
                "| `GLO-P0-002` | second requirement | accepted |",
                duplicate.rstrip(),
                "",
                "### `AC-001` first scenario",
                "",
                "Cross-reference only: `GLO-P0-999`, `GLO-NFR-999`, and `AC-999`.",
                "",
                "## Non-functional requirements",
                "",
                "| ID | Category | Requirement |",
                "|---|---|---|",
                "| `GLO-NFR-001` | correctness | deterministic |",
            ]
        ),
        encoding="utf-8",
    )
    return path


def _write_approved_m0_spec(path: Path) -> Path:
    p0_ids = tuple(f"GLO-P0-{index:03d}" for index in range(1, 13))
    ac_ids = tuple(f"AC-{index:03d}" for index in range(1, 13))
    nfr_ids = tuple(f"GLO-NFR-{index:03d}" for index in range(1, 12))
    lines = [
        "# Approved M0 specification",
        "",
        "| ID | Requirement | Acceptance |",
        "|---|---|---|",
        *(f"| `{spec_id}` | requirement | accepted |" for spec_id in p0_ids),
        "",
        *(f"### `{spec_id}` scenario" for spec_id in ac_ids),
        "",
        "| ID | Category | Requirement |",
        "|---|---|---|",
        *(f"| `{spec_id}` | quality | automated |" for spec_id in nfr_ids),
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def test_load_spec_inventory_reads_only_definition_sites(tmp_path: Path) -> None:
    inventory = load_spec_inventory(_write_spec(tmp_path / "spec.md"))

    assert inventory.p0_ids == ("GLO-P0-001", "GLO-P0-002")
    assert inventory.nfr_ids == ("GLO-NFR-001",)
    assert inventory.ac_ids == ("AC-001",)
    assert "GLO-P0-999" not in inventory.all_ids
    assert "GLO-NFR-999" not in inventory.all_ids
    assert "AC-999" not in inventory.all_ids


def test_load_spec_inventory_rejects_duplicate_definitions(tmp_path: Path) -> None:
    with pytest.raises(
        SpecFormatError,
        match=r"duplicate specification definition.*GLO-P0-001",
    ):
        load_spec_inventory(_write_spec(tmp_path / "spec.md", duplicate_p0=True))


def test_reference_report_contains_forward_and_reverse_relationships(tmp_path: Path) -> None:
    inventory = load_spec_inventory(_write_spec(tmp_path / "spec.md"))
    collected = (
        CollectedTest(
            nodeid="tests/test_search.py::test_search",
            spec_markers=(CollectedSpecMarker(args=("GLO-P0-001", "AC-001"), kwargs=()),),
            is_acceptance=True,
        ),
        CollectedTest(
            nodeid="tests/test_nfr.py::test_deterministic",
            spec_markers=(CollectedSpecMarker(args=("GLO-NFR-001",), kwargs=()),),
        ),
    )

    report = analyze_references(inventory, collected)

    assert report.is_valid
    assert report.forward == {
        "tests/test_nfr.py::test_deterministic": ("GLO-NFR-001",),
        "tests/test_search.py::test_search": ("AC-001", "GLO-P0-001"),
    }
    assert report.reverse["GLO-P0-001"] == ("tests/test_search.py::test_search",)
    assert report.reverse["GLO-P0-002"] == ()
    assert report.reverse["GLO-NFR-001"] == ("tests/test_nfr.py::test_deterministic",)
    assert report.reverse["AC-001"] == ("tests/test_search.py::test_search",)


def test_reference_report_rejects_missing_unknown_duplicate_and_invalid_markers(
    tmp_path: Path,
) -> None:
    inventory = load_spec_inventory(_write_spec(tmp_path / "spec.md"))
    collected = (
        CollectedTest(nodeid="tests/test_missing.py::test_missing", spec_markers=()),
        CollectedTest(
            nodeid="tests/test_unknown.py::test_unknown",
            spec_markers=(
                CollectedSpecMarker(
                    args=(
                        "GLO-P0-001",
                        "GLO-P0-001",
                        "GLO-P0-999",
                        "GLO-P0-01",
                        12,
                    ),
                    kwargs=(("id", "AC-001"),),
                ),
            ),
        ),
        CollectedTest(
            nodeid="tests/test_empty.py::test_empty",
            spec_markers=(CollectedSpecMarker(args=(), kwargs=()),),
        ),
    )

    report = analyze_references(inventory, collected)

    assert not report.is_valid
    issue_codes = {(issue.nodeid, issue.code) for issue in report.issues}
    assert ("tests/test_missing.py::test_missing", "missing-spec-marker") in issue_codes
    assert ("tests/test_unknown.py::test_unknown", "duplicate-spec-id") in issue_codes
    assert ("tests/test_unknown.py::test_unknown", "unknown-spec-id") in issue_codes
    assert ("tests/test_unknown.py::test_unknown", "malformed-spec-id") in issue_codes
    assert ("tests/test_unknown.py::test_unknown", "non-string-spec-id") in issue_codes
    assert ("tests/test_unknown.py::test_unknown", "spec-marker-keyword-arguments") in issue_codes
    assert ("tests/test_empty.py::test_empty", "empty-spec-marker") in issue_codes


def test_collect_pytest_references_reads_effective_markers(
    tmp_path: Path,
) -> None:
    test_file = tmp_path / "test_collected.py"
    test_file.write_text(
        "\n".join(
            [
                "import pytest",
                "",
                "pytestmark = pytest.mark.acceptance",
                "",
                '@pytest.mark.spec("GLO-P0-001", "AC-001")',
                "def test_marked():",
                "    pass",
                "",
                "def test_unmarked():",
                "    pass",
            ]
        ),
        encoding="utf-8",
    )

    collected = collect_pytest_references(
        (test_file,),
        config_path=PROJECT_ROOT / "pyproject.toml",
    )

    by_name = {item.nodeid.rsplit("::", 1)[-1]: item for item in collected}
    assert by_name["test_marked"].spec_markers == (
        CollectedSpecMarker(args=("GLO-P0-001", "AC-001"), kwargs=()),
    )
    assert by_name["test_marked"].is_acceptance
    assert by_name["test_unmarked"].spec_markers == ()
    assert by_name["test_unmarked"].is_acceptance


def test_collect_and_coverage_reject_effective_skip_skipif_and_xfail(
    tmp_path: Path,
) -> None:
    inventory = load_spec_inventory(_write_spec(tmp_path / "spec.md"))
    test_file = tmp_path / "test_inactive.py"
    test_file.write_text(
        "\n".join(
            [
                "import pytest",
                "",
                '@pytest.mark.spec("GLO-P0-001")',
                "def test_active():",
                "    pass",
                "",
                '@pytest.mark.spec("GLO-P0-002")',
                '@pytest.mark.skip(reason="disabled")',
                "def test_skipped():",
                "    pass",
                "",
                "@pytest.mark.acceptance",
                '@pytest.mark.spec("AC-001")',
                '@pytest.mark.xfail(reason="known gap")',
                "def test_xfailed():",
                "    assert False",
                "",
                '@pytest.mark.spec("GLO-NFR-001")',
                '@pytest.mark.skipif(True, reason="disabled")',
                "def test_skipif():",
                "    pass",
            ]
        ),
        encoding="utf-8",
    )

    collected = collect_pytest_references(
        (test_file,),
        config_path=PROJECT_ROOT / "pyproject.toml",
    )
    report = analyze_coverage(inventory, collected)

    by_name = {item.nodeid.rsplit("::", 1)[-1]: item for item in collected}
    assert by_name["test_active"].inactive_markers == ()
    assert by_name["test_skipped"].inactive_markers == ("skip",)
    assert by_name["test_xfailed"].inactive_markers == ("xfail",)
    assert by_name["test_skipif"].inactive_markers == ("skipif",)
    assert {
        (issue.nodeid.rsplit("::", 1)[-1], issue.code) for issue in report.reference_issues
    } == {
        ("test_skipped", "inactive-test-marker"),
        ("test_skipif", "inactive-test-marker"),
        ("test_xfailed", "inactive-test-marker"),
    }
    assert {(issue.spec_id, issue.code) for issue in report.coverage_issues} == {
        ("GLO-P0-002", "uncovered-p0"),
        ("AC-001", "ac-without-black-box-test"),
        ("GLO-NFR-001", "uncovered-nfr"),
    }


def test_coverage_report_requires_every_p0_ac_and_nfr_with_black_box_ac(
    tmp_path: Path,
) -> None:
    inventory = load_spec_inventory(_write_spec(tmp_path / "spec.md"))
    collected = (
        CollectedTest(
            nodeid="tests/unit/test_requirements.py::test_first",
            spec_markers=(CollectedSpecMarker(args=("GLO-P0-001", "AC-001"), kwargs=()),),
        ),
        CollectedTest(
            nodeid="tests/contract/test_requirements.py::test_second",
            spec_markers=(CollectedSpecMarker(args=("GLO-P0-002",), kwargs=()),),
        ),
        CollectedTest(
            nodeid="tests/nfr/test_determinism.py::test_repeatable",
            spec_markers=(CollectedSpecMarker(args=("GLO-NFR-001",), kwargs=()),),
        ),
    )

    report = analyze_coverage(inventory, collected)

    assert not report.is_valid
    assert not report.reference_issues
    assert {(issue.spec_id, issue.code) for issue in report.coverage_issues} == {
        ("AC-001", "ac-without-black-box-test")
    }
    assert report.matrix["GLO-P0-001"].automated_tests == (
        "tests/unit/test_requirements.py::test_first",
    )
    assert report.matrix["AC-001"].black_box_tests == ()
    assert report.matrix["GLO-NFR-001"].coverage_kind == "automated-test"


def test_coverage_report_builds_complete_matrix_for_automated_coverage(
    tmp_path: Path,
) -> None:
    inventory = load_spec_inventory(_write_spec(tmp_path / "spec.md"))
    collected = (
        CollectedTest(
            nodeid="tests/acceptance/test_ac.py::test_ac",
            spec_markers=(
                CollectedSpecMarker(
                    args=("GLO-P0-001", "GLO-P0-002", "AC-001"),
                    kwargs=(),
                ),
            ),
            is_acceptance=True,
        ),
        CollectedTest(
            nodeid="tests/nfr/test_nfr.py::test_nfr",
            spec_markers=(CollectedSpecMarker(args=("GLO-NFR-001",), kwargs=()),),
        ),
    )

    report = analyze_coverage(inventory, collected)

    assert report.is_valid
    assert not report.reference_issues
    assert not report.coverage_issues
    assert tuple(report.matrix) == inventory.all_ids
    assert report.matrix["AC-001"].black_box_tests == ("tests/acceptance/test_ac.py::test_ac",)
    assert report.matrix["AC-001"].coverage_kind == "black-box-test"
    assert report.matrix["GLO-NFR-001"].coverage_kind == "automated-test"


def test_coverage_report_rejects_uncovered_requirements_and_bad_references(
    tmp_path: Path,
) -> None:
    inventory = load_spec_inventory(_write_spec(tmp_path / "spec.md"))
    collected = (
        CollectedTest(
            nodeid="tests/test_partial.py::test_partial",
            spec_markers=(CollectedSpecMarker(args=("GLO-P0-001",), kwargs=()),),
        ),
        CollectedTest(nodeid="tests/test_unmarked.py::test_unmarked", spec_markers=()),
    )

    report = analyze_coverage(inventory, collected)

    assert not report.is_valid
    assert {issue.code for issue in report.reference_issues} == {"missing-spec-marker"}
    assert {(issue.spec_id, issue.code) for issue in report.coverage_issues} == {
        ("GLO-P0-002", "uncovered-p0"),
        ("AC-001", "ac-without-black-box-test"),
        ("GLO-NFR-001", "uncovered-nfr"),
    }


def test_references_mode_returns_nonzero_and_json_for_invalid_references(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    spec_path = _write_spec(tmp_path / "spec.md")
    test_file = tmp_path / "test_invalid_reference.py"
    test_file.write_text(
        "\n".join(
            [
                "import pytest",
                "",
                '@pytest.mark.spec("GLO-P0-999")',
                "def test_unknown():",
                "    pass",
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--mode",
            "references",
            "--spec",
            str(spec_path),
            "--tests",
            str(test_file),
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert output["mode"] == "references"
    assert output["valid"] is False
    assert output["forward"]
    assert output["reverse"]["GLO-P0-002"] == []
    assert any(issue["code"] == "unknown-spec-id" for issue in output["issues"])


def test_coverage_mode_rejects_a_shrunken_spec_inventory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    spec_path = _write_spec(tmp_path / "spec.md")
    test_file = tmp_path / "test_shrunken_coverage.py"
    test_file.write_text(
        "\n".join(
            [
                "import pytest",
                "",
                "@pytest.mark.acceptance",
                '@pytest.mark.spec("GLO-P0-001", "GLO-P0-002", "AC-001")',
                "def test_acceptance():",
                "    pass",
                "",
                "@pytest.mark.nfr",
                '@pytest.mark.spec("GLO-NFR-001")',
                "def test_nfr():",
                "    pass",
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--mode",
            "coverage",
            "--spec",
            str(spec_path),
            "--tests",
            str(test_file),
        ]
    )

    captured = capsys.readouterr()
    output = json.loads(captured.err)
    assert not captured.out
    assert exit_code == 2
    assert output["mode"] == "coverage"
    assert output["valid"] is False
    assert "exact approved M0 inventory" in output["error"]
    assert "GLO-P0-012" in output["error"]
    assert "AC-012" in output["error"]
    assert "GLO-NFR-011" in output["error"]


def test_coverage_mode_accepts_only_the_complete_approved_inventory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    spec_path = _write_approved_m0_spec(tmp_path / "spec.md")
    all_ids = (
        *(f"GLO-P0-{index:03d}" for index in range(1, 13)),
        *(f"AC-{index:03d}" for index in range(1, 13)),
        *(f"GLO-NFR-{index:03d}" for index in range(1, 12)),
    )
    marker_arguments = ", ".join(repr(spec_id) for spec_id in all_ids)
    test_file = tmp_path / "test_approved_coverage.py"
    test_file.write_text(
        "\n".join(
            [
                "import pytest",
                "",
                "@pytest.mark.acceptance",
                f"@pytest.mark.spec({marker_arguments})",
                "def test_complete_m0():",
                "    pass",
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--mode",
            "coverage",
            "--spec",
            str(spec_path),
            "--tests",
            str(test_file),
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["mode"] == "coverage"
    assert output["valid"] is True
    assert set(output["matrix"]) == set(all_ids)
    assert output["reference_issues"] == []
    assert output["coverage_issues"] == []
