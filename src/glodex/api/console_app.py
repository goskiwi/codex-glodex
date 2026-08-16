"""Independent loopback WebConsole AG-UI adapter over the Durable public durable API."""

from __future__ import annotations

import asyncio
import re
from contextlib import suppress
from pathlib import Path
from typing import cast

from fastapi import APIRouter, FastAPI, Request, WebSocket, WebSocketDisconnect, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import TypeAdapter, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import HTTPConnection

from glodex.api.agent_events import AgentResultEvent
from glodex.api.agui import (
    ProjectionGroup,
    initial_state,
    parse_durable_public_event,
    project_event,
    project_payload,
)
from glodex.api.body_limit import RequestBodyLimitMiddleware
from glodex.api.console_contracts import (
    GlodexWebConsoleState,
    WebConsoleRelayCode,
    WebConsoleRelayState,
    WebConsoleTerminalView,
    WebConsoleWsAttach,
    WebConsoleWsCancel,
    WebConsoleWsClose,
    WebConsoleWsClosed,
    WebConsoleWsCommand,
    WebConsoleWsError,
    WebConsoleWsEvent,
    WebConsoleWsReady,
    WebConsoleWsServerFrame,
    WebConsoleWsStart,
)
from glodex.api.contracts import ApiError, ApiErrorEnvelope
from glodex.api.durable_client import (
    DurablePublicEventFrame,
    LoopbackDurablePublicClient,
    WebConsoleDurablePublicClient,
    WebConsoleUpstreamError,
)
from glodex.api.durable_contracts import (
    DurableCreateRunRequest,
    DurableRunAccepted,
    DurableRunStateDTO,
    DurableRunStatusResponse,
)
from glodex.api.identity_contracts import LocalCredentials
from glodex.api.memory_contracts import MemoryEntryInput
from glodex.contracts import SearchRequest

_ROUTER = APIRouter(prefix="/api/v1/web-console")
_MAX_REQUEST_BYTES = 65_536
_TERMINAL_STATES = frozenset(
    {
        DurableRunStateDTO.COMPLETED,
        DurableRunStateDTO.NO_MATCH,
        DurableRunStateDTO.FAILED,
        DurableRunStateDTO.ABORTED,
    }
)
_MEMORY_THREAD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
_MEMORY_ENTRY_ID = re.compile(r"mem-[0-9a-f]{24}\Z")
_M6_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
_WS_COMMAND_ADAPTER: TypeAdapter[WebConsoleWsCommand] = TypeAdapter(WebConsoleWsCommand)
_WS_ALLOWED_ORIGINS = frozenset(
    {
        "http://127.0.0.1:8767",
        "http://127.0.0.1:9527",
    }
)
_MAX_WS_CLIENT_FRAME_BYTES = 65_536
_MAX_WS_SERVER_FRAME_BYTES = 131_072


