from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.spec(
        "GLO-M1B-P0-001",
        "GLO-M1B-P0-006",
        "M1B-AC-001",
        "M1B-AC-006",
        "GLO-M1B-NFR-001",
        "GLO-M1B-NFR-004",
        "GLO-M1B-NFR-006",
    ),
]

PROJECT_ROOT = Path(__file__).parents[3]
_EXPECTED_M1B_IDS = frozenset(
    {
        *(f"GLO-M1B-P0-{index:03d}" for index in range(1, 7)),
        *(f"M1B-AC-{index:03d}" for index in range(1, 7)),
        *(f"GLO-M1B-NFR-{index:03d}" for index in range(1, 7)),
    }
)
_NORMAL_STACK_PROBE = textwrap.dedent(
    """
    import asyncio
    import http.client
    import json
    import socket
    import subprocess
    import sys
    import urllib.request

    import httpx

    external_calls = []

    def denied(name):
        def fail(*args, **kwargs):
            del args, kwargs
            external_calls.append(name)
            raise AssertionError(f"external boundary called: {name}")
        return fail

    asyncio.open_connection = denied("asyncio.open_connection")
    http.client.HTTPConnection.connect = denied("http.client.HTTPConnection.connect")
    http.client.HTTPSConnection.connect = denied("http.client.HTTPSConnection.connect")
    socket.create_connection = denied("socket.create_connection")
    socket.getaddrinfo = denied("socket.getaddrinfo")
    socket.socket.connect = denied("socket.socket.connect")
    subprocess.Popen = denied("subprocess.Popen")
    urllib.request.urlopen = denied("urllib.request.urlopen")

    from glodex.api import create_app
    from glodex.cli import DEMO_QUERY, main

    assert not any(
        name == "glodex.capture" or name.startswith("glodex.capture.")
        for name in sys.modules
    )
    assert main(["demo"]) == 0

    app = create_app()

    async def exercise():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=True)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
                trust_env=False,
            ) as client:
                accepted = await client.post(
                    "/api/v1/runs",
                    json={
                        "thread_id": "thread-m1b-ac-001",
                        "request": {
                            "query": DEMO_QUERY,
                            "locale": "zh-CN",
                            "display_currency": "USD",
                            "top_k": 3,
                            "snapshot_version": "m0-v1",
                        },
                    },
                )
                assert accepted.status_code == 202
                accepted_payload = accepted.json()
                await app.state.coordinator.wait_idle()
                status = await client.get(accepted_payload["status_url"])
                events = await client.get(
                    accepted_payload["events_url"],
                    headers={"accept": "text/event-stream"},
                )
                assert status.status_code == 200
                assert events.status_code == 200
                assert "RUN_STARTED" in events.text
                assert "RUN_FINISHED" in events.text
                return status.json()["state"]

    state = asyncio.run(exercise())
    capture_modules = sorted(
        name
        for name in sys.modules
        if name == "glodex.capture" or name.startswith("glodex.capture.")
    )
    assert external_calls == []
    assert capture_modules == []
    print(
        json.dumps(
            {
                "api_state": state,
                "capture_modules": capture_modules,
                "external_calls": external_calls,
            },
            sort_keys=True,
        )
    )
    """
)


def _sanitized_environment(predicate: Callable[[str], bool]) -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if not predicate(name) and not name.upper().startswith("GLODEX_")
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    return environment


def test_m1b_ac_001_normal_cli_api_and_sse_never_load_capture(
    external_environment_variable_predicate: Callable[[str], bool],
) -> None:
    completed = subprocess.run(
        [sys.executable, "-c", _NORMAL_STACK_PROBE],
        cwd=PROJECT_ROOT,
        env=_sanitized_environment(external_environment_variable_predicate),
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    lines = completed.stdout.splitlines()
    assert len(lines) == 2
    cli_payload = cast(dict[str, Any], json.loads(lines[0]))
    probe = cast(dict[str, Any], json.loads(lines[1]))
    assert cli_payload["status"] == "COMPLETED"
    assert probe["api_state"] in {"COMPLETED", "NO_MATCH"}
    assert probe["capture_modules"] == []
    assert probe["external_calls"] == []


def test_m1b_ac_006_default_gate_has_exact_active_offline_coverage(
    external_environment_variable_predicate: Callable[[str], bool],
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/check_traceability.py",
            "--profile",
            "m1b",
            "--mode",
            "coverage",
        ],
        cwd=PROJECT_ROOT,
        env=_sanitized_environment(external_environment_variable_predicate),
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    report = cast(dict[str, Any], json.loads(completed.stdout))
    assert report["valid"] is True
    assert report["reference_issues"] == []
    assert report["coverage_issues"] == []
    assert frozenset(cast(dict[str, object], report["matrix"])) == _EXPECTED_M1B_IDS
