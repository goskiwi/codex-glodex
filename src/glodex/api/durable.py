"""Side-effect-free FastAPI factory for the durable Agent API."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast

from fastapi import APIRouter, FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from glodex.agent.contracts import AgentDemoResponse
from glodex.api.agent_events import AgentEventProjector
from glodex.api.body_limit import RequestBodyLimitMiddleware
from glodex.api.contracts import ApiError, ApiErrorEnvelope
from glodex.api.durable_contracts import (
    DurableCreateRunRequest,
    DurableRunAccepted,
    DurableRunStateDTO,
    DurableRunStatusResponse,
)
from glodex.api.identity_contracts import LocalAuthStatus, LocalCredentials
from glodex.api.memory_contracts import (
    ConversationThreadList,
    ConversationThreadView,
    ConversationTurnPage,
    ConversationTurnView,
    MemoryEntryInput,
    MemoryEntryView,
    MemoryManagementView,
)
from glodex.api.observability_contracts import (
    M6AlertView,
    M6BreakerView,
    M6CostEstimateView,
    M6CostTotalView,
    M6OperationsView,
    M6TraceEventView,
    M6TraceView,
    M6UsageReceiptView,
)
from glodex.api.quality_contracts import M7OfflineSummaryView, M7P2SummaryView
from glodex.api.sse_cursor import InvalidSseCursor, parse_sse_cursor
from glodex.contracts import RequestRejected, SearchRequest, validate_search_request
from glodex.infrastructure.postgres import DurablePostgresStore
from glodex.memory.conversation import (
    ConversationRequestError,
    ConversationRequestResolver,
)
from glodex.memory.identity import (
    LocalIdentityError,
    LocalSession,
    LocalUser,
    hash_password,
    new_session_token,
    new_user_id,
    password_matches,
    session_expiry,
    session_token_hash,
    validate_username,
)
from glodex.memory.models import (
    ConversationRole,
    UserMemoryCategory,
    UserMemoryEntry,
)
from glodex.memory.normalization import normalize_blacklist_rule
from glodex.memory.terminal import MemoryTerminalWriterPort
from glodex.observability.exporter import M6TraceExporter
from glodex.observability.runtime import (
    M6OperationsView as M6OperationsDomainView,
)
from glodex.observability.runtime import (
    M6OperationsWindow,
    M6RunTrace,
)
from glodex.quality.runtime import M7OfflineSummary
from glodex.runtime.contracts import DurableRun, DurableRunState, DurableStoreError
from glodex.runtime.ports import (
    M6OperationsCachePort,
    M6TraceStorePort,
    RetrievalCachePort,
)
from glodex.runtime.service import DurableAgentCoordinator, DurableAgentExecutor

_ROUTER = APIRouter(prefix="/api/v1")
_HEARTBEAT_SECONDS = 15
_POLL_SECONDS = 0.20
_MAX_REQUEST_BYTES = 65_536
_SESSION_COOKIE = "glodex_local_session"
_SESSION_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


class DurableAgentEventStreamNotAcceptable(ValueError):
    """The durable event endpoint requires explicit SSE negotiation."""


class SemanticQueryCanaryDisabled(ValueError):
    """Raised before run allocation when literal semantic filters are not enabled."""


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
    store = cast(DurablePostgresStore, request.app.state.durable_store)
    session = await _required_session(request)
    if (
        not validated.semantic_filters.is_empty
        and request.app.state.semantic_query_canary_enabled is not True
    ):
        raise SemanticQueryCanaryDisabled("semantic query canary is disabled")
    thread_id = payload.thread_id or cast(Callable[[], str], request.app.state.thread_id_factory)()
    await store.bind_user_thread(user_id=session.user.user_id, thread_id=thread_id)
    resolver = ConversationRequestResolver(store=store)
    resolution = await resolver.resolve(thread_id=thread_id, current=validated)
    await store.append_conversation_turn(
        thread_id=thread_id,
        role=ConversationRole.USER,
        display_content=validated.query,
    )
    run = await coordinator.submit(
        request=resolution.request,
        display_query=validated.query,
        task_turns=resolution.task_turns,
        thread_id=thread_id,
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
    store = cast(DurablePostgresStore, request.app.state.durable_store)
    return _status_response(await _load_owned_run(request=request, store=store, run_id=run_id))


@_ROUTER.post(
    "/durable-agent-runs/{run_id}/cancel",
    response_model=DurableRunStatusResponse,
)
async def _cancel_durable_agent_run(run_id: str, request: Request) -> DurableRunStatusResponse:
    store = cast(DurablePostgresStore, request.app.state.durable_store)
    await _load_owned_run(request=request, store=store, run_id=run_id)
    coordinator = cast(DurableAgentCoordinator, request.app.state.durable_coordinator)
    return _status_response(await coordinator.cancel(run_id=run_id))


@_ROUTER.post(
    "/durable-agent-runs/{run_id}/resume",
    response_model=DurableRunStatusResponse,
)
async def _resume_durable_agent_run(run_id: str, request: Request) -> DurableRunStatusResponse:
    store = cast(DurablePostgresStore, request.app.state.durable_store)
    await _load_owned_run(request=request, store=store, run_id=run_id)
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
        raise InvalidSseCursor("Invalid event cursor.")
    store = cast(DurablePostgresStore, request.app.state.durable_store)
    run = await _load_owned_run(request=request, store=store, run_id=run_id)
    after_sequence = parse_sse_cursor(
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


@_ROUTER.post(
    "/local-auth/register", response_model=LocalAuthStatus, status_code=status.HTTP_201_CREATED
)
async def _register_local_account(
    payload: LocalCredentials,
    request: Request,
    response: Response,
) -> LocalAuthStatus:
    """Register a loopback-only account and issue an opaque HttpOnly cookie."""

    user = LocalUser(user_id=new_user_id(), username=validate_username(payload.username))
    store = cast(DurablePostgresStore, request.app.state.durable_store)
    await store.register_local_user(user=user, password_hash=hash_password(payload.password))
    return await _issue_session(store=store, user=user, response=response)


@_ROUTER.post("/local-auth/login", response_model=LocalAuthStatus)
async def _login_local_account(
    payload: LocalCredentials,
    request: Request,
    response: Response,
) -> LocalAuthStatus:
    """Authenticate a local account without returning a bearer token in JSON."""

    store = cast(DurablePostgresStore, request.app.state.durable_store)
    user_and_hash = await store.password_hash_for_username(
        username=validate_username(payload.username)
    )
    if user_and_hash is None or not password_matches(
        password=payload.password,
        password_hash=user_and_hash[1],
    ):
        raise DurableStoreError("DURABLE_AUTH_INVALID")
    return await _issue_session(store=store, user=user_and_hash[0], response=response)


@_ROUTER.get("/local-auth/me", response_model=LocalAuthStatus)
async def _get_local_account(request: Request) -> LocalAuthStatus:
    """Return only the safe projection of the current loopback identity."""

    return _session_status(await _required_session(request))


@_ROUTER.post("/local-auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def _logout_local_account(request: Request) -> Response:
    """Revoke the presented token if valid and always erase the browser cookie."""

    store = cast(DurablePostgresStore, request.app.state.durable_store)
    token_hash = _request_session_token_hash(request)
    if token_hash is not None:
        await store.revoke_local_session(token_hash=token_hash)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_session_cookie(response)
    return response


@_ROUTER.delete("/local-auth/me", status_code=status.HTTP_204_NO_CONTENT)
async def _delete_local_account(request: Request) -> Response:
    """Tombstone the current account and revoke all of its active sessions."""

    session = await _required_session(request)
    store = cast(DurablePostgresStore, request.app.state.durable_store)
    await store.delete_local_user(user_id=session.user.user_id)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_session_cookie(response)
    return response


@_ROUTER.get("/conversation-threads", response_model=ConversationThreadList)
async def _list_conversation_threads(request: Request) -> ConversationThreadList:
    session = await _required_session(request)
    store = cast(DurablePostgresStore, request.app.state.durable_store)
    threads = await store.list_user_threads(user_id=session.user.user_id)
    return ConversationThreadList(
        threads=tuple(
            ConversationThreadView(thread_id=thread.thread_id, turn_count=thread.turn_count)
            for thread in threads
        )
    )


@_ROUTER.get(
    "/conversation-threads/{thread_id}/turns",
    response_model=ConversationTurnPage,
)
async def _list_conversation_turns(
    thread_id: str,
    request: Request,
    before_ordinal: int | None = None,
) -> ConversationTurnPage:
    store = cast(DurablePostgresStore, request.app.state.durable_store)
    await _require_owned_thread(request=request, store=store, thread_id=thread_id)
    page = await store.list_conversation_turn_page(
        thread_id=thread_id,
        before_ordinal=before_ordinal,
    )
    return ConversationTurnPage(
        thread_id=page.thread_id,
        turns=tuple(
            ConversationTurnView(
                ordinal=turn.ordinal,
                role=turn.role.value,
                content=turn.display_content,
                terminal_run_id=turn.terminal_run_id,
            )
            for turn in page.turns
        ),
        next_before_ordinal=page.next_before_ordinal,
    )


@_ROUTER.delete("/conversation-threads/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
async def _delete_conversation_thread(thread_id: str, request: Request) -> Response:
    store = cast(DurablePostgresStore, request.app.state.durable_store)
    session = await _require_owned_thread(request=request, store=store, thread_id=thread_id)
    if not await store.delete_user_thread(user_id=session.user.user_id, thread_id=thread_id):
        raise DurableStoreError("DURABLE_OWNER_FORBIDDEN")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@_ROUTER.get("/memory", response_model=MemoryManagementView)
async def _list_user_memory(request: Request) -> MemoryManagementView:
    session = await _required_session(request)
    store = cast(DurablePostgresStore, request.app.state.durable_store)
    entries = await store.list_active_memory(user_id=session.user.user_id, limit=128)
    return MemoryManagementView(
        active_entries=tuple(_memory_view(entry) for entry in entries),
    )


@_ROUTER.post("/memory", response_model=MemoryEntryView, status_code=status.HTTP_201_CREATED)
async def _create_user_memory(payload: MemoryEntryInput, request: Request) -> MemoryEntryView:
    session = await _required_session(request)
    store = cast(DurablePostgresStore, request.app.state.durable_store)
    category = UserMemoryCategory(payload.category)
    content = payload.content.strip()
    if category is UserMemoryCategory.BLACKLIST:
        content = normalize_blacklist_rule(content)
    return _memory_view(
        await store.create_manual_memory(
            user_id=session.user.user_id,
            category=category,
            content=content,
        )
    )


@_ROUTER.put("/memory/{entry_id}", response_model=MemoryEntryView)
async def _replace_user_memory(
    entry_id: str,
    payload: MemoryEntryInput,
    request: Request,
) -> MemoryEntryView:
    session = await _required_session(request)
    store = cast(DurablePostgresStore, request.app.state.durable_store)
    category = UserMemoryCategory(payload.category)
    content = payload.content.strip()
    if category is UserMemoryCategory.BLACKLIST:
        content = normalize_blacklist_rule(content)
    entry = await store.replace_memory_as_manual(
        user_id=session.user.user_id,
        entry_id=entry_id,
        category=category,
        content=content,
    )
    if entry is None:
        raise DurableStoreError("DURABLE_OWNER_FORBIDDEN")
    return _memory_view(entry)


@_ROUTER.delete("/memory/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def _delete_user_memory(entry_id: str, request: Request) -> Response:
    session = await _required_session(request)
    store = cast(DurablePostgresStore, request.app.state.durable_store)
    if not await store.tombstone_memory(user_id=session.user.user_id, entry_id=entry_id):
        raise DurableStoreError("DURABLE_OWNER_FORBIDDEN")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@_ROUTER.get("/m6/runs/{run_id}/trace", response_model=M6TraceView)
async def _get_m6_trace(run_id: str, request: Request) -> M6TraceView:
    """Return one owner-checked safe trace, never raw events or private run payloads."""

    store = cast(DurablePostgresStore, request.app.state.durable_store)
    await _load_owned_run(request=request, store=store, run_id=run_id)
    return _m6_trace_view(await store.load_m6_trace(run_id=run_id))


@_ROUTER.get("/m6/operations", response_model=M6OperationsView)
async def _get_m6_operations(
    request: Request,
    window: str = "1h",
) -> M6OperationsView:
    """Return only local aggregate facts to an authenticated loopback account."""

    await _required_session(request)
    try:
        parsed_window = M6OperationsWindow(window)
    except ValueError:
        raise DurableStoreError("DURABLE_M6_OPERATIONS_INVALID") from None
    store = cast(DurablePostgresStore, request.app.state.durable_store)
    cache = cast(M6OperationsCachePort | None, request.app.state.m6_operations_cache)
    if cache is not None:
        cached = await cache.get_m6_operations_projection(window=parsed_window)
        if cached is not None:
            return _m6_operations_view(cached)
    values = await store.load_m6_operations(window=parsed_window, now=datetime.now(UTC))
    if cache is not None:
        await cache.set_m6_operations_projection(value=values, ttl_seconds=300)
    return _m6_operations_view(values)


@_ROUTER.get("/m7/runs/{run_id}/summary", response_model=M7OfflineSummaryView)
async def _get_m7_offline_summary(run_id: str, request: Request) -> M7OfflineSummaryView:
    """Return only the owner-scoped display summary of an offline evaluation."""

    store = cast(DurablePostgresStore, request.app.state.durable_store)
    await _load_owned_run(request=request, store=store, run_id=run_id)
    summary = await store.load_m7_offline_summary(run_id=run_id)
    if summary is None:
        raise DurableStoreError("DURABLE_M7_SUMMARY_NOT_FOUND")
    return _m7_summary_view(summary)


async def _issue_session(
    *,
    store: DurablePostgresStore,
    user: LocalUser,
    response: Response,
) -> LocalAuthStatus:
    """Persist a hash-only session then attach its opaque value in an HttpOnly cookie."""

    token = new_session_token()
    expires_at = session_expiry()
    session = await store.issue_local_session(
        token_hash=session_token_hash(token),
        user_id=user.user_id,
        expires_at=expires_at,
    )
    response.set_cookie(
        key=_SESSION_COOKIE,
        value=token,
        max_age=_SESSION_MAX_AGE_SECONDS,
        path="/",
        httponly=True,
        samesite="strict",
        secure=False,
    )
    return _session_status(session)


def _session_status(session: LocalSession) -> LocalAuthStatus:
    return LocalAuthStatus(
        username=session.user.username,
        expires_at=session.expires_at.isoformat(),
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=_SESSION_COOKIE, path="/", httponly=True, samesite="strict")


def _request_session_token_hash(request: Request) -> str | None:
    raw_token = request.cookies.get(_SESSION_COOKIE)
    if raw_token is None:
        return None
    try:
        return session_token_hash(raw_token)
    except LocalIdentityError:
        return None


async def _optional_session(request: Request) -> LocalSession | None:
    token_hash = _request_session_token_hash(request)
    if token_hash is None:
        return None
    store = cast(DurablePostgresStore, request.app.state.durable_store)
    return await store.session_for_token_hash(token_hash=token_hash)


async def _required_session(request: Request) -> LocalSession:
    session = await _optional_session(request)
    if session is None:
        raise DurableStoreError("DURABLE_AUTH_REQUIRED")
    return session


async def _load_owned_run(
    *,
    request: Request,
    store: DurablePostgresStore,
    run_id: str,
) -> DurableRun:
    """Derive the only admissible run owner from the current authenticated session."""

    session = await _required_session(request)
    run = await store.load_run(run_id=run_id)
    owner = await store.thread_owner(thread_id=run.thread_id)
    if owner is None:
        raise DurableStoreError("DURABLE_OWNER_FORBIDDEN")
    if session.user.user_id != owner:
        raise DurableStoreError("DURABLE_OWNER_FORBIDDEN")
    return run


async def _require_owned_thread(
    *,
    request: Request,
    store: DurablePostgresStore,
    thread_id: str,
) -> LocalSession:
    """Authorize history access with the same owner boundary as durable runs."""

    session = await _required_session(request)
    owner = await store.thread_owner(thread_id=thread_id)
    if owner is None or owner != session.user.user_id:
        raise DurableStoreError("DURABLE_OWNER_FORBIDDEN")
    return session


def _memory_view(entry: UserMemoryEntry) -> MemoryEntryView:
    return MemoryEntryView(
        entry_id=entry.entry_id,
        category=entry.category.value,
        content=entry.content,
        revision=entry.revision,
        origin=entry.origin.value,
        source_thread_id=entry.source_thread_id,
    )


def _m6_trace_view(trace: M6RunTrace) -> M6TraceView:
    return M6TraceView(
        run_id=trace.run_id,
        terminal_state=trace.terminal_state,
        events=tuple(
            M6TraceEventView(
                sequence=event.sequence,
                kind=event.draft.kind.value,
                operation=(None if event.draft.operation is None else event.draft.operation.value),
                outcome=None if event.draft.outcome is None else event.draft.outcome.value,
                safe_code=event.draft.safe_code,
                duration_ms=event.draft.duration_ms,
                version=event.draft.version,
                receipt=(
                    None
                    if event.draft.receipt is None
                    else M6UsageReceiptView(
                        status=event.draft.receipt.status.value,
                        provider=event.draft.receipt.provider,
                        model=event.draft.receipt.model,
                        input_tokens=event.draft.receipt.input_tokens,
                        output_tokens=event.draft.receipt.output_tokens,
                        total_tokens=event.draft.receipt.total_tokens,
                    )
                ),
                cost=(
                    None
                    if event.draft.cost is None
                    else M6CostEstimateView(
                        status=event.draft.cost.status.value,
                        price_table_version=event.draft.cost.price_table_version,
                        currency=event.draft.cost.currency,
                        input_micro_units=event.draft.cost.input_micro_units,
                        output_micro_units=event.draft.cost.output_micro_units,
                    )
                ),
            )
            for event in trace.events
        ),
    )


def _m7_summary_view(summary: M7OfflineSummary) -> M7OfflineSummaryView:
    return M7OfflineSummaryView(
        run_id=summary.run_id,
        judge_status=summary.judge_status.value,
        p0_passed=summary.p0_passed,
        p0_failure_count=summary.p0_failure_count,
        p1_failure_count=summary.p1_failure_count,
        p2_scores=tuple(
            M7P2SummaryView(dimension=value.dimension.value, score=value.score)
            for value in summary.p2_scores
        ),
        reward=summary.reward,
        training_candidate=summary.training_candidate,
        judge_model=summary.judge_model,
        generated_at=summary.generated_at.isoformat(),
    )


def _m6_operations_view(values: M6OperationsDomainView) -> M6OperationsView:
    return M6OperationsView(
        window=values.window.value,
        completed_count=values.completed_count,
        no_match_count=values.no_match_count,
        failed_count=values.failed_count,
        aborted_count=values.aborted_count,
        operation_count=values.operation_count,
        operation_failure_count=values.operation_failure_count,
        receipt_reported_count=values.receipt_reported_count,
        receipt_unavailable_count=values.receipt_unavailable_count,
        cost_totals=tuple(
            M6CostTotalView(currency=value.currency, micro_units=value.micro_units)
            for value in values.cost_totals
        ),
        breakers=tuple(
            M6BreakerView(operation=value.operation.value, state=value.state.value)
            for value in values.breakers
        ),
        alerts=tuple(M6AlertView(code=value.code.value) for value in values.alerts),
    )


async def _event_stream(
    *,
    store: DurablePostgresStore,
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
            raise DurableStoreError("DURABLE_STORE_DATA_INVALID")
        try:
            # PostgreSQL JSONB faithfully returns JSON arrays as Python lists, while
            # the strict public DTO intentionally owns tuple fields. Re-validate in
            # JSON mode so persisted canonical JSON is interpreted by its wire
            # contract rather than by the stricter in-process construction path.
            response = AgentDemoResponse.model_validate_json(
                json.dumps(run.terminal_response, ensure_ascii=False, separators=(",", ":"))
            )
        except Exception as validation_error:
            raise DurableStoreError("DURABLE_STORE_DATA_INVALID") from validation_error
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
    if isinstance(error, LocalIdentityError):
        return _error_response(
            status_code=422,
            code="REQUEST_REJECTED",
            message="Request validation failed.",
        )
    if isinstance(error, SemanticQueryCanaryDisabled):
        return _error_response(
            status_code=422,
            code="REQUEST_REJECTED",
            message="Request validation failed.",
        )
    if isinstance(error, ConversationRequestError):
        return _error_response(
            status_code=422,
            code="CONVERSATION_CONTEXT_TOO_LARGE",
            message="Current conversation conditions exceed the request limit.",
        )
    if isinstance(error, DurableAgentEventStreamNotAcceptable):
        return _error_response(
            status_code=406,
            code="NOT_ACCEPTABLE",
            message="An event-stream representation is required.",
        )
    if isinstance(error, InvalidSseCursor):
        return _error_response(
            status_code=400,
            code="INVALID_EVENT_CURSOR",
            message="Event cursor is invalid.",
        )
    if isinstance(error, DurableStoreError):
        code = str(error)
        if code in {"DURABLE_AUTH_REQUIRED", "DURABLE_AUTH_INVALID"}:
            return _error_response(
                status_code=401,
                code="AUTH_REQUIRED",
                message="Authentication is required.",
            )
        if code == "DURABLE_USERNAME_TAKEN":
            return _error_response(
                status_code=409,
                code="USERNAME_UNAVAILABLE",
                message="Requested account name is unavailable.",
            )
        if code in {"DURABLE_OWNER_FORBIDDEN", "DURABLE_PROFILE_FORBIDDEN"}:
            return _error_response(
                status_code=403,
                code="OWNER_FORBIDDEN",
                message="Requested resource is unavailable.",
            )
        if code in {
            "DURABLE_RUN_NOT_FOUND",
            "DURABLE_M6_TRACE_NOT_FOUND",
            "DURABLE_M7_SUMMARY_NOT_FOUND",
        }:
            return _error_response(
                status_code=404,
                code=(
                    "RUN_NOT_FOUND"
                    if code == "DURABLE_RUN_NOT_FOUND"
                    else "TRACE_NOT_FOUND"
                    if code == "DURABLE_M6_TRACE_NOT_FOUND"
                    else "EVALUATION_NOT_FOUND"
                ),
                message=(
                    "Run was not found."
                    if code == "DURABLE_RUN_NOT_FOUND"
                    else "Trace was not found."
                    if code == "DURABLE_M6_TRACE_NOT_FOUND"
                    else "Offline evaluation summary was not found."
                ),
            )
        if code == "DURABLE_RUN_ALREADY_ACTIVE":
            return _error_response(
                status_code=409,
                code="RUN_ALREADY_ACTIVE",
                message="Thread already has an active Run.",
            )
        if code in {
            "DURABLE_STORE_UNAVAILABLE",
            "DURABLE_PROFILE_UNAVAILABLE",
            "DURABLE_AUTH_UNAVAILABLE",
            "DURABLE_HISTORY_UNAVAILABLE",
            "DURABLE_MEMORY_UNAVAILABLE",
            "DURABLE_M6_TRACE_UNAVAILABLE",
            "DURABLE_M6_OPERATIONS_UNAVAILABLE",
            "DURABLE_M6_BREAKER_UNAVAILABLE",
            "DURABLE_M7_SUMMARY_UNAVAILABLE",
        }:
            return _error_response(
                status_code=503,
                code="DURABLE_STORE_UNAVAILABLE",
                message="Durable runtime storage is unavailable.",
            )
        if code == "DURABLE_MEMORY_CONFLICT":
            return _error_response(
                status_code=409,
                code="MEMORY_CONFLICT",
                message="A matching memory entry already exists.",
            )
        if code in {"DURABLE_REMOTE_STEP_UNCERTAIN", "DURABLE_RUN_NOT_RECOVERABLE"}:
            return _error_response(
                status_code=409,
                code="RUN_NOT_RECOVERABLE",
                message="Run cannot be recovered from its current checkpoint.",
            )
        if code == "DURABLE_M6_OPERATIONS_INVALID":
            return _error_response(
                status_code=422,
                code="REQUEST_REJECTED",
                message="Request validation failed.",
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
    store: DurablePostgresStore,
    projector: AgentEventProjector,
    asset_version: str,
    config_fingerprint: str,
    thread_id_factory: Callable[[], str] | None = None,
    context_cache: RetrievalCachePort | None = None,
    m6_operations_cache: M6OperationsCachePort | None = None,
    terminal_writer: MemoryTerminalWriterPort | None = None,
    m6_trace_store: M6TraceStorePort | None = None,
    m6_trace_exporter: M6TraceExporter | None = None,
    startup_callback: Callable[[], Awaitable[None]] | None = None,
    shutdown_callback: Callable[[], Awaitable[None]] | None = None,
    semantic_query_canary_enabled: bool = False,
) -> FastAPI:
    """Compose one Durable API without import-time I/O or default-provider behavior."""

    if type(semantic_query_canary_enabled) is not bool:
        raise TypeError("semantic query canary flag is invalid")

    coordinator = DurableAgentCoordinator(
        store=store,
        executor=executor,
        projector=projector,
        asset_version=asset_version,
        config_fingerprint=config_fingerprint,
        context_cache=context_cache,
        terminal_writer=terminal_writer,
        m6_trace_store=m6_trace_store,
        m6_trace_exporter=m6_trace_exporter,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await store.open()
        try:
            if startup_callback is not None:
                await startup_callback()
            yield
        finally:
            await coordinator.shutdown()
            if shutdown_callback is not None:
                await shutdown_callback()
            await store.close()

    app = FastAPI(
        title="Glodex Durable Agent API",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.durable_store = store
    app.state.durable_coordinator = coordinator
    app.state.m6_operations_cache = m6_operations_cache
    app.state.semantic_query_canary_enabled = semantic_query_canary_enabled
    app.state.thread_id_factory = thread_id_factory or _new_thread_id
    app.include_router(_ROUTER)
    app.add_exception_handler(RequestValidationError, _request_validation_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(DurableAgentEventStreamNotAcceptable, _runtime_exception_handler)
    app.add_exception_handler(SemanticQueryCanaryDisabled, _runtime_exception_handler)
    app.add_exception_handler(ConversationRequestError, _runtime_exception_handler)
    app.add_exception_handler(InvalidSseCursor, _runtime_exception_handler)
    app.add_exception_handler(LocalIdentityError, _runtime_exception_handler)
    app.add_exception_handler(DurableStoreError, _runtime_exception_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)
    app.add_middleware(_DurableAgentJsonContentTypeMiddleware)
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=_MAX_REQUEST_BYTES)
    return app


__all__ = [
    "DurableAgentEventStreamNotAcceptable",
    "create_durable_agent_app",
]
