"""Run the offline M2e gate; the DeepSeek + A100 browser smoke remains explicit."""

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

_OFFLINE_ENV_PREFIXES = ("AUTOSSH_", "M2C_", "SSH_", "GLODEX_")


@dataclass(frozen=True, slots=True)
class VerificationStep:
    name: str
    command: tuple[str, ...]


_STEPS = (
    VerificationStep(
        "complete M2d gate",
        ("uv", "run", "--locked", "python", "scripts/verify_m2d.py"),
    ),
    VerificationStep(
        "M2e socket-blocked Python suite",
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
            "tests/m2e",
        ),
    ),
    VerificationStep(
        "M2e format and lint",
        ("uv", "run", "--locked", "ruff", "format", "--check", "src", "tests/m2e", "scripts"),
    ),
    VerificationStep("M2e type check", ("uv", "run", "--locked", "mypy", "src/glodex", "scripts")),
    VerificationStep(
        "M2e traceability coverage",
        (
            "uv",
            "run",
            "--locked",
            "python",
            "scripts/check_traceability.py",
            "--profile",
            "m2e",
            "--mode",
            "coverage",
        ),
    ),
)


def offline_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """Clear Provider, proxy, tunnel, and service inputs from the offline gate."""

    sanitized = sanitized_environment(environ)
    return {
        name: value
        for name, value in sanitized.items()
        if not name.upper().startswith(_OFFLINE_ENV_PREFIXES)
    }


def run_verification(*, environ: Mapping[str, str] | None = None) -> int:
    environment = offline_environment(os.environ if environ is None else environ)
    for position, step in enumerate(_STEPS, start=1):
        print(f"[M2e {position}/{len(_STEPS)}] {step.name}", flush=True)
        completed = subprocess.run(
            step.command,
            cwd=PROJECT_ROOT,
            env=environment,
            check=False,
            shell=False,
        )
        if completed.returncode != 0:
            print(f"M2e verification failed at: {step.name}", file=sys.stderr)
            return completed.returncode if completed.returncode > 0 else 1
    print(
        "M2e offline verification passed. "
        "Run the explicit DeepSeek + A100 browser smoke separately."
    )
    return 0


def main() -> int:
    return run_verification()


if __name__ == "__main__":
    raise SystemExit(main())