@_ROUTER.websocket("/ws")
async def websocket_agui(websocket: WebSocket) -> None:
    """Relay one authenticated Durable run as the existing safe AG-UI projection."""

    try:
        cookie_header = _websocket_handshake_cookie(websocket)
    except WebConsoleAdapterError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    client = cast(WebConsoleDurablePublicClient, websocket.app.state.web_console_durable_client)
    active_run_id: str | None = None
    relay_task: asyncio.Task[None] | None = None

    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            text = message.get("text")
            if type(text) is not str or len(text.encode("utf-8")) > _MAX_WS_CLIENT_FRAME_BYTES:
                await _stop_relay(relay_task)
                await _reject_websocket(websocket, code=WebConsoleRelayCode.REQUEST_REJECTED)
                return
            try:
                command = _WS_COMMAND_ADAPTER.validate_json(
                    text,
                    by_alias=True,
                    by_name=False,
                )
            except (ValidationError, ValueError, TypeError):
                await _stop_relay(relay_task)
                await _reject_websocket(websocket, code=WebConsoleRelayCode.REQUEST_REJECTED)
                return

            if isinstance(command, WebConsoleWsClose):
                await _stop_relay(relay_task)
                await _send_ws_frame(websocket, WebConsoleWsClosed(code="CLIENT_CLOSED"))
                await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)
                return

            if isinstance(command, (WebConsoleWsStart, WebConsoleWsAttach)):
                if relay_task is not None and not relay_task.done():
                    await _stop_relay(relay_task)
                    await _reject_websocket(
                        websocket,
                        code=WebConsoleRelayCode.REQUEST_REJECTED,
                    )
                    return
                try:
                    if isinstance(command, WebConsoleWsStart):
                        search_request = SearchRequest.model_validate(
                            command.input.search_request_payload()
                        )
                        accepted = await _create_with_cookie(
                            client,
                            DurableCreateRunRequest(
                                thread_id=command.input.thread_id,
                                request=search_request,
                            ),
                            cookie_header=cookie_header,
                        )
                        active_run_id = accepted.run_id
                        ready_state = initial_state(
                            thread_id=accepted.thread_id,
                            run_id=accepted.run_id,
                        )
                        after_cursor = None
                    else:
                        status_snapshot = await _status_with_cookie(
                            client,
                            command.run_id,
                            cookie_header=cookie_header,
                        )
                        active_run_id = status_snapshot.run_id
                        ready_state = _state_from_status(status_snapshot)
                        after_cursor = command.after_cursor
                except (ValidationError, WebConsoleUpstreamError, ValueError):
                    await _stop_relay(relay_task)
                    await _reject_websocket(
                        websocket,
                        code=WebConsoleRelayCode.UPSTREAM_UNAVAILABLE,
                    )
                    return

                await _send_ws_frame(websocket, WebConsoleWsReady(state=ready_state))
                relay_task = asyncio.create_task(
                    _relay_websocket_events(
                        websocket=websocket,
                        client=client,
                        thread_id=ready_state.thread_id,
                        run_id=ready_state.run_id,
                        after_cursor=after_cursor,
                        cookie_header=cookie_header,
                    )
                )
                continue

            if isinstance(command, WebConsoleWsCancel):
                if (
                    active_run_id is None
                    or command.run_id != active_run_id
                    or relay_task is None
                    or relay_task.done()
                ):
                    await _stop_relay(relay_task)
                    await _reject_websocket(
                        websocket,
                        code=WebConsoleRelayCode.REQUEST_REJECTED,
                    )
                    return
                try:
                    await _cancel_with_cookie(
                        client,
                        command.run_id,
                        cookie_header=cookie_header,
                    )
                except WebConsoleUpstreamError:
                    await _stop_relay(relay_task)
                    await _reject_websocket(
                        websocket,
                        code=WebConsoleRelayCode.UPSTREAM_UNAVAILABLE,
                    )
                    return
    except (WebSocketDisconnect, RuntimeError):
        return
    finally:
        await _stop_relay(relay_task)


@_ROUTER.post("/local-auth/register")
async def register_local_account(payload: LocalCredentials, request: Request) -> Response:
    return await _relay_local_auth(
        request=request,
        method="POST",
        path="/api/v1/local-auth/register",
        payload=payload.model_dump(mode="json"),
    )


@_ROUTER.post("/local-auth/login")
async def login_local_account(payload: LocalCredentials, request: Request) -> Response:
    return await _relay_local_auth(
        request=request,
        method="POST",
        path="/api/v1/local-auth/login",
        payload=payload.model_dump(mode="json"),
    )


@_ROUTER.get("/local-auth/me")
async def get_local_account(request: Request) -> Response:
    return await _relay_local_auth(
        request=request,
        method="GET",
        path="/api/v1/local-auth/me",
        payload=None,
    )


@_ROUTER.post("/local-auth/logout")
async def logout_local_account(request: Request) -> Response:
    return await _relay_local_auth(
        request=request,
        method="POST",
        path="/api/v1/local-auth/logout",
        payload=None,
    )


@_ROUTER.delete("/local-auth/me")
async def delete_local_account(request: Request) -> Response:
    return await _relay_local_auth(
        request=request,
        method="DELETE",
        path="/api/v1/local-auth/me",
        payload=None,
    )


@_ROUTER.get("/conversation-threads")
async def list_conversation_threads(request: Request) -> Response:
    return await _relay_memory_data(
        request=request,
        method="GET",
        path="/api/v1/conversation-threads",
        payload=None,
    )


