"""Black-box M2d acceptance against the public fake durable client only."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from glodex.api.m2d_app import create_m2d_app
from glodex.api.m2d_durable_client import M2bPublicEventFrame
from tests.m2d.contract.test_m2d_app import _FakeDurableClient, _input, _request

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.spec(
        "M2D-AC-001",
        "M2D-AC-002",
        "M2D-AC-004",
        "M2D-AC-005",
        "GLO-M2D-P0-001",
        "GLO-M2D-P0-002",
        "GLO-M2D-P0-004",
        "GLO-M2D-P0-005",
    ),
]


def test_fake_durable_run_streams_safe_terminal_projection_and_replays_without_resubmit() -> None:
    durable = _FakeDurableClient()
    app = create_m2d_app(durable_client=durable)
    started = asyncio.run(
        _request(
            app,
            "POST",
            "/api/v1/m2d/ag-ui",
            headers={"accept": "text/event-stream", "content-type": "application/json"},
            json=_input(),
        )
    )
    replayed = asyncio.run(
        _request(
            app,
            "GET",
            "/api/v1/m2d/runs/durable-m2d-test/events",
            headers={"accept": "text/event-stream", "last-event-id": "durable-m2d-test:1"},
        )
    )

    assert started.status_code == 200
    assert started.text.count("RUN_STARTED") == 1
    assert started.text.count("RUN_FINISHED") == 1
    assert "Safe answer from M2b." in started.text
    assert "推荐轻薄本" not in started.text
    assert replayed.status_code == 200
    assert "RUN_STARTED" not in replayed.text
    assert replayed.text.count("RUN_FINISHED") == 1
    assert len(durable.create_payloads) == 1


def test_bad_source_and_unavailable_upstream_are_reduced_to_safe_m2d_codes() -> None:
    class BrokenDurable(_FakeDurableClient):
        async def _events(self, run_id: str) -> AsyncIterator[M2bPublicEventFrame]:
            yield self._bad_frame(run_id)

        def _bad_frame(self, run_id: str) -> M2bPublicEventFrame:
            return M2bPublicEventFrame(event_id=f"{run_id}:1", data='{"type":"UNKNOWN"}')

    response = asyncio.run(
        _request(
            create_m2d_app(durable_client=BrokenDurable()),
            "POST",
            "/api/v1/m2d/ag-ui",
            headers={"accept": "text/event-stream", "content-type": "application/json"},
            json=_input(),
        )
    )

    assert response.status_code == 200
    assert "M2D_PROJECTION_INVALID" in response.text
    assert "UNKNOWN" not in response.text


@pytest.mark.spec("GLO-M2D-P0-003", "GLO-M2D-P0-006", "M2D-AC-003", "M2D-AC-006")
def test_console_source_is_a_same_origin_agui_consumer_and_not_the_static_m1f_showcase() -> None:
    root = Path(__file__).resolve().parents[3]
    app_source = (root / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
    protocol_source = (root / "frontend" / "src" / "protocol.ts").read_text(encoding="utf-8")

    assert 'M2D_API_PREFIX = "/api/v1/m2d"' in protocol_source
    assert 'from "@ag-ui/core"' in protocol_source
    assert "consumeAgUiStream" in app_source
    assert "showcase" not in app_source
    assert "8765" not in app_source
    assert "8766" not in app_source
    assert "18000" not in app_source
