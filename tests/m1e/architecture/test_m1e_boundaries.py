"""Architecture and final-gate boundaries for the isolated M1e benchmark."""

from __future__ import annotations

import ast
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest

import scripts.check_traceability as traceability
import scripts.verify_m0 as verify_m0
import scripts.verify_m1e as verify
from tests.architecture.test_dependency_allowlist import (
    ALLOWED_RUNTIME_DEPENDENCIES,
    runtime_dependency_names,
)

pytestmark = pytest.mark.architecture

PROJECT_ROOT = Path(__file__).parents[3]
PACKAGE_ROOT = PROJECT_ROOT / "src" / "glodex"
M1E_MODULE_PATH = PACKAGE_ROOT / "esci_benchmark.py"
BUILDER_PATH = PROJECT_ROOT / "scripts" / "build_esci_benchmark.py"
M1E_P0_IDS = tuple(f"GLO-M1E-P0-{index:03d}" for index in range(1, 5))
M1E_AC_IDS = tuple(f"M1E-AC-{index:03d}" for index in range(1, 5))
M1E_NFR_IDS = tuple(f"GLO-M1E-NFR-{index:03d}" for index in range(1, 5))

_PYTEST_PREFIX = (
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
_EXPECTED_M1E_COMMANDS = (
    (
        "uv",
        "run",
        "--locked",
        "python",
        "scripts/verify_m1d.py",
    ),
    (
        *_PYTEST_PREFIX,
        "tests/m1e/architecture",
        "tests/m1e/nfr",
    ),
    (
        *_PYTEST_PREFIX,
        "tests/m1e/unit",
        "tests/m1e/contract",
    ),
    (
        *_PYTEST_PREFIX,
        "tests/m1e/acceptance",
    ),
    (
        "uv",
        "run",
        "--locked",
        "python",
        "scripts/check_traceability.py",
        "--profile",
        "m1e",
        "--mode",
        "coverage",
    ),
)
_FORBIDDEN_M1E_IMPORT_PREFIXES = (
    "glodex.adapters",
    "glodex.agent_bootstrap",
    "glodex.api",
    "glodex.application",
    "glodex.bootstrap",
    "glodex.capture",
    "glodex.config",
    "glodex.contracts",
    "glodex.domain",
    "httpx",
    "pyarrow",
    "socket",
    "urllib",
)


def _import_statements(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    rendered: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            rendered.append(ast.unparse(node))
    return tuple(rendered)


def _source_paths_importing_esci_benchmark() -> tuple[str, ...]:
    importers: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        if path == M1E_MODULE_PATH:
            continue
        if any("esci_benchmark" in statement for statement in _import_statements(path)):
            importers.append(path.relative_to(PACKAGE_ROOT).as_posix())
    return tuple(importers)


@pytest.mark.spec("GLO-M1E-P0-001", "GLO-M1E-NFR-001", "GLO-M1E-NFR-004")
def test_esci_runtime_module_is_local_standard_library_only() -> None:
    assert M1E_MODULE_PATH.is_file()

    forbidden = sorted(
        statement
        for statement in _import_statements(M1E_MODULE_PATH)
        if any(prefix in statement for prefix in _FORBIDDEN_M1E_IMPORT_PREFIXES)
    )

    assert forbidden == []


@pytest.mark.spec("GLO-M1E-P0-004", "GLO-M1E-NFR-001", "GLO-M1E-NFR-004")
def test_esci_benchmark_can_only_be_imported_by_the_explicit_cli() -> None:
    assert _source_paths_importing_esci_benchmark() == ("cli.py",)


@pytest.mark.spec("GLO-M1E-P0-001", "GLO-M1E-P0-004", "GLO-M1E-NFR-001")
def test_pyarrow_is_build_only_and_runtime_dependency_inventory_is_unchanged() -> None:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as pyproject_file:
        pyproject = tomllib.load(pyproject_file)

    dev_dependencies = pyproject["dependency-groups"]["dev"]
    pyarrow_dependencies = [
        dependency for dependency in dev_dependencies if dependency.partition(">")[0] == "pyarrow"
    ]
    pyarrow_owners = tuple(
        path.relative_to(PROJECT_ROOT).as_posix()
        for root in (PROJECT_ROOT / "scripts", PACKAGE_ROOT)
        for path in sorted(root.rglob("*.py"))
        if "pyarrow" in path.read_text(encoding="utf-8")
    )

    assert runtime_dependency_names(pyproject) == ALLOWED_RUNTIME_DEPENDENCIES
    assert pyarrow_dependencies == ["pyarrow>=23,<24"]
    assert BUILDER_PATH.is_file()
    assert pyarrow_owners == ("scripts/build_esci_benchmark.py",)


@pytest.mark.spec("GLO-M1E-P0-004", "GLO-M1E-NFR-004")
def test_m1e_traceability_profile_has_the_exact_owned_inventory_and_root() -> None:
    spec_path = PROJECT_ROOT / "specs" / "005-glodex-m1e-esci-retrieval-benchmark" / "spec.md"
    inventory = traceability.load_spec_inventory(spec_path)
    profile = traceability._TRACEABILITY_PROFILES["m1e"]

    assert inventory.p0_ids == M1E_P0_IDS
    assert inventory.ac_ids == M1E_AC_IDS
    assert inventory.nfr_ids == M1E_NFR_IDS
    assert profile.spec_path == spec_path
    assert profile.test_paths == (PROJECT_ROOT / "tests" / "m1e",)
    assert profile.approved_inventory == inventory


@pytest.mark.spec("GLO-M1E-P0-004", "GLO-M1E-NFR-001", "GLO-M1E-NFR-004")
def test_m1e_verification_gate_runs_one_complete_m1d_parent_then_exact_m1e_steps() -> None:
    steps = verify.steps_for_m1e()

    assert tuple(step.command for step in steps) == _EXPECTED_M1E_COMMANDS
    assert steps[0].name == "complete M1d Definition of Done"
    assert sum("scripts/verify_m1d.py" in step.command for step in steps) == 1
    assert all(isinstance(step.command, tuple) for step in steps)
    assert all(
        command[: len(_PYTEST_PREFIX)] == _PYTEST_PREFIX for command in _EXPECTED_M1E_COMMANDS[1:4]
    )

    arguments = {argument for step in steps for argument in step.command}
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
    ):
        assert forbidden not in arguments
    assert not any("::" in argument for argument in arguments)
    assert not any("skip" in argument or "xfail" in argument for argument in arguments)


@pytest.mark.spec("GLO-M1E-P0-004", "GLO-M1E-NFR-001", "GLO-M1E-NFR-004")
def test_m1e_verification_runner_uses_a_fixed_root_sanitized_environment_and_no_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
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
        "PYTEST_ADDOPTS": "--ignore=tests/m1e",
        "PYTHONPATH": "/untrusted/modules",
    }
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(subprocess, "run", fake_run)

    exit_code = verify.run_verification(environ=original_environment)

    assert exit_code == 0
    assert [tuple(command) for command, _kwargs in calls] == list(_EXPECTED_M1E_COMMANDS)
    expected_environment = verify_m0.sanitized_environment(original_environment)
    for command, kwargs in calls:
        assert isinstance(command, list)
        assert kwargs == {
            "cwd": tmp_path.resolve(),
            "env": expected_environment,
            "check": False,
            "shell": False,
        }


@pytest.mark.spec("GLO-M1E-P0-004", "GLO-M1E-NFR-004")
@pytest.mark.parametrize("return_code, expected", [(9, 9), (-9, 1)])
def test_m1e_verification_runner_stops_at_the_first_failure(
    return_code: int,
    expected: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        code = return_code if len(calls) == 2 else 0
        return subprocess.CompletedProcess(command, code)

    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(subprocess, "run", fake_run)

    exit_code = verify.run_verification(environ={"PATH": "/tools"})

    assert exit_code == expected
    assert [tuple(command) for command in calls] == list(_EXPECTED_M1E_COMMANDS[:2])
