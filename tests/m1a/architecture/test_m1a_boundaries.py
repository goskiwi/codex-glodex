"""Architecture boundaries introduced by the M1a HTTP adapter."""

from __future__ import annotations

import ast
import subprocess
import tomllib
from pathlib import Path

import pytest
from starlette.middleware.cors import CORSMiddleware

from tests.architecture.test_dependency_allowlist import runtime_dependency_names
from tests.architecture.test_import_boundaries import find_import_boundary_violations

pytestmark = [
    pytest.mark.architecture,
    pytest.mark.spec(
        "GLO-M1-P0-009",
        "GLO-M1-NFR-007",
        "GLO-M1-NFR-008",
        "GLO-M1-NFR-009",
        "GLO-M1-NFR-010",
    ),
]

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PYPROJECT_PATH = PROJECT_ROOT / "pyproject.toml"
ARCHITECTURE_IMAGE_ROOT = PROJECT_ROOT / "项目架构"
FORBIDDEN_OFFLINE_IMPORTS = (
    "aiohttp",
    "anthropic",
    "boto3",
    "cohere",
    "google.generativeai",
    "langchain",
    "langgraph",
    "litellm",
    "mistralai",
    "mysql",
    "ollama",
    "openai",
    "psycopg",
    "psycopg2",
    "pymongo",
    "pymysql",
    "redis",
    "requests",
    "socket",
    "sqlalchemy",
    "sqlite3",
    "transformers",
    "urllib.request",
    "urllib3",
)
FORBIDDEN_REALTIME_CALLS = frozenset(
    {
        "asyncio.sleep",
        "time.monotonic",
        "time.monotonic_ns",
        "time.perf_counter",
        "time.perf_counter_ns",
        "time.sleep",
    }
)


def _dependency_names(requirements: list[str]) -> frozenset[str]:
    return runtime_dependency_names({"project": {"dependencies": requirements}})


def _absolute_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
            modules.update(
                f"{node.module}.{alias.name}" for alias in node.names if alias.name != "*"
            )
    return modules


def _qualified_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _qualified_name(node.value)
        return None if owner is None else f"{owner}.{node.attr}"
    return None


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".", 1)[0]] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


def _resolved_call_name(call: ast.Call, aliases: dict[str, str]) -> str | None:
    name = _qualified_name(call.func)
    if name is None:
        return None
    root, separator, remainder = name.partition(".")
    resolved_root = aliases.get(root, root)
    return f"{resolved_root}.{remainder}" if separator else resolved_root


def _is_forbidden_offline_import(module: str) -> bool:
    return any(
        module == forbidden or module.startswith(f"{forbidden}.")
        for forbidden in FORBIDDEN_OFFLINE_IMPORTS
    )


def _api_and_realtime_test_paths() -> tuple[Path, ...]:
    roots = (
        PROJECT_ROOT / "src" / "glodex" / "api",
        PROJECT_ROOT / "tests" / "m1a" / "contract",
        PROJECT_ROOT / "tests" / "m1a" / "acceptance",
    )
    return tuple(path for root in roots for path in sorted(root.rglob("*.py")))


def _git_inventory(*arguments: str) -> frozenset[str]:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return frozenset(line for line in completed.stdout.splitlines() if line)


def test_m1a_dependency_groups_keep_frameworks_in_their_approved_scope() -> None:
    with PYPROJECT_PATH.open("rb") as pyproject_file:
        pyproject = tomllib.load(pyproject_file)

    runtime_names = runtime_dependency_names(pyproject)
    dev_names = _dependency_names(list(pyproject["dependency-groups"]["dev"]))

    assert runtime_names == frozenset(
        {"asyncpg", "fastapi", "httpx", "opensearch-py", "pydantic", "redis"}
    )
    assert "uvicorn" in dev_names
    assert "httpx" not in dev_names
    assert "uvicorn" not in runtime_names


def test_framework_imports_are_allowed_inside_api(tmp_path: Path) -> None:
    package_root = tmp_path / "glodex"
    api_root = package_root / "api"
    api_root.mkdir(parents=True)
    (api_root / "app.py").write_text(
        "from fastapi import FastAPI\nfrom starlette.responses import JSONResponse\n",
        encoding="utf-8",
    )

    assert find_import_boundary_violations(package_root) == []


@pytest.mark.parametrize(
    ("layer", "source", "module"),
    [
        ("application", "from fastapi import FastAPI\n", "fastapi"),
        ("domain", "from fastapi import FastAPI\n", "fastapi"),
        ("adapters", "from starlette.responses import Response\n", "starlette"),
    ],
)
def test_framework_imports_are_rejected_outside_api(
    tmp_path: Path,
    layer: str,
    source: str,
    module: str,
) -> None:
    package_root = tmp_path / "glodex"
    layer_root = package_root / layer
    layer_root.mkdir(parents=True)
    (layer_root / "bad_framework.py").write_text(source, encoding="utf-8")

    violations = find_import_boundary_violations(package_root)

    assert any(module in violation for violation in violations)


def test_httpx_is_allowed_only_in_the_approved_transport_modules(tmp_path: Path) -> None:
    package_root = tmp_path / "glodex"
    adapters_root = package_root / "adapters"
    api_root = package_root / "api"
    adapters_root.mkdir(parents=True)
    api_root.mkdir(parents=True)
    (adapters_root / "deepseek_http.py").write_text("import httpx\n", encoding="utf-8")
    (adapters_root / "agent_live_http.py").write_text("import httpx\n", encoding="utf-8")
    (api_root / "bad_client.py").write_text("import httpx\n", encoding="utf-8")

    violations = find_import_boundary_violations(package_root)

    assert not any("adapters/deepseek_http.py" in item for item in violations)
    assert not any("adapters/agent_live_http.py" in item for item in violations)
    assert any("api/bad_client.py" in item for item in violations)


