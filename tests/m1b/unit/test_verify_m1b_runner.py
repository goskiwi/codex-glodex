"""Unit coverage for the fail-fast, M1a-first M1b verification runner."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

import scripts.verify_m0 as verify_m0  # noqa: E402
import scripts.verify_m1b as verify  # noqa: E402

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec(
        "GLO-M1B-P0-006",
        "GLO-M1B-NFR-004",
        "GLO-M1B-NFR-006",
    ),
]

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
    (
        "uv",
        "run",
        "--locked",
        "python",
        "scripts/verify_m1a.py",
    ),
    (
        *PYTEST_PREFIX,
        "tests/m1b/architecture",
        "tests/m1b/nfr",
    ),
    (
        *PYTEST_PREFIX,
        "tests/m1b/unit",
        "tests/m1b/contract",
    ),
    (
        *PYTEST_PREFIX,
        "tests/m1b/acceptance",
    ),
    (
        "uv",
        "run",
        "--locked",
        "python",
        "scripts/check_traceability.py",
        "--profile",
        "m1b",
        "--mode",
        "coverage",
    ),
)


def test_m1b_profile_has_the_exact_m1a_first_gate_order() -> None:
    steps = verify.steps_for_m1b()

    assert tuple(step.command for step in steps) == EXPECTED_COMMANDS
    assert steps[0].name == "complete M1a Definition of Done"
    assert all(isinstance(step.command, tuple) for step in steps)
    assert all(command[: len(PYTEST_PREFIX)] == PYTEST_PREFIX for command in EXPECTED_COMMANDS[1:4])


def test_m1b_profile_never_uses_live_or_test_selection_escape_hatches() -> None:
    arguments = {argument for step in verify.steps_for_m1b() for argument in step.command}

    assert "--live" not in arguments
    assert "--ignore" not in arguments
    assert "--deselect" not in arguments
    assert "-k" not in arguments
    assert not any("skip" in argument or "xfail" in argument for argument in arguments)


def test_runner_uses_fixed_root_argument_arrays_sanitized_env_and_no_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(
        command: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    original_environment = {
        "PATH": "/tools",
        "HOME": "/safe-home",
        "HTTP_PROXY": "http://proxy.invalid",
        "EBAY_APP_ID": "app-secret",
        "EBAY_CERT_ID": "cert-secret",
        "GLODEX_CONFIG": "/untrusted/config.toml",
        "PYTEST_ADDOPTS": "--ignore=tests/m1b",
        "PYTHONPATH": "/untrusted/modules",
    }
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify.subprocess, "run", fake_run)

    exit_code = verify.run_verification(environ=original_environment)

    assert exit_code == 0
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


def test_runner_stops_at_first_failure_and_propagates_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(
        command: list[str],
        **_kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return_code = 9 if len(calls) == 2 else 0
        return subprocess.CompletedProcess(command, return_code)

    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify.subprocess, "run", fake_run)

    exit_code = verify.run_verification(environ={"PATH": "/tools"})

    assert exit_code == 9
    assert [tuple(command) for command in calls] == list(EXPECTED_COMMANDS[:2])


def test_main_returns_the_runner_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verify, "run_verification", lambda: 7)

    assert verify.main() == 7
