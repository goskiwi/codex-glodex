"""Run the complete M1c-first M1d Definition of Done gate."""

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
    """One subprocess command in the final M1d verification profile."""

    name: str
    command: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.name or not self.command or any(not part for part in self.command):
            raise ValueError("verification step name and command must be non-empty")


_M1D_STEPS = (
    VerificationStep(
        name="complete M1c Definition of Done",
        command=(
            "uv",
            "run",
            "--locked",
            "python",
            "scripts/verify_m1c.py",
        ),
    ),
    VerificationStep(
        name="M1d architecture, offline, and security boundaries",
        command=(
            *_PYTEST_PREFIX,
            "tests/m1d/architecture",
            "tests/m1d/nfr",
        ),
    ),
    VerificationStep(
        name="M1d unit and contract suite",
        command=(
            *_PYTEST_PREFIX,
            "tests/m1d/unit",
            "tests/m1d/contract",
        ),
    ),
    VerificationStep(
        name="M1D-AC-001 through M1D-AC-006",
        command=(
            *_PYTEST_PREFIX,
            "tests/m1d/acceptance",
        ),
    ),
    VerificationStep(
        name="complete M1d specification coverage",
        command=(
            "uv",
            "run",
            "--locked",
            "python",
            "scripts/check_traceability.py",
            "--profile",
            "m1d",
            "--mode",
            "coverage",
        ),
    ),
)


def steps_for_m1d() -> tuple[VerificationStep, ...]:
    """Return the complete ordered M1d gate, including M1c as its first step."""

    return _M1D_STEPS


def run_verification(
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Run every gate from the fixed project root and stop at the first failure."""

    root = PROJECT_ROOT.resolve()
    environment = sanitized_environment(os.environ if environ is None else environ)
    steps = steps_for_m1d()
    total = len(steps)
    for index, step in enumerate(steps, start=1):
        print(f"[M1d {index}/{total}] {step.name}", flush=True)
        completed = subprocess.run(
            list(step.command),
            cwd=root,
            env=environment,
            check=False,
            shell=False,
        )
        if completed.returncode != 0:
            print(
                f"M1d verification failed at: {step.name}",
                file=sys.stderr,
            )
            return completed.returncode if completed.returncode > 0 else 1

    print("M1d verification passed (complete M1c and M1d gates).")
    return 0


def main() -> int:
    """Run the final gate and return a process-compatible status code."""

    return run_verification()


if __name__ == "__main__":
    raise SystemExit(main())
