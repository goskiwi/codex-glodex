"""M1c inventory and verification safety contracts."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, MutableMapping
from pathlib import Path
from typing import Any

import pytest

import scripts.check_traceability as traceability
import scripts.verify_m0 as verify_m0
import scripts.verify_m1c as verify

PROJECT_ROOT = Path(__file__).parents[3]
M1C_P0_IDS = tuple(f"GLO-M1C-P0-{index:03d}" for index in range(1, 7))
M1C_AC_IDS = tuple(f"M1C-AC-{index:03d}" for index in range(1, 7))
M1C_NFR_IDS = tuple(f"GLO-M1C-NFR-{index:03d}" for index in range(1, 7))
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
        "scripts/verify_m1b.py",
    ),
    (
        *PYTEST_PREFIX,
        "tests/m1c/architecture",
        "tests/m1c/nfr",
    ),
    (
        *PYTEST_PREFIX,
        "tests/m1c/unit",
        "tests/m1c/contract",
    ),
    (
        *PYTEST_PREFIX,
        "tests/m1c/acceptance",
    ),
    (
        "uv",
        "run",
        "--locked",
        "python",
        "scripts/check_traceability.py",
        "--profile",
        "m1c",
        "--mode",
        "coverage",
    ),
)


@pytest.mark.unit
@pytest.mark.spec("GLO-M1C-P0-006", "GLO-M1C-NFR-006")
def test_m1c_profile_has_exact_approved_inventory_and_owned_root() -> None:
    spec_path = PROJECT_ROOT / "specs" / "003-glodex-m1c-llm-intent" / "spec.md"
    inventory = traceability.load_spec_inventory(spec_path)
    profile = traceability._TRACEABILITY_PROFILES["m1c"]

    assert inventory.p0_ids == M1C_P0_IDS
    assert inventory.ac_ids == M1C_AC_IDS
    assert inventory.nfr_ids == M1C_NFR_IDS
    assert profile.spec_path == spec_path
    assert profile.test_paths == (PROJECT_ROOT / "tests" / "m1c",)
    assert profile.approved_inventory == inventory


@pytest.mark.unit
@pytest.mark.spec("GLO-M1C-P0-006", "GLO-M1C-NFR-001")
def test_default_environment_sanitizer_removes_deepseek_names(
    external_environment_variable_predicate: Callable[[str], bool],
    external_environment_sanitizer: Callable[
        [MutableMapping[str, str]],
        frozenset[str],
    ],
) -> None:
    environment = {
        "PATH": "/tools",
        "DEEPSEEK_API_KEY": "secret",
        "DEEPSEEK_MODEL": "untrusted",
    }

    removed = external_environment_sanitizer(environment)

    assert external_environment_variable_predicate("DEEPSEEK_API_KEY")
    assert removed == frozenset({"DEEPSEEK_API_KEY", "DEEPSEEK_MODEL"})
    assert environment == {"PATH": "/tools"}


@pytest.mark.unit
@pytest.mark.spec("GLO-M1C-NFR-001")
def test_m1c_profile_has_the_exact_m1b_first_gate_order() -> None:
    steps = verify.steps_for_m1c()

    assert tuple(step.command for step in steps) == EXPECTED_COMMANDS
    assert steps[0].name == "complete M1b Definition of Done"
    assert all(isinstance(step.command, tuple) for step in steps)
    assert all(command[: len(PYTEST_PREFIX)] == PYTEST_PREFIX for command in EXPECTED_COMMANDS[1:4])


@pytest.mark.unit
@pytest.mark.spec("GLO-M1C-NFR-001")
def test_m1c_profile_never_uses_live_or_test_selection_escape_hatches() -> None:
    arguments = {argument for step in verify.steps_for_m1c() for argument in step.command}

    assert "--live" not in arguments
    assert "--live-intent" not in arguments
    assert "--ignore" not in arguments
    assert "--deselect" not in arguments
    assert "-k" not in arguments
    assert "--lf" not in arguments
    assert "--ff" not in arguments
    assert "--write" not in arguments
    assert "scripts/update_goldens.py" not in arguments
    assert not any("::" in argument for argument in arguments)
    assert not any("skip" in argument or "xfail" in argument for argument in arguments)
    assert all(
        step.command.count("-m") == (1 if "pytest" in step.command else 0)
        for step in verify.steps_for_m1c()
    )


@pytest.mark.unit
@pytest.mark.spec("GLO-M1C-NFR-001")
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
        "DEEPSEEK_API_KEY": "secret",
        "DEEPSEEK_MODEL": "untrusted",
        "HTTP_PROXY": "http://proxy.invalid",
        "GLODEX_CONFIG": "/untrusted/config.toml",
        "PYTEST_ADDOPTS": "--ignore=tests/m1c",
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


@pytest.mark.unit
@pytest.mark.spec("GLO-M1C-NFR-001")
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


@pytest.mark.unit
@pytest.mark.spec("GLO-M1C-NFR-001")
def test_main_returns_the_runner_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verify, "run_verification", lambda: 7)

    assert verify.main() == 7


@pytest.mark.unit
@pytest.mark.spec("GLO-M1C-NFR-001")
def test_readme_discloses_the_fixed_live_mode_and_its_limits() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    for required_text in (
        "search --live-intent",
        "glodex.api.live_app:create_live_app",
        "完整的 trimmed query",
        "deepseek-v4-flash",
        "65,536 bytes",
        "max_tokens=1024",
        "不承诺控制 Provider 的训练",
        "scripts/verify_m1c.py",
    ):
        assert required_text in readme

    for obsolete_text in (
        "项目仍不连接真实 LLM",
        "真实 LLM 均未实现",
        "后续 M1**\uff1a真实 LLM Intent",
    ):
        assert obsolete_text not in readme


@pytest.mark.unit
@pytest.mark.spec("GLO-M1C-P0-006", "GLO-M1C-NFR-002")
def test_live_verification_record_contains_only_the_approved_safe_evidence() -> None:
    verification = (
        PROJECT_ROOT / "specs" / "003-glodex-m1c-llm-intent" / "verification.md"
    ).read_text(encoding="utf-8")
    table_rows = [
        line for line in verification.splitlines() if line.startswith("| ") and "---" not in line
    ]

    assert [row.split("|")[1].strip() for row in table_rows] == [
        "字段",
        "Provider",
        "Request model",
        "Calls",
        "Safe terminal",
        "Automatic evidence",
    ]
    for required_text in (
        "`DeepSeek Open Platform`",
        "`deepseek-v4-flash`",
        "| Calls | `1` |",
        "| Safe terminal | `COMPLETED` |",
        "`scripts/verify_m1c.py` exit `0`",
    ):
        assert required_text in verification
    for forbidden_text in (
        "推荐 800 美元以内",
        "DEEPSEEK_API_KEY",
        "Authorization",
        ".env",
        "run_id",
        "thread_id",
        "https://",
        "```",
    ):
        assert forbidden_text not in verification
