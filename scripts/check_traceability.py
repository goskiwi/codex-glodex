"""Validate links from collected pytest tests to the approved specification."""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
from collections import Counter
from collections.abc import Sequence
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
M0_SPEC_PATH = PROJECT_ROOT / "specs" / "000-glodex-mvp" / "spec.md"
M1A_SPEC_PATH = PROJECT_ROOT / "specs" / "001-glodex-m1-api" / "spec.md"
M1B_SPEC_PATH = PROJECT_ROOT / "specs" / "002-glodex-m1b-provider" / "spec.md"
DEFAULT_SPEC_PATH = M0_SPEC_PATH
DEFAULT_TESTS_PATH = PROJECT_ROOT / "tests"
DEFAULT_PYTEST_CONFIG = PROJECT_ROOT / "pyproject.toml"

_P0_DEFINITION = re.compile(
    r"^\|\s*`((?:GLO-P0|GLO-M1-P0|GLO-M1B-P0)-\d{3})`\s*\|",
    re.MULTILINE,
)
_NFR_DEFINITION = re.compile(
    r"^\|\s*`((?:GLO-NFR|GLO-M1-NFR|GLO-M1B-NFR)-\d{3})`\s*\|",
    re.MULTILINE,
)
_AC_DEFINITION = re.compile(
    r"^###\s+`((?:AC|M1-AC|M1B-AC)-\d{3})`(?:\s|$)",
    re.MULTILINE,
)
_SPEC_ID_SHAPE = re.compile(
    r"(?:GLO-(?:P0|NFR)|GLO-M1-(?:P0|NFR)|GLO-M1B-(?:P0|NFR)|AC|M1-AC|M1B-AC)"
    r"-\d{3}\Z"
)


class SpecFormatError(ValueError):
    """Raised when specification definitions cannot form a trustworthy inventory."""


class PytestCollectionError(RuntimeError):
    """Raised when pytest cannot collect the requested tests."""


@dataclass(frozen=True)
class SpecInventory:
    """Specification IDs defined at canonical definition sites."""

    p0_ids: tuple[str, ...]
    nfr_ids: tuple[str, ...]
    ac_ids: tuple[str, ...]

    @property
    def all_ids(self) -> tuple[str, ...]:
        return self.p0_ids + self.nfr_ids + self.ac_ids


_APPROVED_M0_INVENTORY = SpecInventory(
    p0_ids=tuple(f"GLO-P0-{index:03d}" for index in range(1, 13)),
    nfr_ids=tuple(f"GLO-NFR-{index:03d}" for index in range(1, 12)),
    ac_ids=tuple(f"AC-{index:03d}" for index in range(1, 13)),
)

_APPROVED_M1A_INVENTORY = SpecInventory(
    p0_ids=tuple(f"GLO-M1-P0-{index:03d}" for index in range(1, 10)),
    nfr_ids=tuple(f"GLO-M1-NFR-{index:03d}" for index in range(1, 11)),
    ac_ids=tuple(f"M1-AC-{index:03d}" for index in range(1, 11)),
)

_APPROVED_M1B_INVENTORY = SpecInventory(
    p0_ids=tuple(f"GLO-M1B-P0-{index:03d}" for index in range(1, 7)),
    nfr_ids=tuple(f"GLO-M1B-NFR-{index:03d}" for index in range(1, 7)),
    ac_ids=tuple(f"M1B-AC-{index:03d}" for index in range(1, 7)),
)


@dataclass(frozen=True, slots=True)
class TraceabilityProfile:
    """One milestone's authoritative spec, owned tests, and exact inventory."""

    label: str
    spec_path: Path
    test_paths: tuple[Path, ...]
    approved_inventory: SpecInventory


_TRACEABILITY_PROFILES = {
    "m0": TraceabilityProfile(
        label="M0",
        spec_path=M0_SPEC_PATH,
        test_paths=tuple(
            DEFAULT_TESTS_PATH / directory
            for directory in (
                "unit",
                "contract",
                "generated",
                "acceptance",
                "nfr",
                "architecture",
            )
        ),
        approved_inventory=_APPROVED_M0_INVENTORY,
    ),
    "m1a": TraceabilityProfile(
        label="M1a",
        spec_path=M1A_SPEC_PATH,
        test_paths=(DEFAULT_TESTS_PATH / "m1a",),
        approved_inventory=_APPROVED_M1A_INVENTORY,
    ),
    "m1b": TraceabilityProfile(
        label="M1b",
        spec_path=M1B_SPEC_PATH,
        test_paths=(DEFAULT_TESTS_PATH / "m1b",),
        approved_inventory=_APPROVED_M1B_INVENTORY,
    ),
}


