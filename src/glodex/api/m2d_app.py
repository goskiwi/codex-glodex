"""Independent loopback M2d AG-UI adapter over the M2b public durable API."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.sse import EventSourceResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from glodex.api.agent_events import AgentResultEvent
from glodex.api.body_limit import RequestBodyLimitMiddleware
from glodex.api.contracts import ApiError, ApiErrorEnvelope
from glodex.api.durable_agent_contracts import (
    DurableCreateRunRequest,
    DurableRunStateDTO,
    DurableRunStatusResponse,
)
from glodex.api.m2d_agui import (
    ProjectionGroup,
    initial_state,
    parse_m2b_public_event,
    project_event,
    project_payload,
    project_relay_failure,
)
from glodex.api.m2d_contracts import (
    AgUiRunInput,
    GlodexM2dState,
    M2dRelayCode,
    M2dRelayState,
    M2dTerminalView,
    encode_agui_event,
)
from glodex.api.m2d_durable_client import (
    LoopbackM2bPublicClient,
    M2bPublicEventFrame,
    M2dDurablePublicClient,
    M2dUpstreamError,
)
from glodex.contracts import SearchRequest

_ROUTER = APIRouter(prefix="/api/v1/m2d")
_MAX_REQUEST_BYTES = 65_536
_TERMINAL_STATES = frozenset(
    {
        DurableRunStateDTO.COMPLETED,
        DurableRunStateDTO.NO_MATCH,
        DurableRunStateDTO.FAILED,
        DurableRunStateDTO.ABORTED,
    }
)


@_ROUTER.post("/ag-ui")
async def run_agui(payload: AgUiRunInput, request: Request) -> EventSourceResponse:
    """Create exactly one M2b run then expose its safe AG-UI projection."""

    _require_event_stream(request)
    client = cast(M2dDurablePublicClient, request.app.state.m2d_durable_client)
    try:
        search_request = SearchRequest.model_validate(payload.search_request_payload())
        accepted = await client.create(
            DurableCreateRunRequest(thread_id=payload.thread_id, request=search_request)
        )
    except (M2dUpstreamError, ValueError):
        return EventSourceResponse(
            _failed_stream(
                state=initial_state(thread_id=payload.thread_id, run_id=payload.run_id),
                code=M2dRelayCode.UPSTREAM_UNAVAILABLE,
            )
        )
    return EventSourceResponse(
        _projected_stream(
            client=client,
            thread_id=accepted.thread_id,
            run_id=accepted.run_id,
            emit_after_sequence=0,
        )
    )


@_ROUTER.get("/runs/{run_id}", response_model=GlodexM2dState)
async def get_run(run_id: str, request: Request) -> GlodexM2dState:
    """Return a safe status-derived snapshot without reading durable storage directly."""

    client = cast(M2dDurablePublicClient, request.app.state.m2d_durable_client)
    try:
        return _state_from_status(await client.status(run_id))
    except M2dUpstreamError as error:
        raise M2dAdapterError(M2dRelayCode.UPSTREAM_UNAVAILABLE) from error


@_ROUTER.get("/runs/{run_id}/events")
async def get_run_events(run_id: str, request: Request) -> EventSourceResponse:
    """Replay a suffix as full source projection groups for an idempotent React reducer."""

    _require_event_stream(request)
    after = _parse_browser_cursor(request.headers.get("last-event-id"), run_id=run_id)
    client = cast(M2dDurablePublicClient, request.app.state.m2d_durable_client)
    try:
        status = await client.status(run_id)
    except M2dUpstreamError:
        return EventSourceResponse(
            _failed_stream(
                state=initial_state(thread_id="m2d-unavailable", run_id=run_id),
                code=M2dRelayCode.UPSTREAM_UNAVAILABLE,
            )
        )
    return EventSourceResponse(
        _projected_stream(
            client=client,
            thread_id=status.thread_id,
            run_id=status.run_id,
            emit_after_sequence=after,
        )
    )


@_ROUTER.post("/runs/{run_id}/cancel", response_model=GlodexM2dState)
async def cancel_run(run_id: str, request: Request) -> GlodexM2dState:
    """Proxy only the already-approved M2b cancel operation."""

    client = cast(M2dDurablePublicClient, request.app.state.m2d_durable_client)
    try:
        return _state_from_status(await client.cancel(run_id))
    except M2dUpstreamError as error:
        raise M2dAdapterError(M2dRelayCode.UPSTREAM_UNAVAILABLE) from error


@_ROUTER.post("/runs/{run_id}/resume", response_model=GlodexM2dState)
async def resume_run(run_id: str, request: Request) -> GlodexM2dState:
    """Proxy only the already-approved M2b resume operation."""

    client = cast(M2dDurablePublicClient, request.app.state.m2d_durable_client)
    try:
        return _state_from_status(await client.resume(run_id))
    except M2dUpstreamError as error:
        raise M2dAdapterError(M2dRelayCode.UPSTREAM_UNAVAILABLE) from error


class M2dAdapterError(RuntimeError):
    """A browser-safe M2d failure represented by a stable code only."""

    def __init__(self, code: M2dRelayCode) -> None:
        super().__init__(code.value)
        self.code = code


def create_m2d_app(
    *,
    durable_client: M2dDurablePublicClient | None = None,
    static_root: Path | None = None,
) -> FastAPI:
    """Create the side-effect-free M2d app; client calls occur only at request time."""

    app = FastAPI(title="Glodex M2d Console", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.m2d_durable_client = durable_client or LoopbackM2bPublicClient()
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=_MAX_REQUEST_BYTES)
    app.include_router(_ROUTER)
    _mount_console(app, static_root=static_root)

    @app.exception_handler(M2dAdapterError)
    async def m2d_error_handler(_request: Request, error: M2dAdapterError) -> JSONResponse:
        status_code = 422 if error.code is M2dRelayCode.REQUEST_REJECTED else 502
        return _error_response(status_code=status_code, code=error.code.value)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request,
        _error: RequestValidationError,
    ) -> JSONResponse:
        return _error_response(status_code=422, code=M2dRelayCode.REQUEST_REJECTED.value)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(
        _request: Request,
        error: StarletteHTTPException,
    ) -> JSONResponse:
        return _error_response(
            status_code=error.status_code,
            code=M2dRelayCode.REQUEST_REJECTED.value,
        )

    return app


def _mount_console(app: FastAPI, *, static_root: Path | None) -> None:
    """Serve only a built local console, never a dev server or remote asset."""

    index = None if static_root is None else static_root / "index.html"
    assets = None if static_root is None else static_root / "assets"
    if index is not None and index.is_file() and assets is not None and assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="m2d-assets")

        @app.get("/", include_in_schema=False)
        async def console_index() -> FileResponse:
            return FileResponse(index)

        return

    @app.get("/", include_in_schema=False)
    async def missing_console_build() -> HTMLResponse:
        return HTMLResponse(
            status_code=503,
            content=(
                "<!doctype html><title>Glodex M2d</title>"
                "<p>Local console build is unavailable. "
                "Run npm ci and npm run build in frontend/.</p>"
            ),
        )


def _projected_stream(
    *,
    client: M2dDurablePublicClient,
    thread_id: str,
    run_id: str,
    emit_after_sequence: int,
) -> AsyncIterator[str]:
    async def stream() -> AsyncIterator[str]:
        state = initial_state(thread_id=thread_id, run_id=run_id)
        emitted_terminal = False
        try:
            async for frame in client.events(run_id):
                group, source_sequence = await _project_frame(
                    client=client,
                    state=state,
                    frame=frame,
                )
                state = group.state
                if source_sequence > emit_after_sequence:
                    for event in group.events:
                        yield _sse_frame(
                            data=encode_agui_event(event),
                            event_id=group.source_cursor,
                        )
                if state.state in _TERMINAL_STATES or state.relay.state is M2dRelayState.DEGRADED:
                    emitted_terminal = state.state in _TERMINAL_STATES
                    return
        except M2dUpstreamError:
            failure = project_relay_failure(
                state=state,
                code=M2dRelayCode.STREAM_INTERRUPTED,
                timestamp=0,
            )
            for event in failure.events:
                yield _sse_frame(data=encode_agui_event(event), event_id=failure.source_cursor)
            return
        if not emitted_terminal:
            failure = project_relay_failure(
                state=state,
                code=M2dRelayCode.STREAM_INTERRUPTED,
                timestamp=0,
            )
            for event in failure.events:
                yield _sse_frame(data=encode_agui_event(event), event_id=failure.source_cursor)

    return stream()


async def _project_frame(
    *,
    client: M2dDurablePublicClient,
    state: GlodexM2dState,
    frame: M2bPublicEventFrame,
) -> tuple[ProjectionGroup, int]:
    """Fetch terminal response only when the source event says it is required."""

    sequence = _parse_browser_cursor(frame.event_id, run_id=state.run_id)
    try:
        source = parse_m2b_public_event(frame.data)
    except (ValidationError, ValueError, TypeError):
        return project_payload(state=state, payload=frame.data), sequence
    terminal_response = None
    if isinstance(source, AgentResultEvent):
        terminal_response = (await client.status(state.run_id)).response
    return project_event(state=state, event=source, terminal_response=terminal_response), sequence


def _state_from_status(status: DurableRunStatusResponse) -> GlodexM2dState:
    state = initial_state(thread_id=status.thread_id, run_id=status.run_id).model_copy(
        update={"state": status.state, "source_cursor": status.last_event_id}
    )
    if status.state in {DurableRunStateDTO.COMPLETED, DurableRunStateDTO.NO_MATCH}:
        if status.response is None:
            raise M2dAdapterError(M2dRelayCode.PROJECTION_INVALID)
        terminal = M2dTerminalView.from_agent_response(status.response)
        return state.model_copy(update={"terminal": terminal})
    return state


def _failed_stream(
    *,
    state: GlodexM2dState,
    code: M2dRelayCode,
) -> AsyncIterator[str]:
    failure = project_relay_failure(state=state, code=code, timestamp=0)

    async def stream() -> AsyncIterator[str]:
        for event in failure.events:
            yield _sse_frame(data=encode_agui_event(event), event_id=failure.source_cursor)

    return stream()


def _require_event_stream(request: Request) -> None:
    values = request.headers.getlist("accept")
    for value in values:
        if "text/event-stream" in value.lower():
            return
    raise M2dAdapterError(M2dRelayCode.REQUEST_REJECTED)


def _parse_browser_cursor(value: str | None, *, run_id: str) -> int:
    if value is None:
        return 0
    prefix, separator, number = value.rpartition(":")
    if separator != ":" or prefix != run_id:
        raise M2dAdapterError(M2dRelayCode.REQUEST_REJECTED)
    try:
        sequence = int(number)
    except ValueError as error:
        raise M2dAdapterError(M2dRelayCode.REQUEST_REJECTED) from error
    if sequence < 1:
        raise M2dAdapterError(M2dRelayCode.REQUEST_REJECTED)
    return sequence


def _error_response(*, status_code: int, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ApiErrorEnvelope(
            error=ApiError(code=code, message="Request could not be processed."),
        ).model_dump(mode="json", by_alias=True),
    )


def _sse_frame(*, data: str, event_id: str | None) -> str:
    """Build one minimal SSE text frame without allowing event payload passthrough."""

    prefix = "" if event_id is None else f"id: {event_id}\n"
    return f"{prefix}data: {data}\n\n"


__all__ = ["M2dAdapterError", "create_m2d_app"]
