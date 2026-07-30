from __future__ import annotations

import json

import pytest

from glodex.cli import main

pytestmark = [
    pytest.mark.contract,
    pytest.mark.spec(
        "GLO-M2A-P0-007",
        "GLO-M2A-NFR-001",
        "GLO-M2A-NFR-003",
        "GLO-M2A-NFR-005",
    ),
]


def test_m2a_cli_rejects_unsupported_snapshot_without_opening_a_socket(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(("m2a-index", "--action", "build", "--snapshot", "m0-v1")) == 2
    assert json.loads(capsys.readouterr().out) == {
        "code": "M2A_SNAPSHOT_UNSUPPORTED",
        "status": "FAILED",
    }


def test_profile_set_requires_explicit_live_before_credential_or_network_access(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        main(
            (
                "m2a-profile",
                "--action",
                "set",
                "--profile",
                "local-demo",
                "--value",
                "轻薄",
            )
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out) == {
        "code": "M2A_PROFILE_LIVE_REQUIRED",
        "status": "FAILED",
    }
