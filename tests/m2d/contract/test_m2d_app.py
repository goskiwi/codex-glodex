"""ASGI contract tests for the M2d public AG-UI adapter without a live M2b server."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from glodex.api.agent_events import AgentResultEvent, AgentStartedEvent
from glodex.api.durable_agent_contracts import (
    DurableCreateRunRequest,
    DurableRunAccepted,
    DurableRunStateDTO,
    DurableRunStatusResponse,
)
from glodex.api.m2d_app import create_m2d_app
from glodex.api.m2d_durable_client import M2bPublicEventFrame
from glodex.application.agent.contracts import (
    AgentAnswer,
    AgentAnswerKind,
    AgentDemoResponse,
    AgentEventScope,
)
from glodex.contracts import RunStatus

pytestmark = [
    pytest.mark.contract,
    pytest.mark.spec(
        "GLO-M2D-P0-001",
        "GLO-M2D-P0-002",
        "GLO-M2D-P0-004",
        "GLO-M2D-P0-005",
        "GLO-M2D-NFR-001",
        "GLO-M2D-NFR-002",
    ),
]


class _FakeDurableClient:
    def __init__(self) -> None:
        self.create_payloads: list[DurableCreateRunRequest] = []
        self.control_calls: list[str] = []
        self.response = AgentDemoResponse(
            run_id="durable-m2d-test",
            status=RunStatus.COMPLETED,
            answer=AgentAnswer(kind=AgentAnswerKind.CHAT_FALLBACK, text="Safe answer from M2b."),
        )

    async def create(self, payload: DurableCreateRunRequest) -> DurableRunAccepted:
        self.create_payloads.append(payload)
        return DurableRunAccepted(
            thread_id="thread-m2d-test",
            run_id="durable-m2d-test",
            status_url="/api/v1/durable-agent-runs/durable-m2d-test",
            events_url="/api/v1/durable-agent-runs/durable-m2d-test/events",
        )

    async def status(self, run_id: str) -> DurableRunStatusResponse:
        assert run_id == "durable-m2d-test"
        return DurableRunStatusResponse(
            thread_id="thread-m2d-test",
            run_id=run_id,
            state=DurableRunStateDTO.COMPLETED,
            last_event_id="durable-m2d-test:2",
            response=self.response,
        )

    async def cancel(self, run_id: str) -> DurableRunStatusResponse:
        self.control_calls.append(f"cancel:{run_id}")
        return await self.status(run_id)

    async def resume(self, run_id: str) -> DurableRunStatusResponse:
        self.control_calls.append(f"resume:{run_id}")
        return await self.status(run_id)

    async def _events(self, run_id: str) -> AsyncIterator[M2bPublicEventFrame]:
        assert run_id == "durable-m2d-test"
        events = (
            AgentStartedEvent(
                thread_id="thread-m2d-test",
                run_id=run_id,
                sequence=1,
                timestamp=1,
            ),
            AgentResultEvent(
                thread_id="thread-m2d-test",
                run_id=run_id,
                sequence=2,
                timestamp=2,
                scope=AgentEventScope.ROOT,
                status="COMPLETED",
            ),
        )
        for event in events:
            yield M2bPublicEventFrame(
                event_id=f"{run_id}:{event.sequence}",
                data=event.model_dump_json(by_alias=True),
            )

    def events(
        self,
        run_id: str,
        *,
        after: str | None = None,
    ) -> AsyncIterator[M2bPublicEventFrame]:
        assert after is None
        return self._events(run_id)


def _input() -> dict[str, object]:
    return {
        "threadId": "client-thread-m2d",
        "runId": "client-run-m2d",
        "state": {},
        "messages": [{"id": "msg-m2d", "role": "user", "content": "推荐轻薄本"}],
        "tools": [],
        "context": [],
        "forwardedProps": {"locale": "zh-CN", "displayCurrency": "USD", "topK": 3},
    }


async def _request(
    app: Any,
    method: str,
    path: str,
    **kwargs: Any,
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://m2d.test") as client:
        return await client.request(method, path, **kwargs)


def test_standard_agui_post_streams_safe_events_and_creates_once() -> None:
    fake = _FakeDurableClient()
    response = asyncio.run(
        _request(
            create_m2d_app(durable_client=fake),
            "POST",
            "/api/v1/m2d/ag-ui",
            headers={"accept": "text/event-stream", "content-type": "application/json"},
            json=_input(),
        )
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert len(fake.create_payloads) == 1
    assert fake.create_payloads[0].request.query == "推荐轻薄本"
    assert "RUN_STARTED" in response.text
    assert "RUN_FINISHED" in response.text
    assert "Safe answer from M2b." in response.text
    assert "client-run-m2d" not in response.text
    assert "推荐轻薄本" not in response.text


def test_reattach_and_controls_use_only_the_durable_public_client() -> None:
    fake = _FakeDurableClient()
    app = create_m2d_app(durable_client=fake)
    replay = asyncio.run(
        _request(
            app,
            "GET",
            "/api/v1/m2d/runs/durable-m2d-test/events",
            headers={"accept": "text/event-stream", "last-event-id": "durable-m2d-test:1"},
        )
    )
    cancel = asyncio.run(_request(app, "POST", "/api/v1/m2d/runs/durable-m2d-test/cancel"))
    resume = asyncio.run(_request(app, "POST", "/api/v1/m2d/runs/durable-m2d-test/resume"))

    assert replay.status_code == 200
    assert replay.text.count("RUN_STARTED") == 0
    assert replay.text.count("RUN_FINISHED") == 1
    assert cancel.status_code == 200
    assert resume.status_code == 200
    assert fake.control_calls == ["cancel:durable-m2d-test", "resume:durable-m2d-test"]


def test_rejects_non_sse_and_extra_input_before_creating_a_durable_run() -> None:
    fake = _FakeDurableClient()
    app = create_m2d_app(durable_client=fake)
    no_accept = asyncio.run(_request(app, "POST", "/api/v1/m2d/ag-ui", json=_input()))
    extra = asyncio.run(
        _request(
            app,
            "POST",
            "/api/v1/m2d/ag-ui",
            headers={"accept": "text/event-stream"},
            json=_input() | {"parentRunId": "forged"},
        )
    )

    assert no_accept.status_code == 422
    assert extra.status_code == 422
    assert len(fake.create_payloads) == 0
    assert "M2D_REQUEST_REJECTED" in no_accept.text
    assert "M2D_REQUEST_REJECTED" in extra.text


def test_console_root_requires_a_local_build_and_then_serves_only_local_assets(
    tmp_path: Path,
) -> None:
    missing = asyncio.run(_request(create_m2d_app(durable_client=_FakeDurableClient()), "GET", "/"))
    assets = tmp_path / "assets"
    assets.mkdir()
    (tmp_path / "index.html").write_text(
        "<!doctype html><title>M2d Console</title>",
        encoding="utf-8",
    )
    (assets / "app.js").write_text("window.__m2d = true;", encoding="utf-8")
    built = create_m2d_app(durable_client=_FakeDurableClient(), static_root=tmp_path)
    index = asyncio.run(_request(built, "GET", "/"))
    asset = asyncio.run(_request(built, "GET", "/assets/app.js"))

    assert missing.status_code == 503
    assert "npm run build" in missing.text
    assert index.status_code == 200
    assert "M2d Console" in index.text
    assert asset.status_code == 200
    assert "__m2d" in asset.text
