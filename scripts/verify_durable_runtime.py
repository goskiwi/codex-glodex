"""Run the offline Durable gate; Docker and provider work remain explicit smokes."""

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
        "Durable offline tests",
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
            "tests/durable_runtime",
        ),
    ),
    VerificationStep(
        "Durable lint and format",
        (
            "uv",
            "run",
            "--locked",
            "ruff",
            "format",
            "--check",
            "src",
            "tests/durable_runtime",
            "scripts",
        ),
    ),
    VerificationStep(
        "Durable type check",
        ("uv", "run", "--locked", "mypy", "src/glodex", "scripts"),
    ),
    VerificationStep(
        "Durable traceability coverage",
        (
            "uv",
            "run",
            "--locked",
            "python",
            "scripts/check_traceability.py",
            "--profile",
            "durable",
            "--mode",
            "coverage",
        ),
    ),
)


def run_verification(*, environ: Mapping[str, str] | None = None) -> int:
    environment = sanitized_environment(os.environ if environ is None else environ)
    for position, step in enumerate(_STEPS, start=1):
        print(f"[Durable {position}/{len(_STEPS)}] {step.name}", flush=True)
        completed = subprocess.run(
            step.command,
            cwd=PROJECT_ROOT,
            env=environment,
            check=False,
            shell=False,
        )
        if completed.returncode != 0:
            print(f"Durable verification failed at: {step.name}", file=sys.stderr)
            return completed.returncode if completed.returncode > 0 else 1
    print("Durable offline verification passed. Run the explicit Docker smoke separately.")
    return 0


def main() -> int:
    return run_verification()


if __name__ == "__main__":
    raise SystemExit(main())
