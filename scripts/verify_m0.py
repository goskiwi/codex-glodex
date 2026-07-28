"""Fail-fast quality gates for Glodex phases and the complete M0."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_SENSITIVE_EXACT = frozenset(
    {
        "ALL_PROXY",
        "DATABASE_URL",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "PGDATABASE",
        "PGHOST",
        "PGPASSWORD",
        "PGPORT",
        "PGSERVICE",
        "PGSSLMODE",
        "PGUSER",
        "PYTHONHOME",
        "PYTHONPATH",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)
_SENSITIVE_PREFIXES = (
    "ANTHROPIC_",
    "AWS_",
    "AZURE_",
    "BEDROCK_",
    "COHERE_",
    "DATABASE_",
    "DB_",
    "ELASTIC_",
    "GCP_",
    "GEMINI_",
    "GLODEX_",
    "GOOGLE_",
    "GROQ_",
    "HF_",
    "HUGGINGFACE_",
    "LANGCHAIN_",
    "LANGSMITH_",
    "MARIADB_",
    "MISTRAL_",
    "MODEL_",
    "MONGO_",
    "MONGODB_",
    "MYSQL_",
    "OLLAMA_",
    "OPENAI_",
    "OPENSEARCH_",
    "PINECONE_",
    "POSTGRES_",
    "PYTEST_",
    "QDRANT_",
    "REDIS_",
    "SQLALCHEMY_",
    "VERTEX_",
    "WANDB_",
    "WEAVIATE_",
)


@dataclass(frozen=True, slots=True)
class VerificationStep:
    """One subprocess command in a phase verification profile."""

    name: str
    command: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.name or not self.command or any(not part for part in self.command):
            raise ValueError("verification step name and command must be non-empty")


class _StrictOutcomePlugin:
    """Turn non-executed pytest outcomes into a failing M0 session."""

    def __init__(self) -> None:
        self.outcomes: set[str] = set()

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.skipped:
            self.outcomes.add("skipped")

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        was_xfail = bool(getattr(report, "wasxfail", False))
        if was_xfail:
            if report.skipped:
                self.outcomes.add("xfailed")
            elif report.passed or report.failed:
                self.outcomes.add("xpassed")
        elif report.skipped:
            self.outcomes.add("skipped")

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(
        self,
        session: pytest.Session,
        exitstatus: int | pytest.ExitCode,
    ) -> None:
        del exitstatus
        if not self.outcomes:
            return

        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        terminal_reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if terminal_reporter is not None:
            joined = ", ".join(sorted(self.outcomes))
            terminal_reporter.write_sep(
                "!",
                f"strict M0 gate rejected pytest outcomes: {joined}",
            )


def pytest_configure(config: pytest.Config) -> None:
    """Install strict outcome handling when this script is loaded as a pytest plugin."""

    plugin_name = "glodex-strict-outcome-plugin"
    if config.pluginmanager.get_plugin(plugin_name) is None:
        config.pluginmanager.register(_StrictOutcomePlugin(), plugin_name)


_PHASE_A_STEPS = (
    VerificationStep(
        name="locked dependency graph",
        command=("uv", "lock", "--check"),
    ),
    VerificationStep(
        name="format",
        command=("uv", "run", "--locked", "ruff", "format", "--check", "."),
    ),
    VerificationStep(
        name="lint",
        command=("uv", "run", "--locked", "ruff", "check", "."),
    ),
    VerificationStep(
        name="types",
        command=("uv", "run", "--locked", "mypy"),
    ),
    VerificationStep(
        name="offline and architecture boundaries",
        command=(
            "uv",
            "run",
            "--locked",
            "pytest",
            "-q",
            "tests/architecture",
        ),
    ),
    VerificationStep(
        name="specification references",
        command=(
            "uv",
            "run",
            "--locked",
            "python",
            "scripts/check_traceability.py",
            "--profile",
            "m0",
            "--mode",
            "references",
        ),
    ),
    VerificationStep(
        name="implemented test suite",
        command=(
            "uv",
            "run",
            "--locked",
            "pytest",
            "-q",
            "tests",
            "--ignore=tests/m1a",
        ),
    ),
)

_PHASE_B_STEPS = (
    *_PHASE_A_STEPS,
    VerificationStep(
        name="Phase B intent domain, adapter, and contract",
        command=(
            "uv",
            "run",
            "--locked",
            "pytest",
            "-q",
            "tests/unit/domain/test_intent.py",
            "tests/unit/adapters/test_rule_intent.py",
            "tests/contract/test_intent_interpreter_contract.py",
        ),
    ),
    VerificationStep(
        name="Phase B snapshot manifest and quarantine contracts",
        command=(
            "uv",
            "run",
            "--locked",
            "pytest",
            "-q",
            "tests/contract/test_local_snapshot_manifest.py",
            "tests/contract/test_local_snapshot_quarantine.py",
        ),
    ),
    VerificationStep(
        name="Phase B canonical aggregation",
        command=(
            "uv",
            "run",
            "--locked",
            "pytest",
            "-q",
            "tests/unit/domain/test_catalog_aggregation.py",
        ),
    ),
    VerificationStep(
        name="Phase B m0-v1 fixture",
        command=(
            "uv",
            "run",
            "--locked",
            "pytest",
            "-q",
            "tests/contract/test_m0_snapshot_fixture.py",
        ),
    ),
    VerificationStep(
        name="Phase B application contract",
        command=(
            "uv",
            "run",
            "--locked",
            "pytest",
            "-q",
            "tests/contract/test_search_service_phase_b.py",
        ),
    ),
    VerificationStep(
        name="Phase B AC-004, AC-009, and AC-010",
        command=(
            "uv",
            "run",
            "--locked",
            "pytest",
            "-q",
            "tests/acceptance/test_ac_004_009_010.py",
        ),
    ),
)

_PHASE_C_STEPS = (
    *_PHASE_B_STEPS,
    VerificationStep(
        name="Phase C money, pricing, and hard-gate domain",
        command=(
            "uv",
            "run",
            "--locked",
            "pytest",
            "-q",
            "tests/unit/domain/test_money_types.py",
            "tests/unit/domain/test_pricing.py",
            "tests/unit/domain/test_eligibility_order.py",
            "tests/unit/domain/test_product_gates.py",
            "tests/unit/domain/test_offer_gates.py",
            "tests/unit/domain/test_eligibility_pipeline.py",
            "tests/unit/domain/test_filter_summary.py",
        ),
    ),
    VerificationStep(
        name="Phase C AC-002, AC-003, AC-005, and AC-006",
        command=(
            "uv",
            "run",
            "--locked",
            "pytest",
            "-q",
            "tests/acceptance/test_ac_002_003_005_006.py",
        ),
    ),
)

_PHASE_D_STEPS = (
    *_PHASE_C_STEPS,
    VerificationStep(
        name="Phase D ranking domain, adapter, and contract",
        command=(
            "uv",
            "run",
            "--locked",
            "pytest",
            "-q",
            "tests/unit/domain/test_ranking_lexical_v1.py",
            "tests/unit/domain/test_ranking_batch.py",
            "tests/contract/test_query_ranker_contract.py",
        ),
    ),
    VerificationStep(
        name="Phase D evidence, claims, and reason",
        command=(
            "uv",
            "run",
            "--locked",
            "pytest",
            "-q",
            "tests/unit/domain/test_evidence_models.py",
            "tests/unit/domain/test_verified_claims.py",
            "tests/unit/domain/test_reason_renderer.py",
        ),
    ),
    VerificationStep(
        name="Phase D result assembly and final guard",
        command=(
            "uv",
            "run",
            "--locked",
            "pytest",
            "-q",
            "tests/unit/domain/test_result_assembly.py",
            "tests/unit/domain/test_final_guard.py",
        ),
    ),
    VerificationStep(
        name="Phase D application contract",
        command=(
            "uv",
            "run",
            "--locked",
            "pytest",
            "-q",
            "tests/contract/test_search_service_phase_d.py",
        ),
    ),
    VerificationStep(
        name="Phase D AC-001, AC-007, AC-008, and AC-011",
        command=(
            "uv",
            "run",
            "--locked",
            "pytest",
            "-q",
            "tests/acceptance/test_ac_001_007_008_011.py",
        ),
    ),
)

_M0_STEPS = (
    VerificationStep(
        name="locked dependency graph",
        command=("uv", "lock", "--check"),
    ),
    VerificationStep(
        name="format",
        command=("uv", "run", "--locked", "ruff", "format", "--check", "."),
    ),
    VerificationStep(
        name="lint",
        command=("uv", "run", "--locked", "ruff", "check", "."),
    ),
    VerificationStep(
        name="types",
        command=("uv", "run", "--locked", "mypy"),
    ),
    VerificationStep(
        name="architecture, offline, and security boundaries",
        command=(
            "uv",
            "run",
            "--locked",
            "python",
            "-m",
            "pytest",
            "-q",
            "-p",
            "scripts.verify_m0",
            "tests/nfr/test_offline_and_external_calls.py",
            "tests/nfr/test_security_boundaries.py",
            "tests/architecture",
        ),
    ),
    VerificationStep(
        name="unit, contract, and generated invariants",
        command=(
            "uv",
            "run",
            "--locked",
            "python",
            "-m",
            "pytest",
            "-q",
            "-p",
            "scripts.verify_m0",
            "tests/unit",
            "tests/contract",
            "tests/generated",
        ),
    ),
    VerificationStep(
        name="AC-001 through AC-012",
        command=(
            "uv",
            "run",
            "--locked",
            "python",
            "-m",
            "pytest",
            "-q",
            "-p",
            "scripts.verify_m0",
            "tests/acceptance",
            "-m",
            "acceptance",
        ),
    ),
    VerificationStep(
        name="Golden semantic projection diff",
        command=(
            "uv",
            "run",
            "--locked",
            "python",
            "scripts/update_goldens.py",
            "--check",
        ),
    ),
    VerificationStep(
        name="twenty-process determinism",
        command=(
            "uv",
            "run",
            "--locked",
            "python",
            "-m",
            "pytest",
            "-q",
            "-p",
            "scripts.verify_m0",
            "tests/nfr/test_determinism.py",
            "-m",
            "nfr",
        ),
    ),
    VerificationStep(
        name="complete specification coverage",
        command=(
            "uv",
            "run",
            "--locked",
            "python",
            "scripts/check_traceability.py",
            "--profile",
            "m0",
            "--mode",
            "coverage",
        ),
    ),
    VerificationStep(
        name="20k snapshot and 100-request performance workload",
        command=(
            "uv",
            "run",
            "--locked",
            "python",
            "-m",
            "pytest",
            "-q",
            "-p",
            "scripts.verify_m0",
            "tests/nfr/test_performance.py",
            "-m",
            "performance",
            "-s",
        ),
    ),
)


def steps_for_phase(phase: str) -> tuple[VerificationStep, ...]:
    """Return only gates that have been implemented for the requested phase."""

    profiles = {
        "A": _PHASE_A_STEPS,
        "B": _PHASE_B_STEPS,
        "C": _PHASE_C_STEPS,
        "D": _PHASE_D_STEPS,
    }
    try:
        return profiles[phase]
    except KeyError:
        raise ValueError(f"unsupported verification phase: {phase}") from None


def steps_for_m0() -> tuple[VerificationStep, ...]:
    """Return the complete, ordered M0 Definition of Done gate."""

    return _M0_STEPS


def sanitized_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """Copy an environment without external service, credential, or config inputs."""

    reference_ci = environ.get("GLODEX_REFERENCE_CI")
    sanitized = {
        name: value
        for name, value in environ.items()
        if name not in _SENSITIVE_EXACT and not name.upper().startswith(_SENSITIVE_PREFIXES)
    }
    if reference_ci == "1":
        sanitized["GLODEX_REFERENCE_CI"] = reference_ci
    sanitized["NO_COLOR"] = "1"
    sanitized["PYTHONHASHSEED"] = "0"
    sanitized["UV_NO_PROGRESS"] = "1"
    return sanitized


def run_verification(
    steps: tuple[VerificationStep, ...],
    *,
    phase: str,
    project_root: Path = PROJECT_ROOT,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Run commands sequentially from a fixed root, stopping at the first failure."""

    root = project_root.resolve()
    environment = sanitized_environment(os.environ if environ is None else environ)
    total = len(steps)
    profile_label = "M0" if phase == "M0" else f"Phase {phase}"
    for index, step in enumerate(steps, start=1):
        print(f"[{profile_label} {index}/{total}] {step.name}", flush=True)
        completed = subprocess.run(
            list(step.command),
            cwd=root,
            env=environment,
            check=False,
            shell=False,
        )
        if completed.returncode != 0:
            print(
                f"{profile_label} verification failed at: {step.name}",
                file=sys.stderr,
            )
            return completed.returncode if completed.returncode > 0 else 1

    if phase == "M0":
        print("M0 verification passed (all final gates).")
        return 0

    gate = {
        "A": "walking skeleton",
        "B": "snapshot, intent, and canonical aggregation",
        "C": "money, hard gates, and eligible assembly",
        "D": "ranking, evidence, and final result assembly",
    }.get(phase, "implemented")
    print(f"Phase {phase} verification passed ({gate} gate; full M0 is not complete).")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the complete Glodex M0 gate, or a historical phase gate."
    )
    parser.add_argument(
        "--phase",
        choices=("A", "B", "C", "D"),
        help="run a historical phase gate instead of the complete M0 gate",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.phase is None:
        return run_verification(steps_for_m0(), phase="M0")
    return run_verification(steps_for_phase(args.phase), phase=args.phase)


if __name__ == "__main__":
    raise SystemExit(main())
