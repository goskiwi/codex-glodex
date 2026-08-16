"""Run the flat M5b offline gate; live OpenAI-compatible LLM/A100 acceptance remains explicit."""

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
    "tests/m5b",
    "tests/m6",
    "tests/m7",
    "tests/localization",
)

_M5B_QUALITY_PATHS = (
    "src/glodex/memory",
    "src/glodex/retrieval/digital_catalog.py",
    "src/glodex/infrastructure/postgres.py",
    "src/glodex/infrastructure/migrations.py",
    "tests/m5",
    "tests/m5b",
    "tests/retrieval/test_digital_catalog_personalization.py",
    "scripts/verify_m5b.py",
    "scripts/accept_m5b_live.py",
)

_M5B_TYPE_PATHS = (
    "src/glodex/memory",
    "src/glodex/retrieval/digital_catalog.py",
    "src/glodex/infrastructure/postgres.py",
    "src/glodex/infrastructure/migrations.py",
    "scripts/verify_m5b.py",
    "scripts/accept_m5b_live.py",
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
        "M5b changed-surface lint",
        (
            "uv",
            "run",
            "--locked",
            "ruff",
            "check",
            *_M5B_QUALITY_PATHS,
        ),
    ),
    VerificationStep(
        "M5b locked frontend test",
        ("npm", "--prefix", "frontend", "test", "--", "--run"),
    ),
    VerificationStep("M5b frontend build", ("npm", "--prefix", "frontend", "run", "build")),
    VerificationStep(
        "M5b changed-surface format",
        (
            "uv",
            "run",
            "--locked",
            "ruff",
            "format",
            "--check",
            *_M5B_QUALITY_PATHS,
        ),
    ),
    VerificationStep(
        "M5b changed-surface type check",
        ("uv", "run", "--locked", "mypy", *_M5B_TYPE_PATHS),
    ),
    VerificationStep(
        "M5b traceability coverage",
        (
            "uv",
            "run",
            "--locked",
            "python",
            "scripts/check_traceability.py",
            "--profile",
            "m5b",
            "--mode",
            "coverage",
        ),
    ),
)


def run_verification(*, environ: Mapping[str, str] | None = None) -> int:
    environment = offline_environment(os.environ if environ is None else environ)
    for position, step in enumerate(_STEPS, start=1):
        print(f"[M5b {position}/{len(_STEPS)}] {step.name}", flush=True)
        completed = subprocess.run(
            step.command,
            cwd=PROJECT_ROOT,
            env=environment,
            check=False,
            shell=False,
        )
        if completed.returncode != 0:
            print(f"M5b verification failed at: {step.name}", file=sys.stderr)
            return completed.returncode if completed.returncode > 0 else 1
    print("M5b offline verification passed. Run explicit live acceptance separately.")
    return 0


def main() -> int:
    return run_verification()


if __name__ == "__main__":
    raise SystemExit(main())