@dataclass(frozen=True)
class CollectedSpecMarker:
    """Arguments captured from one effective ``pytest.mark.spec`` marker."""

    args: tuple[object, ...]
    kwargs: tuple[tuple[str, object], ...]


@dataclass(frozen=True)
class CollectedTest:
    """A collected pytest item and all effective specification markers."""

    nodeid: str
    spec_markers: tuple[CollectedSpecMarker, ...]
    is_acceptance: bool = False
    inactive_markers: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReferenceIssue:
    """One invalid test-to-specification reference."""

    code: str
    nodeid: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "nodeid": self.nodeid,
            "message": self.message,
        }


@dataclass(frozen=True)
class ReferenceReport:
    """Validated forward and reverse traceability relationships."""

    forward: dict[str, tuple[str, ...]]
    reverse: dict[str, tuple[str, ...]]
    issues: tuple[ReferenceIssue, ...]

    @property
    def is_valid(self) -> bool:
        return not self.issues

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": "references",
            "valid": self.is_valid,
            "forward": self.forward,
            "reverse": self.reverse,
            "issues": [issue.as_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class CoverageIssue:
    """One specification definition without acceptable verification coverage."""

    code: str
    spec_id: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "spec_id": self.spec_id,
            "message": self.message,
        }


@dataclass(frozen=True)
class CoverageEntry:
    """Coverage evidence for one canonical specification definition."""

    category: str
    automated_tests: tuple[str, ...]
    black_box_tests: tuple[str, ...]
    coverage_kind: str
    satisfied: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "automated_tests": self.automated_tests,
            "black_box_tests": self.black_box_tests,
            "coverage_kind": self.coverage_kind,
            "satisfied": self.satisfied,
        }


@dataclass(frozen=True)
class CoverageReport:
    """Full Spec↔Test matrix with category-specific closure rules."""

    forward: dict[str, tuple[str, ...]]
    reverse: dict[str, tuple[str, ...]]
    matrix: dict[str, CoverageEntry]
    reference_issues: tuple[ReferenceIssue, ...]
    coverage_issues: tuple[CoverageIssue, ...]

    @property
    def is_valid(self) -> bool:
        return not self.reference_issues and not self.coverage_issues

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": "coverage",
            "valid": self.is_valid,
            "forward": self.forward,
            "reverse": self.reverse,
            "matrix": {spec_id: entry.as_dict() for spec_id, entry in self.matrix.items()},
            "reference_issues": [issue.as_dict() for issue in self.reference_issues],
            "coverage_issues": [issue.as_dict() for issue in self.coverage_issues],
        }


def _definition_ids(
    text: str,
    *,
    pattern: re.Pattern[str],
    category: str,
) -> tuple[str, ...]:
    matches = pattern.findall(text)
    duplicates = sorted(spec_id for spec_id, count in Counter(matches).items() if count > 1)
    if duplicates:
        joined = ", ".join(duplicates)
        raise SpecFormatError(f"duplicate specification definition in {category}: {joined}")
    if not matches:
        raise SpecFormatError(f"no {category} definitions found")
    return tuple(sorted(matches))


def load_spec_inventory(path: Path) -> SpecInventory:
    """Read real P0, NFR and AC IDs from their canonical Markdown definitions."""

    text = path.read_text(encoding="utf-8")
    return SpecInventory(
        p0_ids=_definition_ids(text, pattern=_P0_DEFINITION, category="P0"),
        nfr_ids=_definition_ids(text, pattern=_NFR_DEFINITION, category="NFR"),
        ac_ids=_definition_ids(text, pattern=_AC_DEFINITION, category="AC"),
    )


def require_approved_inventory(
    inventory: SpecInventory,
    profile: TraceabilityProfile,
) -> None:
    """Reject coverage claims against anything except a profile's approved ID set."""

    expected_inventory = profile.approved_inventory
    if inventory == expected_inventory:
        return

    differences: list[str] = []
    for category, actual, expected in (
        ("P0", inventory.p0_ids, expected_inventory.p0_ids),
        ("AC", inventory.ac_ids, expected_inventory.ac_ids),
        ("NFR", inventory.nfr_ids, expected_inventory.nfr_ids),
    ):
        missing = tuple(sorted(set(expected) - set(actual)))
        unexpected = tuple(sorted(set(actual) - set(expected)))
        if missing:
            differences.append(f"{category} missing: {', '.join(missing)}")
        if unexpected:
            differences.append(f"{category} unexpected: {', '.join(unexpected)}")

    raise SpecFormatError(
        f"coverage mode requires the exact approved {profile.label} inventory; "
        + "; ".join(differences)
    )


def require_approved_m0_inventory(inventory: SpecInventory) -> None:
    """Preserve the original M0 inventory-checking API."""

    require_approved_inventory(inventory, _TRACEABILITY_PROFILES["m0"])


