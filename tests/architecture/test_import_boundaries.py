"""Static import boundaries for the deterministic M0 core."""

from __future__ import annotations

import ast
import sys
from importlib.util import resolve_name
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.architecture,
    pytest.mark.spec("GLO-NFR-006", "GLO-NFR-009", "GLO-NFR-010", "GLO-NFR-011"),
]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "src" / "glodex"

_FORBIDDEN_EXTERNAL_PREFIXES = (
    "aiohttp",
    "anthropic",
    "asyncpg",
    "boto3",
    "botocore",
    "cohere",
    "django",
    "google.cloud",
    "grpc",
    "httpx",
    "langchain",
    "langgraph",
    "litellm",
    "mistralai",
    "motor",
    "openai",
    "peewee",
    "psycopg",
    "psycopg2",
    "pymongo",
    "redis",
    "requests",
    "sqlalchemy",
    "transformers",
)

_API_ONLY_FRAMEWORK_PREFIXES = (
    "fastapi",
    "starlette",
)

_FORBIDDEN_NETWORK_OR_DATABASE_PREFIXES = (
    "ftplib",
    "http.client",
    "http.server",
    "imaplib",
    "poplib",
    "smtplib",
    "socket",
    "sqlite3",
    "telnetlib",
    "urllib.request",
    "xmlrpc.client",
    "xmlrpc.server",
)

_DOMAIN_IO_ROOTS = frozenset(
    {
        "asyncio",
        "io",
        "os",
        "pathlib",
        "shutil",
        "subprocess",
        "tempfile",
    }
)
_CAPTURE_HTTP_PATH = Path("capture/ebay_http.py")
_DEEPSEEK_HTTP_PATH = Path("adapters/deepseek_http.py")
_AGENT_LIVE_HTTP_PATH = Path("adapters/agent_live_http.py")


def _matches_prefix(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(module == prefix or module.startswith(f"{prefix}.") for prefix in prefixes)


def _module_package(path: Path, package_root: Path) -> str:
    relative_parts = list(path.relative_to(package_root).with_suffix("").parts)
    module_parts = [package_root.name, *relative_parts]
    if module_parts[-1] == "__init__":
        module_parts.pop()
        return ".".join(module_parts)
    return ".".join(module_parts[:-1])


def _imports(path: Path, package_root: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = _module_package(path, package_root)
    imports: set[tuple[str, int]] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update((alias.name, node.lineno) for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue

        if node.level:
            relative_name = f"{'.' * node.level}{node.module or ''}"
            imported_from = resolve_name(relative_name, package)
        else:
            imported_from = node.module or ""

        if imported_from:
            imports.add((imported_from, node.lineno))
        for alias in node.names:
            if alias.name != "*":
                imports.add((f"{imported_from}.{alias.name}".lstrip("."), node.lineno))

    return sorted(imports)


def find_import_boundary_violations(package_root: Path) -> list[str]:
    """Return deterministic, human-readable violations below ``package_root``."""

    violations: set[str] = set()
    for path in sorted(package_root.rglob("*.py")):
        relative_path = path.relative_to(package_root.parent)
        package_relative_path = path.relative_to(package_root)
        relative_parts = path.relative_to(package_root).parts
        layer = relative_parts[0] if len(relative_parts) > 1 else None

        for module, line_number in _imports(path, package_root):
            if layer != "api" and _matches_prefix(module, _API_ONLY_FRAMEWORK_PREFIXES):
                violations.add(
                    f"{relative_path}:{line_number}: framework import outside api ({module})"
                )
            if _matches_prefix(module, _FORBIDDEN_EXTERNAL_PREFIXES):
                approved_httpx_transport = package_relative_path in {
                    _DEEPSEEK_HTTP_PATH,
                    _AGENT_LIVE_HTTP_PATH,
                } and _matches_prefix(module, ("httpx",))
                if not approved_httpx_transport:
                    violations.add(
                        f"{relative_path}:{line_number}: external SDK/framework import ({module})"
                    )
            if _matches_prefix(module, _FORBIDDEN_NETWORK_OR_DATABASE_PREFIXES):
                approved_capture_http = (
                    package_relative_path == _CAPTURE_HTTP_PATH
                    and _matches_prefix(module, ("http.client",))
                )
                if not approved_capture_http:
                    violations.add(
                        f"{relative_path}:{line_number}: network/database import ({module})"
                    )

            if layer == "domain":
                root_module = module.partition(".")[0]
                if root_module in _DOMAIN_IO_ROOTS:
                    violations.add(f"{relative_path}:{line_number}: domain I/O import ({module})")
                if root_module == package_root.name and not (
                    module == f"{package_root.name}.domain"
                    or module.startswith(f"{package_root.name}.domain.")
                ):
                    violations.add(
                        f"{relative_path}:{line_number}: domain outward import ({module})"
                    )
                if root_module not in sys.stdlib_module_names and root_module != package_root.name:
                    violations.add(
                        f"{relative_path}:{line_number}: domain third-party import ({module})"
                    )

            if layer == "application" and (
                module == f"{package_root.name}.adapters"
                or module.startswith(f"{package_root.name}.adapters.")
            ):
                violations.add(
                    f"{relative_path}:{line_number}: application-to-adapter import ({module})"
                )

    return sorted(violations)


def test_repository_respects_static_import_boundaries() -> None:
    violations = find_import_boundary_violations(PACKAGE_ROOT)

    assert violations == [], "\n".join(violations)


def test_checker_detects_domain_io_and_framework_imports(tmp_path: Path) -> None:
    package_root = tmp_path / "glodex"
    domain_root = package_root / "domain"
    domain_root.mkdir(parents=True)
    (domain_root / "bad_domain.py").write_text(
        "from pathlib import Path\nfrom pydantic import BaseModel\n",
        encoding="utf-8",
    )

    violations = find_import_boundary_violations(package_root)

    assert any("domain I/O import (pathlib" in violation for violation in violations)
    assert any("domain third-party import (pydantic" in violation for violation in violations)


def test_checker_detects_application_dependency_on_adapter(tmp_path: Path) -> None:
    package_root = tmp_path / "glodex"
    application_root = package_root / "application"
    application_root.mkdir(parents=True)
    (application_root / "bad_service.py").write_text(
        "from ..adapters.local_snapshot import LocalSnapshot\n",
        encoding="utf-8",
    )

    violations = find_import_boundary_violations(package_root)

    assert any("application-to-adapter import" in violation for violation in violations)


def test_checker_detects_network_sdk_outside_domain(tmp_path: Path) -> None:
    package_root = tmp_path / "glodex"
    adapters_root = package_root / "adapters"
    adapters_root.mkdir(parents=True)
    (adapters_root / "bad_provider.py").write_text("import requests\n", encoding="utf-8")

    violations = find_import_boundary_violations(package_root)

    assert any("external SDK/framework import (requests)" in violation for violation in violations)
