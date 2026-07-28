"""Side-effect-free FastAPI application factory."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Protocol, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from glodex.api.body_limit import RequestBodyLimitMiddleware
from glodex.api.contracts import ApiError, ApiErrorEnvelope, ApiFieldError
from glodex.api.events import EventProjector, InvalidEventCursor
from glodex.api.routes import EventStreamNotAcceptable, router
from glodex.api.runtime import (
    CoordinatorShuttingDown,
    RunAlreadyActive,
    RunCapacityExceeded,
    RunCoordinator,
    RunNotFound,
    RunRegistry,
    SubscriberCapacityExceeded,
)
from glodex.api.settings import ApiSettings
from glodex.application.ports import Clock, RunEventObserver, RunIdProvider
from glodex.application.search_service import SearchExecution
from glodex.bootstrap import SystemClock, UuidRunIdProvider, build_service
from glodex.config import GlodexConfig, load_config
from glodex.contracts import SearchRequest


class SearchExecutor(Protocol):
    """Structural boundary accepted by the API composition root."""

    async def execute_run(
        self,
        request: SearchRequest,
        *,
        run_id: str,
        observer: RunEventObserver | None = None,
    ) -> SearchExecution: ...


class _JsonContentTypeMiddleware:
    """Reject unsupported POST media types before FastAPI parses the body."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and _route_path(scope) == "/api/v1/runs"
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


async def _runtime_exception_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    del request
    if isinstance(error, RunNotFound):
        return _error_response(
            status_code=404,
            code="RUN_NOT_FOUND_OR_EXPIRED",
            message="Run was not found or has expired.",
        )
    if isinstance(error, EventStreamNotAcceptable):
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
    if isinstance(error, SubscriberCapacityExceeded):
        return _error_response(
            status_code=429,
            code="CAPACITY_EXCEEDED",
            message="Subscriber capacity is exhausted.",
        )
    if isinstance(error, RunAlreadyActive):
        return _error_response(
            status_code=409,
            code="RUN_ALREADY_ACTIVE",
            message="Thread already has an active Run.",
        )
    if isinstance(error, (RunCapacityExceeded, CoordinatorShuttingDown)):
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


def _new_thread_id() -> str:
    return f"thread-{uuid.uuid4().hex}"


def create_app(
    *,
    settings: ApiSettings | None = None,
    service: SearchExecutor | None = None,
    clock: Clock | None = None,
    run_id_provider: RunIdProvider | None = None,
    thread_id_factory: Callable[[], str] | None = None,
    config: GlodexConfig | None = None,
) -> FastAPI:
    """Compose one bounded local API instance without import-time side effects."""

    effective_settings = settings or ApiSettings()
    effective_clock = clock or SystemClock()
    effective_run_id_provider = run_id_provider or UuidRunIdProvider()
    effective_service = service
    if effective_service is None:
        effective_service = build_service(
            config or load_config(),
            run_id_provider=effective_run_id_provider,
            clock=effective_clock,
        )

    registry = RunRegistry(settings=effective_settings, clock=effective_clock)
    coordinator = RunCoordinator(
        registry=registry,
        projector=EventProjector(),
        service=effective_service,
        run_id_provider=effective_run_id_provider,
        settings=effective_settings,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await coordinator.shutdown()

    app = FastAPI(
        title="Glodex M1a API",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = effective_settings
    app.state.registry = registry
    app.state.coordinator = coordinator
    app.state.thread_id_factory = thread_id_factory or _new_thread_id
    app.include_router(router)
    app.add_exception_handler(RequestValidationError, _request_validation_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RunNotFound, _runtime_exception_handler)
    app.add_exception_handler(RunAlreadyActive, _runtime_exception_handler)
    app.add_exception_handler(RunCapacityExceeded, _runtime_exception_handler)
    app.add_exception_handler(CoordinatorShuttingDown, _runtime_exception_handler)
    app.add_exception_handler(EventStreamNotAcceptable, _runtime_exception_handler)
    app.add_exception_handler(InvalidEventCursor, _runtime_exception_handler)
    app.add_exception_handler(SubscriberCapacityExceeded, _runtime_exception_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)
    app.add_middleware(_JsonContentTypeMiddleware)
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=effective_settings.max_request_body_bytes,
    )
    return app


__all__ = ["SearchExecutor", "create_app"]
