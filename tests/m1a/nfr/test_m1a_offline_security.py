"""Offline and information-disclosure gates for the M1a HTTP adapter."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from glodex.api import create_app
from glodex.application.journal import RunJournal
from glodex.application.ports import RunEventObserver
from glodex.application.search_service import SearchExecution
from glodex.contracts import RunStatus, SearchRequest, SearchResponse

pytestmark = [
    pytest.mark.nfr,
    pytest.mark.spec(
        "GLO-M1-P0-004",
        "GLO-M1-P0-007",
        "GLO-M1-P0-009",
        "GLO-M1-NFR-007",
        "GLO-M1-NFR-009",
        "GLO-M1-NFR-010",
    ),
]

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
FINGERPRINT = "c" * 64
SSE_ACCEPT = "text/event-stream"


@dataclass(slots=True)
class _Clock:
    monotonic_value: int = 0

    def now_utc(self) -> datetime:
        return NOW

    def monotonic_ns(self) -> int:
        self.monotonic_value += 1
        return self.monotonic_value


@dataclass(slots=True)
class _RunIds:
    values: list[str]
    calls: int = 0

    def next_run_id(self) -> str:
        if not self.values:
            raise AssertionError("unexpected Run ID allocation")
        self.calls += 1
        return self.values.pop(0)


class _OfflineService:
    def __init__(self) -> None:
        self.calls = 0

    async def execute_run(
        self,
        request: SearchRequest,
        *,
        run_id: str,
        observer: RunEventObserver | None = None,
    ) -> SearchExecution:
        self.calls += 1
        snapshot_version = request.snapshot_version or "m0-v1"
        journal = RunJournal.new(
            run_id=run_id,
            snapshot_version=snapshot_version,
        ).start(NOW)
        if observer is not None:
            observer.on_event(journal.events[-1])
        journal = journal.finish(RunStatus.NO_MATCH, NOW)
        response = SearchResponse(
            run_id=run_id,
            status=RunStatus.NO_MATCH,
            snapshot_version=snapshot_version,
            config_fingerprint=FINGERPRINT,
            algorithm_version="offline-security-double-v1",
        )
        return SearchExecution(response=response, journal=journal)


def _payload(*, thread_id: str | None, query: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "request": {
            "query": query,
            "locale": "zh-CN",
            "display_currency": "USD",
            "top_k": 3,
            "snapshot_version": "m0-v1",
        }
    }
    if thread_id is not None:
        payload["thread_id"] = thread_id
    return payload


def _sse_payloads(body: str) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for line in body.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        if not line.startswith("data:"):
            continue
        payload = json.loads(line.removeprefix("data:").lstrip())
        assert isinstance(payload, dict)
        payloads.append(payload)
    return payloads


def _block_external_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    calls: list[str] = []

    def denied(name: str) -> Callable[..., Any]:
        def fail(*args: object, **kwargs: object) -> Any:
            del args, kwargs
            calls.append(name)
            raise AssertionError(f"external boundary called: {name}")

        return fail

    for target in (
        "anyio.connect_tcp",
        "asyncio.open_connection",
        "http.client.HTTPConnection.connect",
        "http.client.HTTPSConnection.connect",
        "socket.create_connection",
        "socket.getaddrinfo",
        "sqlite3.connect",
        "subprocess.Popen",
        "urllib.request.urlopen",
    ):
        monkeypatch.setattr(target, denied(target))
    return calls


def test_api_errors_events_and_logs_are_safe_while_external_io_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
    pytestconfig: pytest.Config,
    caplog: pytest.LogCaptureFixture,
    external_environment_variable_predicate: Callable[[str], bool],
) -> None:
    secret = "sk-live-secret-must-never-escape"
    absolute_path = "/Users/alice/.config/provider/private.json"
    full_query = f"{secret} {absolute_path} complete private request"

    for name in tuple(os.environ):
        if name.startswith("GLODEX_") or external_environment_variable_predicate(name):
            monkeypatch.delenv(name, raising=False)

    external_calls = _block_external_boundaries(monkeypatch)
    service = _OfflineService()
    run_ids = _RunIds(["run-offline-security"])
    success_app = create_app(
        service=service,
        clock=_Clock(),
        run_id_provider=run_ids,
        thread_id_factory=lambda: "thread-offline-generated",
    )

    def exploding_thread_id_factory() -> str:
        raise RuntimeError(f"{secret}: {absolute_path}: private factory failure")

    error_app = create_app(
        service=_OfflineService(),
        clock=_Clock(),
        run_id_provider=_RunIds([]),
        thread_id_factory=exploding_thread_id_factory,
    )

    async def exercise() -> tuple[
        httpx.Response,
        httpx.Response,
        httpx.Response,
        httpx.Response,
    ]:
        async with success_app.router.lifespan_context(success_app):
            transport = httpx.ASGITransport(
                app=success_app,
                raise_app_exceptions=False,
            )
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
                trust_env=False,
            ) as client:
                invalid = _payload(thread_id="thread-invalid", query=full_query)
                invalid["request"]["top_k"] = "3"
                invalid[secret] = absolute_path
                rejected = await client.post("/api/v1/runs", json=invalid)

                accepted = await client.post(
                    "/api/v1/runs",
                    json=_payload(thread_id="thread-offline", query=full_query),
                )
                assert accepted.status_code == 202
                await success_app.state.coordinator.wait_idle()
                status = await client.get("/api/v1/runs/run-offline-security")
                events = await client.get(
                    "/api/v1/runs/run-offline-security/events",
                    headers={"accept": SSE_ACCEPT},
                )

        async with error_app.router.lifespan_context(error_app):
            transport = httpx.ASGITransport(
                app=error_app,
                raise_app_exceptions=False,
            )
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
                trust_env=False,
            ) as client:
                internal_error = await client.post(
                    "/api/v1/runs",
                    json=_payload(thread_id=None, query=full_query),
                )

        return rejected, internal_error, status, events

    rejected, internal_error, status, events = asyncio.run(exercise())
    event_payloads = _sse_payloads(events.text)

    assert pytestconfig.getoption("--disable-socket") is True
    assert not any(
        name.startswith("GLODEX_") or external_environment_variable_predicate(name)
        for name in os.environ
    )
    assert rejected.status_code == 422
    assert rejected.json()["schema_version"] == "glodex.error.v1"
    assert internal_error.status_code == 500
    assert internal_error.json() == {
        "schema_version": "glodex.error.v1",
        "type": "api_error",
        "error": {
            "code": "INTERNAL_SERVER_ERROR",
            "message": "Internal server error.",
            "field_errors": [],
        },
    }
    assert status.json()["state"] == "NO_MATCH"
    assert {payload["type"] for payload in event_payloads} == {
        "RUN_STARTED",
        "STATE_SNAPSHOT",
        "RUN_FINISHED",
    }
    assert all(payload["schemaVersion"] == "glodex.event.v1" for payload in event_payloads)
    assert service.calls == 1
    assert run_ids.calls == 1
    assert external_calls == []

    public_outputs = "\n".join(
        (
            rejected.text,
            internal_error.text,
            status.text,
            events.text,
            caplog.text,
        )
    )
    assert secret not in public_outputs
    assert absolute_path not in public_outputs
    assert full_query not in public_outputs
    assert "traceback" not in public_outputs.lower()