@_ROUTER.get("/conversation-threads/{thread_id}/turns")
async def list_conversation_turns(
    thread_id: str,
    request: Request,
    before_ordinal: int | None = None,
) -> Response:
    _validate_m5_thread_id(thread_id)
    if before_ordinal is not None and before_ordinal < 1:
        raise WebConsoleAdapterError(WebConsoleRelayCode.REQUEST_REJECTED)
    suffix = "" if before_ordinal is None else f"?before_ordinal={before_ordinal}"
    return await _relay_memory_data(
        request=request,
        method="GET",
        path=f"/api/v1/conversation-threads/{thread_id}/turns{suffix}",
        payload=None,
    )


@_ROUTER.delete("/conversation-threads/{thread_id}")
async def delete_conversation_thread(thread_id: str, request: Request) -> Response:
    _validate_m5_thread_id(thread_id)
    return await _relay_memory_data(
        request=request,
        method="DELETE",
        path=f"/api/v1/conversation-threads/{thread_id}",
        payload=None,
    )


@_ROUTER.get("/memory")
async def list_user_memory(request: Request) -> Response:
    return await _relay_memory_data(
        request=request,
        method="GET",
        path="/api/v1/memory",
        payload=None,
    )


@_ROUTER.post("/memory")
async def create_user_memory(payload: MemoryEntryInput, request: Request) -> Response:
    return await _relay_memory_data(
        request=request,
        method="POST",
        path="/api/v1/memory",
        payload=payload.model_dump(mode="json"),
    )


@_ROUTER.put("/memory/{entry_id}")
async def replace_user_memory(
    entry_id: str,
    payload: MemoryEntryInput,
    request: Request,
) -> Response:
    _validate_m5_memory_id(entry_id)
    return await _relay_memory_data(
        request=request,
        method="PUT",
        path=f"/api/v1/memory/{entry_id}",
        payload=payload.model_dump(mode="json"),
    )


@_ROUTER.delete("/memory/{entry_id}")
async def delete_user_memory(entry_id: str, request: Request) -> Response:
    _validate_m5_memory_id(entry_id)
    return await _relay_memory_data(
        request=request,
        method="DELETE",
        path=f"/api/v1/memory/{entry_id}",
        payload=None,
    )


@_ROUTER.get("/m6/runs/{run_id}/trace")
async def get_m6_run_trace(run_id: str, request: Request) -> Response:
    _validate_m6_run_id(run_id)
    return await _relay_m6_data(
        request=request,
        path=f"/api/v1/m6/runs/{run_id}/trace",
    )


@_ROUTER.get("/m6/operations")
async def get_m6_operations(request: Request, window: str = "1h") -> Response:
    if window not in {"1h", "24h"}:
        raise WebConsoleAdapterError(WebConsoleRelayCode.REQUEST_REJECTED)
    return await _relay_m6_data(
        request=request,
        path=f"/api/v1/m6/operations?window={window}",
    )


@_ROUTER.get("/runs/{run_id}/evaluation-summary")
async def get_m7_evaluation_summary(run_id: str, request: Request) -> Response:
    """Relay one safe summary without exposing the private offline report."""

    _validate_m6_run_id(run_id)
    client = cast(WebConsoleDurablePublicClient, request.app.state.web_console_durable_client)
    if type(client) is not LoopbackDurablePublicClient:
        raise WebConsoleAdapterError(WebConsoleRelayCode.UPSTREAM_UNAVAILABLE)
    try:
        upstream = await client.m7_summary(
            path=f"/api/v1/m7/runs/{run_id}/summary",
            cookie_header=_opaque_cookie_header(request),
        )
    except WebConsoleUpstreamError as error:
        raise WebConsoleAdapterError(WebConsoleRelayCode.UPSTREAM_UNAVAILABLE) from error
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type="application/json",
    )


class WebConsoleAdapterError(RuntimeError):
    """A browser-safe WebConsole failure represented by a stable code only."""

    def __init__(self, code: WebConsoleRelayCode) -> None:
        super().__init__(code.value)
        self.code = code


def _opaque_cookie_header(request: HTTPConnection) -> str | None:
    """Forward one browser cookie header verbatim; WebConsole never parses or stores its value."""

    values = request.headers.getlist("cookie")
    if not values:
        return None
    if len(values) != 1:
        raise WebConsoleAdapterError(WebConsoleRelayCode.REQUEST_REJECTED)
    return values[0]


