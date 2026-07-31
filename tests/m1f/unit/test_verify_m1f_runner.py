"""Unit coverage for the fail-fast M1e-first M1f verification runner."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

import scripts.verify_m0 as verify_m0
import scripts.verify_m1f as verify

pytestmark = pytest.mark.unit

PYTEST_PREFIX = (
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
EXPECTED_COMMANDS = (
    ("uv", "run", "--locked", "python", "scripts/verify_m1e.py"),
    (*PYTEST_PREFIX, "tests/m1f/architecture", "tests/m1f/nfr"),
    (*PYTEST_PREFIX, "tests/m1f/unit", "tests/m1f/contract"),
    (*PYTEST_PREFIX, "tests/m1f/acceptance"),
    (
        "uv",
        "run",
        "--locked",
        "python",
        "scripts/check_traceability.py",
        "--profile",
        "m1f",
        "--mode",
        "coverage",
    ),
)


@pytest.mark.spec("GLO-M1F-P0-004", "GLO-M1F-NFR-004")
def test_m1f_profile_has_the_exact_m1e_first_five_step_order() -> None:
    steps = verify.steps_for_m1f()

    assert len(steps) == 5
    assert tuple(step.command for step in steps) == EXPECTED_COMMANDS
    assert steps[0].name == "complete M1e Definition of Done"


@pytest.mark.spec("GLO-M1F-P0-004", "GLO-M1F-NFR-001", "GLO-M1F-NFR-004")
def test_runner_uses_a_fixed_root_sanitized_environment_and_no_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    original_environment = {
        "PATH": "/tools",
        "DEEPSEEK_API_KEY": "deepseek-secret",
        "DASHSCOPE_API_KEY": "dashscope-secret",
        "TAVILY_API_KEY": "tavily-secret",
        "EBAY_APP_ID": "ebay-secret",
        "GLODEX_CONFIG": "/untrusted/config.toml",
        "PYTEST_ADDOPTS": "--ignore=tests/m1f",
    }
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert verify.run_verification(environ=original_environment) == 0
    assert [tuple(command) for command, _kwargs in calls] == list(EXPECTED_COMMANDS)
    expected_environment = verify_m0.sanitized_environment(original_environment)
    for command, kwargs in calls:
        assert isinstance(command, list)
        assert kwargs == {
            "cwd": tmp_path.resolve(),
            "env": expected_environment,
            "check": False,
            "shell": False,
        }


@pytest.mark.spec("GLO-M1F-P0-004", "GLO-M1F-NFR-004")
def test_runner_stops_at_the_first_failing_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 7 if len(calls) == 2 else 0)

    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert verify.run_verification(environ={"PATH": "/tools"}) == 7
    assert [tuple(command) for command in calls] == list(EXPECTED_COMMANDS[:2])