class _SpecCollectionPlugin:
    def __init__(self) -> None:
        self.collected: list[CollectedTest] = []

    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        for item in items:
            markers = tuple(
                CollectedSpecMarker(
                    args=tuple(marker.args),
                    kwargs=tuple(sorted(marker.kwargs.items())),
                )
                for marker in item.iter_markers(name="spec")
            )
            is_acceptance = (
                next(
                    item.iter_markers(name="acceptance"),
                    None,
                )
                is not None
            )
            inactive_markers = tuple(
                marker_name
                for marker_name in ("skip", "skipif", "xfail")
                if next(item.iter_markers(name=marker_name), None) is not None
            )
            self.collected.append(
                CollectedTest(
                    nodeid=item.nodeid,
                    spec_markers=markers,
                    is_acceptance=is_acceptance,
                    inactive_markers=inactive_markers,
                )
            )


def collect_pytest_references(
    test_paths: Sequence[Path],
    *,
    config_path: Path | None = DEFAULT_PYTEST_CONFIG,
) -> tuple[CollectedTest, ...]:
    """Collect effective spec markers through pytest without executing tests."""

    plugin = _SpecCollectionPlugin()
    pytest_args = ["--collect-only", "-q"]
    if config_path is not None:
        pytest_args.extend(("-c", str(config_path)))
    pytest_args.extend(str(path) for path in test_paths)

    captured_stdout = io.StringIO()
    captured_stderr = io.StringIO()
    with redirect_stdout(captured_stdout), redirect_stderr(captured_stderr):
        exit_code = pytest.main(pytest_args, plugins=[plugin])

    accepted_exit_codes = {pytest.ExitCode.OK, pytest.ExitCode.NO_TESTS_COLLECTED}
    if exit_code not in accepted_exit_codes:
        details = "\n".join(
            output.strip()
            for output in (captured_stdout.getvalue(), captured_stderr.getvalue())
            if output.strip()
        )
        suffix = f"\n{details}" if details else ""
        raise PytestCollectionError(
            f"pytest collection failed with exit code {int(exit_code)}{suffix}"
        )

    return tuple(sorted(plugin.collected, key=lambda item: item.nodeid))


def _issue(
    issues: list[ReferenceIssue],
    *,
    code: str,
    nodeid: str,
    message: str,
) -> None:
    issues.append(ReferenceIssue(code=code, nodeid=nodeid, message=message))


def analyze_references(
    inventory: SpecInventory,
    collected_tests: Sequence[CollectedTest],
) -> ReferenceReport:
    """Validate collected markers and build test→spec and spec→test maps."""

    known_ids = frozenset(inventory.all_ids)
    forward: dict[str, tuple[str, ...]] = {}
    reverse_lists: dict[str, list[str]] = {spec_id: [] for spec_id in inventory.all_ids}
    issues: list[ReferenceIssue] = []

    for test in sorted(collected_tests, key=lambda item: item.nodeid):
        is_active = not test.inactive_markers
        if not is_active:
            _issue(
                issues,
                code="inactive-test-marker",
                nodeid=test.nodeid,
                message=(
                    "test marked skip/skipif/xfail cannot satisfy traceability: "
                    + ", ".join(test.inactive_markers)
                ),
            )

        if not test.spec_markers:
            _issue(
                issues,
                code="missing-spec-marker",
                nodeid=test.nodeid,
                message="collected test has no @pytest.mark.spec(...) reference",
            )
            forward[test.nodeid] = ()
            continue

        string_ids: list[str] = []
        for marker in test.spec_markers:
            if marker.kwargs:
                names = ", ".join(name for name, _value in marker.kwargs)
                _issue(
                    issues,
                    code="spec-marker-keyword-arguments",
                    nodeid=test.nodeid,
                    message=f"spec marker accepts positional IDs only; got keywords: {names}",
                )
            if not marker.args:
                _issue(
                    issues,
                    code="empty-spec-marker",
                    nodeid=test.nodeid,
                    message="spec marker must reference at least one specification ID",
                )

            for value in marker.args:
                if not isinstance(value, str):
                    _issue(
                        issues,
                        code="non-string-spec-id",
                        nodeid=test.nodeid,
                        message=f"spec marker ID must be a string; got {value!r}",
                    )
                    continue

                string_ids.append(value)
                if _SPEC_ID_SHAPE.fullmatch(value) is None:
                    _issue(
                        issues,
                        code="malformed-spec-id",
                        nodeid=test.nodeid,
                        message=f"specification ID has an invalid shape: {value}",
                    )
                elif value not in known_ids:
                    _issue(
                        issues,
                        code="unknown-spec-id",
                        nodeid=test.nodeid,
                        message=f"specification ID is not defined by the spec: {value}",
                    )

        duplicates = sorted(spec_id for spec_id, count in Counter(string_ids).items() if count > 1)
        for spec_id in duplicates:
            _issue(
                issues,
                code="duplicate-spec-id",
                nodeid=test.nodeid,
                message=f"specification ID is referenced more than once: {spec_id}",
            )

        unique_ids = tuple(sorted(set(string_ids)))
        forward[test.nodeid] = unique_ids
        for spec_id in unique_ids:
            if is_active and spec_id in reverse_lists:
                reverse_lists[spec_id].append(test.nodeid)

    reverse = {spec_id: tuple(sorted(nodeids)) for spec_id, nodeids in reverse_lists.items()}
    ordered_issues = tuple(
        sorted(issues, key=lambda issue: (issue.nodeid, issue.code, issue.message))
    )
    return ReferenceReport(forward=forward, reverse=reverse, issues=ordered_issues)


