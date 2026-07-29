"""Run the complete M1a-first M1b Definition of Done gate."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if not __package__:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.verify_m0 import sanitized_environment  # noqa: E402

_PYTEST_PREFIX = (
    "uv",
    "run",
    "--locked",
    "python",
    "-m",
    "pytest",
    "-q",
    "-p",
    "scripts.verify_m0",
)


@dataclass(frozen=True, slots=True)
class VerificationStep:
    """One subprocess command in the final M1b verification profile."""

    name: str
    command: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.name or not self.command or any(not part for part in self.command):
            raise ValueError("verification step name and command must be non-empty")


_M1B_STEPS = (
    VerificationStep(
        name="complete M1a Definition of Done",
        command=(
            "uv",
            "run",
            "--locked",
            "python",
            "scripts/verify_m1a.py",
        ),
    ),
    VerificationStep(
        name="M1b architecture, offline, and security boundaries",
        command=(
            *_PYTEST_PREFIX,
            "tests/m1b/architecture",
            "tests/m1b/nfr",
        ),
    ),
    VerificationStep(
        name="M1b unit and contract suite",
        command=(
            *_PYTEST_PREFIX,
            "tests/m1b/unit",
            "tests/m1b/contract",
        ),
    ),
    VerificationStep(
        name="M1B-AC-001 through M1B-AC-006",
        command=(
            *_PYTEST_PREFIX,
            "tests/m1b/acceptance",
        ),
    ),
    VerificationStep(
        name="complete M1b specification coverage",
        command=(
            "uv",
            "run",
            "--locked",
            "python",
            "scripts/check_traceability.py",
            "--profile",
            "m1b",
            "--mode",
            "coverage",
        ),
    ),
)


def steps_for_m1b() -> tuple[VerificationStep, ...]:
    """Return the complete ordered M1b gate, including M1a as its first step."""

    return _M1B_STEPS


def run_verification(
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Run every gate from the fixed project root and stop at the first failure."""

    root = PROJECT_ROOT.resolve()
    environment = sanitized_environment(os.environ if environ is None else environ)
    steps = steps_for_m1b()
    total = len(steps)
    for index, step in enumerate(steps, start=1):
        print(f"[M1b {index}/{total}] {step.name}", flush=True)
        completed = subprocess.run(
            list(step.command),
            cwd=root,
            env=environment,
            check=False,
            shell=False,
        )
        if completed.returncode != 0:
            print(
                f"M1b verification failed at: {step.name}",
                file=sys.stderr,
            )
            return completed.returncode if completed.returncode > 0 else 1

    print("M1b verification passed (complete M1a and M1b gates).")
    return 0


def main() -> int:
    """Run the final gate and return a process-compatible status code."""

    return run_verification()


if __name__ == "__main__":
    raise SystemExit(main())