def _websocket_handshake_cookie(websocket: WebSocket) -> str:
    """Accept only the two loopback console origins and one bounded cookie header."""

    if websocket.headers.get("origin") not in _WS_ALLOWED_ORIGINS:
        raise WebConsoleAdapterError(WebConsoleRelayCode.REQUEST_REJECTED)
    if websocket.scope.get("subprotocols"):
        raise WebConsoleAdapterError(WebConsoleRelayCode.REQUEST_REJECTED)
    cookie_header = _opaque_cookie_header(websocket)
    if (
        cookie_header is None
        or not cookie_header
        or len(cookie_header) > 4_096
        or "\r" in cookie_header
        or "\n" in cookie_header
    ):
        raise WebConsoleAdapterError(WebConsoleRelayCode.REQUEST_REJECTED)
    return cookie_header


async def _send_ws_frame(websocket: WebSocket, frame: WebConsoleWsServerFrame) -> None:
    payload = frame.model_dump_json(by_alias=True, exclude_none=True)
    if len(payload.encode("utf-8")) > _MAX_WS_SERVER_FRAME_BYTES:
        raise ValueError("WebConsole WebSocket frame exceeds the size limit")
    await websocket.send_text(payload)


async def _reject_websocket(websocket: WebSocket, *, code: WebConsoleRelayCode) -> None:
    with suppress(RuntimeError, WebSocketDisconnect):
        await _send_ws_frame(websocket, WebConsoleWsError(code=code))
        await _send_ws_frame(websocket, WebConsoleWsClosed(code=code.value))
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)


async def _stop_relay(relay_task: asyncio.Task[None] | None) -> None:
    if relay_task is None:
        return
    if not relay_task.done():
        relay_task.cancel()
    with suppress(asyncio.CancelledError, RuntimeError, WebSocketDisconnect):
        await relay_task


async def _relay_websocket_events(
    *,
    websocket: WebSocket,
    client: WebConsoleDurablePublicClient,
    thread_id: str,
    run_id: str,
    after_cursor: str | None,
    cookie_header: str,
) -> None:
    """Rebuild projection state from Durable truth and emit only the requested suffix."""

    state_snapshot = initial_state(thread_id=thread_id, run_id=run_id)
    after_sequence = _parse_browser_cursor(after_cursor, run_id=run_id)
    terminal_code: str | None = None
    try:
        events = (
            client.events(run_id, cookie_header=cookie_header)
            if type(client) is LoopbackDurablePublicClient
            else client.events(run_id)
        )
        async for source_frame in events:
            group, source_sequence = await _project_frame(
                client=client,
                state=state_snapshot,
                frame=source_frame,
                cookie_header=cookie_header,
            )
            state_snapshot = group.state
            if state_snapshot.relay.state is WebConsoleRelayState.DEGRADED:
                relay_safe_code = state_snapshot.relay.safe_code
                if relay_safe_code is None:
                    raise ValueError("A degraded relay must expose a safe code")
                relay_code = WebConsoleRelayCode(relay_safe_code)
                await _send_ws_frame(websocket, WebConsoleWsError(code=relay_code))
                terminal_code = relay_code.value
                break
            if source_sequence > after_sequence:
                projection_count = len(group.events)
                for ordinal, event in enumerate(group.events):
                    await _send_ws_frame(
                        websocket,
                        WebConsoleWsEvent(
                            event=event,
                            source_cursor=cast(str, group.source_cursor),
                            projection_ordinal=ordinal,
                            projection_count=projection_count,
                        ),
                    )
            if state_snapshot.state in _TERMINAL_STATES:
                terminal_code = state_snapshot.state.value
                break
    except (ValidationError, ValueError, WebConsoleUpstreamError):
        await _send_ws_frame(
            websocket,
            WebConsoleWsError(code=WebConsoleRelayCode.STREAM_INTERRUPTED),
        )
        terminal_code = WebConsoleRelayCode.STREAM_INTERRUPTED.value

    if terminal_code is None:
        await _send_ws_frame(
            websocket,
            WebConsoleWsError(code=WebConsoleRelayCode.STREAM_INTERRUPTED),
        )
        terminal_code = WebConsoleRelayCode.STREAM_INTERRUPTED.value
    await _send_ws_frame(websocket, WebConsoleWsClosed(code=terminal_code))
    await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)