def analyze_coverage(
    inventory: SpecInventory,
    collected_tests: Sequence[CollectedTest],
) -> CoverageReport:
    """Apply P0, AC and NFR closure rules to valid automated test references."""

    references = analyze_references(inventory, collected_tests)
    acceptance_nodeids = frozenset(test.nodeid for test in collected_tests if test.is_acceptance)
    categories = {
        **{spec_id: "P0" for spec_id in inventory.p0_ids},
        **{spec_id: "NFR" for spec_id in inventory.nfr_ids},
        **{spec_id: "AC" for spec_id in inventory.ac_ids},
    }
    matrix: dict[str, CoverageEntry] = {}
    issues: list[CoverageIssue] = []

    for spec_id in inventory.all_ids:
        category = categories[spec_id]
        automated_tests = references.reverse[spec_id]
        black_box_tests = tuple(
            nodeid for nodeid in automated_tests if nodeid in acceptance_nodeids
        )

        if category == "AC":
            satisfied = bool(black_box_tests)
            coverage_kind = "black-box-test" if satisfied else "none"
            if not satisfied:
                issues.append(
                    CoverageIssue(
                        code="ac-without-black-box-test",
                        spec_id=spec_id,
                        message=(
                            "acceptance criterion requires at least one automated "
                            "test carrying the effective acceptance marker"
                        ),
                    )
                )
        else:
            satisfied = bool(automated_tests)
            coverage_kind = "automated-test" if satisfied else "none"
            if not satisfied:
                code = "uncovered-p0" if category == "P0" else "uncovered-nfr"
                issues.append(
                    CoverageIssue(
                        code=code,
                        spec_id=spec_id,
                        message=(f"{category} definition requires automated test coverage"),
                    )
                )

        matrix[spec_id] = CoverageEntry(
            category=category,
            automated_tests=automated_tests,
            black_box_tests=black_box_tests,
            coverage_kind=coverage_kind,
            satisfied=satisfied,
        )

    return CoverageReport(
        forward=references.forward,
        reverse=references.reverse,
        matrix=matrix,
        reference_issues=references.issues,
        coverage_issues=tuple(
            sorted(issues, key=lambda issue: (issue.spec_id, issue.code, issue.message))
        ),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate pytest specification references.")
    parser.add_argument(
        "--profile",
        choices=tuple(_TRACEABILITY_PROFILES),
        default="m0",
        help="milestone profile selecting the default spec, tests, and exact inventory",
    )
    parser.add_argument(
        "--mode",
        choices=("references", "coverage"),
        default="references",
        help=(
            "references validates marker syntax and ID existence; coverage also "
            "requires all P0/NFR definitions and black-box ACs to be closed"
        ),
    )
    parser.add_argument(
        "--spec",
        type=Path,
        help="override the profile's authoritative Markdown specification",
    )
    parser.add_argument(
        "--tests",
        type=Path,
        nargs="+",
        help="override the profile's pytest paths to collect",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the traceability checker and return a process exit code."""

    args = _build_parser().parse_args(argv)
    profile = _TRACEABILITY_PROFILES[args.profile]
    spec_path = profile.spec_path if args.spec is None else args.spec
    test_paths = profile.test_paths if args.tests is None else tuple(args.tests)
    try:
        inventory = load_spec_inventory(spec_path)
        if args.mode == "coverage":
            require_approved_inventory(inventory, profile)
        collected = collect_pytest_references(test_paths)
        report: ReferenceReport | CoverageReport
        if args.mode == "coverage":
            report = analyze_coverage(inventory, collected)
        else:
            report = analyze_references(inventory, collected)
    except (OSError, SpecFormatError, PytestCollectionError) as error:
        print(
            json.dumps(
                {
                    "mode": args.mode,
                    "valid": False,
                    "error": str(error),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2

    print(
        json.dumps(
            report.as_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report.is_valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
