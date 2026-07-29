"""Architecture budgets for the single approved DeepSeek adapter."""

from __future__ import annotations

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
DEEPSEEK_HTTP_PATH = Path("adapters/deepseek_http.py")
EBAY_HTTP_PATH = Path("capture/ebay_http.py")
DEEPSEEK_HTTP_SOURCE = Path("src/glodex") / DEEPSEEK_HTTP_PATH
EBAY_HTTP_SOURCE = Path("src/glodex") / EBAY_HTTP_PATH


@pytest.mark.spec("GLO-M1C-NFR-006")
def test_runtime_dependency_inventory_adds_only_httpx() -> None:
    import tomllib

    with PYPROJECT_PATH.open("rb") as pyproject_file:
        pyproject = tomllib.load(pyproject_file)

    assert frozenset({"fastapi", "httpx", "pydantic"}) == ALLOWED_RUNTIME_DEPENDENCIES
    assert runtime_dependency_names(pyproject) == ALLOWED_RUNTIME_DEPENDENCIES


@pytest.mark.spec("GLO-M1C-P0-001", "GLO-M1C-NFR-006")
def test_only_exact_deepseek_http_path_may_import_httpx(tmp_path: Path) -> None:
    package_root = tmp_path / "glodex"
    adapters_root = package_root / "adapters"
    api_root = package_root / "api"
    adapters_root.mkdir(parents=True)
    api_root.mkdir()
    (adapters_root / "deepseek_http.py").write_text("import httpx\n", encoding="utf-8")
    (adapters_root / "deepseek_intent.py").write_text("import httpx\n", encoding="utf-8")
    (api_root / "live_app.py").write_text("import httpx\n", encoding="utf-8")

    violations = find_import_boundary_violations(package_root)

    assert not any("adapters/deepseek_http.py" in violation for violation in violations)
    assert any("adapters/deepseek_intent.py" in violation for violation in violations)
    assert any("api/live_app.py" in violation for violation in violations)


@pytest.mark.spec("GLO-M1C-P0-001", "GLO-M1C-NFR-002", "GLO-M1C-NFR-006")
def test_deepseek_network_literals_stay_in_the_approved_transport() -> None:
    protected_literals = ("DEEPSEEK_API_KEY", "api.deepseek.com")
    violations: list[str] = []
    for source_root in (PROJECT_ROOT / "src", PROJECT_ROOT / "scripts"):
        for path in sorted(source_root.rglob("*.py")):
            relative_path = path.relative_to(PROJECT_ROOT)
            text = path.read_text(encoding="utf-8")
            if relative_path != DEEPSEEK_HTTP_SOURCE:
                for literal in protected_literals:
                    if literal in text:
                        violations.append(f"{relative_path}: {literal}")
            constructs_bearer_header = '"Authorization"' in text and "Bearer " in text
            if constructs_bearer_header and relative_path not in {
                DEEPSEEK_HTTP_SOURCE,
                EBAY_HTTP_SOURCE,
            }:
                violations.append(f"{relative_path}: Bearer Authorization")

    assert violations == []


@pytest.mark.spec("GLO-M1C-NFR-006")
def test_no_generic_llm_infrastructure_is_added() -> None:
    forbidden_directories = {"llm", "providers"}
    forbidden_files = {
        "cache.py",
        "llm_receipt.py",
        "model_gateway.py",
        "model_cache.py",
        "provider_registry.py",
        "prompt_registry.py",
        "receipt.py",
        "retry.py",
        "retry_policy.py",
        "router.py",
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
