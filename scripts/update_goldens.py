"""Check or explicitly update the committed M0 semantic golden files."""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.golden_support import (  # noqa: E402
    GOLDEN_ROOT,
    GoldenProjection,
    build_all_golden_projections,
)

type UpdateMode = Literal["check", "write"]


@dataclass(frozen=True, slots=True)
class GoldenChange:
    """One add, update, or removal and its reviewable unified diff."""

    path: Path
    rendered: str | None
    diff: str


def render_projection(projection: GoldenProjection) -> str:
    """Serialize one projection without losing list order."""

    return (
        json.dumps(
            projection,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def collect_changes(
    rendered: dict[str, str],
    *,
    golden_root: Path = GOLDEN_ROOT,
) -> tuple[GoldenChange, ...]:
    """Compare generated content with the exact committed golden inventory."""

    changes: list[GoldenChange] = []
    expected_paths = {golden_root / f"{name}.json" for name in rendered}
    for name, current in rendered.items():
        path = golden_root / f"{name}.json"
        committed = path.read_text(encoding="utf-8") if path.is_file() else ""
        if committed == current:
            continue
        changes.append(
            GoldenChange(
                path=path,
                rendered=current,
                diff=_unified_diff(path, committed, current),
            )
        )

    if golden_root.is_dir():
        for path in sorted(golden_root.glob("*.json")):
            if path in expected_paths:
                continue
            committed = path.read_text(encoding="utf-8")
            changes.append(
                GoldenChange(
                    path=path,
                    rendered=None,
                    diff=_unified_diff(path, committed, ""),
                )
            )
    return tuple(changes)


def run_update(
    mode: UpdateMode,
    *,
    golden_root: Path = GOLDEN_ROOT,
) -> int:
    """Check goldens or print all diffs before an explicit write."""

    projections = asyncio.run(build_all_golden_projections())
    rendered = {name: render_projection(projection) for name, projection in projections.items()}
    changes = collect_changes(rendered, golden_root=golden_root)
    if not changes:
        print(f"Golden files are current: {len(rendered)} scenario(s).")
        return 0

    for change in changes:
        print(change.diff, end="" if change.diff.endswith("\n") else "\n")

    if mode == "check":
        print(
            f"Golden check failed: {len(changes)} file(s) differ.",
            file=sys.stderr,
        )
        return 1

    golden_root.mkdir(parents=True, exist_ok=True)
    for change in changes:
        if change.rendered is None:
            change.path.unlink(missing_ok=True)
        else:
            change.path.write_text(change.rendered, encoding="utf-8")
    print(f"Golden files updated after diff review: {len(changes)} file(s).")
    return 0


def _unified_diff(path: Path, committed: str, current: str) -> str:
    relative = path.relative_to(PROJECT_ROOT) if path.is_relative_to(PROJECT_ROOT) else path
    before_name = f"a/{relative}" if committed else "/dev/null"
    after_name = f"b/{relative}" if current else "/dev/null"
    return "".join(
        difflib.unified_diff(
            committed.splitlines(keepends=True),
            current.splitlines(keepends=True),
            fromfile=str(before_name),
            tofile=str(after_name),
        )
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--check",
        action="store_true",
        help="compare generated projections without writing",
    )
    action.add_argument(
        "--write",
        action="store_true",
        help="print unified diffs, then explicitly update golden files",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    mode: UpdateMode = "write" if args.write else "check"
    return run_update(mode)


if __name__ == "__main__":
    raise SystemExit(main())
