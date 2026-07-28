"""C01 acceptance baseline for API equivalence and business terminal states."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI

from glodex.api import create_app
from glodex.api.contracts import ProjectionStatus, RunState, RunStatusResponse
from glodex.api.runtime import RunCoordinator
from glodex.api.settings import ApiSettings
from glodex.application.journal import RunJournal, StageResult
from glodex.application.ports import RunEventObserver
from glodex.application.search_service import SearchExecution, SearchService
from glodex.bootstrap import build_service
from glodex.config import GlodexConfig
from glodex.contracts import IssueSeverity, RunStatus, SearchRequest, SearchResponse

pytestmark = pytest.mark.acceptance

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT_ROOT = PROJECT_ROOT / "data" / "snapshots"
FIXED_NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
THREAD_ID = "thread-m1-acceptance-001"
COMPLETED_QUERY = "推荐 800 美元以内、有库存、适合出差的轻薄本"
NO_MATCH_QUERY = "预算 1 美元以内的笔记本"
FAILED_QUERY = "推荐笔记本"
SSE_ACCEPT = "text/event-stream"


@dataclass(frozen=True, slots=True)
class _FixedClock:
    """Make business durations independent of Registry clock reads."""

    def now_utc(self) -> datetime:
        return FIXED_NOW

    def monotonic_ns(self) -> int:
        return 7_000_000


@dataclass(slots=True)
class _FixedRunIds:
    run_id: str
    calls: int = 0

    def next_run_id(self) -> str:
        self.calls += 1
        return self.run_id


@dataclass(slots=True)
class _NoCallService:
    calls: int = 0

    async def execute_run(
        self,
        request: SearchRequest,
        *,
        run_id: str,
        observer: RunEventObserver | None = None,
    ) -> SearchExecution:
        del request, run_id, observer
        self.calls += 1
        raise AssertionError("invalid transport input must not execute a Run")


@dataclass(slots=True)
class _ProgressService:
    blocked: bool
    calls: int = 0
    progress_emitted: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    returned: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        if not self.blocked:
            self.release.set()

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
        ).start(FIXED_NOW)
        _notify(observer, journal)
        journal = journal.stage_started("ranking", FIXED_NOW)
        _notify(observer, journal)
        journal = journal.stage_completed(
            "ranking",
            FIXED_NOW,
            duration_ms=0,
            result=StageResult(before=1, after=1),
        )
        _notify(observer, journal)
        self.progress_emitted.set()
        await self.release.wait()

        journal = journal.finish(RunStatus.NO_MATCH, FIXED_NOW)
        response = SearchResponse(
            run_id=run_id,
            status=RunStatus.NO_MATCH,
            snapshot_version=snapshot_version,
            config_fingerprint="d" * 64,
            algorithm_version="acceptance-progress-double-v1",
        )
        execution = SearchExecution(response=response, journal=journal)
        self.returned.set()
        return execution


@dataclass(slots=True)
class _LiveSse:
    task: asyncio.Task[None]
    messages: list[dict[str, Any]]
    progress_seen: asyncio.Event
    disconnect: asyncio.Event


def _notify(observer: RunEventObserver | None, journal: RunJournal) -> None:
    if observer is not None:
        observer.on_event(journal.events[-1])


async def _wait_for(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), timeout=1)


def _sse_text(messages: list[dict[str, Any]]) -> str:
    return b"".join(
        bytes(message.get("body", b""))
        for message in messages
        if message["type"] == "http.response.body"
    ).decode()


def _sse_records(body: str) -> list[tuple[str, dict[str, Any]]]:
    records: list[tuple[str, dict[str, Any]]] = []
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    for block in normalized.split("\n\n"):
        fields: dict[str, list[str]] = {}
        for line in block.splitlines():
            if not line or line.startswith(":"):
                continue
            name, separator, value = line.partition(":")
            if separator:
                fields.setdefault(name, []).append(value.removeprefix(" "))
        if "id" not in fields or "data" not in fields:
            continue
        payload = json.loads("\n".join(fields["data"]))
        assert isinstance(payload, dict)
        records.append((fields["id"][0], payload))
    return records


def _open_live_sse(app: FastAPI, events_url: str) -> _LiveSse:
    request_sent = False
    messages: list[dict[str, Any]] = []
    progress_seen = asyncio.Event()
    disconnect = asyncio.Event()

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
        if b"STEP_FINISHED" in _sse_text(messages).encode():
            progress_seen.set()

    task = asyncio.create_task(
        app(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": events_url,
                "raw_path": events_url.encode(),
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
    return _LiveSse(
        task=task,
        messages=messages,
        progress_seen=progress_seen,
        disconnect=disconnect,
    )


def _config() -> GlodexConfig:
    return GlodexConfig(
        data_dir=SNAPSHOT_ROOT,
        default_snapshot="m0-v1",
        default_locale="zh-CN",
        default_currency="USD",
        default_top_k=3,
        fingerprint="a" * 64,
    )


def _request(
    query: str,
    *,
    snapshot_version: str = "m0-v1",
) -> SearchRequest:
    return SearchRequest(
        query=query,
        locale="zh-CN",
        display_currency="USD",
        top_k=3,
        snapshot_version=snapshot_version,
    )


def _real_stack(
    *,
    run_id: str,
) -> tuple[SearchService, FastAPI, _FixedRunIds]:
    config = _config()
    clock = _FixedClock()
    run_ids = _FixedRunIds(run_id)
    service = build_service(
        config,
        run_id_provider=run_ids,
        clock=clock,
    )
    app = create_app(
        settings=ApiSettings(),
        service=service,
        clock=clock,
        run_id_provider=run_ids,
        config=config,
    )
    return service, app, run_ids


async def _post_wait_get(
    app: FastAPI,
    search_request: SearchRequest,
) -> tuple[dict[str, Any], dict[str, Any], RunStatusResponse]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(
            app=app,
            raise_app_exceptions=True,
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            accepted = await client.post(
                "/api/v1/runs",
                json={
                    "thread_id": THREAD_ID,
                    "request": search_request.model_dump(mode="json"),
                },
            )
            assert accepted.status_code == 202
            accepted_payload = cast(dict[str, Any], accepted.json())

            coordinator = cast(RunCoordinator, app.state.coordinator)
            await coordinator.wait_idle()

            status_response = await client.get(
                cast(str, accepted_payload["status_url"]),
            )
            assert status_response.status_code == 200
            status_payload = cast(dict[str, Any], status_response.json())
            status = RunStatusResponse.model_validate_json(status_response.content)
    return accepted_payload, status_payload, status


@pytest.mark.spec("M1-AC-001")
def test_m1_ac_001_api_and_explicit_run_execution_return_identical_response() -> None:
    async def exercise() -> tuple[
        SearchExecution,
        dict[str, Any],
        dict[str, Any],
        RunStatusResponse,
        _FixedRunIds,
    ]:
        run_id = "run-m1-ac-001"
        request = _request(COMPLETED_QUERY)
        service, app, run_ids = _real_stack(run_id=run_id)

        direct = await service.execute_run(
            request,
            run_id=run_id,
        )
        assert run_ids.calls == 0
        accepted, status_payload, status = await _post_wait_get(app, request)
        return direct, accepted, status_payload, status, run_ids

    direct, accepted_payload, status_payload, api_status, run_ids = asyncio.run(exercise())

    assert run_ids.calls == 1
    assert accepted_payload["run_id"] == direct.response.run_id
    assert accepted_payload["state"] == RunState.ACCEPTED
    assert api_status.state is RunState.COMPLETED
    assert api_status.response == direct.response
    assert status_payload["response"] == direct.response.model_dump(mode="json")
    assert all(stage.duration_ms == 0 for stage in direct.response.diagnostics.stages)


@pytest.mark.parametrize(
    ("query", "snapshot_version", "expected_status"),
    [
        pytest.param(
            COMPLETED_QUERY,
            "m0-v1",
            RunStatus.COMPLETED,
            id="completed",
        ),
        pytest.param(
            NO_MATCH_QUERY,
            "m0-v1",
            RunStatus.NO_MATCH,
            id="no-match",
        ),
        pytest.param(
            FAILED_QUERY,
            "missing-v1",
            RunStatus.FAILED,
            id="failed",
        ),
    ],
)
@pytest.mark.spec("M1-AC-005")
def test_m1_ac_005_real_service_preserves_each_business_terminal_response_shape(
    query: str,
    snapshot_version: str,
    expected_status: RunStatus,
) -> None:
    run_id = f"run-m1-ac-005-{expected_status.value.lower()}"
    _, app, run_ids = _real_stack(run_id=run_id)

    accepted_payload, status_payload, status = asyncio.run(
        _post_wait_get(
            app,
            _request(query, snapshot_version=snapshot_version),
        )
    )

    assert run_ids.calls == 1
    assert accepted_payload["run_id"] == run_id
    assert status.state.value == expected_status.value
    assert status.projection_status is ProjectionStatus.OK
    assert status.response is not None
    assert status.response.run_id == run_id
    assert status.response.status is expected_status
    assert status.error is None
    assert status.last_event_id is not None
    assert status_payload == status.model_dump(mode="json")

    if expected_status is RunStatus.COMPLETED:
        assert status.response.results
    else:
        assert status.response.results == ()
    if expected_status is RunStatus.FAILED:
        assert any(
            issue.code == "catalog.manifest-missing" and issue.severity is IssueSeverity.ERROR
            for issue in status.response.warnings
        )


@pytest.mark.spec("M1-AC-002")
def test_m1_ac_002_invalid_request_creates_no_run_or_service_call() -> None:
    async def exercise() -> tuple[httpx.Response, _NoCallService, _FixedRunIds]:
        config = _config()
        clock = _FixedClock()
        run_ids = _FixedRunIds("run-m1-ac-002")
        service = _NoCallService()
        app = create_app(
            settings=ApiSettings(),
            service=service,
            clock=clock,
            run_id_provider=run_ids,
            config=config,
        )

        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(
                app=app,
                raise_app_exceptions=True,
            )
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                response = await client.post(
                    "/api/v1/runs",
                    json={
                        "thread_id": THREAD_ID,
                        "request": {
                            "query": COMPLETED_QUERY,
                            "locale": "zh-CN",
                            "display_currency": "USD",
                            "top_k": "3",
                            "snapshot_version": "m0-v1",
                        },
                    },
                )
        return response, service, run_ids

    response, service, run_ids = asyncio.run(exercise())

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_REJECTED"
    assert service.calls == 0
    assert run_ids.calls == 0


@pytest.mark.spec("M1-AC-003")
def test_m1_ac_003_sse_exposes_progress_after_202_and_before_terminal_state() -> None:
    run_id = "run-m1-ac-003"
    service = _ProgressService(blocked=True)
    clock = _FixedClock()
    run_ids = _FixedRunIds(run_id)
    app = create_app(
        settings=ApiSettings(),
        service=service,
        clock=clock,
        run_id_provider=run_ids,
        config=_config(),
    )

    async def exercise() -> tuple[
        dict[str, Any],
        dict[str, Any],
        list[tuple[str, dict[str, Any]]],
        bool,
        dict[str, Any],
    ]:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(
                app=app,
                raise_app_exceptions=True,
            )
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                accepted_response = await client.post(
                    "/api/v1/runs",
                    json={
                        "thread_id": "thread-m1-ac-003",
                        "request": _request(COMPLETED_QUERY).model_dump(mode="json"),
                    },
                )
                assert accepted_response.status_code == 202
                accepted = cast(dict[str, Any], accepted_response.json())
                await _wait_for(service.progress_emitted)

                live = _open_live_sse(app, cast(str, accepted["events_url"]))
                try:
                    await _wait_for(live.progress_seen)
                    running_response = await client.get(
                        cast(str, accepted["status_url"]),
                    )
                    assert running_response.status_code == 200
                    running = cast(dict[str, Any], running_response.json())
                    progress_records = _sse_records(_sse_text(live.messages))
                    returned_before_release = service.returned.is_set()

                    service.release.set()
                    coordinator = cast(RunCoordinator, app.state.coordinator)
                    await coordinator.wait_idle()
                    await asyncio.wait_for(live.task, timeout=1)
                finally:
                    service.release.set()
                    if not live.task.done():
                        live.disconnect.set()
                        await asyncio.wait_for(live.task, timeout=1)

                terminal_response = await client.get(
                    cast(str, accepted["status_url"]),
                )
                assert terminal_response.status_code == 200
                terminal = cast(dict[str, Any], terminal_response.json())
        return accepted, running, progress_records, returned_before_release, terminal

    accepted, running, progress_records, returned_before_release, terminal = asyncio.run(exercise())

    assert accepted["state"] == "ACCEPTED"
    assert running["state"] == "RUNNING"
    assert running["response"] is None
    assert returned_before_release is False
    assert [payload["type"] for _, payload in progress_records] == [
        "RUN_STARTED",
        "STEP_STARTED",
        "STEP_FINISHED",
    ]
    assert all(payload["runId"] == run_id for _, payload in progress_records)
    assert [event_id for event_id, _ in progress_records] == [
        f"{run_id}:1",
        f"{run_id}:2",
        f"{run_id}:3",
    ]
    assert terminal["state"] == "NO_MATCH"
    assert service.calls == 1
    assert run_ids.calls == 1


@pytest.mark.spec("M1-AC-004")
def test_m1_ac_004_last_event_id_replays_exact_suffix_without_reexecution() -> None:
    run_id = "run-m1-ac-004"
    service = _ProgressService(blocked=False)
    clock = _FixedClock()
    run_ids = _FixedRunIds(run_id)
    app = create_app(
        settings=ApiSettings(),
        service=service,
        clock=clock,
        run_id_provider=run_ids,
        config=_config(),
    )

    async def exercise() -> tuple[
        list[tuple[str, dict[str, Any]]],
        list[tuple[str, dict[str, Any]]],
        dict[str, Any],
        dict[str, Any],
    ]:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(
                app=app,
                raise_app_exceptions=True,
            )
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                accepted_response = await client.post(
                    "/api/v1/runs",
                    json={
                        "thread_id": "thread-m1-ac-004",
                        "request": _request(COMPLETED_QUERY).model_dump(mode="json"),
                    },
                )
                assert accepted_response.status_code == 202
                accepted = cast(dict[str, Any], accepted_response.json())
                coordinator = cast(RunCoordinator, app.state.coordinator)
                await coordinator.wait_idle()

                before_response = await client.get(cast(str, accepted["status_url"]))
                first_stream = await client.get(
                    cast(str, accepted["events_url"]),
                    headers={"accept": SSE_ACCEPT},
                )
                full_records = _sse_records(first_stream.text)
                cursor = full_records[1][0]
                replay_stream = await client.get(
                    cast(str, accepted["events_url"]),
                    headers={
                        "accept": SSE_ACCEPT,
                        "last-event-id": cursor,
                    },
                )
                after_response = await client.get(cast(str, accepted["status_url"]))

        return (
            full_records,
            _sse_records(replay_stream.text),
            cast(dict[str, Any], before_response.json()),
            cast(dict[str, Any], after_response.json()),
        )

    full_records, replayed_records, before, after = asyncio.run(exercise())

    assert [event_id for event_id, _ in full_records] == [
        f"{run_id}:{sequence}" for sequence in range(1, 6)
    ]
    assert [payload["type"] for _, payload in full_records] == [
        "RUN_STARTED",
        "STEP_STARTED",
        "STEP_FINISHED",
        "STATE_SNAPSHOT",
        "RUN_FINISHED",
    ]
    assert replayed_records == full_records[2:]
    assert before == after
    assert before["state"] == "NO_MATCH"
    assert service.calls == 1
    assert run_ids.calls == 1
