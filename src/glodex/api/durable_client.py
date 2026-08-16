"""Narrow fixed-loopback HTTP client for the existing Durable public durable API."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from typing import Final, Protocol, cast

import httpx

from glodex.api.contracts import ApiErrorEnvelope
from glodex.api.durable_contracts import (
    DurableCreateRunRequest,
    DurableRunAccepted,
    DurableRunStatusResponse,
)

DURABLE_PUBLIC_BASE_URL: Final = "http://127.0.0.1:8766"
_DURABLE_EVENT_ID = re.compile(
    r"(?P<run_id>[A-Za-z0-9][A-Za-z0-9._/-]*):(?P<sequence>[1-9][0-9]*)\Z"
)
_MEMORY_THREAD_TURNS_PATH = re.compile(
    r"/api/v1/conversation-threads/[A-Za-z0-9][A-Za-z0-9._:-]{0,63}/turns"
    r"(?:\?before_ordinal=[1-9][0-9]*)?\Z"
)
_MEMORY_THREAD_PATH = re.compile(r"/api/v1/conversation-threads/[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
_MEMORY_ENTRY_PATH = re.compile(r"/api/v1/memory/mem-[0-9a-f]{24}\Z")
_M6_TRACE_PATH = re.compile(r"/api/v1/m6/runs/[A-Za-z0-9][A-Za-z0-9._:-]{0,63}/trace\Z")
_M6_OPERATIONS_PATH = re.compile(r"/api/v1/m6/operations\?window=(?:1h|24h)\Z")
_M7_SUMMARY_PATH = re.compile(r"/api/v1/m7/runs/[A-Za-z0-9][A-Za-z0-9._:-]{0,63}/summary\Z")
_MAX_JSON_BYTES: Final = 65_536
_CONNECT_TIMEOUT_SECONDS: Final = 2.0
_READ_TIMEOUT_SECONDS: Final = 30.0


class WebConsoleUpstreamError(RuntimeError):
    """A safe failure category that intentionally carries no upstream text."""


@dataclass(frozen=True, slots=True)
class DurablePublicEventFrame:
    """One raw-but-already-public Durable SSE data frame and its source cursor."""

    event_id: str
    data: str


@dataclass(frozen=True, slots=True)
class DurableLocalAuthRelayResponse:
    """Safe fixed-route response relay; cookie values are forwarded but never inspected."""

    status_code: int
    content: bytes
    set_cookie: tuple[str, ...] = ()


class WebConsoleDurablePublicClient(Protocol):
    """The only durable dependency the WebConsole adapter is permitted to use."""

    async def create(
        self,
        payload: DurableCreateRunRequest,
        *,
        cookie_header: str | None = None,
    ) -> DurableRunAccepted: ...

    async def status(
        self,
        run_id: str,
        *,
        cookie_header: str | None = None,
    ) -> DurableRunStatusResponse: ...

    async def cancel(
        self,
        run_id: str,
        *,
        cookie_header: str | None = None,
    ) -> DurableRunStatusResponse: ...

    async def resume(
        self,
        run_id: str,
        *,
        cookie_header: str | None = None,
    ) -> DurableRunStatusResponse: ...

    def events(
        self,
        run_id: str,
        *,
        after: str | None = None,
        cookie_header: str | None = None,
    ) -> AsyncIterator[DurablePublicEventFrame]: ...


class LoopbackDurablePublicClient:
    """One strict no-proxy/no-redirect client for the Durable loopback public API."""

    def __init__(self, *, base_url: str = DURABLE_PUBLIC_BASE_URL) -> None:
        if base_url != DURABLE_PUBLIC_BASE_URL:
            raise ValueError("WebConsole durable upstream must use the fixed loopback URL")
        self._base_url = base_url

    async def create(
        self,
        payload: DurableCreateRunRequest,
        *,
        cookie_header: str | None = None,
    ) -> DurableRunAccepted:
        response = await self._request(
            "POST",
            "/api/v1/durable-agent-runs",
            json=payload.model_dump(mode="json", by_alias=True, exclude_none=True),
            cookie_header=cookie_header,
        )
        if response.status_code != 202:
            await self._raise_safe_error(response)
        return _parse_json(response, DurableRunAccepted)

    async def status(
        self,
        run_id: str,
        *,
        cookie_header: str | None = None,
    ) -> DurableRunStatusResponse:
        response = await self._request(
            "GET",
            f"/api/v1/durable-agent-runs/{run_id}",
            cookie_header=cookie_header,
        )
        if response.status_code != 200:
            await self._raise_safe_error(response)
        return _parse_json(response, DurableRunStatusResponse)

    async def local_auth(
        self,
        *,
        method: str,
        path: str,
        payload: dict[str, object] | None,
        cookie_header: str | None,
    ) -> DurableLocalAuthRelayResponse:
        """Relay only the fixed local-account routes and no caller-selected upstream path."""

        allowed_routes = frozenset(
            {
                ("POST", "/api/v1/local-auth/register"),
                ("POST", "/api/v1/local-auth/login"),
                ("GET", "/api/v1/local-auth/me"),
                ("POST", "/api/v1/local-auth/logout"),
                ("DELETE", "/api/v1/local-auth/me"),
            }
        )
        if (method, path) not in allowed_routes:
            raise WebConsoleUpstreamError("WebConsole local auth route is invalid")
        response = await self._request(
            method,
            path,
            json=payload,
            cookie_header=cookie_header,
            expected_content_type=None,
        )
        if response.status_code == 204:
            if response.content:
                raise WebConsoleUpstreamError("Durable local auth response is invalid")
        else:
            _validate_response(response, expected_content_type="application/json")
        return DurableLocalAuthRelayResponse(
            status_code=response.status_code,
            content=response.content,
            set_cookie=tuple(response.headers.get_list("set-cookie")),
        )

    async def memory_data(
        self,
        *,
        method: str,
        path: str,
        payload: dict[str, object] | None,
        cookie_header: str | None,
    ) -> DurableLocalAuthRelayResponse:
        """Relay one fixed user-memory route, never a caller-selected upstream URL."""

        if not _is_allowed_memory_route(method=method, path=path):
            raise WebConsoleUpstreamError("WebConsole user-memory route is invalid")
        response = await self._request(
            method,
            path,
            json=payload,
            cookie_header=cookie_header,
            expected_content_type=None,
        )
        if response.status_code == 204:
            if response.content:
                raise WebConsoleUpstreamError("Durable user-memory response is invalid")
        else:
            _validate_response(response, expected_content_type="application/json")
        return DurableLocalAuthRelayResponse(
            status_code=response.status_code,
            content=response.content,
            set_cookie=(),
        )

    async def m6_data(
        self,
        *,
        path: str,
        cookie_header: str | None,
    ) -> DurableLocalAuthRelayResponse:
        """Relay one fixed M6 safe DTO path and never a caller-selected upstream URL."""

        if not _is_allowed_m6_route(path=path):
            raise WebConsoleUpstreamError("WebConsole M6 route is invalid")
        response = await self._request(
            "GET",
            path,
            cookie_header=cookie_header,
            expected_content_type=None,
        )
        _validate_response(response, expected_content_type="application/json")
        return DurableLocalAuthRelayResponse(
            status_code=response.status_code,
            content=response.content,
            set_cookie=(),
        )

    async def m7_summary(
        self,
        *,
        path: str,
        cookie_header: str | None,
    ) -> DurableLocalAuthRelayResponse:
        """Relay only one owner-scoped, already-generated offline summary."""

        if _M7_SUMMARY_PATH.fullmatch(path) is None:
            raise WebConsoleUpstreamError("WebConsole M7 summary route is invalid")
        response = await self._request(
            "GET",
            path,
            cookie_header=cookie_header,
            expected_content_type=None,
        )
        _validate_response(response, expected_content_type="application/json")
        return DurableLocalAuthRelayResponse(
            status_code=response.status_code,
            content=response.content,
            set_cookie=(),
        )

    async def cancel(
        self,
        run_id: str,
        *,
        cookie_header: str | None = None,
    ) -> DurableRunStatusResponse:
        response = await self._request(
            "POST",
            f"/api/v1/durable-agent-runs/{run_id}/cancel",
            cookie_header=cookie_header,
        )
        if response.status_code != 200:
            await self._raise_safe_error(response)
        return _parse_json(response, DurableRunStatusResponse)

    async def resume(
        self,
        run_id: str,
        *,
        cookie_header: str | None = None,
    ) -> DurableRunStatusResponse:
        response = await self._request(
            "POST",
            f"/api/v1/durable-agent-runs/{run_id}/resume",
            cookie_header=cookie_header,
        )
        if response.status_code != 200:
            await self._raise_safe_error(response)
        return _parse_json(response, DurableRunStatusResponse)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
        cookie_header: str | None = None,
        expected_content_type: str | None = "application/json",
    ) -> httpx.Response:
        try:
            async with _client(cookie_header=cookie_header) as client:
                response = await client.request(method, path, json=json)
        except (httpx.HTTPError, ValueError) as error:
            raise WebConsoleUpstreamError("Durable public API is unavailable") from error
        if expected_content_type is not None:
            _validate_response(response, expected_content_type=expected_content_type)
        return response

    async def _raise_safe_error(self, response: httpx.Response) -> None:
        with suppress(WebConsoleUpstreamError):
            _parse_json(response, ApiErrorEnvelope)
        raise WebConsoleUpstreamError("Durable public API rejected the operation")

    async def _events(
        self,
        run_id: str,
        *,
        after: str | None,
        cookie_header: str | None,
    ) -> AsyncIterator[DurablePublicEventFrame]:
        headers = {"accept": "text/event-stream"}
        if after is not None:
            _validate_event_id(after, run_id=run_id)
            headers["last-event-id"] = after
        try:
            async with (
                _client(cookie_header=cookie_header) as client,
                client.stream(
                    "GET",
                    f"/api/v1/durable-agent-runs/{run_id}/events",
                    headers=headers,
                ) as response,
            ):
                if response.status_code != 200:
                    await response.aread()
                    await self._raise_safe_error(response)
                _validate_response(response, expected_content_type="text/event-stream")
                pending_id: str | None = None
                data_lines: list[str] = []
                async for line in response.aiter_lines():
                    if not line:
                        if pending_id is not None and data_lines:
                            yield DurablePublicEventFrame(
                                event_id=pending_id,
                                data="\n".join(data_lines),
                            )
                        pending_id = None
                        data_lines = []
                        continue
                    if line.startswith(":"):
                        continue
                    field, separator, value = line.partition(":")
                    if not separator:
                        raise WebConsoleUpstreamError("Durable event stream is invalid")
                    normalized = value[1:] if value.startswith(" ") else value
                    if field == "id":
                        _validate_event_id(normalized, run_id=run_id)
                        pending_id = normalized
                    elif field == "data":
                        if len(normalized.encode("utf-8")) > _MAX_JSON_BYTES:
                            raise WebConsoleUpstreamError("Durable event payload is too large")
                        data_lines.append(normalized)
        except WebConsoleUpstreamError:
            raise
        except (httpx.HTTPError, ValueError) as error:
            raise WebConsoleUpstreamError("Durable event stream is unavailable") from error

    def events(
        self,
        run_id: str,
        *,
        after: str | None = None,
        cookie_header: str | None = None,
    ) -> AsyncIterator[DurablePublicEventFrame]:
        return self._events(run_id, after=after, cookie_header=cookie_header)


def _client(*, cookie_header: str | None = None) -> httpx.AsyncClient:
    headers = {"accept": "application/json"}
    if cookie_header is not None:
        if (
            type(cookie_header) is not str
            or not cookie_header
            or len(cookie_header) > 4_096
            or "\r" in cookie_header
            or "\n" in cookie_header
        ):
            raise WebConsoleUpstreamError("WebConsole cookie relay input is invalid")
        headers["cookie"] = cookie_header
    return httpx.AsyncClient(
        base_url=DURABLE_PUBLIC_BASE_URL,
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(
            connect=_CONNECT_TIMEOUT_SECONDS,
            read=_READ_TIMEOUT_SECONDS,
            write=_READ_TIMEOUT_SECONDS,
            pool=_CONNECT_TIMEOUT_SECONDS,
        ),
        headers=headers,
    )


def _validate_response(response: httpx.Response, *, expected_content_type: str) -> None:
    if response.is_redirect:
        raise WebConsoleUpstreamError("Durable public API redirect was rejected")
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != expected_content_type:
        raise WebConsoleUpstreamError("Durable public API content type is invalid")
    if expected_content_type == "application/json" and len(response.content) > _MAX_JSON_BYTES:
        raise WebConsoleUpstreamError("Durable public API response is too large")


def _parse_json[T](response: httpx.Response, model: type[T]) -> T:
    try:
        return cast(T, model.model_validate_json(response.content))  # type: ignore[attr-defined]
    except Exception as error:
        raise WebConsoleUpstreamError("Durable public API response is invalid") from error


def _validate_event_id(value: str, *, run_id: str) -> None:
    match = _DURABLE_EVENT_ID.fullmatch(value)
    if match is None or match["run_id"] != run_id:
        raise WebConsoleUpstreamError("Durable event cursor is invalid")


def _is_allowed_memory_route(*, method: str, path: str) -> bool:
    """Keep the WebConsole relay finite: no arbitrary proxy path or query is admitted."""

    if (method, path) in {
        ("GET", "/api/v1/conversation-threads"),
        ("GET", "/api/v1/memory"),
        ("POST", "/api/v1/memory"),
    }:
        return True
    if method == "GET" and _MEMORY_THREAD_TURNS_PATH.fullmatch(path) is not None:
        return True
    if method == "DELETE" and _MEMORY_THREAD_PATH.fullmatch(path) is not None:
        return True
    return method in {"PUT", "DELETE"} and _MEMORY_ENTRY_PATH.fullmatch(path) is not None


def _is_allowed_m6_route(*, path: str) -> bool:
    return (
        _M6_TRACE_PATH.fullmatch(path) is not None
        or _M6_OPERATIONS_PATH.fullmatch(path) is not None
    )


__all__ = [
    "DURABLE_PUBLIC_BASE_URL",
    "DurableLocalAuthRelayResponse",
    "DurablePublicEventFrame",
    "LoopbackDurablePublicClient",
    "WebConsoleDurablePublicClient",
    "WebConsoleUpstreamError",
]
