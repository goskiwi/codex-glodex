"""Narrow fixed-loopback HTTP client for the existing M2b public durable API."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from typing import Final, Protocol, cast

import httpx

from glodex.api.contracts import ApiErrorEnvelope
from glodex.api.durable_agent_contracts import (
    DurableCreateRunRequest,
    DurableRunAccepted,
    DurableRunStatusResponse,
)

M2B_PUBLIC_BASE_URL: Final = "http://127.0.0.1:8766"
_M2B_EVENT_ID = re.compile(r"(?P<run_id>[A-Za-z0-9][A-Za-z0-9._/-]*):(?P<sequence>[1-9][0-9]*)\Z")
_MAX_JSON_BYTES: Final = 65_536
_CONNECT_TIMEOUT_SECONDS: Final = 2.0
_READ_TIMEOUT_SECONDS: Final = 30.0


class M2dUpstreamError(RuntimeError):
    """A safe failure category that intentionally carries no upstream text."""


@dataclass(frozen=True, slots=True)
class M2bPublicEventFrame:
    """One raw-but-already-public M2b SSE data frame and its source cursor."""

    event_id: str
    data: str


class M2dDurablePublicClient(Protocol):
    """The only durable dependency the M2d adapter is permitted to use."""

    async def create(self, payload: DurableCreateRunRequest) -> DurableRunAccepted: ...

    async def status(self, run_id: str) -> DurableRunStatusResponse: ...

    async def cancel(self, run_id: str) -> DurableRunStatusResponse: ...

    async def resume(self, run_id: str) -> DurableRunStatusResponse: ...

    def events(
        self,
        run_id: str,
        *,
        after: str | None = None,
    ) -> AsyncIterator[M2bPublicEventFrame]: ...


class LoopbackM2bPublicClient:
    """One strict no-proxy/no-redirect client for the M2b loopback public API."""

    def __init__(self, *, base_url: str = M2B_PUBLIC_BASE_URL) -> None:
        if base_url != M2B_PUBLIC_BASE_URL:
            raise ValueError("M2d durable upstream must use the fixed loopback URL")
        self._base_url = base_url

    async def create(self, payload: DurableCreateRunRequest) -> DurableRunAccepted:
        response = await self._request(
            "POST",
            "/api/v1/durable-agent-runs",
            json=payload.model_dump(mode="json", by_alias=True, exclude_none=True),
        )
        if response.status_code != 202:
            await self._raise_safe_error(response)
        return _parse_json(response, DurableRunAccepted)

    async def status(self, run_id: str) -> DurableRunStatusResponse:
        response = await self._request("GET", f"/api/v1/durable-agent-runs/{run_id}")
        if response.status_code != 200:
            await self._raise_safe_error(response)
        return _parse_json(response, DurableRunStatusResponse)

    async def cancel(self, run_id: str) -> DurableRunStatusResponse:
        response = await self._request("POST", f"/api/v1/durable-agent-runs/{run_id}/cancel")
        if response.status_code != 200:
            await self._raise_safe_error(response)
        return _parse_json(response, DurableRunStatusResponse)

    async def resume(self, run_id: str) -> DurableRunStatusResponse:
        response = await self._request("POST", f"/api/v1/durable-agent-runs/{run_id}/resume")
        if response.status_code != 200:
            await self._raise_safe_error(response)
        return _parse_json(response, DurableRunStatusResponse)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
    ) -> httpx.Response:
        try:
            async with _client() as client:
                response = await client.request(method, path, json=json)
        except (httpx.HTTPError, ValueError) as error:
            raise M2dUpstreamError("M2b public API is unavailable") from error
        _validate_response(response, expected_content_type="application/json")
        return response

    async def _raise_safe_error(self, response: httpx.Response) -> None:
        with suppress(M2dUpstreamError):
            _parse_json(response, ApiErrorEnvelope)
        raise M2dUpstreamError("M2b public API rejected the operation")

    async def _events(
        self,
        run_id: str,
        *,
        after: str | None,
    ) -> AsyncIterator[M2bPublicEventFrame]:
        headers = {"accept": "text/event-stream"}
        if after is not None:
            _validate_event_id(after, run_id=run_id)
            headers["last-event-id"] = after
        try:
            async with (
                _client() as client,
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
                            yield M2bPublicEventFrame(
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
                        raise M2dUpstreamError("M2b event stream is invalid")
                    normalized = value[1:] if value.startswith(" ") else value
                    if field == "id":
                        _validate_event_id(normalized, run_id=run_id)
                        pending_id = normalized
                    elif field == "data":
                        if len(normalized.encode("utf-8")) > _MAX_JSON_BYTES:
                            raise M2dUpstreamError("M2b event payload is too large")
                        data_lines.append(normalized)
        except M2dUpstreamError:
            raise
        except (httpx.HTTPError, ValueError) as error:
            raise M2dUpstreamError("M2b event stream is unavailable") from error

    def events(
        self,
        run_id: str,
        *,
        after: str | None = None,
    ) -> AsyncIterator[M2bPublicEventFrame]:
        return self._events(run_id, after=after)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=M2B_PUBLIC_BASE_URL,
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(
            connect=_CONNECT_TIMEOUT_SECONDS,
            read=_READ_TIMEOUT_SECONDS,
            write=_READ_TIMEOUT_SECONDS,
            pool=_CONNECT_TIMEOUT_SECONDS,
        ),
        headers={"accept": "application/json"},
    )


def _validate_response(response: httpx.Response, *, expected_content_type: str) -> None:
    if response.is_redirect:
        raise M2dUpstreamError("M2b public API redirect was rejected")
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != expected_content_type:
        raise M2dUpstreamError("M2b public API content type is invalid")
    if expected_content_type == "application/json" and len(response.content) > _MAX_JSON_BYTES:
        raise M2dUpstreamError("M2b public API response is too large")


def _parse_json[T](response: httpx.Response, model: type[T]) -> T:
    try:
        return cast(T, model.model_validate_json(response.content))  # type: ignore[attr-defined]
    except Exception as error:
        raise M2dUpstreamError("M2b public API response is invalid") from error


def _validate_event_id(value: str, *, run_id: str) -> None:
    match = _M2B_EVENT_ID.fullmatch(value)
    if match is None or match["run_id"] != run_id:
        raise M2dUpstreamError("M2b event cursor is invalid")


__all__ = [
    "M2B_PUBLIC_BASE_URL",
    "LoopbackM2bPublicClient",
    "M2bPublicEventFrame",
    "M2dDurablePublicClient",
    "M2dUpstreamError",
]
