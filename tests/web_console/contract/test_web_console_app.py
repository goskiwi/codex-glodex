"""ASGI contract tests for the WebConsole public AG-UI adapter without a live Durable server."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from glodex.agent.contracts import (
    AgentAnswer,
    AgentAnswerKind,
    AgentDemoResponse,
    AgentEventScope,
)
from glodex.api.agent_events import AgentErrorEvent, AgentResultEvent, AgentStartedEvent
from glodex.api.console_app import create_web_console_app
from glodex.api.durable_client import DurablePublicEventFrame
from glodex.api.durable_contracts import (
    DurableCreateRunRequest,
    DurableRunAccepted,
    DurableRunStateDTO,
    DurableRunStatusResponse,
)
from glodex.contracts import (
    Diagnostics,
    EvidenceSummary,
    FilterSummary,
    MoneySummary,
    OfferSummary,
    RunStatus,
    SearchResponse,
    SearchResult,
)
from glodex.runtime.contracts import LoopKind

pytestmark = [
    pytest.mark.contract,
    pytest.mark.spec(
        "GLO-WEB_CONSOLE-P0-001",
        "GLO-WEB_CONSOLE-P0-002",
        "GLO-WEB_CONSOLE-P0-004",
        "GLO-WEB_CONSOLE-P0-005",
        "GLO-WEB_CONSOLE-NFR-001",
        "GLO-WEB_CONSOLE-NFR-002",
    ),
]


class _FakeDurableClient:
    def __init__(self, *, response: AgentDemoResponse | None = None) -> None:
        self.create_payloads: list[DurableCreateRunRequest] = []
        self.control_calls: list[str] = []
        self.response = response or AgentDemoResponse(
            run_id="durable-web_console-test",
            status=RunStatus.COMPLETED,
            answer=AgentAnswer(
                kind=AgentAnswerKind.CHAT_FALLBACK, text="Safe answer from Durable."
            ),
        )

    async def create(
        self,
        payload: DurableCreateRunRequest,
        *,
        cookie_header: str | None = None,
    ) -> DurableRunAccepted:
        self.create_payloads.append(payload)
        return DurableRunAccepted(
            thread_id="thread-web_console-test",
            run_id="durable-web_console-test",
            status_url="/api/v1/durable-agent-runs/durable-web_console-test",
            events_url="/api/v1/durable-agent-runs/durable-web_console-test/events",
        )

    async def status(
        self,
        run_id: str,
        *,
        cookie_header: str | None = None,
    ) -> DurableRunStatusResponse:
        assert run_id == "durable-web_console-test"
        return DurableRunStatusResponse(
            thread_id="thread-web_console-test",
            run_id=run_id,
            state=DurableRunStateDTO.COMPLETED,
            last_event_id="durable-web_console-test:2",
            response=self.response,
        )

    async def cancel(
        self,
        run_id: str,
        *,
        cookie_header: str | None = None,
    ) -> DurableRunStatusResponse:
        self.control_calls.append(f"cancel:{run_id}")
        return await self.status(run_id)

    async def resume(
        self,
        run_id: str,
        *,
        cookie_header: str | None = None,
    ) -> DurableRunStatusResponse:
        self.control_calls.append(f"resume:{run_id}")
        return await self.status(run_id)

    async def _events(self, run_id: str) -> AsyncIterator[DurablePublicEventFrame]:
        assert run_id == "durable-web_console-test"
        events = (
            AgentStartedEvent(
                thread_id="thread-web_console-test",
                run_id=run_id,
                root_run_id=run_id,
                parent_run_id=None,
                loop_kind=LoopKind.ROOT,
                run_depth=0,
                sequence=1,
                timestamp=1,
            ),
            AgentResultEvent(
                thread_id="thread-web_console-test",
                run_id=run_id,
                root_run_id=run_id,
                parent_run_id=None,
                loop_kind=LoopKind.ROOT,
                run_depth=0,
                sequence=2,
                timestamp=2,
                scope=AgentEventScope.ROOT,
                status="COMPLETED",
            ),
        )
        for event in events:
            yield DurablePublicEventFrame(
                event_id=f"{run_id}:{event.sequence}",
                data=event.model_dump_json(by_alias=True),
            )

    def events(
        self,
        run_id: str,
        *,
        after: str | None = None,
        cookie_header: str | None = None,
    ) -> AsyncIterator[DurablePublicEventFrame]:
        assert after is None
        return self._events(run_id)


def _input() -> dict[str, object]:
    return {
        "threadId": "client-thread-web_console",
        "runId": "client-run-web_console",
        "state": {},
        "messages": [{"id": "msg-web_console", "role": "user", "content": "推荐轻薄本"}],
        "tools": [],
        "context": [],
        "forwardedProps": {"locale": "zh-CN", "displayCurrency": "CNY", "topK": 3},
    }


_WS_HEADERS = {
    "origin": "http://127.0.0.1:8767",
    "cookie": "glodex_session=test-session",
}


def _receive_closed_run(websocket: Any) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    while True:
        frame = websocket.receive_json()
        frames.append(frame)
        if frame["type"] == "CLOSED":
            return frames


def _shopping_response_with_65_evidence_ids() -> AgentDemoResponse:
    results: list[SearchResult] = []
    for index, evidence_count in enumerate((22, 22, 21), start=1):
        cost = MoneySummary(
            currency="CNY",
            exact=str(1_000 + index),
            display=f"{1_000 + index}.00",
        )
        offer = OfferSummary(
            offer_id=f"offer-{index}",
            provider_id=f"provider-{index}",
            market="CN",
            landed_cost=cost,
        )
        results.append(
            SearchResult(
                product_id=f"product-{index}",
                title=f"Product {index}",
                category="laptop",
                selected_offer=offer,
                eligible_offers=(offer,),
                landed_cost=cost,
                reason="Verified fixture",
                evidence=tuple(
                    EvidenceSummary(
                        evidence_id=f"evidence-{index}-{evidence_index}",
                        provider_id="manufacturer-spec-lenovo",
                        source_uri="https://example.com/specification",
                        field_path=f"product.attributes.fact_{evidence_index}",
                        captured_at="2026-08-11T00:00:00+00:00",
                    )
                    for evidence_index in range(evidence_count)
                ),
            )
        )
    search = SearchResponse(
        run_id="durable-web_console-test",
        status=RunStatus.COMPLETED,
        snapshot_version="test-v1",
        config_fingerprint="a" * 64,
        algorithm_version="test-v1",
        results=tuple(results),
        filter_summary=FilterSummary(),
        diagnostics=Diagnostics(),
    )
    return AgentDemoResponse(
        run_id=search.run_id,
        status=RunStatus.COMPLETED,
        answer=AgentAnswer(
            kind=AgentAnswerKind.SHOPPING_SUMMARY,
            text=("FULL_COMPARISON_SENTINEL: 第1项,第2项,第3项;对应证据摘要;待确认;综合取舍。"),
        ),
        search_response=search,
        selected_product_ids=tuple(result.product_id for result in results),
        evidence_ids=tuple(
            evidence.evidence_id for result in results for evidence in result.evidence
        ),
    )


async def _request(
    app: Any,
    method: str,
    path: str,
    **kwargs: Any,
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://web_console.test") as client:
        return await client.request(method, path, **kwargs)


def test_websocket_start_streams_safe_agui_events_and_creates_once() -> None:
    fake = _FakeDurableClient()
    with (
        TestClient(create_web_console_app(durable_client=fake)) as client,
        client.websocket_connect(
            "/api/v1/web-console/ws",
            headers=_WS_HEADERS,
        ) as websocket,
    ):
        websocket.send_json({"type": "START", "input": _input()})
        frames = _receive_closed_run(websocket)

    assert frames[0]["type"] == "READY"
    assert frames[0]["state"]["state"] == "ACCEPTED"
    events = [frame["event"] for frame in frames if frame["type"] == "EVENT"]
    assert len(fake.create_payloads) == 1
    assert fake.create_payloads[0].request.query == "推荐轻薄本"
    assert sum(event["type"] == "RUN_STARTED" for event in events) == 1
    assert sum(event["type"] == "RUN_FINISHED" for event in events) == 1
    assert any(
        event["type"] == "STATE_SNAPSHOT"
        and event["snapshot"].get("terminal", {}).get("summary") == "Safe answer from Durable."
        for event in events
    )
    serialized = str(frames)
    assert "client-run-web_console" not in serialized
    assert "推荐轻薄本" not in serialized


def test_websocket_terminal_projection_keeps_the_existing_safe_shape() -> None:
    fake = _FakeDurableClient(response=_shopping_response_with_65_evidence_ids())
    with (
        TestClient(create_web_console_app(durable_client=fake)) as client,
        client.websocket_connect(
            "/api/v1/web-console/ws",
            headers=_WS_HEADERS,
        ) as websocket,
    ):
        websocket.send_json({"type": "START", "input": _input()})
        frames = _receive_closed_run(websocket)

    serialized = str(frames)
    assert serialized.count("RUN_FINISHED") == 1
    assert "glodex.web-console.ui-state.v6" in serialized
    assert "https://example.com/specification" in serialized
    assert "evidence-1-0" in serialized
    assert "FULL_COMPARISON_SENTINEL" not in serialized
    assert "Product 3" in serialized
    assert "WEB_CONSOLE_STREAM_INTERRUPTED" not in serialized


def test_websocket_attach_replays_only_after_the_committed_cursor() -> None:
    fake = _FakeDurableClient()
    app = create_web_console_app(durable_client=fake)
    with TestClient(app) as client:
        with client.websocket_connect(
            "/api/v1/web-console/ws",
            headers=_WS_HEADERS,
        ) as websocket:
            websocket.send_json({"type": "START", "input": _input()})
            _receive_closed_run(websocket)
        with client.websocket_connect(
            "/api/v1/web-console/ws",
            headers=_WS_HEADERS,
        ) as websocket:
            websocket.send_json(
                {
                    "type": "ATTACH",
                    "runId": "durable-web_console-test",
                    "afterCursor": "durable-web_console-test:1",
                }
            )
            frames = _receive_closed_run(websocket)

    events = [frame["event"] for frame in frames if frame["type"] == "EVENT"]
    assert frames[0]["type"] == "READY"
    assert all(event["type"] != "RUN_STARTED" for event in events)
    assert sum(event["type"] == "RUN_FINISHED" for event in events) == 1
    assert len(fake.create_payloads) == 1


def test_websocket_cancel_targets_only_the_active_run() -> None:
    class CancelableDurable(_FakeDurableClient):
        def __init__(self) -> None:
            super().__init__()
            self.cancelled = asyncio.Event()

        async def cancel(
            self,
            run_id: str,
            *,
            cookie_header: str | None = None,
        ) -> DurableRunStatusResponse:
            self.control_calls.append(f"cancel:{run_id}")
            self.cancelled.set()
            return DurableRunStatusResponse(
                thread_id="thread-web_console-test",
                run_id=run_id,
                state=DurableRunStateDTO.CANCEL_REQUESTED,
                last_event_id=f"{run_id}:1",
            )

        async def _events(self, run_id: str) -> AsyncIterator[DurablePublicEventFrame]:
            started = AgentStartedEvent(
                thread_id="thread-web_console-test",
                run_id=run_id,
                root_run_id=run_id,
                parent_run_id=None,
                loop_kind=LoopKind.ROOT,
                run_depth=0,
                sequence=1,
                timestamp=1,
            )
            yield DurablePublicEventFrame(
                event_id=f"{run_id}:1",
                data=started.model_dump_json(by_alias=True),
            )
            await self.cancelled.wait()
            cancelled = AgentErrorEvent(
                thread_id="thread-web_console-test",
                run_id=run_id,
                root_run_id=run_id,
                parent_run_id=None,
                loop_kind=LoopKind.ROOT,
                run_depth=0,
                sequence=2,
                timestamp=2,
                status="ABORTED",
                safe_code="USER_CANCELLED",
            )
            yield DurablePublicEventFrame(
                event_id=f"{run_id}:2",
                data=cancelled.model_dump_json(by_alias=True),
            )

    fake = CancelableDurable()
    with (
        TestClient(create_web_console_app(durable_client=fake)) as client,
        client.websocket_connect(
            "/api/v1/web-console/ws",
            headers=_WS_HEADERS,
        ) as websocket,
    ):
        websocket.send_json({"type": "START", "input": _input()})
        assert websocket.receive_json()["type"] == "READY"
        started_frames: list[dict[str, Any]] = []
        while not any(
            frame.get("event", {}).get("type") == "RUN_STARTED" for frame in started_frames
        ):
            started_frames.append(websocket.receive_json())
        websocket.send_json({"type": "CANCEL", "runId": "durable-web_console-test"})
        terminal_frames = _receive_closed_run(websocket)

    assert fake.control_calls == ["cancel:durable-web_console-test"]
    assert any(frame.get("event", {}).get("type") == "RUN_ERROR" for frame in terminal_frames)
    assert terminal_frames[-1] == {"type": "CLOSED", "code": "ABORTED"}


def test_websocket_rejects_missing_auth_unknown_and_binary_frames() -> None:
    app = create_web_console_app(durable_client=_FakeDurableClient())
    with TestClient(app) as client:
        with (
            pytest.raises(WebSocketDisconnect) as missing_auth,
            client.websocket_connect(
                "/api/v1/web-console/ws",
                headers={"origin": "http://127.0.0.1:8767"},
            ),
        ):
            pass
        assert missing_auth.value.code == 1008

        with (
            pytest.raises(WebSocketDisconnect) as invalid_origin,
            client.websocket_connect(
                "/api/v1/web-console/ws",
                headers={"origin": "http://attacker.invalid", "cookie": "local-session=opaque"},
            ),
        ):
            pass
        assert invalid_origin.value.code == 1008

        with client.websocket_connect(
            "/api/v1/web-console/ws",
            headers=_WS_HEADERS,
        ) as websocket:
            websocket.send_json({"type": "UNKNOWN"})
            assert websocket.receive_json() == {
                "type": "ERROR",
                "code": "WEB_CONSOLE_REQUEST_REJECTED",
            }
            assert websocket.receive_json()["type"] == "CLOSED"

        with client.websocket_connect(
            "/api/v1/web-console/ws",
            headers=_WS_HEADERS,
        ) as websocket:
            websocket.send_bytes(b"not-json")
            assert websocket.receive_json()["type"] == "ERROR"


def test_websocket_rejects_extra_input() -> None:
    fake = _FakeDurableClient()
    app = create_web_console_app(durable_client=fake)
    with (
        TestClient(app) as client,
        client.websocket_connect(
            "/api/v1/web-console/ws",
            headers=_WS_HEADERS,
        ) as websocket,
    ):
        websocket.send_json({"type": "START", "input": _input(), "parentRunId": "forged"})
        assert websocket.receive_json()["type"] == "ERROR"

    assert len(fake.create_payloads) == 0


def test_websocket_rejects_removed_snake_case_command_fields() -> None:
    app = create_web_console_app(durable_client=_FakeDurableClient())
    with (
        TestClient(app) as client,
        client.websocket_connect(
            "/api/v1/web-console/ws",
            headers=_WS_HEADERS,
        ) as websocket,
    ):
        websocket.send_json(
            {
                "type": "ATTACH",
                "run_id": "durable-web_console-test",
            }
        )
        assert websocket.receive_json() == {
            "type": "ERROR",
            "code": "WEB_CONSOLE_REQUEST_REJECTED",
        }
        assert websocket.receive_json()["type"] == "CLOSED"


def test_console_root_requires_a_local_build_and_then_serves_only_local_assets(
    tmp_path: Path,
) -> None:
    missing = asyncio.run(
        _request(create_web_console_app(durable_client=_FakeDurableClient()), "GET", "/")
    )
    assets = tmp_path / "assets"
    assets.mkdir()
    (tmp_path / "index.html").write_text(
        "<!doctype html><title>WebConsole Console</title>",
        encoding="utf-8",
    )
    (assets / "app.js").write_text("window.__web_console = true;", encoding="utf-8")
    built = create_web_console_app(durable_client=_FakeDurableClient(), static_root=tmp_path)
    index = asyncio.run(_request(built, "GET", "/"))
    asset = asyncio.run(_request(built, "GET", "/assets/app.js"))

    assert missing.status_code == 503
    assert "pnpm --dir frontend run build" in missing.text
    assert index.status_code == 200
    assert "WebConsole Console" in index.text
    assert asset.status_code == 200
    assert "__web_console" in asset.text
