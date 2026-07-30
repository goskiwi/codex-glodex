"""Run the offline M2a Definition of Done gate; real services remain separate smokes."""

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


@dataclass(frozen=True, slots=True)
class VerificationStep:
    name: str
    command: tuple[str, ...]


_STEPS = (
    VerificationStep(
        "complete M1f gate",
        ("uv", "run", "--locked", "python", "scripts/verify_m1f.py"),
    ),
    VerificationStep(
        "M2a offline tests",
        (
            "uv",
            "run",
            "--locked",
            "python",
            "-m",
            "pytest",
            "-q",
            "-p",
            "scripts.verify_m0",
            "tests/m2a",
        ),
    ),
    VerificationStep(
        "M2a format and lint",
        ("uv", "run", "--locked", "ruff", "format", "--check", "src", "tests/m2a", "scripts"),
    ),
    VerificationStep(
        "M2a type check",
        ("uv", "run", "--locked", "mypy", "src/glodex", "scripts"),
    ),
    VerificationStep(
        "M2a traceability coverage",
        (
            "uv",
            "run",
            "--locked",
            "python",
            "scripts/check_traceability.py",
            "--profile",
            "m2a",
            "--mode",
            "coverage",
        ),
    ),
)


def run_verification(*, environ: Mapping[str, str] | None = None) -> int:
    environment = sanitized_environment(os.environ if environ is None else environ)
    for position, step in enumerate(_STEPS, start=1):
        print(f"[M2a {position}/{len(_STEPS)}] {step.name}", flush=True)
        completed = subprocess.run(
            step.command,
            cwd=PROJECT_ROOT,
            env=environment,
            check=False,
            shell=False,
        )
        if completed.returncode != 0:
            print(f"M2a verification failed at: {step.name}", file=sys.stderr)
            return completed.returncode if completed.returncode > 0 else 1
    print("M2a offline verification passed. Run both explicit real smoke scripts separately.")
    return 0


def main() -> int:
    return run_verification()


if __name__ == "__main__":
    raise SystemExit(main())
