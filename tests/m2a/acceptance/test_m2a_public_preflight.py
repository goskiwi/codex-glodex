from __future__ import annotations

import json

import pytest

from glodex.cli import main

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.spec(
        "M2A-AC-001",
        "M2A-AC-002",
        "M2A-AC-003",
        "M2A-AC-004",
        "M2A-AC-005",
        "M2A-AC-006",
        "M2A-AC-007",
    ),
]


def test_m2a_public_commands_fail_closed_before_any_unapproved_backend(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Public CLI has no implicit M1d fallback, credentials, or remote endpoint switch."""

    assert main(("m2a-index", "--action", "verify", "--snapshot", "other")) == 2
    assert json.loads(capsys.readouterr().out) == {
        "code": "M2A_SNAPSHOT_UNSUPPORTED",
        "status": "FAILED",
    }

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
