"""M1 acceptance scenarios for isolation, bounds, recovery, and secrecy."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from glodex.api import create_app
from glodex.api.settings import ApiSettings
from glodex.application.journal import RunJournal, StageResult
from glodex.application.ports import RunEventObserver
from glodex.application.search_service import SearchExecution
from glodex.contracts import RunStatus, SearchRequest, SearchResponse

pytestmark = pytest.mark.acceptance

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
SSE_ACCEPT = "text/event-stream"
FINGERPRINT = "b" * 64
BASE_QUERY = "推荐一台轻薄笔记本"


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


class _TerminalService:
    def __init__(
        self,
        *,
        expected_starts: int = 1,
        blocked: bool = False,
        degraded_stage: bool = False,
    ) -> None:
        self.expected_starts = expected_starts
        self.degraded_stage = degraded_stage
        self.calls: list[tuple[str, SearchRequest]] = []
        self.executions: dict[str, SearchExecution] = {}
        self.ready = asyncio.Event()
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()

    async def execute_run(
        self,
        request: SearchRequest,
        *,
        run_id: str,
        observer: RunEventObserver | None = None,
    ) -> SearchExecution:
        self.calls.append((run_id, request))
        if len(self.calls) == self.expected_starts:
            self.ready.set()

        snapshot_version = request.snapshot_version or "m0-v1"
        journal = RunJournal.new(
            run_id=run_id,
            snapshot_version=snapshot_version,
        ).start(NOW)
        _notify(observer, journal)
        await self.release.wait()

        journal = journal.stage_started("ranking", NOW)
        _notify(observer, journal)
        if self.degraded_stage:
            journal = journal.stage_degraded(
                "ranking",
                NOW,
                result=StageResult(
                    before=1,
                    after=1,
                    degraded=True,
                    issue_codes=("ranking.degraded",),
                ),
            )
            _notify(observer, journal)
        journal = journal.stage_completed(
            "ranking",
            NOW,
            duration_ms=0,
            result=StageResult(before=1, after=1),
        )
        _notify(observer, journal)
        journal = journal.finish(RunStatus.NO_MATCH, NOW)

        response = SearchResponse(
            run_id=run_id,
            status=RunStatus.NO_MATCH,
            snapshot_version=snapshot_version,
            config_fingerprint=FINGERPRINT,
            algorithm_version="acceptance-double-v1",
        )
        execution = SearchExecution(response=response, journal=journal)
        self.executions[run_id] = execution
        return execution


class _ExplodingService:
    def __init__(self, message: str) -> None:
        self.message = message
        self.calls = 0

    async def execute_run(
        self,
        request: SearchRequest,
        *,
        run_id: str,
        observer: RunEventObserver | None = None,
    ) -> SearchExecution:
        del request
        self.calls += 1
        journal = RunJournal.new(
            run_id=run_id,
            snapshot_version="m0-v1",
        ).start(NOW)
        _notify(observer, journal)
        raise RuntimeError(self.message)


class _NoCallService:
    async def execute_run(
        self,
        request: SearchRequest,
        *,
        run_id: str,
        observer: RunEventObserver | None = None,
    ) -> SearchExecution:
        del request, run_id, observer
        raise AssertionError("fresh process lookup must not execute business logic")


def _notify(observer: RunEventObserver | None, journal: RunJournal) -> None:
    if observer is not None:
        observer.on_event(journal.events[-1])


def _payload(
    thread_id: str,
    *,
    query: str = BASE_QUERY,
    snapshot_version: str = "m0-v1",
) -> dict[str, Any]:
    return {
        "thread_id": thread_id,
        "request": {
            "query": query,
            "locale": "zh-CN",
            "display_currency": "USD",
            "top_k": 3,
            "snapshot_version": snapshot_version,
        },
    }


def _app(
    *,
    service: _TerminalService | _ExplodingService | _NoCallService,
    run_ids: _RunIds,
    settings: ApiSettings | None = None,
) -> FastAPI:
    return create_app(
        settings=settings or ApiSettings(),
        service=service,
        clock=_Clock(),
        run_id_provider=run_ids,
        thread_id_factory=lambda: "thread-generated-acceptance",
    )


async def _wait(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), timeout=1)


def _sse_events(body: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    for block in normalized.split("\n\n"):
        fields: dict[str, list[str]] = {}
        for line in block.splitlines():
            if not line or line.startswith(":"):
                continue
            name, separator, value = line.partition(":")
            if not separator:
                continue
            fields.setdefault(name, []).append(value.removeprefix(" "))
        if "data" not in fields:
            continue
        payload = json.loads("\n".join(fields["data"]))
        assert isinstance(payload, dict)
        events.append(payload)
    return events


@dataclass(slots=True)
class _LiveStream:
    task: asyncio.Task[None]
    disconnect: asyncio.Event
    messages: list[dict[str, Any]]


async def _open_live_stream(app: FastAPI, run_id: str) -> _LiveStream:
    response_started = asyncio.Event()
    disconnect = asyncio.Event()
    request_sent = False
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {
                "type": "http.request",
                "body": b"",
                "more_body": False,
            }
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(dict(message))
        if message["type"] == "http.response.start":
            response_started.set()

    path = f"/api/v1/runs/{run_id}/events"
    task = asyncio.create_task(
        app(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": path,
                "raw_path": path.encode(),
                "query_string": b"",
                "root_path": "",
                "headers": [
                    (b"accept", SSE_ACCEPT.encode()),
                    (b"host", b"testserver"),
                ],
                "client": ("127.0.0.1", 12345),
                "server": ("testserver", 80),
            },
            receive,
            send,
        )
    )
    await _wait(response_started)
    return _LiveStream(task=task, disconnect=disconnect, messages=messages)


async def _close_live_stream(stream: _LiveStream) -> None:
    stream.disconnect.set()
    await asyncio.wait_for(stream.task, timeout=1)


def _response_status(messages: list[dict[str, Any]]) -> int:
    response_start = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    return int(response_start["status"])


@pytest.mark.spec(
    "M1-AC-006",
    "GLO-M1-P0-002",
    "GLO-M1-P0-006",
    "GLO-M1-NFR-002",
    "GLO-M1-NFR-003",
    "GLO-M1-NFR-006",
    "GLO-M1-NFR-009",
)
def test_m1_ac_006_concurrent_threads_and_same_thread_race_are_isolated() -> None:
    same_query = "same Thread race"
    queries = (same_query, same_query, "isolated alpha", "isolated beta")
    thread_ids = ("thread-race", "thread-race", "thread-alpha", "thread-beta")
    snapshots = ("snap-race", "snap-race", "snap-alpha", "snap-beta")
    service = _TerminalService(expected_starts=3, blocked=True)
    app = _app(
        service=service,
        run_ids=_RunIds(
            [
                "run-concurrent-001",
                "run-concurrent-002",
                "run-concurrent-003",
                "run-concurrent-004",
            ]
        ),
    )

    async def exercise() -> tuple[
        list[httpx.Response],
        list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]],
    ]:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                created = await asyncio.gather(
                    *(
                        client.post(
                            "/api/v1/runs",
                            json=_payload(
                                thread_id,
                                query=query,
                                snapshot_version=snapshot,
                            ),
                        )
                        for thread_id, query, snapshot in zip(
                            thread_ids,
                            queries,
                            snapshots,
                            strict=True,
                        )
                    )
                )
                await _wait(service.ready)
                service.release.set()
                await app.state.coordinator.wait_idle()

                observations = []
                for response in created:
                    if response.status_code != 202:
                        continue
                    accepted: dict[str, Any] = response.json()
                    status = await client.get(accepted["status_url"])
                    stream = await client.get(
                        accepted["events_url"],
                        headers={"accept": SSE_ACCEPT},
                    )
                    assert status.status_code == 200
                    assert stream.status_code == 200
                    observations.append((accepted, status.json(), _sse_events(stream.text)))
                return created, observations

    created, observations = asyncio.run(exercise())

    assert sorted(response.status_code for response in created) == [202, 202, 202, 409]
    conflict = next(response for response in created if response.status_code == 409)
    assert conflict.json()["error"]["code"] == "RUN_ALREADY_ACTIVE"
    assert len(service.calls) == 3
    assert sum(request.query == same_query for _, request in service.calls) == 1
    assert {request.query for _, request in service.calls} == {
        same_query,
        "isolated alpha",
        "isolated beta",
    }

    assert len(observations) == 3
    expected_snapshots = {
        "thread-race": "snap-race",
        "thread-alpha": "snap-alpha",
        "thread-beta": "snap-beta",
    }
    for accepted, status, events in observations:
        run_id = accepted["run_id"]
        thread_id = accepted["thread_id"]
        assert status["thread_id"] == thread_id
        assert status["run_id"] == run_id
        assert status["response"]["run_id"] == run_id
        assert status["response"]["snapshot_version"] == expected_snapshots[thread_id]
        assert events
        assert events[-1]["type"] == "RUN_FINISHED"
        assert all(event["threadId"] == thread_id for event in events)
        assert all(event["runId"] == run_id for event in events)
        snapshot_event = next(event for event in events if event["type"] == "STATE_SNAPSHOT")
        assert snapshot_event["snapshot"]["response"] == status["response"]


@pytest.mark.spec(
    "M1-AC-007",
    "M1-AC-008",
    "GLO-M1-P0-003",
    "GLO-M1-P0-004",
    "GLO-M1-P0-006",
    "GLO-M1-P0-007",
    "GLO-M1-NFR-002",
    "GLO-M1-NFR-003",
    "GLO-M1-NFR-006",
    "GLO-M1-NFR-009",
)
def test_m1_ac_007_event_cap_degrades_projection_without_changing_response() -> None:
    run_id = "run-event-cap-001"
    service = _TerminalService(degraded_stage=True)
    app = _app(
        service=service,
        run_ids=_RunIds([run_id]),
        settings=ApiSettings(max_events_per_run=2),
    )

    async def exercise() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                accepted = await client.post(
                    "/api/v1/runs",
                    json=_payload("thread-event-cap"),
                )
                await app.state.coordinator.wait_idle()
                status = await client.get(f"/api/v1/runs/{run_id}")
                stream = await client.get(
                    f"/api/v1/runs/{run_id}/events",
                    headers={"accept": SSE_ACCEPT},
                )
                return accepted, status, stream

    accepted, status, stream = asyncio.run(exercise())

    assert accepted.status_code == 202
    assert status.status_code == 200
    expected = service.executions[run_id].response.model_dump(mode="json")
    status_body = status.json()
    assert status_body["state"] == "NO_MATCH"
    assert status_body["projection_status"] == "DEGRADED"
    assert status_body["last_event_id"] == f"{run_id}:2"
    assert status_body["response"] == expected
    assert status_body["error"] is None
    assert [event["sequence"] for event in _sse_events(stream.text)] == [1, 2]
    assert len(service.calls) == 1
    assert service.calls[0][0] == run_id


@pytest.mark.spec(
    "M1-AC-008",
    "GLO-M1-P0-003",
    "GLO-M1-P0-007",
    "GLO-M1-NFR-006",
    "GLO-M1-NFR-009",
)
def test_m1_ac_008_active_and_subscriber_capacity_do_not_evict_active_run() -> None:
    run_id = "run-bounded-001"
    service = _TerminalService(blocked=True)
    app = _app(
        service=service,
        run_ids=_RunIds([run_id, "run-over-capacity"]),
        settings=ApiSettings(
            max_active_runs=1,
            max_subscribers_per_run=1,
        ),
    )

    async def exercise() -> tuple[
        httpx.Response,
        httpx.Response,
        httpx.Response,
        httpx.Response,
    ]:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                accepted = await client.post(
                    "/api/v1/runs",
                    json=_payload("thread-bounded"),
                )
                await _wait(service.ready)
                live = await _open_live_stream(app, run_id)
                try:
                    assert _response_status(live.messages) == 200
                    subscriber_limit = await client.get(
                        f"/api/v1/runs/{run_id}/events",
                        headers={"accept": SSE_ACCEPT},
                    )
                    active_limit = await client.post(
                        "/api/v1/runs",
                        json=_payload("thread-over-capacity"),
                    )
                    still_active = await client.get(f"/api/v1/runs/{run_id}")
                finally:
                    await _close_live_stream(live)

                service.release.set()
                await app.state.coordinator.wait_idle()
                terminal = await client.get(f"/api/v1/runs/{run_id}")
                assert terminal.json()["state"] == "NO_MATCH"
                return accepted, subscriber_limit, active_limit, still_active

    accepted, subscriber_limit, active_limit, still_active = asyncio.run(exercise())

    assert accepted.status_code == 202
    assert subscriber_limit.status_code == 429
    assert subscriber_limit.json()["error"]["code"] == "CAPACITY_EXCEEDED"
    assert active_limit.status_code == 503
    assert active_limit.json()["error"]["code"] == "CAPACITY_EXCEEDED"
    assert still_active.status_code == 200
    assert still_active.json()["run_id"] == run_id
    assert still_active.json()["state"] == "RUNNING"
    assert len(service.calls) == 1


@pytest.mark.spec(
    "M1-AC-008",
    "M1-AC-009",
    "GLO-M1-P0-003",
    "GLO-M1-P0-004",
    "GLO-M1-P0-007",
    "GLO-M1-NFR-002",
    "GLO-M1-NFR-003",
    "GLO-M1-NFR-007",
    "GLO-M1-NFR-009",
)
def test_m1_ac_009_errors_restart_and_every_event_shape_are_safe(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "sk-live-secret-value"
    absolute_path = "/Users/alice/.ssh/id_ed25519"
    full_query = f"{secret} {absolute_path} Traceback complete request"
    success_run_id = "run-safe-success"
    aborted_run_id = "run-safe-aborted"

    success_service = _TerminalService(degraded_stage=True)
    success_run_ids = _RunIds([success_run_id])
    success_app = _app(service=success_service, run_ids=success_run_ids)
    exception_message = f"{full_query}: RuntimeError from private runner"
    exploding_service = _ExplodingService(exception_message)
    exploding_app = _app(
        service=exploding_service,
        run_ids=_RunIds([aborted_run_id]),
    )
    fresh_app = _app(service=_NoCallService(), run_ids=_RunIds([]))

    async def exercise() -> tuple[
        httpx.Response,
        httpx.Response,
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
            ) as client:
                malicious = _payload("thread-malicious", query=full_query)
                malicious["request"]["top_k"] = "3"
                malicious["private_blob"] = exception_message
                rejected = await client.post("/api/v1/runs", json=malicious)
                assert success_run_ids.calls == 0

                accepted = await client.post(
                    "/api/v1/runs",
                    json=_payload("thread-safe-success", query=full_query),
                )
                assert accepted.status_code == 202
                await success_app.state.coordinator.wait_idle()
                success_status = await client.get(
                    f"/api/v1/runs/{success_run_id}",
                )
                success_stream = await client.get(
                    f"/api/v1/runs/{success_run_id}/events",
                    headers={"accept": SSE_ACCEPT},
                )

        async with exploding_app.router.lifespan_context(exploding_app):
            transport = httpx.ASGITransport(
                app=exploding_app,
                raise_app_exceptions=False,
            )
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                accepted = await client.post(
                    "/api/v1/runs",
                    json=_payload("thread-safe-aborted", query=full_query),
                )
                assert accepted.status_code == 202
                await exploding_app.state.coordinator.wait_idle()
                aborted_status = await client.get(
                    f"/api/v1/runs/{aborted_run_id}",
                )
                aborted_stream = await client.get(
                    f"/api/v1/runs/{aborted_run_id}/events",
                    headers={"accept": SSE_ACCEPT},
                )

        async with fresh_app.router.lifespan_context(fresh_app):
            transport = httpx.ASGITransport(
                app=fresh_app,
                raise_app_exceptions=False,
            )
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                missing_after_restart = await client.get(
                    f"/api/v1/runs/{aborted_run_id}",
                )

        return (
            rejected,
            success_status,
            success_stream,
            aborted_status,
            aborted_stream,
            missing_after_restart,
        )

    (
        rejected,
        success_status,
        success_stream,
        aborted_status,
        aborted_stream,
        missing_after_restart,
    ) = asyncio.run(exercise())

    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "REQUEST_REJECTED"
    assert success_status.json()["state"] == "NO_MATCH"
    assert aborted_status.json()["state"] == "ABORTED"
    assert aborted_status.json()["response"] is None
    assert aborted_status.json()["error"]["code"] == "RUN_ABORTED"
    assert missing_after_restart.status_code == 404
    assert missing_after_restart.json()["error"]["code"] == "RUN_NOT_FOUND_OR_EXPIRED"
    assert exploding_service.calls == 1

    success_events = _sse_events(success_stream.text)
    aborted_events = _sse_events(aborted_stream.text)
    assert {event["type"] for event in [*success_events, *aborted_events]} == {
        "RUN_STARTED",
        "STEP_STARTED",
        "STEP_DEGRADED",
        "STEP_FINISHED",
        "STATE_SNAPSHOT",
        "RUN_FINISHED",
        "RUN_ERROR",
    }
    allowed_fields = {
        "RUN_STARTED": {
            "type",
            "schemaVersion",
            "threadId",
            "runId",
            "sequence",
            "timestamp",
        },
        "STEP_STARTED": {
            "type",
            "schemaVersion",
            "threadId",
            "runId",
            "sequence",
            "timestamp",
            "stepName",
        },
        "STEP_DEGRADED": {
            "type",
            "schemaVersion",
            "threadId",
            "runId",
            "sequence",
            "timestamp",
            "stepName",
            "issueCodes",
        },
        "STEP_FINISHED": {
            "type",
            "schemaVersion",
            "threadId",
            "runId",
            "sequence",
            "timestamp",
            "stepName",
        },
        "STATE_SNAPSHOT": {
            "type",
            "schemaVersion",
            "threadId",
            "runId",
            "sequence",
            "timestamp",
            "snapshot",
        },
        "RUN_FINISHED": {
            "type",
            "schemaVersion",
            "threadId",
            "runId",
            "sequence",
            "timestamp",
            "result",
        },
        "RUN_ERROR": {
            "type",
            "schemaVersion",
            "threadId",
            "runId",
            "sequence",
            "timestamp",
            "code",
            "message",
        },
    }
    for event in [*success_events, *aborted_events]:
        assert set(event) == allowed_fields[event["type"]]

    public_outputs = "\n".join(
        (
            rejected.text,
            success_status.text,
            success_stream.text,
            aborted_status.text,
            aborted_stream.text,
            missing_after_restart.text,
            caplog.text,
        )
    )
    assert secret not in public_outputs
    assert absolute_path not in public_outputs
    assert full_query not in public_outputs
    assert "traceback" not in public_outputs.lower()


@pytest.mark.spec(
    "M1-AC-010",
    "GLO-M1-P0-008",
    "GLO-M1-P0-009",
)
def test_m1_ac_010_process_local_api_and_sse_complete_with_zero_external_calls(
    monkeypatch: pytest.MonkeyPatch,
    pytestconfig: pytest.Config,
    external_environment_variable_predicate: Any,
) -> None:
    import os

    for name in tuple(os.environ):
        if name.startswith("GLODEX_") or external_environment_variable_predicate(name):
            monkeypatch.delenv(name, raising=False)

    external_calls: list[str] = []

    def deny_external_call(name: str) -> Any:
        def fail(*args: object, **kwargs: object) -> Any:
            del args, kwargs
            external_calls.append(name)
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
        "urllib.request.urlopen",
    ):
        monkeypatch.setattr(target, deny_external_call(target))

    run_id = "run-ac-010-offline"
    service = _TerminalService()
    app = _app(
        service=service,
        run_ids=_RunIds([run_id]),
    )

    async def exercise() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(
                app=app,
                raise_app_exceptions=False,
            )
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
                trust_env=False,
            ) as client:
                accepted = await client.post(
                    "/api/v1/runs",
                    json=_payload("thread-ac-010-offline"),
                )
                assert accepted.status_code == 202
                await app.state.coordinator.wait_idle()
                status = await client.get(f"/api/v1/runs/{run_id}")
                events = await client.get(
                    f"/api/v1/runs/{run_id}/events",
                    headers={"accept": SSE_ACCEPT},
                )
        return accepted, status, events

    accepted, status, events = asyncio.run(exercise())
    event_payloads = _sse_events(events.text)
    execution = service.executions[run_id]

    assert pytestconfig.getoption("--disable-socket") is True
    assert not any(
        name.startswith("GLODEX_") or external_environment_variable_predicate(name)
        for name in os.environ
    )
    assert accepted.json()["run_id"] == run_id
    assert status.status_code == 200
    assert status.json()["response"] == execution.response.model_dump(mode="json")
    assert event_payloads[-1]["type"] == "RUN_FINISHED"
    assert event_payloads[-1]["result"]["status"] == "NO_MATCH"
    assert len(service.calls) == 1
    assert external_calls == []
