"""Run the offline M2c gate; GPU, tunnel, and provider work remain explicit."""

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

_OFFLINE_ENV_PREFIXES = ("AUTOSSH_", "M2C_", "SSH_")


@dataclass(frozen=True, slots=True)
class VerificationStep:
    """One deterministic subprocess command in the M2c verification profile."""

    name: str
    command: tuple[str, ...]


_STEPS = (
    VerificationStep(
        "complete M2b gate",
        ("uv", "run", "--locked", "python", "scripts/verify_m2b.py"),
    ),
    VerificationStep(
        "M2c socket-blocked tests",
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
            "tests/m2c",
        ),
    ),
    VerificationStep(
        "M2c format and lint",
        ("uv", "run", "--locked", "ruff", "format", "--check", "src", "tests/m2c", "scripts"),
    ),
    VerificationStep(
        "M2c type check",
        ("uv", "run", "--locked", "mypy", "src/glodex", "scripts"),
    ),
    VerificationStep(
        "M2c traceability coverage",
        (
            "uv",
            "run",
            "--locked",
            "python",
            "scripts/check_traceability.py",
            "--profile",
            "m2c",
            "--mode",
            "coverage",
        ),
    ),
)


def offline_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """Remove credentials, proxies, and any local tunnel process inputs."""

    sanitized = sanitized_environment(environ)
    return {
        name: value
        for name, value in sanitized.items()
        if not name.upper().startswith(_OFFLINE_ENV_PREFIXES)
    }


def run_verification(*, environ: Mapping[str, str] | None = None) -> int:
    """Run the M2b parent and all M2c offline evidence from the fixed project root."""

    environment = offline_environment(os.environ if environ is None else environ)
    for position, step in enumerate(_STEPS, start=1):
        print(f"[M2c {position}/{len(_STEPS)}] {step.name}", flush=True)
        completed = subprocess.run(
            step.command,
            cwd=PROJECT_ROOT,
            env=environment,
            check=False,
            shell=False,
        )
        if completed.returncode != 0:
            print(f"M2c verification failed at: {step.name}", file=sys.stderr)
            return completed.returncode if completed.returncode > 0 else 1
    print("M2c offline verification passed. Run GPU/tunnel smokes separately.")
    return 0


def main() -> int:
    return run_verification()


if __name__ == "__main__":
    raise SystemExit(main())
