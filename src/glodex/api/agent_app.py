"""Side-effect-free FastAPI factory for the independent M1d Agent API."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, cast

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import TypeAdapter
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from glodex.api.agent_contracts import AgentRunStatusResponse
from glodex.api.agent_events import AgentEventProjector
from glodex.api.agent_runtime import (
    AgentCoordinatorShuttingDown,
    AgentExecutor,
    AgentRunAlreadyActive,
    AgentRunCapacityExceeded,
    AgentRunCoordinator,
    AgentRunNotFound,
    AgentRunRegistry,
    AgentRunSubscription,
    AgentSubscriberCapacityExceeded,
)
from glodex.api.body_limit import RequestBodyLimitMiddleware
from glodex.api.contracts import (
    ApiError,
    ApiErrorEnvelope,
    ApiFieldError,
    CreateRunRequest,
    RunAccepted,
)
from glodex.api.events import InvalidEventCursor, parse_event_cursor
from glodex.api.settings import ApiSettings
from glodex.application.agent.contracts import DataMode
from glodex.application.ports import Clock, RunIdProvider
from glodex.contracts import (
    Identifier,
    RequestRejected,
    SearchRequest,
    validate_search_request,
)

_router = APIRouter(prefix="/api/v1")
_IDENTIFIER_ADAPTER = TypeAdapter(Identifier)
_MINIMUM_AGENT_TIMEOUT_SECONDS = 240


class AgentEventStreamNotAcceptable(ValueError):
    """The request did not explicitly accept the SSE representation."""


class AgentRequestPolicyRejected(ValueError):
    """A valid SearchRequest conflicts with the operator-fixed Agent mode."""


@dataclass(frozen=True, slots=True)
class _AgentSseSession:
    registry: AgentRunRegistry
    subscription: AgentRunSubscription
    heartbeat_seconds: int


async def _release_start(
    coordinator: AgentRunCoordinator,
    run_id: str,
) -> None:
    coordinator.release_start(run_id)


@_router.post("/agent-runs", response_model=RunAccepted, status_code=202)
async def _create_agent_run(
    payload: CreateRunRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> RunAccepted:
    validated = validate_search_request(payload.request)
    if isinstance(validated, RequestRejected):
        raise RuntimeError("validated transport request failed pre-run validation")
    assert isinstance(validated, SearchRequest)
    data_mode = cast(DataMode, request.app.state.data_mode)
    if (
        data_mode is DataMode.DEMO_SNAPSHOT
        and validated.snapshot_version not in {None, "m1d-demo-v1"}
    ) or (data_mode is DataMode.LIVE_MARKETPLACE and validated.snapshot_version is not None):
        raise AgentRequestPolicyRejected("snapshot conflicts with Agent data mode")

    coordinator = cast(AgentRunCoordinator, request.app.state.coordinator)
    thread_id = payload.thread_id
    if thread_id is None:
        thread_id_factory = cast(
            Callable[[], str],
            request.app.state.thread_id_factory,
        )
        thread_id = _IDENTIFIER_ADAPTER.validate_python(
            thread_id_factory(),
            strict=True,
        )

    accepted = coordinator.submit(validated, thread_id=thread_id)
    background_tasks.add_task(_release_start, coordinator, accepted.run_id)
    root_path = str(request.scope.get("root_path", "")).rstrip("/")
    return RunAccepted(
        thread_id=accepted.thread_id,
        run_id=accepted.run_id,
        status_url=f"{root_path}/api/v1/agent-runs/{accepted.run_id}",
        events_url=f"{root_path}/api/v1/agent-runs/{accepted.run_id}/events",
    )


@_router.get(
    "/agent-runs/{run_id}",
    response_model=AgentRunStatusResponse,
)
async def _get_agent_run(
    run_id: str,
    request: Request,
) -> AgentRunStatusResponse:
    registry = cast(AgentRunRegistry, request.app.state.registry)
    return registry.status(run_id)


async def _prepare_agent_sse_session(
    run_id: str,
    request: Request,
) -> AsyncIterator[_AgentSseSession]:
    if not _accepts_event_stream(request.headers.getlist("accept")):
        raise AgentEventStreamNotAcceptable("SSE representation was not accepted")

    cursor_values = request.headers.getlist("last-event-id")
    if len(cursor_values) > 1:
        raise InvalidEventCursor("Invalid event cursor.")

    registry = cast(AgentRunRegistry, request.app.state.registry)
    settings = cast(ApiSettings, request.app.state.settings)
    last_sequence = registry.next_sequence(run_id) - 1
    after_sequence = parse_event_cursor(
        cursor_values[0] if cursor_values else None,
        run_id=run_id,
        last_sequence=last_sequence,
    )
    subscription = registry.subscribe(run_id, after_sequence=after_sequence)
    try:
        yield _AgentSseSession(
            registry=registry,
            subscription=subscription,
            heartbeat_seconds=settings.sse_heartbeat_seconds,
        )
    finally:
        registry.unsubscribe(subscription)


@_router.get(
    "/agent-runs/{run_id}/events",
    response_class=EventSourceResponse,
)
async def _get_agent_run_events(
    session: Annotated[_AgentSseSession, Depends(_prepare_agent_sse_session)],
) -> AsyncIterator[ServerSentEvent]:
    subscription = session.subscription
    try:
        while True:
            try:
                batch = session.registry.read(subscription)
            except AgentRunNotFound:
                return

            for event in batch.events:
                yield ServerSentEvent(
                    data=event.model_dump(
                        mode="json",
                        by_alias=True,
                        exclude_none=True,
                    ),
                    id=f"{event.run_id}:{event.sequence}",
                )

            if batch.terminal or batch.projection_degraded:
                return

            try:
                async with asyncio.timeout(session.heartbeat_seconds):
                    await subscription.notification.get()
            except TimeoutError:
                yield ServerSentEvent(comment="ping")
    finally:
        session.registry.unsubscribe(subscription)


class _AgentJsonContentTypeMiddleware:
    """Reject unsupported Agent POST media types before body parsing."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and _route_path(scope) == "/api/v1/agent-runs"
            and not _has_json_content_type(scope)
        ):
            response = _error_response(
                status_code=415,
                code="BAD_REQUEST",
                message="Request could not be processed.",
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _route_path(scope: Scope) -> str:
    path = str(scope.get("path", ""))
    root_path = str(scope.get("root_path", "")).rstrip("/")
    if root_path and (path == root_path or path.startswith(f"{root_path}/")):
        return path[len(root_path) :] or "/"
    return path


def _has_json_content_type(scope: Scope) -> bool:
    headers = cast(list[tuple[bytes, bytes]], scope.get("headers", []))
    raw_values = tuple(value for name, value in headers if name.lower() == b"content-type")
    if len(raw_values) != 1:
        return False
    media_type = raw_values[0].split(b";", 1)[0].strip().lower()
    return media_type == b"application/json" or (
        media_type.startswith(b"application/") and media_type.endswith(b"+json")
    )


def _accepts_event_stream(values: list[str]) -> bool:
    for value in values:
        for media_range in value.split(","):
            segments = media_range.split(";")
            if segments[0].strip().lower() != "text/event-stream":
                continue
            quality = 1.0
            quality_seen = False
            valid = True
            for parameter in segments[1:]:
                name, separator, raw_value = parameter.partition("=")
                if name.strip().lower() != "q":
                    continue
                if quality_seen or not separator:
                    valid = False
                    break
                quality_seen = True
                try:
                    quality = float(raw_value.strip())
                except ValueError:
                    valid = False
                    break
                if not 0.0 <= quality <= 1.0:
                    valid = False
                    break
            if valid and quality > 0.0:
                return True
    return False


def _error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    envelope = ApiErrorEnvelope(error=ApiError(code=code, message=message))
    return JSONResponse(
        status_code=status_code,
        content=envelope.model_dump(mode="json"),
        headers=headers,
    )


def _format_location(location: tuple[int | str, ...]) -> str:
    if not location:
        return "$"
    approved_parts = {
        "body",
        "thread_id",
        "request",
        "query",
        "locale",
        "display_currency",
        "top_k",
        "snapshot_version",
    }
    safe_parts: list[str] = []
    for part in location:
        if isinstance(part, str) and part in approved_parts:
            safe_parts.append(part)
        else:
            safe_parts.append("unknown")
            break
    return ".".join(safe_parts)


async def _request_validation_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    del request
    if not isinstance(error, RequestValidationError):
        return _error_response(
            status_code=500,
            code="INTERNAL_SERVER_ERROR",
            message="Internal server error.",
        )
    validation_errors = error.errors()
    if any(item.get("type") == "json_invalid" for item in validation_errors):
        return _error_response(
            status_code=400,
            code="BAD_REQUEST",
            message="Request could not be processed.",
        )

    field_errors = tuple(
        ApiFieldError(
            field=_format_location(tuple(item["loc"])),
            code=str(item["type"]),
            message=str(item["msg"]),
        )
        for item in validation_errors
    )
    envelope = ApiErrorEnvelope(
        error=ApiError(
            code="REQUEST_REJECTED",
            message="Request validation failed.",
            field_errors=field_errors,
        )
    )
    return JSONResponse(status_code=422, content=envelope.model_dump(mode="json"))


async def _http_exception_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    del request
    if not isinstance(error, StarletteHTTPException):
        return _error_response(
            status_code=500,
            code="INTERNAL_SERVER_ERROR",
            message="Internal server error.",
        )
    return _error_response(
        status_code=error.status_code,
        code="BAD_REQUEST",
        message="Request could not be processed.",
        headers=(
            {"Allow": error.headers["Allow"]}
            if error.status_code == 405 and error.headers is not None and "Allow" in error.headers
            else None
        ),
    )


async def _runtime_exception_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    del request
    if isinstance(error, AgentRunNotFound):
        return _error_response(
            status_code=404,
            code="RUN_NOT_FOUND_OR_EXPIRED",
            message="Run was not found or has expired.",
        )
    if isinstance(error, AgentRequestPolicyRejected):
        envelope = ApiErrorEnvelope(
            error=ApiError(
                code="REQUEST_REJECTED",
                message="Request validation failed.",
                field_errors=(
                    ApiFieldError(
                        field="body.request.snapshot_version",
                        code="snapshot_mode",
                        message="Snapshot version is not allowed for this Agent mode.",
                    ),
                ),
            )
        )
        return JSONResponse(
            status_code=422,
            content=envelope.model_dump(mode="json"),
        )
    if isinstance(error, AgentEventStreamNotAcceptable):
        return _error_response(
            status_code=406,
            code="NOT_ACCEPTABLE",
            message="An event-stream representation is required.",
        )
    if isinstance(error, InvalidEventCursor):
        return _error_response(
            status_code=400,
            code="INVALID_EVENT_CURSOR",
            message="Event cursor is invalid.",
        )
    if isinstance(error, AgentSubscriberCapacityExceeded):
        return _error_response(
            status_code=429,
            code="CAPACITY_EXCEEDED",
            message="Subscriber capacity is exhausted.",
        )
    if isinstance(error, AgentRunAlreadyActive):
        return _error_response(
            status_code=409,
            code="RUN_ALREADY_ACTIVE",
            message="Thread already has an active Run.",
        )
    if isinstance(
        error,
        (AgentRunCapacityExceeded, AgentCoordinatorShuttingDown),
    ):
        return _error_response(
            status_code=503,
            code="CAPACITY_EXCEEDED",
            message="Run capacity is exhausted.",
        )
    return _error_response(
        status_code=500,
        code="INTERNAL_SERVER_ERROR",
        message="Internal server error.",
    )


async def _unhandled_exception_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    del request, error
    return _error_response(
        status_code=500,
        code="INTERNAL_SERVER_ERROR",
        message="Internal server error.",
    )


def _new_thread_id() -> str:
    return f"thread-{uuid.uuid4().hex}"


class _SystemClock:
    def now_utc(self) -> datetime:
        return datetime.now(UTC)

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


class _UuidRunIdProvider:
    def next_run_id(self) -> str:
        return f"run-{uuid.uuid4().hex}"


def create_agent_app(
    *,
    service: AgentExecutor,
    data_mode: DataMode = DataMode.DEMO_SNAPSHOT,
    settings: ApiSettings | None = None,
    clock: Clock | None = None,
    run_id_provider: RunIdProvider | None = None,
    thread_id_factory: Callable[[], str] | None = None,
    timeout_factory: (Callable[[float | None], AbstractAsyncContextManager[object]] | None) = None,
) -> FastAPI:
    """Compose one Agent-only local API without import-time side effects."""

    if type(data_mode) is not DataMode:
        raise TypeError("Agent API data_mode must be an exact DataMode")
    effective_settings = settings or ApiSettings(run_timeout_seconds=300)
    if effective_settings.run_timeout_seconds <= _MINIMUM_AGENT_TIMEOUT_SECONDS:
        raise ValueError("Agent API timeout must be greater than 240 seconds")
    effective_clock = clock or _SystemClock()
    effective_run_id_provider = run_id_provider or _UuidRunIdProvider()

    registry = AgentRunRegistry(
        settings=effective_settings,
        clock=effective_clock,
    )
    coordinator = AgentRunCoordinator(
        registry=registry,
        projector=AgentEventProjector(clock=effective_clock),
        service=service,
        run_id_provider=effective_run_id_provider,
        settings=effective_settings,
        timeout_factory=timeout_factory,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await coordinator.shutdown()

    app = FastAPI(
        title="Glodex M1d Agent API",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = effective_settings
    app.state.data_mode = data_mode
    app.state.registry = registry
    app.state.coordinator = coordinator
    app.state.thread_id_factory = thread_id_factory or _new_thread_id
    app.include_router(_router)
    app.add_exception_handler(RequestValidationError, _request_validation_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(AgentRunNotFound, _runtime_exception_handler)
    app.add_exception_handler(AgentRunAlreadyActive, _runtime_exception_handler)
    app.add_exception_handler(AgentRunCapacityExceeded, _runtime_exception_handler)
    app.add_exception_handler(
        AgentCoordinatorShuttingDown,
        _runtime_exception_handler,
    )
    app.add_exception_handler(
        AgentEventStreamNotAcceptable,
        _runtime_exception_handler,
    )
    app.add_exception_handler(AgentRequestPolicyRejected, _runtime_exception_handler)
    app.add_exception_handler(InvalidEventCursor, _runtime_exception_handler)
    app.add_exception_handler(
        AgentSubscriberCapacityExceeded,
        _runtime_exception_handler,
    )
    app.add_exception_handler(Exception, _unhandled_exception_handler)
    app.add_middleware(_AgentJsonContentTypeMiddleware)
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=effective_settings.max_request_body_bytes,
    )
    return app


__all__ = [
    "AgentEventStreamNotAcceptable",
    "AgentExecutor",
    "AgentRequestPolicyRejected",
    "create_agent_app",
]