async def _create_with_cookie(
    client: WebConsoleDurablePublicClient,
    payload: DurableCreateRunRequest,
    *,
    cookie_header: str | None,
) -> DurableRunAccepted:
    if type(client) is LoopbackDurablePublicClient:
        return await client.create(payload, cookie_header=cookie_header)
    return await client.create(payload)


async def _status_with_cookie(
    client: WebConsoleDurablePublicClient,
    run_id: str,
    *,
    cookie_header: str | None,
) -> DurableRunStatusResponse:
    if type(client) is LoopbackDurablePublicClient:
        return await client.status(run_id, cookie_header=cookie_header)
    return await client.status(run_id)


async def _cancel_with_cookie(
    client: WebConsoleDurablePublicClient,
    run_id: str,
    *,
    cookie_header: str | None,
) -> DurableRunStatusResponse:
    if type(client) is LoopbackDurablePublicClient:
        return await client.cancel(run_id, cookie_header=cookie_header)
    return await client.cancel(run_id)


async def _relay_local_auth(
    *,
    request: Request,
    method: str,
    path: str,
    payload: dict[str, object] | None,
) -> Response:
    """Relay only fixed local-auth requests and only the resulting Set-Cookie header."""

    client = cast(WebConsoleDurablePublicClient, request.app.state.web_console_durable_client)
    if type(client) is not LoopbackDurablePublicClient:
        raise WebConsoleAdapterError(WebConsoleRelayCode.UPSTREAM_UNAVAILABLE)
    try:
        upstream = await client.local_auth(
            method=method,
            path=path,
            payload=payload,
            cookie_header=_opaque_cookie_header(request),
        )
    except WebConsoleUpstreamError as error:
        raise WebConsoleAdapterError(WebConsoleRelayCode.UPSTREAM_UNAVAILABLE) from error
    content_type = "application/json" if upstream.status_code != 204 else None
    response = Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=content_type,
    )
    for value in upstream.set_cookie:
        response.headers.append("set-cookie", value)
    return response


async def _relay_memory_data(
    *,
    request: Request,
    method: str,
    path: str,
    payload: dict[str, object] | None,
) -> Response:
    """Relay a fixed owner-scoped user-memory operation without WebConsole reading private data."""

    client = cast(WebConsoleDurablePublicClient, request.app.state.web_console_durable_client)
    if type(client) is not LoopbackDurablePublicClient:
        raise WebConsoleAdapterError(WebConsoleRelayCode.UPSTREAM_UNAVAILABLE)
    try:
        upstream = await client.memory_data(
            method=method,
            path=path,
            payload=payload,
            cookie_header=_opaque_cookie_header(request),
        )
    except WebConsoleUpstreamError as error:
        raise WebConsoleAdapterError(WebConsoleRelayCode.UPSTREAM_UNAVAILABLE) from error
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=None if upstream.status_code == 204 else "application/json",
    )


async def _relay_m6_data(
    *,
    request: Request,
    path: str,
) -> Response:
    """Relay one fixed M6 safe read; WebConsole does not inspect any trace content."""

    client = cast(WebConsoleDurablePublicClient, request.app.state.web_console_durable_client)
    if type(client) is not LoopbackDurablePublicClient:
        raise WebConsoleAdapterError(WebConsoleRelayCode.UPSTREAM_UNAVAILABLE)
    try:
        upstream = await client.m6_data(
            path=path,
            cookie_header=_opaque_cookie_header(request),
        )
    except WebConsoleUpstreamError as error:
        raise WebConsoleAdapterError(WebConsoleRelayCode.UPSTREAM_UNAVAILABLE) from error
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type="application/json",
    )


def _validate_m5_thread_id(thread_id: str) -> None:
    if _MEMORY_THREAD_ID.fullmatch(thread_id) is None:
        raise WebConsoleAdapterError(WebConsoleRelayCode.REQUEST_REJECTED)


def _validate_m5_memory_id(entry_id: str) -> None:
    if _MEMORY_ENTRY_ID.fullmatch(entry_id) is None:
        raise WebConsoleAdapterError(WebConsoleRelayCode.REQUEST_REJECTED)


def _validate_m6_run_id(run_id: str) -> None:
    if _M6_RUN_ID.fullmatch(run_id) is None:
        raise WebConsoleAdapterError(WebConsoleRelayCode.REQUEST_REJECTED)


