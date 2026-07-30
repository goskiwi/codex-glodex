"""Contract tests for the independent M1d Agent HTTP and SSE surface."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from starlette.types import Message, Scope

import glodex.api as search_api
from glodex.api.agent_app import create_agent_app
from glodex.api.agent_contracts import AgentRunStatusResponse
from glodex.api.agent_events import AgentEventProjector
from glodex.api.agent_runtime import AgentRunRegistry
from glodex.api.contracts import ApiError, ProjectionStatus, RunState
from glodex.api.settings import ApiSettings
from glodex.application.agent.contracts import (
    AgentAnswer,
    AgentAnswerKind,
    AgentDemoResponse,
    AgentEventKind,
    AgentEventScope,
    AgentExecution,
    AgentRunEvent,
    AgentRunRecord,
    DataMode,
    ToolName,
)
from glodex.application.agent.ports import AgentEventObserver
from glodex.application.ports import RunEventObserver
from glodex.application.search_service import SearchExecution
from glodex.contracts import RunStatus, SearchRequest, SearchResponse

pytestmark = [
    pytest.mark.contract,
    pytest.mark.spec(
        "GLO-M1D-P0-001",
        "GLO-M1D-P0-006",
        "GLO-M1D-NFR-001",
        "GLO-M1D-NFR-004",
        "GLO-M1D-NFR-005",
        "GLO-M1D-NFR-006",
    ),
]

_NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


class _Clock:
    def now_utc(self) -> datetime:
        return _NOW

    def monotonic_ns(self) -> int:
        return 1_000_000_000


class _RunIds:
    def __init__(self, *values: str) -> None:
        self._values = iter(values)
        self.calls = 0

    def next_run_id(self) -> str:
        self.calls += 1
        return next(self._values)


class _CompletedService:
    def __init__(self, *, timeline: list[str] | None = None) -> None:
        self.calls = 0
        self.timeline = timeline

    async def execute_run(
        self,
        request: SearchRequest,
        *,
        run_id: str,
        observer: AgentEventObserver | None = None,
    ) -> AgentExecution:
        del request
        self.calls += 1
        if self.timeline is not None:
            self.timeline.append("service_execute")
        events = _completed_events(run_id)
        assert observer is not None
        for event in events:
            observer.on_event(event)
        response = AgentDemoResponse(
            run_id=run_id,
            status=RunStatus.COMPLETED,
            answer=AgentAnswer(
                kind=AgentAnswerKind.CHAT_FALLBACK,
                text="This demo request is outside the shopping path.",
            ),
        )
        return AgentExecution(
            response=response,
            record=AgentRunRecord(
                run_id=run_id,
                status=RunStatus.COMPLETED,
                model_calls=1,
                tool_calls=1,
                child_runs=0,
                events=events,
            ),
        )


class _FailedService:
    def __init__(self) -> None:
        self.calls = 0

    async def execute_run(
        self,
        request: SearchRequest,
        *,
        run_id: str,
        observer: AgentEventObserver | None = None,
    ) -> AgentExecution:
        del request
        self.calls += 1
        events = (
            AgentRunEvent(kind=AgentEventKind.AGENT_STARTED, run_id=run_id),
            AgentRunEvent(
                kind=AgentEventKind.AGENT_ERROR,
                run_id=run_id,
                status="FAILED",
                safe_code="INTERNAL_ERROR",
            ),
        )
        assert observer is not None
        for event in events:
            observer.on_event(event)
        response = AgentDemoResponse(
            run_id=run_id,
            status=RunStatus.FAILED,
        )
        return AgentExecution(
            response=response,
            record=AgentRunRecord(
                run_id=run_id,
                status=RunStatus.FAILED,
                model_calls=0,
                tool_calls=0,
                child_runs=0,
                terminal_code="INTERNAL_ERROR",
                events=events,
            ),
        )


class _NoMatchService:
    async def execute_run(
        self,
        request: SearchRequest,
        *,
        run_id: str,
        observer: AgentEventObserver | None = None,
    ) -> AgentExecution:
        events = _tool_terminal_events(
            run_id,
            tool_name=ToolName.SHOPPING_SUMMARY,
            status="NO_MATCH",
        )
        assert observer is not None
        for event in events:
            observer.on_event(event)
        search_response = SearchResponse(
            run_id=run_id,
            status=RunStatus.NO_MATCH,
            snapshot_version=request.snapshot_version or "m1d-demo-v1",
            config_fingerprint="a" * 64,
            algorithm_version="agent-contract-v1",
        )
        response = AgentDemoResponse(
            run_id=run_id,
            status=RunStatus.NO_MATCH,
            answer=AgentAnswer(
                kind=AgentAnswerKind.SHOPPING_SUMMARY,
                text="No eligible products matched.",
            ),
            search_response=search_response,
        )
        return AgentExecution(
            response=response,
            record=AgentRunRecord(
                run_id=run_id,
                status=RunStatus.NO_MATCH,
                model_calls=1,
                tool_calls=1,
                child_runs=0,
                events=events,
            ),
        )


class _BlockingService:
    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cleaned = asyncio.Event()
        self.registry: AgentRunRegistry | None = None
        self.state_during_cleanup: RunState | None = None

    async def execute_run(
        self,
        request: SearchRequest,
        *,
        run_id: str,
        observer: AgentEventObserver | None = None,
    ) -> AgentExecution:
        del request
        self.calls += 1
        events = list(_completed_events(run_id))
        assert observer is not None
        observer.on_event(events[0])
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            if self.registry is not None:
                self.state_during_cleanup = self.registry.status(run_id).state
            self.cleaned.set()
            raise
        for event in events[1:]:
            observer.on_event(event)
        response = AgentDemoResponse(
            run_id=run_id,
            status=RunStatus.COMPLETED,
            answer=AgentAnswer(
                kind=AgentAnswerKind.CHAT_FALLBACK,
                text="Safe fallback.",
            ),
        )
        return AgentExecution(
            response=response,
            record=AgentRunRecord(
                run_id=run_id,
                status=RunStatus.COMPLETED,
                model_calls=1,
                tool_calls=1,
                child_runs=0,
                events=tuple(events),
            ),
        )


class _PrematureTerminalBlockingService(_BlockingService):
    async def execute_run(
        self,
        request: SearchRequest,
        *,
        run_id: str,
        observer: AgentEventObserver | None = None,
    ) -> AgentExecution:
        del request
        self.calls += 1
        events = _completed_events(run_id)
        assert observer is not None
        for event in events:
            observer.on_event(event)
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            if self.registry is not None:
                self.state_during_cleanup = self.registry.status(run_id).state
            self.cleaned.set()
            raise
        response = AgentDemoResponse(
            run_id=run_id,
            status=RunStatus.COMPLETED,
            answer=AgentAnswer(
                kind=AgentAnswerKind.CHAT_FALLBACK,
                text="Safe fallback.",
            ),
        )
        return AgentExecution(
            response=response,
            record=AgentRunRecord(
                run_id=run_id,
                status=RunStatus.COMPLETED,
                model_calls=1,
                tool_calls=1,
                child_runs=0,
                events=events,
            ),
        )


class _InterleavedService:
    async def execute_run(
        self,
        request: SearchRequest,
        *,
        run_id: str,
        observer: AgentEventObserver | None = None,
    ) -> AgentExecution:
        del request
        events = _interleaved_events(run_id)
        assert observer is not None
        for event in events:
            observer.on_event(event)
        response = AgentDemoResponse(
            run_id=run_id,
            status=RunStatus.COMPLETED,
            answer=AgentAnswer(
                kind=AgentAnswerKind.CHAT_FALLBACK,
                text="Safe fallback.",
            ),
        )
        return AgentExecution(
            response=response,
            record=AgentRunRecord(
                run_id=run_id,
                status=RunStatus.COMPLETED,
                model_calls=4,
                tool_calls=4,
                child_runs=2,
                events=events,
            ),
        )


class _MalformedBracketService:
    async def execute_run(
        self,
        request: SearchRequest,
        *,
        run_id: str,
        observer: AgentEventObserver | None = None,
    ) -> AgentExecution:
        del request
        events = (
            AgentRunEvent(kind=AgentEventKind.AGENT_STARTED, run_id=run_id),
            AgentRunEvent(
                kind=AgentEventKind.MODEL_STARTED,
                run_id=run_id,
                scope=AgentEventScope.CHILD,
                child_id="child-outside-bracket",
                depth=1,
                round=1,
            ),
            AgentRunEvent(
                kind=AgentEventKind.AGENT_RESULT,
                run_id=run_id,
                status="COMPLETED",
            ),
        )
        assert observer is not None
        for event in events:
            observer.on_event(event)
        response = AgentDemoResponse(
            run_id=run_id,
            status=RunStatus.COMPLETED,
            answer=AgentAnswer(
                kind=AgentAnswerKind.CHAT_FALLBACK,
                text="Safe fallback.",
            ),
        )
        return AgentExecution(
            response=response,
            record=AgentRunRecord(
                run_id=run_id,
                status=RunStatus.COMPLETED,
                model_calls=1,
                tool_calls=0,
                child_runs=0,
                events=events,
            ),
        )


class _NeverSearchService:
    async def execute_run(
        self,
        request: SearchRequest,
        *,
        run_id: str,
        observer: RunEventObserver | None = None,
    ) -> SearchExecution:
        del request, run_id, observer
        raise AssertionError("default Search service must not execute")


def _completed_events(run_id: str) -> tuple[AgentRunEvent, ...]:
    return (
        AgentRunEvent(kind=AgentEventKind.AGENT_STARTED, run_id=run_id),
        AgentRunEvent(
            kind=AgentEventKind.MODEL_STARTED,
            run_id=run_id,
            round=1,
        ),
        AgentRunEvent(
            kind=AgentEventKind.MODEL_FINISHED,
            run_id=run_id,
            round=1,
            tool_name=ToolName.CHAT_FALLBACK,
        ),
        AgentRunEvent(
            kind=AgentEventKind.TOOL_STARTED,
            run_id=run_id,
            tool_name=ToolName.CHAT_FALLBACK,
        ),
        AgentRunEvent(
            kind=AgentEventKind.TOOL_FINISHED,
            run_id=run_id,
            tool_name=ToolName.CHAT_FALLBACK,
            safe_code="FALLBACK_READY",
        ),
        AgentRunEvent(
            kind=AgentEventKind.AGENT_RESULT,
            run_id=run_id,
            status="COMPLETED",
        ),
    )


def _tool_terminal_events(
    run_id: str,
    *,
    tool_name: ToolName,
    status: str,
) -> tuple[AgentRunEvent, ...]:
    terminal_kind = (
        AgentEventKind.AGENT_RESULT
        if status in {"COMPLETED", "NO_MATCH"}
        else AgentEventKind.AGENT_ERROR
    )
    return (
        AgentRunEvent(kind=AgentEventKind.AGENT_STARTED, run_id=run_id),
        AgentRunEvent(
            kind=AgentEventKind.MODEL_STARTED,
            run_id=run_id,
            round=1,
        ),
        AgentRunEvent(
            kind=AgentEventKind.MODEL_FINISHED,
            run_id=run_id,
            round=1,
            tool_name=tool_name,
        ),
        AgentRunEvent(
            kind=AgentEventKind.TOOL_STARTED,
            run_id=run_id,
            tool_name=tool_name,
        ),
        AgentRunEvent(
            kind=AgentEventKind.TOOL_FINISHED,
            run_id=run_id,
            tool_name=tool_name,
            safe_code="SUCCESS",
        ),
        AgentRunEvent(
            kind=terminal_kind,
            run_id=run_id,
            status=cast(Any, status),
            safe_code=("INTERNAL_ERROR" if status == "FAILED" else None),
        ),
    )


def _interleaved_events(run_id: str) -> tuple[AgentRunEvent, ...]:
    def child_event(
        kind: AgentEventKind,
        child_id: str,
        *,
        round_number: int | None = None,
        tool_name: ToolName | None = None,
        safe_code: str | None = None,
    ) -> AgentRunEvent:
        return AgentRunEvent(
            kind=kind,
            run_id=run_id,
            scope=AgentEventScope.CHILD,
            child_id=child_id,
            depth=1,
            round=round_number,
            tool_name=tool_name,
            safe_code=safe_code,
        )

    return (
        AgentRunEvent(kind=AgentEventKind.AGENT_STARTED, run_id=run_id),
        AgentRunEvent(
            kind=AgentEventKind.MODEL_STARTED,
            run_id=run_id,
            round=1,
        ),
        AgentRunEvent(
            kind=AgentEventKind.MODEL_FINISHED,
            run_id=run_id,
            round=1,
            tool_name=ToolName.DISPATCH_TOOL,
        ),
        AgentRunEvent(
            kind=AgentEventKind.TOOL_STARTED,
            run_id=run_id,
            tool_name=ToolName.DISPATCH_TOOL,
        ),
        AgentRunEvent(
            kind=AgentEventKind.FORK_STARTED,
            run_id=run_id,
            child_id="child-1",
            depth=1,
        ),
        AgentRunEvent(
            kind=AgentEventKind.FORK_STARTED,
            run_id=run_id,
            child_id="child-2",
            depth=1,
        ),
        child_event(
            AgentEventKind.MODEL_STARTED,
            "child-1",
            round_number=1,
        ),
        child_event(
            AgentEventKind.MODEL_STARTED,
            "child-2",
            round_number=1,
        ),
        child_event(
            AgentEventKind.MODEL_FINISHED,
            "child-1",
            round_number=1,
            tool_name=ToolName.ITEM_SEARCH,
        ),
        child_event(
            AgentEventKind.MODEL_FINISHED,
            "child-2",
            round_number=1,
            tool_name=ToolName.ITEM_SEARCH,
        ),
        child_event(
            AgentEventKind.TOOL_STARTED,
            "child-2",
            tool_name=ToolName.ITEM_SEARCH,
        ),
        child_event(
            AgentEventKind.TOOL_STARTED,
            "child-1",
            tool_name=ToolName.ITEM_SEARCH,
        ),
        child_event(
            AgentEventKind.TOOL_FINISHED,
            "child-1",
            tool_name=ToolName.ITEM_SEARCH,
            safe_code="SUCCESS",
        ),
        AgentRunEvent(
            kind=AgentEventKind.FORK_FINISHED,
            run_id=run_id,
            child_id="child-1",
            depth=1,
            status="COMPLETED",
        ),
        child_event(
            AgentEventKind.TOOL_FINISHED,
            "child-2",
            tool_name=ToolName.ITEM_SEARCH,
            safe_code="SUCCESS",
        ),
        AgentRunEvent(
            kind=AgentEventKind.FORK_FINISHED,
            run_id=run_id,
            child_id="child-2",
            depth=1,
            status="COMPLETED",
        ),
        AgentRunEvent(
            kind=AgentEventKind.TOOL_FINISHED,
            run_id=run_id,
            tool_name=ToolName.DISPATCH_TOOL,
            safe_code="SUCCESS",
        ),
        AgentRunEvent(
            kind=AgentEventKind.MODEL_STARTED,
            run_id=run_id,
            round=2,
        ),
        AgentRunEvent(
            kind=AgentEventKind.MODEL_FINISHED,
            run_id=run_id,
            round=2,
            tool_name=ToolName.CHAT_FALLBACK,
        ),
        AgentRunEvent(
            kind=AgentEventKind.TOOL_STARTED,
            run_id=run_id,
            tool_name=ToolName.CHAT_FALLBACK,
        ),
        AgentRunEvent(
            kind=AgentEventKind.TOOL_FINISHED,
            run_id=run_id,
            tool_name=ToolName.CHAT_FALLBACK,
            safe_code="SUCCESS",
        ),
        AgentRunEvent(
            kind=AgentEventKind.AGENT_RESULT,
            run_id=run_id,
            status="COMPLETED",
        ),
    )


def _payload(thread_id: str = "thread-agent-001") -> dict[str, object]:
    return {
        "thread_id": thread_id,
        "request": {
            "query": "phone under 500 USD",
            "locale": "zh-CN",
            "display_currency": "USD",
            "top_k": 1,
            "snapshot_version": "m1d-demo-v1",
        },
    }


async def _wait_for(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), timeout=1)


async def _invoke_agent_post(
    app: FastAPI,
    *,
    body: bytes,
    timeline: list[str],
) -> list[Message]:
    incoming: list[Message] = [
        {
            "type": "http.request",
            "body": body,
            "more_body": False,
        }
    ]
    sent: list[Message] = []

    async def receive() -> Message:
        if incoming:
            return incoming.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(dict(message))
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            timeline.append("final_response_body")

    path = "/api/v1/agent-runs"
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ],
        "client": ("test-client", 1),
        "server": ("testserver", 80),
    }
    await app(scope, receive, send)
    return sent


class _DirectAgentSse:
    def __init__(self, app: FastAPI, *, run_id: str) -> None:
        path = f"/api/v1/agent-runs/{run_id}/events"
        self.app = app
        self.scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
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
        self.sent: list[Message] = []
        self.response_started = asyncio.Event()
        self.body_available = asyncio.Event()
        self.finished = asyncio.Event()

    async def receive(self) -> Message:
        return await self.incoming.get()

    async def send(self, message: Message) -> None:
        self.sent.append(dict(message))
        if message["type"] == "http.response.start":
            self.response_started.set()
        elif message["type"] == "http.response.body" and message.get("body", b""):
            self.body_available.set()

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
            cast(bytes, message.get("body", b""))
            for message in self.sent
            if message["type"] == "http.response.body"
        ).decode()


def _timeout_after_service_start(
    service: _BlockingService,
) -> Callable[[float | None], AbstractAsyncContextManager[object]]:
    @asynccontextmanager
    async def controlled_timeout(
        delay: float | None,
    ) -> AsyncIterator[object]:
        assert delay == 300
        run_task = asyncio.current_task()
        assert run_task is not None

        async def cancel_after_start() -> None:
            await service.started.wait()
            run_task.cancel()

        trigger = asyncio.create_task(cancel_after_start())
        try:
            try:
                yield object()
            except asyncio.CancelledError as error:
                raise TimeoutError from error
        finally:
            trigger.cancel()
            await asyncio.gather(trigger, return_exceptions=True)

    return controlled_timeout


def _parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    for block in normalized.split("\n\n"):
        identifier: str | None = None
        data: str | None = None
        for line in block.splitlines():
            if line.startswith("id: "):
                identifier = line.removeprefix("id: ")
            elif line.startswith("data: "):
                data = line.removeprefix("data: ")
        if identifier is not None and data is not None:
            parsed = json.loads(data)
            assert isinstance(parsed, dict)
            events.append((identifier, parsed))
    return events


def test_agent_status_contract_has_six_consistent_states() -> None:
    for state in (RunState.ACCEPTED, RunState.RUNNING):
        active = AgentRunStatusResponse(
            thread_id="thread-agent-001",
            run_id="run-agent-001",
            state=state,
            projection_status=ProjectionStatus.OK,
        )
        assert active.response is None
        assert active.error is None

    response = AgentDemoResponse(
        run_id="run-agent-001",
        status=RunStatus.COMPLETED,
        answer=AgentAnswer(
            kind=AgentAnswerKind.CHAT_FALLBACK,
            text="Safe fallback.",
        ),
    )
    completed = AgentRunStatusResponse(
        thread_id="thread-agent-001",
        run_id="run-agent-001",
        state=RunState.COMPLETED,
        projection_status=ProjectionStatus.OK,
        response=response,
    )
    assert completed.response is response

    aborted = AgentRunStatusResponse(
        thread_id="thread-agent-001",
        run_id="run-agent-001",
        state=RunState.ABORTED,
        projection_status=ProjectionStatus.OK,
        error=ApiError(
            code="RUN_ABORTED",
            message="Run execution was aborted.",
        ),
    )
    assert aborted.response is None

    with pytest.raises(ValueError):
        AgentRunStatusResponse(
            thread_id="thread-agent-001",
            run_id="run-agent-001",
            state=RunState.ABORTED,
            projection_status=ProjectionStatus.OK,
            response=response,
            error=ApiError(
                code="RUN_ABORTED",
                message="Run execution was aborted.",
            ),
        )


def test_agent_projector_emits_bounded_camel_case_payloads() -> None:
    projector = AgentEventProjector(clock=_Clock())
    child = projector.project_event(
        thread_id="thread-agent-001",
        event=AgentRunEvent(
            kind=AgentEventKind.MODEL_FINISHED,
            run_id="run-agent-001",
            scope=AgentEventScope.CHILD,
            round=1,
            tool_name=ToolName.ITEM_SEARCH,
            child_id="child-amazon",
            depth=1,
        ),
        sequence=7,
    )
    payload = child.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert payload == {
        "schemaVersion": "glodex.agent.event.v1",
        "threadId": "thread-agent-001",
        "runId": "run-agent-001",
        "sequence": 7,
        "timestamp": 1785412800000,
        "type": "MODEL_FINISHED",
        "scope": "child",
        "childId": "child-amazon",
        "depth": 1,
        "round": 1,
        "toolName": "item_search",
    }
    assert not ({"query", "prompt", "args", "output", "reasoning", "action"} & payload.keys())


def test_agent_factory_exposes_only_three_routes_and_uses_300_second_default() -> None:
    app = create_agent_app(
        service=_CompletedService(),
        clock=_Clock(),
        run_id_provider=_RunIds("run-agent-001"),
    )
    assert set(app.openapi()["paths"]) == {
        "/api/v1/agent-runs",
        "/api/v1/agent-runs/{run_id}",
        "/api/v1/agent-runs/{run_id}/events",
    }
    assert app.state.settings.run_timeout_seconds == 300
    assert app.state.data_mode is DataMode.DEMO_SNAPSHOT

    with pytest.raises(ValueError, match="greater than 240"):
        create_agent_app(
            service=_CompletedService(),
            settings=ApiSettings(run_timeout_seconds=240),
        )
    with pytest.raises(TypeError, match="exact DataMode"):
        create_agent_app(
            service=_CompletedService(),
            data_mode=cast(Any, "LIVE_MARKETPLACE"),
        )


def test_operator_data_mode_rejects_snapshot_switches_before_run_allocation() -> None:
    async def scenario() -> tuple[list[httpx.Response], int, int]:
        service = _CompletedService()
        demo_run_ids = _RunIds()
        demo = create_agent_app(
            service=service,
            data_mode=DataMode.DEMO_SNAPSHOT,
            clock=_Clock(),
            run_id_provider=demo_run_ids,
        )
        demo_transport = httpx.ASGITransport(
            app=demo,
            raise_app_exceptions=False,
        )
        async with httpx.AsyncClient(
            transport=demo_transport,
            base_url="http://testserver",
        ) as client:
            unsupported_demo = _payload("thread-demo-policy")
            cast(dict[str, Any], unsupported_demo["request"])["snapshot_version"] = (
                "operator-cannot-switch"
            )
            demo_rejected = await client.post(
                "/api/v1/agent-runs?data_mode=LIVE_MARKETPLACE",
                json=unsupported_demo,
                headers={"X-Data-Mode": "LIVE_MARKETPLACE"},
            )
            body_switch = _payload("thread-demo-body-switch")
            body_switch["data_mode"] = "LIVE_MARKETPLACE"
            body_rejected = await client.post(
                "/api/v1/agent-runs",
                json=body_switch,
            )

        live_run_ids = _RunIds("run-agent-live-policy")
        live = create_agent_app(
            service=service,
            data_mode=DataMode.LIVE_MARKETPLACE,
            clock=_Clock(),
            run_id_provider=live_run_ids,
        )
        live_transport = httpx.ASGITransport(
            app=live,
            raise_app_exceptions=False,
        )
        async with httpx.AsyncClient(
            transport=live_transport,
            base_url="http://testserver",
        ) as client:
            live_rejected = await client.post(
                "/api/v1/agent-runs?data_mode=DEMO_SNAPSHOT",
                json=_payload("thread-live-policy-rejected"),
                headers={"X-Data-Mode": "DEMO_SNAPSHOT"},
            )
            live_payload = _payload("thread-live-policy-accepted")
            cast(dict[str, Any], live_payload["request"]).pop("snapshot_version")
            live_accepted = await client.post(
                "/api/v1/agent-runs",
                json=live_payload,
            )
            await live.state.coordinator.wait_idle()
        return (
            [
                demo_rejected,
                body_rejected,
                live_rejected,
                live_accepted,
            ],
            demo_run_ids.calls + live_run_ids.calls,
            service.calls,
        )

    responses, run_id_calls, service_calls = asyncio.run(scenario())
    assert [response.status_code for response in responses] == [422, 422, 422, 202]
    for response in (responses[0], responses[2]):
        body = response.json()
        assert body["error"]["code"] == "REQUEST_REJECTED"
        assert body["error"]["field_errors"] == [
            {
                "field": "body.request.snapshot_version",
                "code": "snapshot_mode",
                "message": ("Snapshot version is not allowed for this Agent mode."),
            }
        ]
    assert run_id_calls == 1
    assert service_calls == 1


def test_agent_post_status_and_replay_share_one_canonical_execution() -> None:
    async def scenario() -> tuple[httpx.Response, httpx.Response, httpx.Response, int]:
        service = _CompletedService()
        app = create_agent_app(
            service=service,
            clock=_Clock(),
            run_id_provider=_RunIds("run-agent-001"),
        )
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            accepted = await client.post("/api/v1/agent-runs", json=_payload())
            await app.state.coordinator.wait_idle()
            status = await client.get("/api/v1/agent-runs/run-agent-001")
            replay = await client.get(
                "/api/v1/agent-runs/run-agent-001/events",
                headers={"Accept": "text/event-stream"},
            )
        return accepted, status, replay, service.calls

    accepted, status, replay, calls = asyncio.run(scenario())
    assert accepted.status_code == 202
    assert accepted.json()["events_url"] == ("/api/v1/agent-runs/run-agent-001/events")
    assert status.status_code == 200
    assert status.json()["state"] == "COMPLETED"
    assert status.json()["response"]["schema_version"] == "glodex.agent-result.v1"
    assert calls == 1
    assert replay.status_code == 200
    assert [
        line.removeprefix("id: ") for line in replay.text.splitlines() if line.startswith("id: ")
    ] == [f"run-agent-001:{sequence}" for sequence in range(1, 7)]
    assert '"type": "AGENT_RESULT"' in replay.text
    assert "STATE_SNAPSHOT" not in replay.text


def test_agent_execution_starts_only_after_complete_202_body() -> None:
    async def scenario() -> tuple[list[Message], list[str]]:
        timeline: list[str] = []
        service = _CompletedService(timeline=timeline)
        app = create_agent_app(
            service=service,
            clock=_Clock(),
            run_id_provider=_RunIds("run-agent-ordering"),
        )
        sent = await _invoke_agent_post(
            app,
            body=json.dumps(_payload()).encode(),
            timeline=timeline,
        )
        await app.state.coordinator.wait_idle()
        return sent, timeline

    sent, timeline = asyncio.run(scenario())
    response_start = next(message for message in sent if message["type"] == "http.response.start")
    assert response_start["status"] == 202
    assert timeline == ["final_response_body", "service_execute"]


def test_strict_rejections_never_allocate_or_execute_agent_run() -> None:
    async def scenario() -> tuple[list[httpx.Response], int, int]:
        service = _CompletedService()
        run_ids = _RunIds()
        app = create_agent_app(
            service=service,
            clock=_Clock(),
            run_id_provider=run_ids,
            settings=ApiSettings(
                max_request_body_bytes=512,
                run_timeout_seconds=300,
            ),
        )
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            unknown = _payload()
            unknown["provider"] = "untrusted"
            coerced = _payload()
            cast(dict[str, Any], coerced["request"])["top_k"] = "1"
            responses = [
                await client.post("/api/v1/agent-runs", json=unknown),
                await client.post("/api/v1/agent-runs", json=coerced),
                await client.post(
                    "/api/v1/agent-runs",
                    content=b"{}",
                    headers={"Content-Type": "text/plain"},
                ),
                await client.post(
                    "/api/v1/agent-runs",
                    content=b"x" * 513,
                    headers={"Content-Type": "application/json"},
                ),
            ]
        return responses, run_ids.calls, service.calls

    responses, run_id_calls, service_calls = asyncio.run(scenario())
    assert [response.status_code for response in responses] == [422, 422, 415, 413]
    assert run_id_calls == 0
    assert service_calls == 0
    assert all("untrusted" not in response.text for response in responses)


def test_business_failed_and_no_match_remain_business_responses() -> None:
    async def run_service(
        service: _FailedService | _NoMatchService,
        *,
        run_id: str,
        thread_id: str,
    ) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
        app = create_agent_app(
            service=service,
            clock=_Clock(),
            run_id_provider=_RunIds(run_id),
        )
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            accepted = await client.post(
                "/api/v1/agent-runs",
                json=_payload(thread_id),
            )
            assert accepted.status_code == 202
            await app.state.coordinator.wait_idle()
            status = await client.get(f"/api/v1/agent-runs/{run_id}")
            replay = await client.get(
                f"/api/v1/agent-runs/{run_id}/events",
                headers={"Accept": "text/event-stream"},
            )
        return status.json(), _parse_sse(replay.text)

    async def scenario() -> tuple[
        tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]],
        tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]],
    ]:
        failed = await run_service(
            _FailedService(),
            run_id="run-agent-failed",
            thread_id="thread-agent-failed",
        )
        no_match = await run_service(
            _NoMatchService(),
            run_id="run-agent-no-match",
            thread_id="thread-agent-no-match",
        )
        return failed, no_match

    (failed, failed_events), (no_match, no_match_events) = asyncio.run(scenario())
    assert failed["state"] == "FAILED"
    assert failed["response"]["status"] == "FAILED"
    assert failed["error"] is None
    assert failed_events[-1][1] == {
        "schemaVersion": "glodex.agent.event.v1",
        "threadId": "thread-agent-failed",
        "runId": "run-agent-failed",
        "sequence": 2,
        "timestamp": 1785412800000,
        "type": "AGENT_ERROR",
        "scope": "root",
        "status": "FAILED",
        "safeCode": "INTERNAL_ERROR",
    }

    assert no_match["state"] == "NO_MATCH"
    assert no_match["response"]["status"] == "NO_MATCH"
    assert no_match["error"] is None
    assert no_match_events[-1][1]["type"] == "AGENT_RESULT"
    assert no_match_events[-1][1]["status"] == "NO_MATCH"


def test_cursor_suffix_replay_is_strict_and_never_reexecutes() -> None:
    async def scenario() -> tuple[
        httpx.Response,
        httpx.Response,
        list[httpx.Response],
        int,
    ]:
        service = _CompletedService()
        app = create_agent_app(
            service=service,
            clock=_Clock(),
            run_id_provider=_RunIds("run-agent-cursor"),
        )
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            accepted = await client.post(
                "/api/v1/agent-runs",
                json=_payload("thread-agent-cursor"),
            )
            assert accepted.status_code == 202
            await app.state.coordinator.wait_idle()
            suffix = await client.get(
                "/api/v1/agent-runs/run-agent-cursor/events",
                headers={
                    "Accept": "text/event-stream",
                    "Last-Event-ID": "run-agent-cursor:3",
                },
            )
            tail = await client.get(
                "/api/v1/agent-runs/run-agent-cursor/events",
                headers={
                    "Accept": "text/event-stream",
                    "Last-Event-ID": "run-agent-cursor:6",
                },
            )
            invalid = [
                await client.get(
                    "/api/v1/agent-runs/run-agent-cursor/events",
                    headers={"Accept": "text/event-stream"},
                )
            ]
            for cursor in (
                "other-run:1",
                "run-agent-cursor:0",
                "run-agent-cursor:7",
            ):
                invalid.append(
                    await client.get(
                        "/api/v1/agent-runs/run-agent-cursor/events",
                        headers={
                            "Accept": "text/event-stream",
                            "Last-Event-ID": cursor,
                        },
                    )
                )
            duplicate = await client.get(
                "/api/v1/agent-runs/run-agent-cursor/events",
                headers=[
                    ("Accept", "text/event-stream"),
                    ("Last-Event-ID", "run-agent-cursor:1"),
                    ("Last-Event-ID", "run-agent-cursor:2"),
                ],
            )
            invalid.append(duplicate)
            unacceptable = await client.get(
                "/api/v1/agent-runs/run-agent-cursor/events",
                headers={"Accept": "*/*"},
            )
            invalid.append(unacceptable)
        return suffix, tail, invalid, service.calls

    suffix, tail, invalid, calls = asyncio.run(scenario())
    assert [identifier for identifier, _ in _parse_sse(suffix.text)] == [
        "run-agent-cursor:4",
        "run-agent-cursor:5",
        "run-agent-cursor:6",
    ]
    assert tail.status_code == 200
    assert _parse_sse(tail.text) == []
    assert [response.status_code for response in invalid] == [
        200,
        400,
        400,
        400,
        400,
        406,
    ]
    assert calls == 1


def test_projection_capacity_degrades_without_changing_business_result() -> None:
    async def scenario() -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
        app = create_agent_app(
            service=_NoMatchService(),
            clock=_Clock(),
            run_id_provider=_RunIds("run-agent-degraded"),
            settings=ApiSettings(
                max_events_per_run=1,
                run_timeout_seconds=300,
            ),
        )
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            accepted = await client.post(
                "/api/v1/agent-runs",
                json=_payload("thread-agent-degraded"),
            )
            assert accepted.status_code == 202
            await app.state.coordinator.wait_idle()
            status = await client.get("/api/v1/agent-runs/run-agent-degraded")
            replay = await client.get(
                "/api/v1/agent-runs/run-agent-degraded/events",
                headers={"Accept": "text/event-stream"},
            )
        return status.json(), _parse_sse(replay.text)

    status, replay = asyncio.run(scenario())
    assert status["state"] == "NO_MATCH"
    assert status["response"]["status"] == "NO_MATCH"
    assert status["response"]["search_response"]["status"] == "NO_MATCH"
    assert status["response"]["search_response"]["snapshot_version"] == "m1d-demo-v1"
    assert status["projection_status"] == "DEGRADED"
    assert status["last_event_id"] == "run-agent-degraded:1"
    assert [payload["type"] for _, payload in replay] == ["AGENT_STARTED"]


def test_thread_admission_active_capacity_and_terminal_eviction_are_bounded() -> None:
    async def scenario() -> tuple[list[int], int, int]:
        service = _BlockingService()
        run_ids = _RunIds(
            "run-agent-first",
            "run-agent-thread-conflict",
            "run-agent-capacity-conflict",
            "run-agent-reused-thread",
        )
        app = create_agent_app(
            service=service,
            clock=_Clock(),
            run_id_provider=run_ids,
            settings=ApiSettings(
                max_active_runs=1,
                max_terminal_runs=1,
                run_timeout_seconds=300,
            ),
        )
        service.registry = app.state.registry
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            first = await client.post(
                "/api/v1/agent-runs",
                json=_payload("thread-agent-bounded"),
            )
            await _wait_for(service.started)
            same_thread = await client.post(
                "/api/v1/agent-runs",
                json=_payload("thread-agent-bounded"),
            )
            at_capacity = await client.post(
                "/api/v1/agent-runs",
                json=_payload("thread-agent-other"),
            )
            service.release.set()
            await app.state.coordinator.wait_idle()
            reused = await client.post(
                "/api/v1/agent-runs",
                json=_payload("thread-agent-bounded"),
            )
            await app.state.coordinator.wait_idle()
            evicted = await client.get("/api/v1/agent-runs/run-agent-first")
            retained = await client.get("/api/v1/agent-runs/run-agent-reused-thread")
        return (
            [
                first.status_code,
                same_thread.status_code,
                at_capacity.status_code,
                reused.status_code,
                evicted.status_code,
                retained.status_code,
            ],
            run_ids.calls,
            service.calls,
        )

    statuses, run_id_calls, service_calls = asyncio.run(scenario())
    assert statuses == [202, 409, 503, 202, 404, 200]
    assert run_id_calls == 4
    assert service_calls == 2


def test_interleaved_children_stay_inside_their_own_fork_brackets() -> None:
    async def scenario() -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
        app = create_agent_app(
            service=_InterleavedService(),
            clock=_Clock(),
            run_id_provider=_RunIds("run-agent-forks"),
        )
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            accepted = await client.post(
                "/api/v1/agent-runs",
                json=_payload("thread-agent-forks"),
            )
            assert accepted.status_code == 202
            await app.state.coordinator.wait_idle()
            status = await client.get("/api/v1/agent-runs/run-agent-forks")
            replay = await client.get(
                "/api/v1/agent-runs/run-agent-forks/events",
                headers={"Accept": "text/event-stream"},
            )
        return status.json(), _parse_sse(replay.text)

    status, replay = asyncio.run(scenario())
    payloads = [payload for _, payload in replay]
    assert status["projection_status"] == "OK"
    assert [identifier for identifier, _ in replay] == [
        f"run-agent-forks:{sequence}" for sequence in range(1, 23)
    ]
    assert payloads[-1]["type"] == "AGENT_RESULT"
    for child_id in ("child-1", "child-2"):
        started = next(
            index
            for index, payload in enumerate(payloads)
            if payload["type"] == "FORK_STARTED" and payload["childId"] == child_id
        )
        finished = next(
            index
            for index, payload in enumerate(payloads)
            if payload["type"] == "FORK_FINISHED" and payload["childId"] == child_id
        )
        child_steps = [
            index
            for index, payload in enumerate(payloads)
            if payload.get("scope") == "child" and payload.get("childId") == child_id
        ]
        assert child_steps
        assert all(started < index < finished for index in child_steps)
    serialized = json.dumps(payloads)
    for forbidden in ("phone under", "prompt", "reasoning", "demands", "args"):
        assert forbidden not in serialized


def test_child_event_outside_fork_bracket_degrades_only_projection() -> None:
    async def scenario() -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
        app = create_agent_app(
            service=_MalformedBracketService(),
            clock=_Clock(),
            run_id_provider=_RunIds("run-agent-bad-bracket"),
        )
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            accepted = await client.post(
                "/api/v1/agent-runs",
                json=_payload("thread-agent-bad-bracket"),
            )
            assert accepted.status_code == 202
            await app.state.coordinator.wait_idle()
            status = await client.get("/api/v1/agent-runs/run-agent-bad-bracket")
            replay = await client.get(
                "/api/v1/agent-runs/run-agent-bad-bracket/events",
                headers={"Accept": "text/event-stream"},
            )
        return status.json(), _parse_sse(replay.text)

    status, replay = asyncio.run(scenario())
    assert status["state"] == "COMPLETED"
    assert status["response"]["status"] == "COMPLETED"
    assert status["projection_status"] == "DEGRADED"
    assert [payload["type"] for _, payload in replay] == ["AGENT_STARTED"]


def test_sse_disconnect_unsubscribes_without_cancelling_agent_run() -> None:
    async def scenario() -> tuple[int, bool, str]:
        service = _BlockingService()
        app = create_agent_app(
            service=service,
            clock=_Clock(),
            run_id_provider=_RunIds("run-agent-disconnect"),
        )
        service.registry = app.state.registry
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            accepted = await client.post(
                "/api/v1/agent-runs",
                json=_payload("thread-agent-disconnect"),
            )
            assert accepted.status_code == 202
        await _wait_for(service.started)

        harness = _DirectAgentSse(app, run_id="run-agent-disconnect")
        stream_task = asyncio.create_task(harness.run())
        await _wait_for(harness.response_started)
        await _wait_for(harness.body_available)
        harness.disconnect()
        await _wait_for(harness.finished)
        in_flight = app.state.coordinator.in_flight_count
        cancelled = service.cleaned.is_set()
        body = harness.body

        service.release.set()
        await app.state.coordinator.wait_idle()
        await stream_task
        return in_flight, cancelled, body

    in_flight, cancelled, body = asyncio.run(scenario())
    assert in_flight == 1
    assert not cancelled
    assert '"type": "AGENT_STARTED"' in body


def test_timeout_cleans_service_before_committing_transport_aborted() -> None:
    async def scenario() -> tuple[dict[str, Any], RunState | None, str]:
        service = _BlockingService()
        app = create_agent_app(
            service=service,
            clock=_Clock(),
            run_id_provider=_RunIds("run-agent-timeout"),
            timeout_factory=_timeout_after_service_start(service),
        )
        service.registry = app.state.registry
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            accepted = await client.post(
                "/api/v1/agent-runs",
                json=_payload("thread-agent-timeout"),
            )
            assert accepted.status_code == 202
            await app.state.coordinator.wait_idle()
            assert service.cleaned.is_set()
            status = await client.get("/api/v1/agent-runs/run-agent-timeout")
            replay = await client.get(
                "/api/v1/agent-runs/run-agent-timeout/events",
                headers={"Accept": "text/event-stream"},
            )
        return status.json(), service.state_during_cleanup, replay.text

    status, state_during_cleanup, replay = asyncio.run(scenario())
    assert state_during_cleanup is RunState.RUNNING
    assert status["state"] == "ABORTED"
    assert status["response"] is None
    assert status["error"]["code"] == "RUN_ABORTED"
    assert '"type": "AGENT_ERROR"' in replay
    assert '"status": "ABORTED"' in replay


def test_observed_business_terminal_is_buffered_until_execution_returns() -> None:
    async def scenario() -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
        service = _PrematureTerminalBlockingService()
        app = create_agent_app(
            service=service,
            clock=_Clock(),
            run_id_provider=_RunIds("run-agent-buffered-terminal"),
            timeout_factory=_timeout_after_service_start(service),
        )
        service.registry = app.state.registry
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            accepted = await client.post(
                "/api/v1/agent-runs",
                json=_payload("thread-agent-buffered-terminal"),
            )
            assert accepted.status_code == 202
            await app.state.coordinator.wait_idle()
            status = await client.get("/api/v1/agent-runs/run-agent-buffered-terminal")
            replay = await client.get(
                "/api/v1/agent-runs/run-agent-buffered-terminal/events",
                headers={"Accept": "text/event-stream"},
            )
        return status.json(), _parse_sse(replay.text)

    status, replay = asyncio.run(scenario())
    event_types = [payload["type"] for _, payload in replay]
    assert status["state"] == "ABORTED"
    assert event_types[-1] == "AGENT_ERROR"
    assert event_types.count("AGENT_ERROR") == 1
    assert "AGENT_RESULT" not in event_types


def test_shutdown_cleans_service_then_aborts_and_stops_admission() -> None:
    async def scenario() -> tuple[
        dict[str, Any],
        httpx.Response,
        RunState | None,
    ]:
        service = _BlockingService()
        app = create_agent_app(
            service=service,
            clock=_Clock(),
            run_id_provider=_RunIds(
                "run-agent-shutdown",
                "run-agent-after-shutdown",
            ),
        )
        service.registry = app.state.registry
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client,
        ):
            accepted = await client.post(
                "/api/v1/agent-runs",
                json=_payload("thread-agent-shutdown"),
            )
            assert accepted.status_code == 202
            await _wait_for(service.started)

        assert service.cleaned.is_set()
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            status = await client.get("/api/v1/agent-runs/run-agent-shutdown")
            unavailable = await client.post(
                "/api/v1/agent-runs",
                json=_payload("thread-agent-after-shutdown"),
            )
        return status.json(), unavailable, service.state_during_cleanup

    status, unavailable, state_during_cleanup = asyncio.run(scenario())
    assert state_during_cleanup is RunState.RUNNING
    assert status["state"] == "ABORTED"
    assert status["response"] is None
    assert unavailable.status_code == 503


def test_shutdown_of_unreleased_accepted_run_emits_one_safe_abort() -> None:
    async def scenario() -> tuple[
        RunState,
        RunState,
        ProjectionStatus,
        dict[str, Any],
        int,
    ]:
        service = _CompletedService()
        app = create_agent_app(
            service=service,
            clock=_Clock(),
            run_id_provider=_RunIds("run-agent-never-released"),
        )
        accepted = app.state.coordinator.submit(
            SearchRequest(
                query="phone",
                snapshot_version="m1d-demo-v1",
            ),
            thread_id="thread-agent-never-released",
        )
        await app.state.coordinator.shutdown()
        status = app.state.registry.status("run-agent-never-released")
        subscription = app.state.registry.subscribe(
            "run-agent-never-released",
            after_sequence=0,
        )
        batch = app.state.registry.read(subscription)
        app.state.registry.unsubscribe(subscription)
        return (
            accepted.state,
            status.state,
            status.projection_status,
            batch.events[0].model_dump(
                mode="json",
                by_alias=True,
                exclude_none=True,
            ),
            service.calls,
        )

    accepted_state, final_state, projection_status, event, calls = asyncio.run(scenario())
    assert accepted_state is RunState.ACCEPTED
    assert final_state is RunState.ABORTED
    assert projection_status is ProjectionStatus.OK
    assert event["type"] == "AGENT_ERROR"
    assert event["status"] == "ABORTED"
    assert event["safeCode"] == "RUN_ABORTED"
    assert event["sequence"] == 1
    assert calls == 0


def test_default_search_factory_stays_on_original_routes_and_timeout() -> None:
    app = search_api.create_app(
        service=_NeverSearchService(),
        clock=_Clock(),
        run_id_provider=_RunIds("unused-search-run"),
    )
    assert set(app.openapi()["paths"]) == {
        "/api/v1/runs",
        "/api/v1/runs/{run_id}",
        "/api/v1/runs/{run_id}/events",
    }
    assert app.state.settings.run_timeout_seconds == 30
    assert not hasattr(search_api, "create_agent_app")
