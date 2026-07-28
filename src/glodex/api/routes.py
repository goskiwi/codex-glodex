"""Versioned HTTP routes backed by the process-local Run runtime."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Annotated, cast

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import TypeAdapter

from glodex.api.contracts import CreateRunRequest, RunAccepted, RunStatusResponse
from glodex.api.events import InvalidEventCursor, parse_event_cursor
from glodex.api.runtime import (
    RunCoordinator,
    RunNotFound,
    RunRegistry,
    RunSubscription,
)
from glodex.api.settings import ApiSettings
from glodex.contracts import Identifier, RequestRejected, SearchRequest, validate_search_request

router = APIRouter(prefix="/api/v1")
_IDENTIFIER_ADAPTER = TypeAdapter(Identifier)


class EventStreamNotAcceptable(ValueError):
    """The request did not explicitly accept the SSE representation."""


@dataclass(frozen=True, slots=True)
class _SseSession:
    registry: RunRegistry
    subscription: RunSubscription
    heartbeat_seconds: int


async def _release_start(coordinator: RunCoordinator, run_id: str) -> None:
    """Release the runner gate after Starlette has sent the complete 202 body."""

    coordinator.release_start(run_id)


@router.post("/runs", response_model=RunAccepted, status_code=202)
async def create_run(
    payload: CreateRunRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> RunAccepted:
    """Validate, reserve, and defer execution until the 202 response is sent."""

    validated = validate_search_request(payload.request)
    if isinstance(validated, RequestRejected):
        raise RuntimeError("validated transport request failed pre-run validation")
    assert isinstance(validated, SearchRequest)

    coordinator = cast(RunCoordinator, request.app.state.coordinator)
    thread_id = payload.thread_id
    if thread_id is None:
        thread_id_factory = cast(Callable[[], str], request.app.state.thread_id_factory)
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
        status_url=f"{root_path}/api/v1/runs/{accepted.run_id}",
        events_url=f"{root_path}/api/v1/runs/{accepted.run_id}/events",
    )


@router.get("/runs/{run_id}", response_model=RunStatusResponse)
async def get_run(run_id: str, request: Request) -> RunStatusResponse:
    """Read the canonical retained Run snapshot."""

    registry = cast(RunRegistry, request.app.state.registry)
    return registry.status(run_id)


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


async def _prepare_sse_session(
    run_id: str,
    request: Request,
) -> AsyncIterator[_SseSession]:
    """Validate stream preconditions before FastAPI sends a 200 response."""

    if not _accepts_event_stream(request.headers.getlist("accept")):
        raise EventStreamNotAcceptable("SSE representation was not accepted")

    cursor_values = request.headers.getlist("last-event-id")
    if len(cursor_values) > 1:
        raise InvalidEventCursor("Invalid event cursor.")

    registry = cast(RunRegistry, request.app.state.registry)
    settings = cast(ApiSettings, request.app.state.settings)
    last_sequence = registry.next_sequence(run_id) - 1
    after_sequence = parse_event_cursor(
        cursor_values[0] if cursor_values else None,
        run_id=run_id,
        last_sequence=last_sequence,
    )
    subscription = registry.subscribe(run_id, after_sequence=after_sequence)
    try:
        yield _SseSession(
            registry=registry,
            subscription=subscription,
            heartbeat_seconds=settings.sse_heartbeat_seconds,
        )
    finally:
        registry.unsubscribe(subscription)


@router.get("/runs/{run_id}/events", response_class=EventSourceResponse)
async def get_run_events(
    session: Annotated[_SseSession, Depends(_prepare_sse_session)],
) -> AsyncIterator[ServerSentEvent]:
    """Replay retained events, then follow the Run without coupling its lifecycle."""

    subscription = session.subscription
    try:
        while True:
            try:
                batch = session.registry.read(subscription)
            except RunNotFound:
                return

            for event in batch.events:
                yield ServerSentEvent(
                    data=event.model_dump(mode="json", by_alias=True),
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


__all__ = ["EventStreamNotAcceptable", "router"]
