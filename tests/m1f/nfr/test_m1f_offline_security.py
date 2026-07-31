"""Offline, inventory, and data-minimization evidence for M1f."""

from __future__ import annotations

import os
import socket
from collections.abc import Callable
from pathlib import Path

import pytest
from pytest_socket import SocketBlockedError

from scripts.validate_m1f_showcase import MAX_ASSET_BYTES, REPLAY_FILENAME, SUMMARY_FILENAME

pytestmark = pytest.mark.nfr

PROJECT_ROOT = Path(__file__).parents[3]
SHOWCASE_ROOT = PROJECT_ROOT / "showcase"
ASSETS_ROOT = SHOWCASE_ROOT / "assets"


@pytest.mark.spec("GLO-M1F-NFR-001", "GLO-M1F-NFR-004")
def test_default_pytest_process_blocks_sockets_and_has_no_provider_credentials(
    pytestconfig: pytest.Config,
    external_environment_variable_predicate: Callable[[str], bool],
) -> None:
    assert pytestconfig.getoption("--disable-socket") is True
    assert not any(external_environment_variable_predicate(name) for name in os.environ)
    with (
        pytest.warns(UserWarning, match=r"tried to use socket\.socket"),
        pytest.raises(SocketBlockedError),
    ):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)


@pytest.mark.spec("GLO-M1F-NFR-002", "GLO-M1F-NFR-003")
def test_showcase_inventory_is_small_text_only_and_contains_no_architecture_pngs() -> None:
    asset_paths = tuple(sorted(ASSETS_ROOT.iterdir()))

    assert tuple(path.name for path in asset_paths) == (REPLAY_FILENAME, SUMMARY_FILENAME)
    assert all(
        path.suffix == ".json" and path.is_file() and not path.is_symlink() for path in asset_paths
    )
    assert sum(path.stat().st_size for path in asset_paths) <= MAX_ASSET_BYTES
    assert not list(SHOWCASE_ROOT.rglob("*.png"))
    for path in asset_paths:
        text = path.read_text(encoding="utf-8")
        for forbidden in ("product_title", "product_description", "judgements.jsonl", "query"):
            assert forbidden not in text