def test_repository_keeps_http_clients_and_web_frameworks_in_approved_roots() -> None:
    violations: list[str] = []
    for source_root in (PROJECT_ROOT / "src", PROJECT_ROOT / "scripts"):
        for path in sorted(source_root.rglob("*.py")):
            relative_path = path.relative_to(PROJECT_ROOT)
            api_module = relative_path.parts[:3] == ("src", "glodex", "api")
            for module in sorted(_absolute_imports(path)):
                approved_httpx_path = relative_path in {
                    Path("src/glodex/adapters/deepseek_http.py"),
                    Path("src/glodex/adapters/agent_live_http.py"),
                    Path("src/glodex/adapters/dashscope_rerank.py"),
                    Path("src/glodex/adapters/m2c_model_service.py"),
                }
                if (module == "httpx" or module.startswith("httpx.")) and not approved_httpx_path:
                    violations.append(f"{relative_path}: httpx import outside tests")
                approved_m2c_gpu_service = relative_path == Path("src/glodex/m2c_gpu_service.py")
                if (
                    not api_module
                    and not approved_m2c_gpu_service
                    and (
                        module in {"fastapi", "starlette"}
                        or module.startswith(("fastapi.", "starlette."))
                    )
                ):
                    violations.append(f"{relative_path}: framework import outside glodex.api")

    assert violations == []


def test_api_and_realtime_tests_have_no_external_sdk_database_or_socket_imports() -> None:
    violations = [
        f"{path.relative_to(PROJECT_ROOT)}: {module}"
        for path in _api_and_realtime_test_paths()
        for module in sorted(_absolute_imports(path))
        if _is_forbidden_offline_import(module)
    ]

    assert violations == []


def test_httpx_clients_in_m1a_tests_always_use_an_explicit_in_process_transport() -> None:
    violations: list[str] = []
    test_roots = (
        PROJECT_ROOT / "tests" / "m1a" / "contract",
        PROJECT_ROOT / "tests" / "m1a" / "acceptance",
    )
    for root in test_roots:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            aliases = _import_aliases(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if _resolved_call_name(node, aliases) not in {
                    "httpx.AsyncClient",
                    "httpx.Client",
                }:
                    continue
                if not any(keyword.arg == "transport" for keyword in node.keywords):
                    relative_path = path.relative_to(PROJECT_ROOT)
                    violations.append(f"{relative_path}:{node.lineno}")

    assert violations == []


def test_m1a_realtime_tests_do_not_use_sleep_or_machine_timing() -> None:
    violations: list[str] = []
    for path in sorted((PROJECT_ROOT / "tests" / "m1a").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        aliases = _import_aliases(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            call_name = _resolved_call_name(node, aliases)
            if call_name in FORBIDDEN_REALTIME_CALLS:
                relative_path = path.relative_to(PROJECT_ROOT)
                violations.append(f"{relative_path}:{node.lineno}: {call_name}")

    assert violations == []


def test_api_imports_do_not_compose_services_start_tasks_or_read_configuration() -> None:
    forbidden_call_names = {
        "build_service",
        "create_task",
        "create_app",
        "load_config",
    }
    violations: list[str] = []
    for path in sorted((PROJECT_ROOT / "src" / "glodex" / "api").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        aliases = _import_aliases(tree)
        for statement in tree.body:
            if isinstance(statement, (ast.AsyncFunctionDef, ast.ClassDef, ast.FunctionDef)):
                continue
            for node in ast.walk(statement):
                if not isinstance(node, ast.Call):
                    continue
                call_name = _resolved_call_name(node, aliases)
                if call_name is not None and call_name.rsplit(".", 1)[-1] in forbidden_call_names:
                    relative_path = path.relative_to(PROJECT_ROOT)
                    violations.append(f"{relative_path}:{node.lineno}: {call_name}")

    assert violations == []


def test_injected_app_factory_is_idle_config_free_and_has_no_default_cors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import glodex.api.app as app_module

    accessed: list[str] = []

    def forbidden_factory_access(*args: object, **kwargs: object) -> object:
        del args, kwargs
        accessed.append("composition")
        raise AssertionError("injected application factory touched external composition")

    untouched_double = object()
    monkeypatch.setattr(app_module, "load_config", forbidden_factory_access)
    monkeypatch.setattr(app_module, "build_service", forbidden_factory_access)

    app = app_module.create_app(
        service=untouched_double,
        clock=untouched_double,
        run_id_provider=untouched_double,
        thread_id_factory=lambda: "thread-not-called",
    )

    assert accessed == []
    assert app.state.coordinator.in_flight_count == 0
    assert all(middleware.cls is not CORSMiddleware for middleware in app.user_middleware)


def test_architecture_pngs_remain_reference_only_and_outside_git_inventory() -> None:
    images = tuple(sorted(ARCHITECTURE_IMAGE_ROOT.rglob("*.png")))
    image_paths = frozenset(path.relative_to(PROJECT_ROOT).as_posix() for path in images)

    tracked = _git_inventory("ls-files", "--cached", "--", "*.png")
    staged = _git_inventory(
        "diff",
        "--cached",
        "--name-only",
        "--diff-filter=ACMR",
        "--",
        "*.png",
    )

    assert len(image_paths) == 26
    assert image_paths.isdisjoint(tracked)
    assert image_paths.isdisjoint(staged)
