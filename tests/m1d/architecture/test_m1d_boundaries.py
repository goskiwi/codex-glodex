"""Architecture budgets for the fixed M1d Agent composition."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.architecture.test_dependency_allowlist import (
    ALLOWED_RUNTIME_DEPENDENCIES,
    PYPROJECT_PATH,
    runtime_dependency_names,
)
from tests.architecture.test_import_boundaries import find_import_boundary_violations

pytestmark = pytest.mark.architecture

PROJECT_ROOT = Path(__file__).parents[3]
PACKAGE_ROOT = PROJECT_ROOT / "src" / "glodex"
_OUTBOUND_LITERAL_OWNERS = {
    "DEEPSEEK_API_KEY": frozenset({"adapters/deepseek_http.py"}),
    "DASHSCOPE_API_KEY": frozenset({"adapters/agent_live_http.py"}),
    "TAVILY_API_KEY": frozenset({"adapters/agent_live_http.py"}),
    "EBAY_APP_ID": frozenset(
        {
            "adapters/agent_item_search.py",
            "capture/config.py",
        }
    ),
    "EBAY_CERT_ID": frozenset(
        {
            "adapters/agent_item_search.py",
            "capture/config.py",
        }
    ),
    "api.deepseek.com": frozenset({"adapters/deepseek_http.py"}),
    "api.tavily.com": frozenset({"adapters/agent_live_http.py"}),
    "dashscope.aliyuncs.com": frozenset({"adapters/agent_live_http.py"}),
    "api.ebay.com": frozenset({"capture/config.py"}),
    "deepseek-v4-flash": frozenset({"adapters/deepseek_intent.py"}),
    "text-embedding-v4": frozenset(
        {
            "adapters/agent_indexes.py",
            "adapters/agent_live_http.py",
        }
    ),
}


@pytest.mark.spec("GLO-M1D-P0-001", "GLO-M1D-NFR-001", "GLO-M1D-NFR-006")
def test_m1d_keeps_the_existing_runtime_dependency_inventory() -> None:
    import tomllib

    with PYPROJECT_PATH.open("rb") as pyproject_file:
        pyproject = tomllib.load(pyproject_file)

    assert runtime_dependency_names(pyproject) == ALLOWED_RUNTIME_DEPENDENCIES


@pytest.mark.spec("GLO-M1D-P0-001", "GLO-M1D-NFR-005", "GLO-M1D-NFR-006")
def test_httpx_is_limited_to_the_two_approved_transport_modules(tmp_path: Path) -> None:
    package_root = tmp_path / "glodex"
    adapters_root = package_root / "adapters"
    api_root = package_root / "api"
    adapters_root.mkdir(parents=True)
    api_root.mkdir()
    (adapters_root / "deepseek_http.py").write_text("import httpx\n", encoding="utf-8")
    (adapters_root / "agent_live_http.py").write_text("import httpx\n", encoding="utf-8")
    (adapters_root / "deepseek_agent.py").write_text("import httpx\n", encoding="utf-8")
    (api_root / "agent_app.py").write_text("import httpx\n", encoding="utf-8")

    violations = find_import_boundary_violations(package_root)

    assert not any("adapters/deepseek_http.py" in item for item in violations)
    assert not any("adapters/agent_live_http.py" in item for item in violations)
    assert any("adapters/deepseek_agent.py" in item for item in violations)
    assert any("api/agent_app.py" in item for item in violations)


@pytest.mark.spec("GLO-M1D-P0-001", "GLO-M1D-NFR-006")
def test_m1d_does_not_add_generic_agent_or_provider_frameworks() -> None:
    forbidden_directories = {"llm", "providers"}
    forbidden_files = {
        "agent_framework.py",
        "base_tool.py",
        "model_gateway.py",
        "provider_registry.py",
        "retry.py",
        "retry_policy.py",
        "tool_registry.py",
    }

    directory_violations = sorted(
        path.relative_to(PACKAGE_ROOT).as_posix()
        for path in PACKAGE_ROOT.rglob("*")
        if path.is_dir() and path.name in forbidden_directories
    )
    file_violations = sorted(
        path.relative_to(PACKAGE_ROOT).as_posix()
        for path in PACKAGE_ROOT.rglob("*.py")
        if path.name in forbidden_files
    )

    assert directory_violations == []
    assert file_violations == []


@pytest.mark.spec("GLO-M1D-P0-001", "GLO-M1D-NFR-005", "GLO-M1D-NFR-006")
def test_agent_outbound_credentials_hosts_and_models_have_exact_owners() -> None:
    actual_owners: dict[str, set[str]] = {literal: set() for literal in _OUTBOUND_LITERAL_OWNERS}
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        relative_path = path.relative_to(PACKAGE_ROOT).as_posix()
        for literal in actual_owners:
            if literal in source:
                actual_owners[literal].add(relative_path)

    assert {
        literal: frozenset(paths) for literal, paths in actual_owners.items()
    } == _OUTBOUND_LITERAL_OWNERS


@pytest.mark.spec("GLO-M1D-P0-006", "GLO-M1D-NFR-006")
def test_each_ac_marker_is_function_level_and_owned_by_one_acceptance_test() -> None:
    expected = {f"M1D-AC-{index:03d}" for index in range(1, 7)}
    owners: dict[str, list[str]] = {spec_id: [] for spec_id in expected}
    module_level_claims: list[str] = []
    non_acceptance_claims: list[str] = []

    for path in sorted((PROJECT_ROOT / "tests" / "m1d").rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative_path = path.relative_to(PROJECT_ROOT).as_posix()
        module_is_acceptance = False
        for statement in tree.body:
            if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
                continue
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            if not any(
                isinstance(target, ast.Name) and target.id == "pytestmark" for target in targets
            ):
                continue
            value = statement.value
            constants = {
                node.value
                for node in ast.walk(value)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            }
            module_is_acceptance = "acceptance" in ast.unparse(value)
            for spec_id in sorted(constants & expected):
                module_level_claims.append(f"{relative_path}: {spec_id}")

        for statement in tree.body:
            if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            function_is_acceptance = module_is_acceptance
            for decorator in statement.decorator_list:
                rendered = ast.unparse(decorator)
                function_is_acceptance = (
                    function_is_acceptance or rendered == "pytest.mark.acceptance"
                )
                if not (
                    isinstance(decorator, ast.Call)
                    and ast.unparse(decorator.func) == "pytest.mark.spec"
                ):
                    continue
                for argument in decorator.args:
                    if (
                        isinstance(argument, ast.Constant)
                        and isinstance(argument.value, str)
                        and argument.value in expected
                    ):
                        owner = f"{relative_path}::{statement.name}"
                        owners[argument.value].append(owner)
                        if not function_is_acceptance:
                            non_acceptance_claims.append(owner)

    assert module_level_claims == []
    assert non_acceptance_claims == []
    assert set(owners) == expected
    assert all(len(node_ids) == 1 for node_ids in owners.values())
