"""Run one flat offline delivery gate for the current real Durable-M7 stack."""

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

from scripts.verify_agent_composition import offline_environment  # noqa: E402


@dataclass(frozen=True, slots=True)
class VerificationStep:
    name: str
    command: tuple[str, ...]


_CURRENT_TEST_PATHS = (
    "tests/m1c/contract/test_openai_compatible_http.py",
    "tests/m1d",
    "tests/durable_runtime",
    "tests/retrieval_model",
    "tests/web_console",
    "tests/composition",
    "tests/m5",
    "tests/m6",
    "tests/m7",
)


_STEPS = (
    VerificationStep(
        "current durable-stack Python suite",
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
            *_CURRENT_TEST_PATHS,
        ),
    ),
    VerificationStep(
        "current durable-stack lint",
        (
            "uv",
            "run",
            "--locked",
            "ruff",
            "check",
            "src",
            *_CURRENT_TEST_PATHS,
            "scripts",
        ),
    ),
    VerificationStep(
        "M7 locked frontend test",
        ("npm", "--prefix", "frontend", "test", "--", "--run"),
    ),
    VerificationStep("M7 frontend build", ("npm", "--prefix", "frontend", "run", "build")),
    VerificationStep(
        "current durable-stack format",
        (
            "uv",
            "run",
            "--locked",
            "ruff",
            "format",
            "--check",
            "src",
            *_CURRENT_TEST_PATHS,
            "scripts",
        ),
    ),
    VerificationStep(
        "current durable-stack type check",
        ("uv", "run", "--locked", "mypy", "src/glodex", "scripts"),
    ),
    VerificationStep(
        "M7 traceability coverage",
        (
            "uv",
            "run",
            "--locked",
            "python",
            "scripts/check_traceability.py",
            "--profile",
            "m7",
            "--mode",
            "coverage",
        ),
    ),
)


def run_verification(*, environ: Mapping[str, str] | None = None) -> int:
    environment = offline_environment(os.environ if environ is None else environ)
    for position, step in enumerate(_STEPS, start=1):
        print(f"[M7 {position}/{len(_STEPS)}] {step.name}", flush=True)
        completed = subprocess.run(
            step.command,
            cwd=PROJECT_ROOT,
            env=environment,
            check=False,
            shell=False,
        )
        if completed.returncode != 0:
            print(f"M7 verification failed at: {step.name}", file=sys.stderr)
            return completed.returncode if completed.returncode > 0 else 1
    print(
        "Current-stack offline verification passed. "
        "Real OpenAI-compatible LLM + A100 acceptance is explicit."
    )
    return 0


def main() -> int:
    return run_verification()


if __name__ == "__main__":
    raise SystemExit(main())
