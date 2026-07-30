"""Side-effect-free FastAPI factory for the opt-in M2b durable Agent API."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import cast

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from glodex.adapters.m2b_postgres import M2bPostgresStore
from glodex.api.agent_events import AgentEventProjector
from glodex.api.body_limit import RequestBodyLimitMiddleware
from glodex.api.contracts import ApiError, ApiErrorEnvelope
from glodex.api.durable_agent_contracts import (
    DurableCreateRunRequest,
    DurableRunAccepted,
    DurableRunStateDTO,
    DurableRunStatusResponse,
)
from glodex.api.events import InvalidEventCursor, parse_event_cursor
from glodex.application.agent.contracts import AgentDemoResponse
from glodex.application.durable.contracts import DurableRun, DurableRunState, DurableStoreError
from glodex.application.durable.ports import RetrievalCachePort
from glodex.application.durable.runtime import DurableAgentCoordinator, DurableAgentExecutor
from glodex.contracts import RequestRejected, SearchRequest, validate_search_request

_ROUTER = APIRouter(prefix="/api/v1")
_HEARTBEAT_SECONDS = 15
_POLL_SECONDS = 0.20
_MAX_REQUEST_BYTES = 65_536


class DurableAgentEventStreamNotAcceptable(ValueError):
    """The durable event endpoint requires explicit SSE negotiation."""


@_ROUTER.post(
    "/durable-agent-runs",
    response_model=DurableRunAccepted,
    status_code=202,
)
async def _create_durable_agent_run(
    payload: DurableCreateRunRequest,
    request: Request,
) -> DurableRunAccepted:
    validated = validate_search_request(payload.request)
    if isinstance(validated, RequestRejected):
        raise RuntimeError("validated durable request failed pre-run validation")
    assert isinstance(validated, SearchRequest)
    coordinator = cast(DurableAgentCoordinator, request.app.state.durable_coordinator)
    store = cast(M2bPostgresStore, request.app.state.durable_store)
    thread_id = payload.thread_id or cast(Callable[[], str], request.app.state.thread_id_factory)()
    profile_revision = 0
    if payload.profile_id is not None:
        profile = await store.profile_snapshot(profile_id=payload.profile_id)
        profile_revision = profile.revision
    run = await coordinator.submit(
        request=validated,
        thread_id=thread_id,
        profile_id=payload.profile_id,
        profile_revision=profile_revision,
    )
    root_path = str(request.scope.get("root_path", "")).rstrip("/")
    return DurableRunAccepted(
        thread_id=run.thread_id,
        run_id=run.run_id,
        status_url=f"{root_path}/api/v1/durable-agent-runs/{run.run_id}",
        events_url=f"{root_path}/api/v1/durable-agent-runs/{run.run_id}/events",
    )


@_ROUTER.get(
    "/durable-agent-runs/{run_id}",
    response_model=DurableRunStatusResponse,
)
async def _get_durable_agent_run(run_id: str, request: Request) -> DurableRunStatusResponse:
    store = cast(M2bPostgresStore, request.app.state.durable_store)
    return _status_response(await store.load_run(run_id=run_id))


@_ROUTER.post(
    "/durable-agent-runs/{run_id}/cancel",
    response_model=DurableRunStatusResponse,
)
async def _cancel_durable_agent_run(run_id: str, request: Request) -> DurableRunStatusResponse:
    coordinator = cast(DurableAgentCoordinator, request.app.state.durable_coordinator)
    return _status_response(await coordinator.cancel(run_id=run_id))


@_ROUTER.post(
    "/durable-agent-runs/{run_id}/resume",
    response_model=DurableRunStatusResponse,
)
async def _resume_durable_agent_run(run_id: str, request: Request) -> DurableRunStatusResponse:
    coordinator = cast(DurableAgentCoordinator, request.app.state.durable_coordinator)
    return _status_response(await coordinator.resume(run_id=run_id))


@_ROUTER.get(
    "/durable-agent-runs/{run_id}/events",
    response_class=EventSourceResponse,
)
async def _get_durable_agent_events(
    run_id: str,
    request: Request,
) -> AsyncIterator[ServerSentEvent]:
    if not _accepts_event_stream(request.headers.getlist("accept")):
        raise DurableAgentEventStreamNotAcceptable("SSE representation was not accepted")
    cursor_values = request.headers.getlist("last-event-id")
    if len(cursor_values) > 1:
        raise InvalidEventCursor("Invalid event cursor.")
    store = cast(M2bPostgresStore, request.app.state.durable_store)
    run = await store.load_run(run_id=run_id)
    after_sequence = parse_event_cursor(
        cursor_values[0] if cursor_values else None,
        run_id=run.run_id,
        last_sequence=run.event_sequence,
    )
    async for event in _event_stream(
        store=store,
        run_id=run.run_id,
        after_sequence=after_sequence,
    ):
        yield event


async def _event_stream(
    *,
    store: M2bPostgresStore,
    run_id: str,
    after_sequence: int,
) -> AsyncIterator[ServerSentEvent]:
    cursor = after_sequence
    next_ping = asyncio.get_running_loop().time() + _HEARTBEAT_SECONDS
    while True:
        events = await store.load_events(run_id=run_id, after_sequence=cursor)
        for event in events:
            cursor = event.sequence
            yield ServerSentEvent(data=event.payload, id=f"{run_id}:{event.sequence}")
        run = await store.load_run(run_id=run_id)
        if run.state in {
            DurableRunState.COMPLETED,
            DurableRunState.NO_MATCH,
            DurableRunState.FAILED,
            DurableRunState.ABORTED,
        }:
            return
        now = asyncio.get_running_loop().time()
        if now >= next_ping:
            yield ServerSentEvent(comment="ping")
            next_ping = now + _HEARTBEAT_SECONDS
        await asyncio.sleep(_POLL_SECONDS)


def _status_response(run: DurableRun) -> DurableRunStatusResponse:
    response: AgentDemoResponse | None = None
    api_error: ApiError | None = None
    if run.state in {
        DurableRunState.COMPLETED,
        DurableRunState.NO_MATCH,
        DurableRunState.FAILED,
    }:
        if run.terminal_response is None:
            raise DurableStoreError("M2B_STORE_DATA_INVALID")
        try:
            # PostgreSQL JSONB faithfully returns JSON arrays as Python lists, while
            # the strict public DTO intentionally owns tuple fields. Re-validate in
            # JSON mode so persisted canonical JSON is interpreted by its wire
            # contract rather than by the stricter in-process construction path.
            response = AgentDemoResponse.model_validate_json(
                json.dumps(run.terminal_response, ensure_ascii=False, separators=(",", ":"))
            )
        except Exception as validation_error:
            raise DurableStoreError("M2B_STORE_DATA_INVALID") from validation_error
    elif run.state is DurableRunState.ABORTED:
        api_error = ApiError(code="RUN_ABORTED", message="Run execution was aborted.")
    return DurableRunStatusResponse(
        thread_id=run.thread_id,
        run_id=run.run_id,
        state=DurableRunStateDTO(run.state.value),
        last_event_id=(f"{run.run_id}:{run.event_sequence}" if run.event_sequence > 0 else None),
        response=response,
        error=api_error,
    )


class _DurableAgentJsonContentTypeMiddleware:
    """Reject non-JSON durable submissions before FastAPI reads the body."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and _route_path(scope) == "/api/v1/durable-agent-runs"
            and not _has_json_content_type(scope)
        ):
            await _error_response(
                status_code=415,
                code="BAD_REQUEST",
                message="Request could not be processed.",
            )(scope, receive, send)
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
    values = tuple(value for name, value in headers if name.lower() == b"content-type")
    if len(values) != 1:
        return False
    media_type = values[0].split(b";", 1)[0].strip().lower()
    return media_type == b"application/json" or (
        media_type.startswith(b"application/") and media_type.endswith(b"+json")
    )


