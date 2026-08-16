"""Run the Agent composition gate; live model and browser smokes stay explicit."""

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

_OFFLINE_ENV_PREFIXES = ("AUTOSSH_", "RETRIEVAL_MODEL_", "SSH_", "GLODEX_")


@dataclass(frozen=True, slots=True)
class VerificationStep:
    name: str
    command: tuple[str, ...]


_STEPS = (
    VerificationStep(
        "complete WebConsole gate",
        ("uv", "run", "--locked", "python", "scripts/verify_web_console.py"),
    ),
    VerificationStep(
        "Agent composition socket-blocked Python suite",
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
            "tests/composition",
        ),
    ),
    VerificationStep(
        "Agent composition format",
        (
            "uv",
            "run",
            "--locked",
            "ruff",
            "format",
            "--check",
            "src",
            "tests/composition",
            "scripts",
        ),
    ),
    VerificationStep(
        "Agent composition lint",
        (
            "uv",
            "run",
            "--locked",
            "ruff",
            "check",
            "--ignore",
            "RUF001,RUF100",
            "src/glodex/agent",
            "tests/composition",
            "scripts/verify_agent_composition.py",
        ),
    ),
    VerificationStep(
        "Agent composition type check",
        (
            "uv",
            "run",
            "--locked",
            "mypy",
            "src/glodex",
            "scripts/verify_agent_composition.py",
        ),
    ),
    VerificationStep(
        "Agent composition traceability coverage",
        (
            "uv",
            "run",
            "--locked",
            "python",
            "scripts/check_traceability.py",
            "--profile",
            "agent_composition",
            "--mode",
            "coverage",
        ),
    ),
)


def offline_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """Clear LLM, proxy, tunnel, and service inputs from the offline gate."""

    sanitized = sanitized_environment(environ)
    return {
        name: value
        for name, value in sanitized.items()
        if not name.upper().startswith(_OFFLINE_ENV_PREFIXES)
    }


def run_verification(*, environ: Mapping[str, str] | None = None) -> int:
    environment = offline_environment(os.environ if environ is None else environ)
    for position, step in enumerate(_STEPS, start=1):
        print(f"[Agent composition {position}/{len(_STEPS)}] {step.name}", flush=True)
        completed = subprocess.run(
            step.command,
            cwd=PROJECT_ROOT,
            env=environment,
            check=False,
            shell=False,
        )
        if completed.returncode != 0:
            print(f"Agent composition verification failed at: {step.name}", file=sys.stderr)
            return completed.returncode if completed.returncode > 0 else 1
    print(
        "Agent composition offline verification passed. "
        "Run the explicit OpenAI-compatible LLM + A100 browser smoke separately."
    )
    return 0


def main() -> int:
    return run_verification()


if __name__ == "__main__":
    raise SystemExit(main())
