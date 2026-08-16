"""Loopback adapter for the independent CategoryInsight RAG service."""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from glodex.agent.contracts import (
    CategoryInsightInput,
    CategoryInsightOutput,
    ToolFailureCode,
)
from glodex.agent.ports import ToolPortError

_SCHEMA = "glodex.category-insight-gateway.v1"


@dataclass(frozen=True, slots=True)
class CategoryKnowledgeIdentity:
    index_alias: str
    index_version: str
    document_count: int

    def __post_init__(self) -> None:
        if (
            type(self.index_alias) is not str
            or not self.index_alias
            or type(self.index_version) is not str
            or not self.index_version
            or type(self.document_count) is not int
            or isinstance(self.document_count, bool)
            or self.document_count < 1
        ):
            raise ValueError("category knowledge identity is invalid")


class CategoryKnowledgeInsight:
    """Retrieve structured category knowledge; never expose internal Card IDs."""

    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:18087",
        *,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.port is None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("category knowledge endpoint must be a loopback HTTP URL")
        self._endpoint = "http://127.0.0.1:" + str(parsed.port)
        self._transport = http_transport

    async def health(self) -> CategoryKnowledgeIdentity:
        try:
            body = await self._request("GET", "/v1/health")
            if (
                set(body)
                != {
                    "schema_version",
                    "status",
                    "index_alias",
                    "index_version",
                    "document_count",
                }
                or body.get("schema_version") != _SCHEMA
                or body.get("status") != "ready"
            ):
                raise ValueError
            index_alias = body["index_alias"]
            index_version = body["index_version"]
            document_count = body["document_count"]
            if (
                type(index_alias) is not str
                or type(index_version) is not str
                or type(document_count) is not int
            ):
                raise ValueError
            return CategoryKnowledgeIdentity(
                index_alias=index_alias,
                index_version=index_version,
                document_count=document_count,
            )
        except Exception as error:
            raise ToolPortError(ToolFailureCode.INDEX_INVALID) from error

    async def retrieve(self, request: CategoryInsightInput) -> CategoryInsightOutput:
        if type(request) is not CategoryInsightInput:
            raise TypeError("category insight request must be exact")
        try:
            body = await self._request(
                "POST",
                "/v1/category-insight",
                payload={"category": request.category, "depth": request.depth.value},
            )
            if set(body) != {"schema_version", "insight"} or body.get("schema_version") != _SCHEMA:
                raise ValueError
            return CategoryInsightOutput.model_validate_json(
                json.dumps(body.get("insight"), ensure_ascii=False, allow_nan=False)
            )
        except Exception as error:
            raise ToolPortError(ToolFailureCode.INDEX_INVALID) from error

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        async with httpx.AsyncClient(
            base_url=self._endpoint,
            transport=self._transport,
            timeout=httpx.Timeout(10.0),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.request(method, path, json=payload)
        if response.status_code != 200 or len(response.content) > 256 * 1024:
            raise ValueError("category knowledge gateway rejected the request")
        body = json.loads(response.content)
        if type(body) is not dict:
            raise ValueError("category knowledge gateway returned invalid JSON")
        return body


__all__ = ["CategoryKnowledgeIdentity", "CategoryKnowledgeInsight"]
