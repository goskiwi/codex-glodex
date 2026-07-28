"""Strict HTTP contracts for the side-effect-free FastAPI composition root."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from glodex.api import create_app
from glodex.api.contracts import (
    ApiError,
    CreateRunRequest,
    ProjectionStatus,
    RunState,
    RunStatusResponse,
)
from glodex.api.settings import ApiSettings
from glodex.application.journal import RunJournal
from glodex.application.ports import RunEventObserver
from glodex.application.search_service import SearchExecution
from glodex.config import GlodexConfig
from glodex.contracts import RunStatus, SearchRequest, SearchResponse

pytestmark = [
    pytest.mark.contract,
    pytest.mark.spec(
        "GLO-M1-P0-001",
        "GLO-M1-P0-002",
        "GLO-M1-P0-003",
        "GLO-M1-P0-007",
        "GLO-M1-P0-008",
        "GLO-M1-NFR-001",
        "GLO-M1-NFR-002",
        "GLO-M1-NFR-004",
        "GLO-M1-NFR-006",
        "GLO-M1-NFR-007",
        "GLO-M1-NFR-008",
        "GLO-M1-NFR-009",
    ),
]

_NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_CONFIG_FINGERPRINT = "a" * 64
_TEST_CONFIG = GlodexConfig(
    data_dir=_PROJECT_ROOT / "data" / "snapshots",
    default_snapshot="m0-v1",
    default_locale="zh-CN",
    default_currency="USD",
    default_top_k=3,
    fingerprint=_CONFIG_FINGERPRINT,
)


@dataclass(slots=True)
class _FixedClock:
    monotonic_value: int = 0

    def now_utc(self) -> datetime:
        return _NOW

    def monotonic_ns(self) -> int:
        self.monotonic_value += 1
        return self.monotonic_value


@dataclass(slots=True)
class _SequenceRunIds:
    values: list[str]
    calls: int = 0

    def next_run_id(self) -> str:
        if self.calls >= len(self.values):
            raise AssertionError("unexpected run ID allocation")
        value = self.values[self.calls]
        self.calls += 1
        return value


@dataclass(slots=True)
class _SequenceThreadIds:
    values: list[str]
    calls: int = 0

    def __call__(self) -> str:
        if self.calls >= len(self.values):
            raise AssertionError("unexpected Thread ID allocation")
        value = self.values[self.calls]
        self.calls += 1
        return value


class _ExplodingRunIds:
    def __init__(self, secret: str) -> None:
        self.secret = secret
        self.calls = 0

    def next_run_id(self) -> str:
        self.calls += 1
        raise RuntimeError(self.secret)


@dataclass(slots=True)
class _ControlledService:
    blocked: bool = True
    timeline: list[str] | None = None
    calls: int = 0
    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    returned: asyncio.Event = field(default_factory=asyncio.Event)
    last_execution: SearchExecution | None = None

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
        if self.timeline is not None:
            self.timeline.append("service_execute")

        snapshot_version = request.snapshot_version or _TEST_CONFIG.default_snapshot
        journal = RunJournal.new(
            run_id=run_id,
            snapshot_version=snapshot_version,
        ).start(_NOW)
        if observer is not None:
            observer.on_event(journal.events[-1])
        self.started.set()
        await self.release.wait()

        journal = journal.finish(RunStatus.NO_MATCH, _NOW)
        response = SearchResponse(
            run_id=run_id,
            status=RunStatus.NO_MATCH,
            snapshot_version=snapshot_version,
            config_fingerprint=_CONFIG_FINGERPRINT,
            algorithm_version="contract-double-v1",
        )
        execution = SearchExecution(response=response, journal=journal)
        self.last_execution = execution
        self.returned.set()
        return execution


def _build_app(
    *,
    service: _ControlledService,
    run_ids: _SequenceRunIds | _ExplodingRunIds,
    thread_ids: _SequenceThreadIds | None = None,
    settings: ApiSettings | None = None,
) -> FastAPI:
    return create_app(
        settings=settings or ApiSettings(),
        service=service,
        clock=_FixedClock(),
        run_id_provider=run_ids,
        thread_id_factory=thread_ids or _SequenceThreadIds(["thread-generated-001"]),
        config=_TEST_CONFIG,
    )


async def _checkpoint() -> None:
    loop = asyncio.get_running_loop()
    reached = loop.create_future()
    loop.call_soon(reached.set_result, None)
    await reached


async def _wait_for_event(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), timeout=1)


async def _wait_for_state(
    client: httpx.AsyncClient,
    run_id: str,
    expected: RunState,
) -> httpx.Response:
    last: httpx.Response | None = None
    for _ in range(50):
        last = await client.get(f"/api/v1/runs/{run_id}")
        if last.status_code == 200 and last.json()["state"] == expected.value:
            return last
        await _checkpoint()
    raise AssertionError(
        f"Run {run_id!r} did not reach {expected.value}; "
        f"last response was {None if last is None else last.text}"
    )


def _assert_safe_error(
    response: httpx.Response,
    *,
    status_code: int,
    code: str,
    secret: str | None = None,
) -> dict[str, Any]:
    assert response.status_code == status_code
    body: dict[str, Any] = response.json()
    assert set(body) == {"schema_version", "type", "error"}
    assert body["schema_version"] == "glodex.error.v1"
    assert body["type"] == "api_error"
    assert set(body["error"]) == {"code", "message", "field_errors"}
    assert body["error"]["code"] == code
    assert "detail" not in response.text
    assert "traceback" not in response.text.lower()
    if secret is not None:
        assert secret not in response.text
    return body


async def _invoke_asgi(
    app: FastAPI,
    *,
    body_chunks: tuple[bytes, ...],
    headers: tuple[tuple[bytes, bytes], ...],
    timeline: list[str] | None = None,
) -> list[dict[str, Any]]:
    requests = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(body_chunks) - 1,
        }
        for index, chunk in enumerate(body_chunks)
    ]
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        if requests:
            return requests.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        copied = dict(message)
        messages.append(copied)
        if (
            timeline is not None
            and copied["type"] == "http.response.body"
            and not copied.get("more_body", False)
        ):
            timeline.append("final_response_body")

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/runs",
            "raw_path": b"/api/v1/runs",
            "query_string": b"",
            "root_path": "",
            "headers": list(headers),
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        },
        receive,
        send,
    )
    return messages


def _recorded_response(messages: list[dict[str, Any]]) -> tuple[int, dict[str, Any]]:
    start = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    return int(start["status"]), json.loads(body)


def test_api_settings_defaults_are_bounded_and_separate_from_m0_config() -> None:
    settings = ApiSettings()

    assert settings.model_dump() == {
        "max_active_runs": 16,
        "max_terminal_runs": 128,
        "terminal_ttl_seconds": 900,
        "max_events_per_run": 128,
        "max_subscribers_per_run": 4,
        "max_request_body_bytes": 16_384,
        "run_timeout_seconds": 30,
        "sse_heartbeat_seconds": 15,
    }


@pytest.mark.parametrize("value", [True, "1", 0, -1, 1.5])
def test_api_settings_reject_non_strict_positive_values(value: object) -> None:
    with pytest.raises(ValidationError):
        ApiSettings(max_active_runs=value)  # type: ignore[arg-type]


def test_create_run_request_reuses_strict_m0_contract(
    valid_create_run_payload: Callable[..., dict[str, Any]],
) -> None:
    request = CreateRunRequest.model_validate(valid_create_run_payload())

    assert request.thread_id == "thread-test-001"
    assert request.request.query.startswith("推荐")

    with pytest.raises(ValidationError):
        CreateRunRequest.model_validate(valid_create_run_payload(extra=True))
    with pytest.raises(ValidationError):
        CreateRunRequest.model_validate(valid_create_run_payload(thread_id="bad id"))
    with pytest.raises(ValidationError):
        CreateRunRequest.model_validate(
            valid_create_run_payload(
                request={
                    "query": "test",
                    "locale": "zh-CN",
                    "display_currency": "USD",
                    "top_k": "3",
                }
            )
        )


def test_run_resource_enforces_state_response_error_combinations() -> None:
    active = RunStatusResponse(
        thread_id="thread-test-001",
        run_id="run-test-001",
        state=RunState.ACCEPTED,
        projection_status=ProjectionStatus.OK,
    )
    assert active.response is None
    assert active.error is None

    aborted = RunStatusResponse(
        thread_id="thread-test-001",
        run_id="run-test-001",
        state=RunState.ABORTED,
        projection_status=ProjectionStatus.OK,
        error=ApiError(
            code="RUN_ABORTED",
            message="Run execution was aborted.",
        ),
    )
    assert aborted.error is not None
    assert aborted.error.code == "RUN_ABORTED"

    with pytest.raises(ValidationError):
        RunStatusResponse(
            thread_id="thread-test-001",
            run_id="run-test-001",
            state=RunState.RUNNING,
            projection_status=ProjectionStatus.OK,
            error=ApiError(code="RUN_ABORTED", message="Run execution was aborted."),
        )


def test_app_factory_registers_only_the_three_m1a_paths() -> None:
    app = create_app()
    schema = app.openapi()

    assert set(schema["paths"]) == {
        "/api/v1/runs",
        "/api/v1/runs/{run_id}",
        "/api/v1/runs/{run_id}/events",
    }


@pytest.mark.spec("M1-AC-001", "M1-AC-003", "M1-AC-005")
def test_create_run_returns_202_urls_and_canonical_status(
    valid_create_run_payload: Callable[..., dict[str, Any]],
) -> None:
    service = _ControlledService()
    run_ids = _SequenceRunIds(["run-http-001"])
    thread_ids = _SequenceThreadIds(["thread-unused-001"])
    app = _build_app(service=service, run_ids=run_ids, thread_ids=thread_ids)

    async def exercise() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                accepted = await client.post(
                    "/api/v1/runs",
                    json=valid_create_run_payload(),
                )
                await _wait_for_event(service.started)
                running = await client.get("/api/v1/runs/run-http-001")
                service.release.set()
                await _wait_for_event(service.returned)
                terminal = await _wait_for_state(
                    client,
                    "run-http-001",
                    RunState.NO_MATCH,
                )
        return accepted, running, terminal

    accepted, running, terminal = asyncio.run(exercise())

    assert accepted.status_code == 202
    assert accepted.json() == {
        "schema_version": "glodex.run.v1",
        "thread_id": "thread-test-001",
        "run_id": "run-http-001",
        "state": "ACCEPTED",
        "status_url": "/api/v1/runs/run-http-001",
        "events_url": "/api/v1/runs/run-http-001/events",
    }
    running_body = running.json()
    assert running.status_code == 200
    assert running_body["state"] == "RUNNING"
    assert running_body["response"] is None
    assert running_body["error"] is None

    execution = service.last_execution
    assert execution is not None
    terminal_body = terminal.json()
    assert terminal_body["thread_id"] == "thread-test-001"
    assert terminal_body["run_id"] == "run-http-001"
    assert terminal_body["state"] == "NO_MATCH"
    assert terminal_body["response"] == execution.response.model_dump(mode="json")
    assert terminal_body["error"] is None
    assert run_ids.calls == 1
    assert thread_ids.calls == 0


def test_create_run_without_thread_uses_injected_factory(
    valid_create_run_payload: Callable[..., dict[str, Any]],
) -> None:
    service = _ControlledService(blocked=False)
    run_ids = _SequenceRunIds(["run-generated-thread-001"])
    thread_ids = _SequenceThreadIds(["thread-generated-001"])
    app = _build_app(service=service, run_ids=run_ids, thread_ids=thread_ids)
    payload = valid_create_run_payload()
    payload.pop("thread_id")

    async def exercise() -> httpx.Response:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                accepted = await client.post("/api/v1/runs", json=payload)
                await _wait_for_event(service.returned)
                await _wait_for_state(
                    client,
                    "run-generated-thread-001",
                    RunState.NO_MATCH,
                )
                return accepted

    accepted = asyncio.run(exercise())

    assert accepted.status_code == 202
    assert accepted.json()["thread_id"] == "thread-generated-001"
    assert thread_ids.calls == 1
    assert run_ids.calls == 1
    assert service.calls == 1


@pytest.mark.spec("M1-AC-002", "M1-AC-009")
def test_strict_invalid_requests_have_no_pre_run_side_effects(
    valid_create_run_payload: Callable[..., dict[str, Any]],
) -> None:
    service = _ControlledService()
    run_ids = _SequenceRunIds([])
    thread_ids = _SequenceThreadIds([])
    app = _build_app(service=service, run_ids=run_ids, thread_ids=thread_ids)

    nested_extra = valid_create_run_payload()
    nested_extra["request"] = {
        **nested_extra["request"],
        "unexpected": "value",
    }
    secret = "secret-DO-NOT-ECHO"
    secret_payload = valid_create_run_payload()
    secret_payload["request"] = {
        **secret_payload["request"],
        "query": secret,
        "top_k": "3",
    }
    invalid_payloads = [
        valid_create_run_payload(thread_id="bad id"),
        valid_create_run_payload(
            request={
                **valid_create_run_payload()["request"],
                "query": "   ",
            }
        ),
        valid_create_run_payload(
            request={
                **valid_create_run_payload()["request"],
                "top_k": "3",
            }
        ),
        valid_create_run_payload(
            request={
                **valid_create_run_payload()["request"],
                "locale": "en-US",
            }
        ),
        valid_create_run_payload(
            request={
                **valid_create_run_payload()["request"],
                "display_currency": "usd",
            }
        ),
        valid_create_run_payload(extra=True),
        nested_extra,
        secret_payload,
    ]

    async def exercise() -> list[httpx.Response]:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                return [
                    await client.post("/api/v1/runs", json=payload) for payload in invalid_payloads
                ]

    responses = asyncio.run(exercise())

    for response in responses:
        body = _assert_safe_error(
            response,
            status_code=422,
            code="REQUEST_REJECTED",
            secret=secret,
        )
        assert body["error"]["field_errors"]
    assert run_ids.calls == 0
    assert thread_ids.calls == 0
    assert service.calls == 0


def test_malformed_json_and_non_json_media_type_are_safe_bad_requests(
    valid_create_run_payload: Callable[..., dict[str, Any]],
) -> None:
    service = _ControlledService()
    run_ids = _SequenceRunIds([])
    thread_ids = _SequenceThreadIds([])
    app = _build_app(service=service, run_ids=run_ids, thread_ids=thread_ids)
    secret = "secret-malformed-payload"

    async def exercise() -> tuple[httpx.Response, httpx.Response]:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                malformed = await client.post(
                    "/api/v1/runs",
                    content=f'{{"query":"{secret}"'.encode(),
                    headers={"content-type": "application/json"},
                )
                unsupported = await client.post(
                    "/api/v1/runs",
                    content=json.dumps(valid_create_run_payload()).encode(),
                    headers={"content-type": "text/plain"},
                )
                return malformed, unsupported

    malformed, unsupported = asyncio.run(exercise())

    _assert_safe_error(
        malformed,
        status_code=400,
        code="BAD_REQUEST",
        secret=secret,
    )
    _assert_safe_error(
        unsupported,
        status_code=415,
        code="BAD_REQUEST",
        secret=secret,
    )
    assert run_ids.calls == 0
    assert thread_ids.calls == 0
    assert service.calls == 0


def test_actual_chunked_body_limit_rejects_before_submission() -> None:
    service = _ControlledService()
    run_ids = _SequenceRunIds([])
    thread_ids = _SequenceThreadIds([])
    app = _build_app(
        service=service,
        run_ids=run_ids,
        thread_ids=thread_ids,
        settings=ApiSettings(max_request_body_bytes=8),
    )

    async def exercise() -> list[dict[str, Any]]:
        async with app.router.lifespan_context(app):
            return await _invoke_asgi(
                app,
                body_chunks=(b'{"reques', b't":"too-large"}'),
                headers=(
                    (b"content-type", b"application/json"),
                    (b"content-length", b"1"),
                ),
            )

    status_code, body = _recorded_response(asyncio.run(exercise()))

    assert status_code == 413
    assert body == {
        "schema_version": "glodex.error.v1",
        "type": "api_error",
        "error": {
            "code": "REQUEST_BODY_TOO_LARGE",
            "message": "Request body is too large.",
            "field_errors": [],
        },
    }
    assert run_ids.calls == 0
    assert thread_ids.calls == 0
    assert service.calls == 0


@pytest.mark.spec("M1-AC-006", "M1-AC-008")
def test_conflict_capacity_and_unknown_run_use_stable_errors(
    valid_create_run_payload: Callable[..., dict[str, Any]],
) -> None:
    service = _ControlledService()
    run_ids = _SequenceRunIds(
        [
            "run-active-001",
            "run-conflict-001",
            "run-capacity-001",
        ]
    )
    app = _build_app(
        service=service,
        run_ids=run_ids,
        settings=ApiSettings(max_active_runs=1),
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
                    json=valid_create_run_payload(thread_id="thread-active-001"),
                )
                await _wait_for_event(service.started)
                conflict = await client.post(
                    "/api/v1/runs",
                    json=valid_create_run_payload(thread_id="thread-active-001"),
                )
                capacity = await client.post(
                    "/api/v1/runs",
                    json=valid_create_run_payload(thread_id="thread-other-001"),
                )
                missing = await client.get("/api/v1/runs/run-unknown-001")
                service.release.set()
                await _wait_for_event(service.returned)
                await _wait_for_state(client, "run-active-001", RunState.NO_MATCH)
                return accepted, conflict, capacity, missing

    accepted, conflict, capacity, missing = asyncio.run(exercise())

    assert accepted.status_code == 202
    _assert_safe_error(
        conflict,
        status_code=409,
        code="RUN_ALREADY_ACTIVE",
    )
    _assert_safe_error(
        capacity,
        status_code=503,
        code="CAPACITY_EXCEEDED",
    )
    _assert_safe_error(
        missing,
        status_code=404,
        code="RUN_NOT_FOUND_OR_EXPIRED",
    )
    assert service.calls == 1


def test_internal_error_does_not_leak_exception_detail(
    valid_create_run_payload: Callable[..., dict[str, Any]],
) -> None:
    secret = "super-secret-provider-token"
    service = _ControlledService()
    run_ids = _ExplodingRunIds(secret)
    app = _build_app(service=service, run_ids=run_ids)

    async def exercise() -> httpx.Response:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                return await client.post(
                    "/api/v1/runs",
                    json=valid_create_run_payload(),
                )

    response = asyncio.run(exercise())

    _assert_safe_error(
        response,
        status_code=500,
        code="INTERNAL_SERVER_ERROR",
        secret=secret,
    )
    assert run_ids.calls == 1
    assert service.calls == 0


def test_untrusted_extra_field_name_is_not_reflected_or_allowed_to_break_handler(
    valid_create_run_payload: Callable[..., dict[str, Any]],
) -> None:
    secret_key = "/Users/alice/" + ("secret-" * 400)
    payload = valid_create_run_payload()
    payload[secret_key] = True
    service = _ControlledService()
    run_ids = _SequenceRunIds([])
    app = _build_app(service=service, run_ids=run_ids)

    async def exercise() -> httpx.Response:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                return await client.post("/api/v1/runs", json=payload)

    response = asyncio.run(exercise())

    body = _assert_safe_error(
        response,
        status_code=422,
        code="REQUEST_REJECTED",
        secret=secret_key,
    )
    assert body["error"]["field_errors"] == [
        {
            "field": "body.unknown",
            "code": "extra_forbidden",
            "message": "Extra inputs are not permitted",
        }
    ]
    assert run_ids.calls == 0
    assert service.calls == 0


def test_generated_run_id_must_be_one_safe_url_segment(
    valid_create_run_payload: Callable[..., dict[str, Any]],
) -> None:
    service = _ControlledService()
    run_ids = _SequenceRunIds(["run/unsafe"])
    app = _build_app(service=service, run_ids=run_ids)

    async def exercise() -> httpx.Response:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                return await client.post(
                    "/api/v1/runs",
                    json=valid_create_run_payload(),
                )

    response = asyncio.run(exercise())

    _assert_safe_error(
        response,
        status_code=500,
        code="INTERNAL_SERVER_ERROR",
        secret="run/unsafe",
    )
    assert run_ids.calls == 1
    assert service.calls == 0
    assert app.state.coordinator.in_flight_count == 0


def test_root_path_media_validation_and_returned_urls_remain_consistent(
    valid_create_run_payload: Callable[..., dict[str, Any]],
) -> None:
    service = _ControlledService(blocked=False)
    run_ids = _SequenceRunIds(["run-mounted-001"])
    app = _build_app(service=service, run_ids=run_ids)

    async def exercise() -> tuple[httpx.Response, httpx.Response]:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(
                app=app,
                root_path="/prefix",
                raise_app_exceptions=False,
            )
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver/prefix",
            ) as client:
                unsupported = await client.post("/api/v1/runs", content=b"{}")
                accepted = await client.post(
                    "/api/v1/runs",
                    json=valid_create_run_payload(),
                )
                await _wait_for_event(service.returned)
                return unsupported, accepted

    unsupported, accepted = asyncio.run(exercise())

    _assert_safe_error(unsupported, status_code=415, code="BAD_REQUEST")
    assert accepted.status_code == 202
    assert accepted.json()["status_url"] == "/prefix/api/v1/runs/run-mounted-001"
    assert accepted.json()["events_url"] == "/prefix/api/v1/runs/run-mounted-001/events"


def test_duplicate_content_type_is_rejected_and_405_retains_allow_header() -> None:
    service = _ControlledService()
    run_ids = _SequenceRunIds([])
    app = _build_app(service=service, run_ids=run_ids)

    async def exercise() -> tuple[httpx.Response, httpx.Response]:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                duplicate = await client.post(
                    "/api/v1/runs",
                    content=b"{}",
                    headers=[
                        ("content-type", "application/json"),
                        ("content-type", "text/plain"),
                    ],
                )
                wrong_method = await client.put("/api/v1/runs")
                return duplicate, wrong_method

    duplicate, wrong_method = asyncio.run(exercise())

    _assert_safe_error(duplicate, status_code=415, code="BAD_REQUEST")
    _assert_safe_error(wrong_method, status_code=405, code="BAD_REQUEST")
    assert wrong_method.headers["allow"] == "POST"
    assert run_ids.calls == 0
    assert service.calls == 0


@pytest.mark.spec("M1-AC-003")
def test_final_202_body_is_sent_before_service_execution(
    valid_create_run_payload: Callable[..., dict[str, Any]],
) -> None:
    timeline: list[str] = []
    service = _ControlledService(blocked=False, timeline=timeline)
    run_ids = _SequenceRunIds(["run-ordering-001"])
    app = _build_app(service=service, run_ids=run_ids)
    body = json.dumps(valid_create_run_payload()).encode()

    async def exercise() -> list[dict[str, Any]]:
        async with app.router.lifespan_context(app):
            messages = await _invoke_asgi(
                app,
                body_chunks=(body[:17], body[17:]),
                headers=(
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ),
                timeline=timeline,
            )
            await _wait_for_event(service.returned)
            return messages

    messages = asyncio.run(exercise())
    status_code, response_body = _recorded_response(messages)

    assert status_code == 202
    assert response_body["run_id"] == "run-ordering-001"
    assert timeline == ["final_response_body", "service_execute"]


@pytest.mark.spec("M1-AC-008")
def test_lifespan_shutdown_aborts_active_run_and_stops_admission(
    valid_create_run_payload: Callable[..., dict[str, Any]],
) -> None:
    service = _ControlledService()
    run_ids = _SequenceRunIds(["run-shutdown-001"])
    app = _build_app(service=service, run_ids=run_ids)

    async def exercise() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client,
        ):
            accepted = await client.post(
                "/api/v1/runs",
                json=valid_create_run_payload(thread_id="thread-shutdown-001"),
            )
            await _wait_for_event(service.started)

        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            aborted = await client.get("/api/v1/runs/run-shutdown-001")
            unavailable = await client.post(
                "/api/v1/runs",
                json=valid_create_run_payload(thread_id="thread-after-shutdown"),
            )
        return accepted, aborted, unavailable

    accepted, aborted, unavailable = asyncio.run(exercise())

    assert accepted.status_code == 202
    assert aborted.status_code == 200
    aborted_body = aborted.json()
    assert aborted_body["state"] == "ABORTED"
    assert aborted_body["response"] is None
    assert aborted_body["error"] == {
        "code": "RUN_ABORTED",
        "message": "Run execution was aborted.",
        "field_errors": [],
    }
    _assert_safe_error(
        unavailable,
        status_code=503,
        code="CAPACITY_EXCEEDED",
    )
    assert run_ids.calls == 1
    assert service.calls == 1
