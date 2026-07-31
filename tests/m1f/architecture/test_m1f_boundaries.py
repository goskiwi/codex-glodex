"""Architecture budgets for the standalone M1f static showcase."""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

import scripts.check_traceability as traceability
import scripts.verify_m1f as verify
from tests.architecture.test_dependency_allowlist import (
    ALLOWED_RUNTIME_DEPENDENCIES,
    runtime_dependency_names,
)

pytestmark = pytest.mark.architecture

PROJECT_ROOT = Path(__file__).parents[3]
PACKAGE_ROOT = PROJECT_ROOT / "src" / "glodex"
SHOWCASE_ROOT = PROJECT_ROOT / "showcase"
SERVICE_PATH = PROJECT_ROOT / "scripts" / "serve_m1f_showcase.py"
VALIDATOR_PATH = PROJECT_ROOT / "scripts" / "validate_m1f_showcase.py"
M1F_P0_IDS = tuple(f"GLO-M1F-P0-{index:03d}" for index in range(1, 5))
M1F_NFR_IDS = tuple(f"GLO-M1F-NFR-{index:03d}" for index in range(1, 5))
M1F_AC_IDS = tuple(f"M1F-AC-{index:03d}" for index in range(1, 5))


def _import_statements(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return tuple(
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    )


@pytest.mark.spec("GLO-M1F-P0-004", "GLO-M1F-NFR-001")
def test_static_server_is_standard_library_only_and_does_not_import_glodex() -> None:
    imports = _import_statements(SERVICE_PATH)

    assert not any("glodex" in statement for statement in imports)
    assert not any("os" in statement or "socket" in statement for statement in imports)
    assert not any("httpx" in statement or "urllib" in statement for statement in imports)
    assert "127.0.0.1" in SERVICE_PATH.read_text(encoding="utf-8")


@pytest.mark.spec("GLO-M1F-P0-004", "GLO-M1F-NFR-001", "GLO-M1F-NFR-004")
def test_validator_has_exactly_the_two_approved_glodex_contract_imports() -> None:
    imports = _import_statements(VALIDATOR_PATH)
    glodex_imports = tuple(statement for statement in imports if "glodex" in statement)

    assert glodex_imports == (
        "from glodex.api.agent_events import AgentPublicEvent",
        "from glodex.esci_benchmark import benchmark_summary",
    )


@pytest.mark.spec("GLO-M1F-P0-004", "GLO-M1F-NFR-002", "GLO-M1F-NFR-003")
def test_static_assets_have_no_remote_resource_references_or_raster_images() -> None:
    static_paths = (
        SHOWCASE_ROOT / "index.html",
        SHOWCASE_ROOT / "styles.css",
        SHOWCASE_ROOT / "app.js",
    )
    rendered = "\n".join(path.read_text(encoding="utf-8") for path in static_paths)

    assert not list(SHOWCASE_ROOT.rglob("*.png"))
    for forbidden in ("http://", "https://", "@import", "<iframe", "WebSocket"):
        assert forbidden not in rendered


@pytest.mark.spec("GLO-M1F-P0-004", "GLO-M1F-NFR-004")
def test_m1f_traceability_profile_and_runtime_dependency_inventory_are_exact() -> None:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as pyproject_file:
        pyproject = tomllib.load(pyproject_file)
    spec_path = PROJECT_ROOT / "specs" / "006-glodex-m1f-local-showcase" / "spec.md"
    inventory = traceability.load_spec_inventory(spec_path)
    profile = traceability._TRACEABILITY_PROFILES["m1f"]

    assert runtime_dependency_names(pyproject) == ALLOWED_RUNTIME_DEPENDENCIES
    assert inventory.p0_ids == M1F_P0_IDS
    assert inventory.nfr_ids == M1F_NFR_IDS
    assert inventory.ac_ids == M1F_AC_IDS
    assert profile.spec_path == spec_path
    assert profile.test_paths == (PROJECT_ROOT / "tests" / "m1f",)
    assert profile.approved_inventory == inventory


@pytest.mark.spec("GLO-M1F-P0-004", "GLO-M1F-NFR-004")
def test_parent_runtime_never_imports_the_m1f_showcase() -> None:
    imports = {
        path.relative_to(PACKAGE_ROOT).as_posix(): _import_statements(path)
        for path in sorted(PACKAGE_ROOT.rglob("*.py"))
    }

    assert not [
        path
        for path, statements in imports.items()
        if any(
            "m1f" in statement.casefold() or "showcase" in statement.casefold()
            for statement in statements
        )
    ]


@pytest.mark.spec("GLO-M1F-P0-004", "GLO-M1F-NFR-004")
def test_m1f_runner_starts_with_one_complete_m1e_gate() -> None:
    steps = verify.steps_for_m1f()

    assert len(steps) == 5
    assert steps[0].command == (
        "uv",
        "run",
        "--locked",
        "python",
        "scripts/verify_m1e.py",
    )
    assert sum("scripts/verify_m1e.py" in step.command for step in steps) == 1
