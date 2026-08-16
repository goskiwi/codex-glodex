"""Architecture evidence for RetrievalModel's isolated delivery gate."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

import scripts.check_traceability as traceability
import scripts.verify_m0 as verify_m0
import scripts.verify_retrieval_model as verify

pytestmark = [
    pytest.mark.architecture,
    pytest.mark.spec(
        "GLO-RETRIEVAL_MODEL-P0-007",
        "GLO-RETRIEVAL_MODEL-NFR-001",
        "GLO-RETRIEVAL_MODEL-NFR-004",
        "GLO-RETRIEVAL_MODEL-NFR-005",
    ),
]

PROJECT_ROOT = Path(__file__).parents[3]
_EXPECTED_COMMANDS = (
    ("uv", "run", "--locked", "python", "scripts/verify_durable_runtime.py"),
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
        "tests/retrieval_model",
    ),
    (
        "uv",
        "run",
        "--locked",
        "ruff",
        "format",
        "--check",
        "src",
        "tests/retrieval_model",
        "scripts",
    ),
    ("uv", "run", "--locked", "mypy", "src/glodex", "scripts"),
    (
        "uv",
        "run",
        "--locked",
        "python",
        "scripts/check_traceability.py",
        "--profile",
        "retrieval_model",
        "--mode",
        "coverage",
    ),
)


def test_retrieval_model_traceability_profile_has_the_exact_approved_inventory() -> None:
    spec_path = PROJECT_ROOT / "specs" / "009-glodex-retrieval-model-service" / "spec.md"
    inventory = traceability.load_spec_inventory(spec_path)
    profile = traceability._TRACEABILITY_PROFILES["retrieval_model"]

    assert inventory.p0_ids == tuple(f"GLO-RETRIEVAL_MODEL-P0-{index:03d}" for index in range(1, 8))
    assert inventory.nfr_ids == tuple(
        f"GLO-RETRIEVAL_MODEL-NFR-{index:03d}" for index in range(1, 7)
    )
    assert inventory.ac_ids == tuple(f"RETRIEVAL_MODEL-AC-{index:03d}" for index in range(1, 7))
    assert profile.spec_path == spec_path
    assert profile.test_paths == (
        PROJECT_ROOT / "tests" / "retrieval_model",
        PROJECT_ROOT
        / "opensearch"
        / "current-product"
        / "test_current_product_hybrid_gateway.py",
    )
    assert profile.approved_inventory == inventory


def test_retrieval_model_runner_has_one_durable_parent_and_only_socket_blocked_offline_steps() -> (
    None
):
    assert tuple(step.command for step in verify._STEPS) == _EXPECTED_COMMANDS
    assert sum("scripts/verify_durable_runtime.py" in step.command for step in verify._STEPS) == 1
    arguments = {argument for step in verify._STEPS for argument in step.command}
    for forbidden in ("--live", "--ignore", "--deselect", "-k", "--lf", "--ff"):
        assert forbidden not in arguments


def test_retrieval_model_runner_clears_tunnel_proxy_and_credential_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    original_environment = {
        "PATH": "/tools",
        "LLM_API_KEY": "secret",
        "LLM_BASE_URL": "https://llm.invalid/v1",
        "LLM_MODEL_NAME": "test-model",
        "HTTP_PROXY": "http://proxy.invalid",
        "RETRIEVAL_MODEL_TUNNEL_PORT": "18000",
        "AUTOSSH_GATETIME": "0",
        "SSH_AUTH_SOCK": "/private/agent.sock",
    }
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert verify.run_verification(environ=original_environment) == 0
    expected_environment = verify_m0.sanitized_environment(original_environment)
    expected_environment = {
        name: value
        for name, value in expected_environment.items()
        if not name.upper().startswith(("AUTOSSH_", "RETRIEVAL_MODEL_", "SSH_"))
    }
    assert [tuple(command) for command, _kwargs in calls] == list(_EXPECTED_COMMANDS)
    for _command, kwargs in calls:
        assert kwargs == {
            "cwd": tmp_path,
            "env": expected_environment,
            "check": False,
            "shell": False,
        }