def _accepts_event_stream(values: list[str]) -> bool:
    for value in values:
        for media_range in value.split(","):
            segments = media_range.split(";")
            if segments[0].strip().lower() != "text/event-stream":
                continue
            try:
                quality = next(
                    (
                        float(segment.partition("=")[2].strip())
                        for segment in segments[1:]
                        if segment.partition("=")[0].strip().lower() == "q"
                    ),
                    1.0,
                )
            except ValueError:
                continue
            if 0.0 < quality <= 1.0:
                return True
    return False


def _error_response(*, status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ApiErrorEnvelope(error=ApiError(code=code, message=message)).model_dump(
            mode="json"
        ),
    )


async def _request_validation_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    del request, error
    return _error_response(
        status_code=422,
        code="REQUEST_REJECTED",
        message="Request validation failed.",
    )


async def _http_exception_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    if isinstance(error, StarletteHTTPException):
        return _error_response(
            status_code=error.status_code,
            code="BAD_REQUEST",
            message="Request could not be processed.",
        )
    return _error_response(
        status_code=500,
        code="INTERNAL_SERVER_ERROR",
        message="Internal server error.",
    )


async def _runtime_exception_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    if isinstance(error, DurableAgentEventStreamNotAcceptable):
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
    if isinstance(error, DurableStoreError):
        code = str(error)
        if code == "M2B_RUN_NOT_FOUND":
            return _error_response(
                status_code=404,
                code="RUN_NOT_FOUND",
                message="Run was not found.",
            )
        if code == "M2B_RUN_ALREADY_ACTIVE":
            return _error_response(
                status_code=409,
                code="RUN_ALREADY_ACTIVE",
                message="Thread already has an active Run.",
            )
        if code in {"M2B_STORE_UNAVAILABLE", "M2B_PROFILE_UNAVAILABLE"}:
            return _error_response(
                status_code=503,
                code="DURABLE_STORE_UNAVAILABLE",
                message="Durable runtime storage is unavailable.",
            )
        if code in {"M2B_REMOTE_STEP_UNCERTAIN", "M2B_RUN_NOT_RECOVERABLE"}:
            return _error_response(
                status_code=409,
                code="RUN_NOT_RECOVERABLE",
                message="Run cannot be recovered from its current checkpoint.",
            )
    return _error_response(
        status_code=500,
        code="INTERNAL_SERVER_ERROR",
        message="Internal server error.",
    )


