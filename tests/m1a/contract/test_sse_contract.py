"""Versioned public event DTO contracts before transport integration."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import TypeAdapter, ValidationError
from starlette.types import Message, Scope

from glodex.api import create_app
from glodex.api.contracts import RunState
from glodex.api.events import (
    PublicEvent,
    RunErrorEvent,
    RunFinishedEvent,
    RunFinishedResult,
    RunStartedEvent,
    StepDegradedEvent,
    StepFinishedEvent,
)
from glodex.api.settings import ApiSettings
from glodex.application.journal import RunJournal, StageResult
from glodex.application.ports import RunEventObserver
from glodex.application.search_service import SearchExecution
from glodex.contracts import RunStatus, SearchRequest, SearchResponse

pytestmark = [
    pytest.mark.contract,
    pytest.mark.spec(
        "GLO-M1-P0-003",
        "GLO-M1-P0-004",
        "GLO-M1-P0-005",
        "GLO-M1-P0-006",
        "GLO-M1-P0-007",
        "GLO-M1-NFR-002",
        "GLO-M1-NFR-003",
        "GLO-M1-NFR-004",
        "GLO-M1-NFR-005",
        "GLO-M1-NFR-006",
        "GLO-M1-NFR-007",
        "GLO-M1-NFR-009",
    ),
]

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
RUN_ID = "run-sse-001"
THREAD_ID = "thread-sse-001"
SSE_ACCEPT = "text/event-stream"
CREATE_PAYLOAD = {
    "thread_id": THREAD_ID,
    "request": {
        "query": "推荐轻薄本",
        "locale": "zh-CN",
        "display_currency": "USD",
        "top_k": 3,
        "snapshot_version": "m0-v1",
    },
}


@dataclass(slots=True)
class _Clock:
    def now_utc(self) -> datetime:
        return NOW

    def monotonic_ns(self) -> int:
        return 0


@dataclass(slots=True)
class _RunIds:
    values: list[str]
    calls: int = 0

    def next_run_id(self) -> str:
        self.calls += 1
        if not self.values:
            raise AssertionError("unexpected run ID request")
        return self.values.pop(0)


def _terminal_execution(run_id: str) -> SearchExecution:
    journal = RunJournal.new(run_id=run_id, snapshot_version="m0-v1")
    journal = journal.start(NOW)
    journal = journal.stage_started("ranking", NOW)
    journal = journal.stage_degraded(
        "ranking",
        NOW,
        result=StageResult(
            before=2,
            after=2,
            degraded=True,
            issue_codes=("ranking.degraded",),
        ),
    )
    journal = journal.stage_completed(
        "ranking",
        NOW,
        duration_ms=0,
        result=StageResult(before=2, after=2),
    )
    journal = journal.finish(RunStatus.NO_MATCH, NOW)
    response = SearchResponse(
        run_id=run_id,
        status=RunStatus.NO_MATCH,
        snapshot_version="m0-v1",
        config_fingerprint="a" * 64,
        algorithm_version="phase-d-v1",
    )
    return SearchExecution(response=response, journal=journal)


class _TerminalService:
    def __init__(self) -> None:
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
        execution = _terminal_execution(run_id)
        if observer is not None:
            for event in execution.journal.events[:-1]:
                observer.on_event(event)
        return execution


class _BlockingService:
    def __init__(self) -> None:
        self.calls = 0
        self.release = asyncio.Event()

    async def execute_run(
        self,
        request: SearchRequest,
        *,
        run_id: str,
        observer: RunEventObserver | None = None,
    ) -> SearchExecution:
        del request, observer
        self.calls += 1
        await self.release.wait()
        return _terminal_execution(run_id)


class _LiveControlledService:
    def __init__(self) -> None:
        self.calls = 0
        self.live_emitted = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def execute_run(
        self,
        request: SearchRequest,
        *,
        run_id: str,
        observer: RunEventObserver | None = None,
    ) -> SearchExecution:
        del request
        self.calls += 1
        journal = RunJournal.new(run_id=run_id, snapshot_version="m0-v1")
        journal = journal.start(NOW)
        if observer is not None:
            observer.on_event(journal.events[-1])
        journal = journal.stage_started("ranking", NOW)
        if observer is not None:
            observer.on_event(journal.events[-1])
        journal = journal.stage_completed(
            "ranking",
            NOW,
            duration_ms=0,
            result=StageResult(before=2, after=2),
        )
        if observer is not None:
            observer.on_event(journal.events[-1])
        self.live_emitted.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        journal = journal.finish(RunStatus.NO_MATCH, NOW)
        response = SearchResponse(
            run_id=run_id,
            status=RunStatus.NO_MATCH,
            snapshot_version="m0-v1",
            config_fingerprint="a" * 64,
            algorithm_version="phase-d-v1",
        )
        return SearchExecution(response=response, journal=journal)


class _DirectSseHarness:
    def __init__(
        self,
        app: FastAPI,
        *,
        run_id: str,
        spec_version: str = "2.3",
    ) -> None:
        path = f"/api/v1/runs/{run_id}/events"
        self.app = app
        self.scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": spec_version},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"testserver"),
                (b"accept", b"text/event-stream"),
            ],
            "client": ("test-client", 1),
            "server": ("testserver", 80),
        }
        self.incoming: asyncio.Queue[Message] = asyncio.Queue()
        self.incoming.put_nowait({"type": "http.request", "body": b"", "more_body": False})
        self.body_chunks: asyncio.Queue[bytes] = asyncio.Queue()
        self.sent: list[Message] = []
        self.response_started = asyncio.Event()
        self.response_closed = asyncio.Event()
        self.finished = asyncio.Event()

    async def receive(self) -> Message:
        return await self.incoming.get()

    async def send(self, message: Message) -> None:
        self.sent.append(message)
        if message["type"] == "http.response.start":
            self.response_started.set()
            return
        if message["type"] != "http.response.body":
            return
        body = message.get("body", b"")
        if body:
            self.body_chunks.put_nowait(body)
        if not message.get("more_body", False):
            self.response_closed.set()

    async def run(self) -> None:
        try:
            await self.app(self.scope, self.receive, self.send)
        finally:
            self.finished.set()

    def disconnect(self) -> None:
        self.incoming.put_nowait({"type": "http.disconnect"})

    @property
    def body(self) -> str:
        return b"".join(
            message.get("body", b"")
            for message in self.sent
            if message["type"] == "http.response.body"
        ).decode("utf-8")

    async def wait_for_types(
        self,
        expected: set[str],
    ) -> list[tuple[str, dict[str, Any]]]:
        buffered = b""
        while True:
            buffered += await self.body_chunks.get()
            events = _sse_events(buffered.decode("utf-8"))
            if expected.issubset({payload["type"] for _, payload in events}):
                return events


def _terminal_app() -> tuple[FastAPI, _TerminalService]:
    service = _TerminalService()
    app = create_app(
        service=service,
        clock=_Clock(),
        run_id_provider=_RunIds([RUN_ID]),
        thread_id_factory=lambda: THREAD_ID,
    )
    return app, service


async def _create_run(client: httpx.AsyncClient) -> str:
    response = await client.post("/api/v1/runs", json=CREATE_PAYLOAD)
    assert response.status_code == 202
    body: dict[str, Any] = response.json()
    return str(body["run_id"])


def _sse_events(body: str) -> list[tuple[str, dict[str, Any]]]:
    parsed: list[tuple[str, dict[str, Any]]] = []
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    blocks = normalized.split("\n\n")
    if normalized and not normalized.endswith("\n\n"):
        blocks = blocks[:-1]
    for block in blocks:
        fields: dict[str, list[str]] = {}
        for line in block.splitlines():
            if not line or line.startswith(":"):
                continue
            name, separator, value = line.partition(":")
            if not separator:
                continue
            if value.startswith(" "):
                value = value[1:]
            fields.setdefault(name, []).append(value)
        if "data" not in fields:
            continue
        assert "id" in fields
        payload = json.loads("\n".join(fields["data"]))
        assert isinstance(payload, dict)
        parsed.append((fields["id"][-1], payload))
    return parsed


def test_event_serialization_is_strict_camel_case() -> None:
    event = StepFinishedEvent(
        thread_id="thread-test-001",
        run_id="run-test-001",
        sequence=4,
        timestamp=1_785_227_400_123,
        step_name="ranking",
    )

    assert event.model_dump(mode="json", by_alias=True) == {
        "type": "STEP_FINISHED",
        "schemaVersion": "glodex.event.v1",
        "threadId": "thread-test-001",
        "runId": "run-test-001",
        "sequence": 4,
        "timestamp": 1_785_227_400_123,
        "stepName": "ranking",
    }


def test_event_union_accepts_all_terminal_shapes_and_rejects_extensions() -> None:
    adapter = TypeAdapter(PublicEvent)
    started = adapter.validate_python(
        {
            "type": "RUN_STARTED",
            "schemaVersion": "glodex.event.v1",
            "threadId": "thread-test-001",
            "runId": "run-test-001",
            "sequence": 1,
            "timestamp": 1,
        }
    )
    assert isinstance(started, RunStartedEvent)

    finished = RunFinishedEvent(
        thread_id="thread-test-001",
        run_id="run-test-001",
        sequence=2,
        timestamp=2,
        result=RunFinishedResult(status=RunStatus.NO_MATCH),
    )
    assert finished.result.status is RunStatus.NO_MATCH

    error = RunErrorEvent(
        thread_id="thread-test-001",
        run_id="run-test-001",
        sequence=2,
        timestamp=2,
        code="RUN_ABORTED",
        message="Run execution was aborted.",
    )
    assert error.code == "RUN_ABORTED"

    with pytest.raises(ValidationError):
        adapter.validate_python(
            {
                "type": "RUN_STARTED",
                "schemaVersion": "glodex.event.v1",
                "threadId": "thread-test-001",
                "runId": "run-test-001",
                "sequence": "1",
                "timestamp": 1,
                "extra": True,
            }
        )


def test_step_names_and_issue_codes_are_not_rewritten() -> None:
    finished = StepFinishedEvent(
        thread_id="thread-test-001",
        run_id="run-test-001",
        sequence=1,
        timestamp=1,
        step_name="ranking",
    )
    degraded = StepDegradedEvent(
        thread_id="thread-test-001",
        run_id="run-test-001",
        sequence=2,
        timestamp=2,
        step_name="ranking",
        issue_codes=("RANKER_DEGRADED",),
    )

    assert finished.step_name == "ranking"
    assert degraded.step_name == "ranking"
    assert degraded.issue_codes == ("RANKER_DEGRADED",)


@pytest.mark.spec("M1-AC-005")
def test_terminal_stream_replays_complete_camel_case_sequence_and_closes() -> None:
    async def exercise() -> tuple[httpx.Response, int]:
        app, service = _terminal_app()
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            run_id = await _create_run(client)
            response = await client.get(
                f"/api/v1/runs/{run_id}/events",
                headers={"accept": SSE_ACCEPT},
            )
            await app.state.coordinator.wait_idle()
        return response, service.calls

    response, service_calls = asyncio.run(exercise())

    assert response.status_code == 200
    assert response.headers["content-type"].split(";", 1)[0] == SSE_ACCEPT
    assert response.is_stream_consumed
    events = _sse_events(response.text)
    assert [payload["type"] for _, payload in events] == [
        "RUN_STARTED",
        "STEP_STARTED",
        "STEP_DEGRADED",
        "STEP_FINISHED",
        "STATE_SNAPSHOT",
        "RUN_FINISHED",
    ]
    assert [payload["sequence"] for _, payload in events] == list(range(1, 7))
    for event_id, payload in events:
        assert event_id == f"{RUN_ID}:{payload['sequence']}"
        assert payload["schemaVersion"] == "glodex.event.v1"
        assert payload["threadId"] == THREAD_ID
        assert payload["runId"] == RUN_ID
        assert "schema_version" not in payload
        assert "thread_id" not in payload
        assert "run_id" not in payload
    assert events[2][1]["stepName"] == "ranking"
    assert events[2][1]["issueCodes"] == ["ranking.degraded"]
    assert events[-2][1]["snapshot"]["response"]["status"] == "NO_MATCH"
    assert events[-1][1]["result"]["status"] == "NO_MATCH"
    assert service_calls == 1


@pytest.mark.spec("M1-AC-004")
def test_last_event_id_replays_only_suffix_without_reexecuting_service() -> None:
    async def exercise() -> tuple[list[tuple[str, dict[str, Any]]], int]:
        app, service = _terminal_app()
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            run_id = await _create_run(client)
            full = await client.get(
                f"/api/v1/runs/{run_id}/events",
                headers={"accept": SSE_ACCEPT},
            )
            assert full.status_code == 200
            calls_after_completion = service.calls
            suffix = await client.get(
                f"/api/v1/runs/{run_id}/events",
                headers={
                    "accept": SSE_ACCEPT,
                    "last-event-id": f"{run_id}:3",
                },
            )
            empty_suffix = await client.get(
                f"/api/v1/runs/{run_id}/events",
                headers={
                    "accept": SSE_ACCEPT,
                    "last-event-id": f"{run_id}:6",
                },
            )
            await app.state.coordinator.wait_idle()
        assert empty_suffix.status_code == 200
        assert _sse_events(empty_suffix.text) == []
        assert service.calls == calls_after_completion
        return _sse_events(suffix.text), service.calls

    events, service_calls = asyncio.run(exercise())

    assert [payload["sequence"] for _, payload in events] == [4, 5, 6]
    assert [payload["type"] for _, payload in events] == [
        "STEP_FINISHED",
        "STATE_SNAPSHOT",
        "RUN_FINISHED",
    ]
    assert service_calls == 1


@pytest.mark.spec("M1-AC-009")
def test_invalid_or_duplicate_last_event_id_is_rejected_safely() -> None:
    async def exercise() -> tuple[list[httpx.Response], int]:
        app, service = _terminal_app()
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            run_id = await _create_run(client)
            completed = await client.get(
                f"/api/v1/runs/{run_id}/events",
                headers={"accept": SSE_ACCEPT},
            )
            assert completed.status_code == 200
            responses = [
                await client.get(
                    f"/api/v1/runs/{run_id}/events",
                    headers={
                        "accept": SSE_ACCEPT,
                        "last-event-id": cursor,
                    },
                )
                for cursor in (
                    "not-a-cursor",
                    "run-other:1",
                    f"{run_id}:7",
                )
            ]
            duplicate = client.build_request(
                "GET",
                f"/api/v1/runs/{run_id}/events",
                headers=[
                    ("accept", SSE_ACCEPT),
                    ("last-event-id", f"{run_id}:1"),
                    ("last-event-id", f"{run_id}:1"),
                ],
            )
            responses.append(await client.send(duplicate))
            await app.state.coordinator.wait_idle()
        return responses, service.calls

    responses, service_calls = asyncio.run(exercise())

    assert service_calls == 1
    for response in responses:
        assert response.status_code == 400
        body: dict[str, Any] = response.json()
        assert body["error"]["code"] == "INVALID_EVENT_CURSOR"
        assert "detail" not in body


@pytest.mark.spec("M1-AC-009")
def test_accept_requires_positive_exact_event_stream_media_range() -> None:
    async def exercise() -> tuple[list[httpx.Response], httpx.Response, int]:
        app, service = _terminal_app()
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            run_id = await _create_run(client)
            url = f"/api/v1/runs/{run_id}/events"
            missing_request = client.build_request("GET", url)
            del missing_request.headers["accept"]
            rejected = [await client.send(missing_request)]
            rejected.extend(
                [
                    await client.get(url, headers={"accept": value})
                    for value in (
                        "*/*",
                        "text/*",
                        "text/event-stream;q=0",
                        "application/x-text/event-stream",
                        "text/event-streaming",
                    )
                ]
            )
            accepted = await client.get(
                url,
                headers={
                    "accept": "application/json;q=0.9, TEXT/EVENT-STREAM; q=0.5",
                },
            )
            await app.state.coordinator.wait_idle()
        return rejected, accepted, service.calls

    rejected, accepted, service_calls = asyncio.run(exercise())

    for response in rejected:
        assert response.status_code == 406
        body: dict[str, Any] = response.json()
        assert body["error"]["code"] == "NOT_ACCEPTABLE"
    assert accepted.status_code == 200
    assert _sse_events(accepted.text)
    assert service_calls == 1


@pytest.mark.spec("M1-AC-008")
def test_active_run_subscriber_capacity_is_rejected_during_preflight() -> None:
    async def exercise() -> tuple[httpx.Response, int]:
        service = _BlockingService()
        app = create_app(
            settings=ApiSettings(max_subscribers_per_run=1),
            service=service,
            clock=_Clock(),
            run_id_provider=_RunIds([RUN_ID]),
            thread_id_factory=lambda: THREAD_ID,
        )
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            run_id = await _create_run(client)
            occupied = app.state.registry.subscribe(run_id, after_sequence=0)
            try:
                response = await client.get(
                    f"/api/v1/runs/{run_id}/events",
                    headers={"accept": SSE_ACCEPT},
                )
            finally:
                app.state.registry.unsubscribe(occupied)
                await app.state.coordinator.shutdown()
        return response, service.calls

    response, service_calls = asyncio.run(exercise())

    assert response.status_code == 429
    body: dict[str, Any] = response.json()
    assert body["error"]["code"] == "CAPACITY_EXCEEDED"
    assert "detail" not in body
    assert service_calls <= 1


@pytest.mark.spec("M1-AC-003")
def test_direct_asgi_stream_exposes_live_events_before_business_completion() -> None:
    async def exercise() -> tuple[
        list[tuple[str, dict[str, Any]]],
        RunState,
        int,
        bool,
    ]:
        service = _LiveControlledService()
        app = create_app(
            service=service,
            clock=_Clock(),
            run_id_provider=_RunIds([RUN_ID]),
            thread_id_factory=lambda: THREAD_ID,
        )
        accepted = app.state.coordinator.submit(
            SearchRequest(query="推荐轻薄本", snapshot_version="m0-v1"),
            thread_id=THREAD_ID,
        )
        harness = _DirectSseHarness(app, run_id=accepted.run_id)
        stream_task = asyncio.create_task(harness.run())
        try:
            await harness.response_started.wait()
            assert app.state.coordinator.release_start(accepted.run_id)
            await service.live_emitted.wait()
            live_events = await harness.wait_for_types({"RUN_STARTED", "STEP_FINISHED"})
            assert not service.release.is_set()
            assert app.state.registry.status(accepted.run_id).state is RunState.RUNNING

            service.release.set()
            await app.state.coordinator.wait_idle()
            await harness.finished.wait()
            await stream_task

            assert harness.response_closed.is_set()
            return (
                _sse_events(harness.body),
                app.state.registry.status(accepted.run_id).state,
                service.calls,
                any(payload["type"] == "STEP_FINISHED" for _, payload in live_events),
            )
        finally:
            service.release.set()
            if not harness.finished.is_set():
                harness.disconnect()
            await app.state.coordinator.shutdown()
            await asyncio.gather(stream_task, return_exceptions=True)

    events, terminal_state, service_calls, saw_live_step = asyncio.run(exercise())

    assert saw_live_step
    assert [payload["type"] for _, payload in events] == [
        "RUN_STARTED",
        "STEP_STARTED",
        "STEP_FINISHED",
        "STATE_SNAPSHOT",
        "RUN_FINISHED",
    ]
    assert [payload["sequence"] for _, payload in events] == [1, 2, 3, 4, 5]
    assert terminal_state is RunState.NO_MATCH
    assert service_calls == 1


@pytest.mark.spec("M1-AC-007")
def test_direct_asgi_disconnect_unsubscribes_without_cancelling_runner() -> None:
    async def exercise() -> tuple[RunState, bool, int]:
        service = _LiveControlledService()
        app = create_app(
            settings=ApiSettings(max_subscribers_per_run=1),
            service=service,
            clock=_Clock(),
            run_id_provider=_RunIds([RUN_ID]),
            thread_id_factory=lambda: THREAD_ID,
        )
        accepted = app.state.coordinator.submit(
            SearchRequest(query="推荐轻薄本", snapshot_version="m0-v1"),
            thread_id=THREAD_ID,
        )
        harness = _DirectSseHarness(
            app,
            run_id=accepted.run_id,
            spec_version="2.3",
        )
        stream_task = asyncio.create_task(harness.run())
        try:
            await harness.response_started.wait()
            assert app.state.coordinator.release_start(accepted.run_id)
            await service.live_emitted.wait()
            await harness.wait_for_types({"RUN_STARTED", "STEP_FINISHED"})

            harness.disconnect()
            await harness.finished.wait()
            await stream_task
            assert app.state.registry.status(accepted.run_id).state is RunState.RUNNING
            assert not service.cancelled.is_set()
            assert app.state.coordinator.in_flight_count == 1

            replacement = app.state.registry.subscribe(
                accepted.run_id,
                after_sequence=app.state.registry.next_sequence(accepted.run_id) - 1,
            )
            app.state.registry.unsubscribe(replacement)

            service.release.set()
            await app.state.coordinator.wait_idle()
            return (
                app.state.registry.status(accepted.run_id).state,
                service.cancelled.is_set(),
                service.calls,
            )
        finally:
            service.release.set()
            if not harness.finished.is_set():
                harness.disconnect()
            await app.state.coordinator.shutdown()
            await asyncio.gather(stream_task, return_exceptions=True)

    terminal_state, service_cancelled, service_calls = asyncio.run(exercise())

    assert terminal_state is RunState.NO_MATCH
    assert not service_cancelled
    assert service_calls == 1
