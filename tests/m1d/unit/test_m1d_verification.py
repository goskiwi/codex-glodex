"""M1d inventory and inherited verification safety contracts."""

from __future__ import annotations

from collections.abc import Callable, MutableMapping
from pathlib import Path

import pytest

import scripts.check_traceability as traceability
import scripts.verify_m0 as verify_m0

PROJECT_ROOT = Path(__file__).parents[3]
M1D_P0_IDS = tuple(f"GLO-M1D-P0-{index:03d}" for index in range(1, 7))
M1D_AC_IDS = tuple(f"M1D-AC-{index:03d}" for index in range(1, 7))
M1D_NFR_IDS = tuple(f"GLO-M1D-NFR-{index:03d}" for index in range(1, 7))


@pytest.mark.unit
@pytest.mark.spec("GLO-M1D-P0-006", "GLO-M1D-NFR-006")
def test_m1d_profile_has_exact_approved_inventory_and_owned_root() -> None:
    spec_path = PROJECT_ROOT / "specs" / "004-glodex-m1d-agent-demo" / "spec.md"
    inventory = traceability.load_spec_inventory(spec_path)
    profile = traceability._TRACEABILITY_PROFILES["m1d"]

    assert inventory.p0_ids == M1D_P0_IDS
    assert inventory.ac_ids == M1D_AC_IDS
    assert inventory.nfr_ids == M1D_NFR_IDS
    assert profile.spec_path == spec_path
    assert profile.test_paths == (PROJECT_ROOT / "tests" / "m1d",)
    assert profile.approved_inventory == inventory


@pytest.mark.unit
@pytest.mark.spec("GLO-M1D-P0-001", "GLO-M1D-NFR-001", "GLO-M1D-NFR-005")
def test_default_environment_sanitizer_removes_m1d_provider_names(
    external_environment_variable_predicate: Callable[[str], bool],
    external_environment_sanitizer: Callable[
        [MutableMapping[str, str]],
        frozenset[str],
    ],
) -> None:
    environment = {
        "PATH": "/tools",
        "DASHSCOPE_API_KEY": "secret",
        "DASHSCOPE_MODEL": "untrusted",
        "TAVILY_API_KEY": "secret",
        "TAVILY_ENDPOINT": "untrusted",
    }

    removed = external_environment_sanitizer(environment)

    assert external_environment_variable_predicate("DASHSCOPE_API_KEY")
    assert external_environment_variable_predicate("TAVILY_API_KEY")
    assert removed == frozenset(
        {
            "DASHSCOPE_API_KEY",
            "DASHSCOPE_MODEL",
            "TAVILY_API_KEY",
            "TAVILY_ENDPOINT",
        }
    )
    assert environment == {"PATH": "/tools"}


@pytest.mark.unit
@pytest.mark.spec("GLO-M1D-P0-001", "GLO-M1D-NFR-001")
def test_m0_historical_phase_excludes_all_later_milestones() -> None:
    implemented_suite = next(
        step for step in verify_m0.steps_for_phase("A") if step.name == "implemented test suite"
    )

    assert implemented_suite.command[-4:] == (
        "--ignore=tests/m1a",
        "--ignore=tests/m1b",
        "--ignore=tests/m1c",
        "--ignore=tests/m1d",
    )
