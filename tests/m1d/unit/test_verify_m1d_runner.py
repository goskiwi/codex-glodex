"""Unit coverage for the fail-fast, M1c-first M1d verification runner."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

import scripts.verify_m0 as verify_m0
import scripts.verify_m1d as verify

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec("GLO-M1D-P0-001", "GLO-M1D-NFR-001"),
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
        "scripts/verify_m1c.py",
    ),
    (
        *PYTEST_PREFIX,
        "tests/m1d/architecture",
        "tests/m1d/nfr",
    ),
    (
        *PYTEST_PREFIX,
        "tests/m1d/unit",
        "tests/m1d/contract",
    ),
    (
        *PYTEST_PREFIX,
        "tests/m1d/acceptance",
    ),
    (
        "uv",
        "run",
        "--locked",
        "python",
        "scripts/check_traceability.py",
        "--profile",
        "m1d",
        "--mode",
        "coverage",
    ),
)


def test_m1d_profile_has_the_exact_m1c_first_five_step_order() -> None:
    steps = verify.steps_for_m1d()

    assert len(steps) == 5
    assert tuple(step.command for step in steps) == EXPECTED_COMMANDS
    assert steps[0].name == "complete M1c Definition of Done"
    assert all(isinstance(step.command, tuple) for step in steps)
    assert all(command[: len(PYTEST_PREFIX)] == PYTEST_PREFIX for command in EXPECTED_COMMANDS[1:4])


def test_m1d_profile_never_uses_live_selection_or_golden_escape_hatches() -> None:
    arguments = {argument for step in verify.steps_for_m1d() for argument in step.command}

    for forbidden in (
        "--live",
        "--live-data",
        "--live-intent",
        "--ignore",
        "--deselect",
        "-k",
        "--lf",
        "--ff",
        "--write",
        "scripts/update_goldens.py",
    ):
        assert forbidden not in arguments
    assert not any("::" in argument for argument in arguments)
    assert not any("skip" in argument or "xfail" in argument for argument in arguments)
    assert all(
        step.command.count("-m") == (1 if "pytest" in step.command else 0)
        for step in verify.steps_for_m1d()
    )


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
        "DEEPSEEK_API_KEY": "deepseek-secret",
        "DASHSCOPE_API_KEY": "dashscope-secret",
        "TAVILY_API_KEY": "tavily-secret",
        "EBAY_APP_ID": "ebay-secret",
        "HTTP_PROXY": "http://proxy.invalid",
        "GLODEX_CONFIG": "/untrusted/config.toml",
        "PYTEST_ADDOPTS": "--ignore=tests/m1d",
        "PYTHONPATH": "/untrusted/modules",
    }
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(subprocess, "run", fake_run)

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


@pytest.mark.parametrize("return_code, expected", [(9, 9), (-9, 1)])
def test_runner_stops_at_first_failure_and_returns_process_safe_status(
    return_code: int,
    expected: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(
        command: list[str],
        **_kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        code = return_code if len(calls) == 2 else 0
        return subprocess.CompletedProcess(command, code)

    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(subprocess, "run", fake_run)

    exit_code = verify.run_verification(environ={"PATH": "/tools"})

    assert exit_code == expected
    assert [tuple(command) for command in calls] == list(EXPECTED_COMMANDS[:2])


def test_main_returns_the_runner_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verify, "run_verification", lambda: 7)

    assert verify.main() == 7