async def _unhandled_exception_handler(request: Request, error: Exception) -> JSONResponse:
    del request, error
    return _error_response(
        status_code=500,
        code="INTERNAL_SERVER_ERROR",
        message="Internal server error.",
    )


def _new_thread_id() -> str:
    return f"thread-{uuid.uuid4().hex}"


def create_durable_agent_app(
    *,
    executor: DurableAgentExecutor,
    store: M2bPostgresStore,
    projector: AgentEventProjector,
    asset_version: str,
    config_fingerprint: str,
    thread_id_factory: Callable[[], str] | None = None,
    context_cache: RetrievalCachePort | None = None,
    shutdown_callback: Callable[[], Awaitable[None]] | None = None,
) -> FastAPI:
    """Compose one M2b API without import-time I/O or default-provider behavior."""

    coordinator = DurableAgentCoordinator(
        store=store,
        executor=executor,
        projector=projector,
        asset_version=asset_version,
        config_fingerprint=config_fingerprint,
        context_cache=context_cache,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await store.open()
        try:
            yield
        finally:
            await coordinator.shutdown()
            if shutdown_callback is not None:
                await shutdown_callback()
            await store.close()

    app = FastAPI(
        title="Glodex M2b Durable Agent API",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.durable_store = store
    app.state.durable_coordinator = coordinator
    app.state.thread_id_factory = thread_id_factory or _new_thread_id
    app.include_router(_ROUTER)
    app.add_exception_handler(RequestValidationError, _request_validation_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(DurableAgentEventStreamNotAcceptable, _runtime_exception_handler)
    app.add_exception_handler(InvalidEventCursor, _runtime_exception_handler)
    app.add_exception_handler(DurableStoreError, _runtime_exception_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)
    app.add_middleware(_DurableAgentJsonContentTypeMiddleware)
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=_MAX_REQUEST_BYTES)
    return app


__all__ = [
    "DurableAgentEventStreamNotAcceptable",
    "create_durable_agent_app",
]
