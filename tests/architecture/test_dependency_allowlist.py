"""Guard the intentionally tiny Glodex runtime dependency surface."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

pytestmark = [
    pytest.mark.architecture,
    pytest.mark.spec("GLO-P0-012", "GLO-NFR-006", "GLO-NFR-010", "GLO-NFR-011"),
]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT_PATH = PROJECT_ROOT / "pyproject.toml"
ALLOWED_RUNTIME_DEPENDENCIES = frozenset({"fastapi", "pydantic"})

_DISTRIBUTION_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


def _normalize_distribution_name(requirement: str) -> str:
    match = _DISTRIBUTION_NAME.match(requirement.strip())
    if match is None:
        raise AssertionError(f"Invalid PEP 508 dependency entry: {requirement!r}")
    return re.sub(r"[-_.]+", "-", match.group(0)).lower()


def _runtime_dependency_entries(pyproject: dict[str, Any]) -> list[str]:
    project = pyproject.get("project", {})
    dependencies = list(project.get("dependencies", []))
    for optional_group in project.get("optional-dependencies", {}).values():
        dependencies.extend(optional_group)
    return dependencies


def runtime_dependency_names(pyproject: dict[str, Any]) -> frozenset[str]:
    return frozenset(
        _normalize_distribution_name(requirement)
        for requirement in _runtime_dependency_entries(pyproject)
    )


def test_runtime_dependency_allowlist_contains_only_fastapi_and_pydantic() -> None:
    with PYPROJECT_PATH.open("rb") as pyproject_file:
        pyproject = tomllib.load(pyproject_file)

    dependency_names = runtime_dependency_names(pyproject)

    assert dependency_names == ALLOWED_RUNTIME_DEPENDENCIES


def test_allowlist_checker_rejects_network_database_and_model_sdks() -> None:
    violating_pyproject = {
        "project": {
            "dependencies": [
                "fastapi>=0.135",
                "pydantic>=2",
                "requests>=2",
                "openai>=2",
            ],
            "optional-dependencies": {
                "database": ["SQLAlchemy>=2"],
            },
        }
    }

    dependency_names = runtime_dependency_names(violating_pyproject)
    forbidden_names = dependency_names - ALLOWED_RUNTIME_DEPENDENCIES

    assert forbidden_names == frozenset({"openai", "requests", "sqlalchemy"})