def create_web_console_app(
    *,
    durable_client: WebConsoleDurablePublicClient | None = None,
    static_root: Path | None = None,
) -> FastAPI:
    """Create the side-effect-free WebConsole app; client calls occur only at request time."""

    app = FastAPI(
        title="Glodex Web Console",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.web_console_durable_client = durable_client or LoopbackDurablePublicClient()
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=_MAX_REQUEST_BYTES)
    app.include_router(_ROUTER)
    _mount_console(app, static_root=static_root)

    @app.exception_handler(WebConsoleAdapterError)
    async def web_console_error_handler(
        _request: Request,
        error: WebConsoleAdapterError,
    ) -> JSONResponse:
        status_code = 422 if error.code is WebConsoleRelayCode.REQUEST_REJECTED else 502
        return _error_response(status_code=status_code, code=error.code.value)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request,
        _error: RequestValidationError,
    ) -> JSONResponse:
        return _error_response(status_code=422, code=WebConsoleRelayCode.REQUEST_REJECTED.value)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(
        _request: Request,
        error: StarletteHTTPException,
    ) -> JSONResponse:
        return _error_response(
            status_code=error.status_code,
            code=WebConsoleRelayCode.REQUEST_REJECTED.value,
        )

    return app


def _mount_console(app: FastAPI, *, static_root: Path | None) -> None:
    """Serve only a built local console, never a dev server or remote asset."""

    index = None if static_root is None else static_root / "index.html"
    assets = None if static_root is None else static_root / "assets"
    if index is not None and index.is_file() and assets is not None and assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="web_console-assets")

        @app.get("/", include_in_schema=False)
        async def console_index() -> FileResponse:
            return FileResponse(index)

        return

    @app.get("/", include_in_schema=False)
    async def missing_console_build() -> HTMLResponse:
        return HTMLResponse(
            status_code=503,
            content=(
                "<!doctype html><title>Glodex WebConsole</title>"
                "<p>Local console build is unavailable. "
                "Run pnpm --dir frontend install --frozen-lockfile, then "
                "pnpm --dir frontend run build.</p>"
            ),
        )


async def _project_frame(
    *,
    client: WebConsoleDurablePublicClient,
    state: GlodexWebConsoleState,
    frame: DurablePublicEventFrame,
    cookie_header: str | None,
) -> tuple[ProjectionGroup, int]:
    """Fetch terminal response only when the source event says it is required."""

    sequence = _parse_browser_cursor(frame.event_id, run_id=state.run_id)
    try:
        source = parse_durable_public_event(frame.data)
    except (ValidationError, ValueError, TypeError):
        return project_payload(state=state, payload=frame.data), sequence
    terminal_response = None
    if isinstance(source, AgentResultEvent):
        terminal_response = (
            await _status_with_cookie(client, state.run_id, cookie_header=cookie_header)
        ).response
    return project_event(state=state, event=source, terminal_response=terminal_response), sequence


def _state_from_status(status: DurableRunStatusResponse) -> GlodexWebConsoleState:
    state = initial_state(thread_id=status.thread_id, run_id=status.run_id).model_copy(
        update={"state": status.state, "source_cursor": status.last_event_id}
    )
    if status.state in {DurableRunStateDTO.COMPLETED, DurableRunStateDTO.NO_MATCH}:
        if status.response is None:
            raise WebConsoleAdapterError(WebConsoleRelayCode.PROJECTION_INVALID)
        terminal = WebConsoleTerminalView.from_agent_response(status.response)
        return state.model_copy(update={"terminal": terminal})
    return state


def _parse_browser_cursor(value: str | None, *, run_id: str) -> int:
    if value is None:
        return 0
    prefix, separator, number = value.rpartition(":")
    if separator != ":" or prefix != run_id:
        raise WebConsoleAdapterError(WebConsoleRelayCode.REQUEST_REJECTED)
    try:
        sequence = int(number)
    except ValueError as error:
        raise WebConsoleAdapterError(WebConsoleRelayCode.REQUEST_REJECTED) from error
    if sequence < 1:
        raise WebConsoleAdapterError(WebConsoleRelayCode.REQUEST_REJECTED)
    return sequence


def _error_response(*, status_code: int, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ApiErrorEnvelope(
            error=ApiError(code=code, message="Request could not be processed."),
        ).model_dump(mode="json", by_alias=True),
    )


__all__ = ["WebConsoleAdapterError", "create_web_console_app"]
