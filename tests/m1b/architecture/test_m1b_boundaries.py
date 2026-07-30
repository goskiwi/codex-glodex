from __future__ import annotations

import ast
import subprocess
import tomllib
from pathlib import Path

import pytest

from tests.architecture.test_dependency_allowlist import (
    ALLOWED_RUNTIME_DEPENDENCIES,
    runtime_dependency_names,
)
from tests.architecture.test_import_boundaries import (
    PACKAGE_ROOT,
    find_import_boundary_violations,
)

pytestmark = [
    pytest.mark.architecture,
    pytest.mark.spec(
        "GLO-M1B-P0-001",
        "GLO-M1B-P0-006",
        "M1B-AC-001",
        "M1B-AC-006",
        "GLO-M1B-NFR-001",
        "GLO-M1B-NFR-004",
    ),
]

PROJECT_ROOT = Path(__file__).parents[3]
PYPROJECT_PATH = PROJECT_ROOT / "pyproject.toml"


def _capture_imports(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(
                alias.name for alias in node.names if alias.name.startswith("glodex.capture")
            )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith("glodex.capture") or (
                node.level > 0
                and (
                    module == "capture"
                    or module.startswith("capture.")
                    or (not module and any(alias.name == "capture" for alias in node.names))
                )
            ):
                imports.append(module or "capture")
    return tuple(imports)


def test_only_exact_ebay_http_path_may_import_http_client(tmp_path: Path) -> None:
    package_root = tmp_path / "glodex"
    capture_root = package_root / "capture"
    adapters_root = package_root / "adapters"
    capture_root.mkdir(parents=True)
    adapters_root.mkdir()
    (capture_root / "ebay_http.py").write_text(
        "import http.client\nfrom http.client import HTTPSConnection\n",
        encoding="utf-8",
    )
    (capture_root / "service.py").write_text("import http.client\n", encoding="utf-8")
    (adapters_root / "provider.py").write_text("import http.client\n", encoding="utf-8")

    violations = find_import_boundary_violations(package_root)

    assert not any("capture/ebay_http.py" in violation for violation in violations)
    assert any("capture/service.py" in violation for violation in violations)
    assert any("adapters/provider.py" in violation for violation in violations)


def test_approved_transport_file_cannot_open_other_network_channels(tmp_path: Path) -> None:
    package_root = tmp_path / "glodex"
    capture_root = package_root / "capture"
    capture_root.mkdir(parents=True)
    (capture_root / "ebay_http.py").write_text(
        "import socket\nimport urllib.request\n",
        encoding="utf-8",
    )

    violations = find_import_boundary_violations(package_root)

    assert any("network/database import (socket)" in violation for violation in violations)
    assert any("network/database import (urllib.request)" in violation for violation in violations)


def test_real_repository_respects_narrow_capture_boundary() -> None:
    assert find_import_boundary_violations(PACKAGE_ROOT) == []


def test_default_search_composition_has_no_capture_dependency() -> None:
    forbidden_paths = [PACKAGE_ROOT / "bootstrap.py"]
    approved_agent_item_source = PACKAGE_ROOT / "adapters" / "agent_item_search.py"
    for layer in ("adapters", "api", "application", "domain"):
        forbidden_paths.extend(
            path
            for path in sorted((PACKAGE_ROOT / layer).rglob("*.py"))
            if path != approved_agent_item_source
        )

    violations = {
        str(path.relative_to(PACKAGE_ROOT)): _capture_imports(path)
        for path in forbidden_paths
        if _capture_imports(path)
    }

    assert violations == {}


def test_cli_keeps_capture_imports_below_the_normal_dispatch_boundary() -> None:
    cli_path = PACKAGE_ROOT / "cli.py"
    tree = ast.parse(cli_path.read_text(encoding="utf-8"), filename=str(cli_path))
    top_level_imports = ast.Module(
        body=[node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))],
        type_ignores=[],
    )
    imports: list[str] = []
    for node in ast.walk(top_level_imports):
        if isinstance(node, ast.Import):
            imports.extend(
                alias.name for alias in node.names if alias.name.startswith("glodex.capture")
            )
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("glodex.capture"):
            imports.append(node.module or "")

    assert imports == []


def test_m1b_does_not_expand_the_runtime_dependency_inventory() -> None:
    with PYPROJECT_PATH.open("rb") as pyproject_file:
        pyproject = tomllib.load(pyproject_file)

    assert runtime_dependency_names(pyproject) == ALLOWED_RUNTIME_DEPENDENCIES


def test_local_env_files_are_ignored_but_example_is_allowed() -> None:
    for path in (".env", ".env.local"):
        ignored = subprocess.run(
            ["git", "check-ignore", "--no-index", "--quiet", "--", path],
            cwd=PROJECT_ROOT,
            check=False,
        )
        assert ignored.returncode == 0

    example = subprocess.run(
        ["git", "check-ignore", "--no-index", "--quiet", "--", ".env.example"],
        cwd=PROJECT_ROOT,
        check=False,
    )
    assert example.returncode == 1
