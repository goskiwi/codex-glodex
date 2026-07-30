"""The single fixed, bounded DashScope qwen3-rerank adapter used by M2a."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final, cast

import httpx

_RERANK_URL: Final = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
_MODEL: Final = "qwen3-rerank"
_M2C_MODEL: Final = "glodex-bge-reranker-v1"
_CREDENTIAL: Final = "DASHSCOPE_API_KEY"
_DEADLINE_SECONDS: Final = 15.0
_MAX_REQUEST_BYTES: Final = 32 * 1024
_MAX_RESPONSE_BYTES: Final = 128 * 1024
_MAX_CANDIDATES: Final = 40
_MAX_QUERY_CHARACTERS: Final = 512
_MAX_DOCUMENT_CHARACTERS: Final = 2_000


class M2aRerankError(RuntimeError):
    """A safe reranking failure that omits provider text and credentials."""


@dataclass(frozen=True, slots=True)
class RerankDocument:
    identity: str
    text: str

    def __post_init__(self) -> None:
        if (
            type(self.identity) is not str
            or not self.identity
            or len(self.identity) > 128
            or type(self.text) is not str
            or not self.text.strip()
            or len(self.text) > _MAX_DOCUMENT_CHARACTERS
            or "\0" in self.text
        ):
            raise ValueError("M2a rerank document is invalid")


@dataclass(frozen=True, slots=True)
class RerankRequest:
    query: str
    documents: tuple[RerankDocument, ...]

    def __post_init__(self) -> None:
        if (
            type(self.query) is not str
            or not self.query.strip()
            or len(self.query) > _MAX_QUERY_CHARACTERS
            or "\0" in self.query
            or type(self.documents) is not tuple
            or not self.documents
            or len(self.documents) > _MAX_CANDIDATES
            or any(type(document) is not RerankDocument for document in self.documents)
        ):
            raise ValueError("M2a rerank request is invalid")
        identities = tuple(document.identity for document in self.documents)
        if len(identities) != len(set(identities)):
            raise ValueError("M2a rerank identities must be unique")


@dataclass(frozen=True, slots=True)
class RerankResult:
    identities: tuple[str, ...]
    model: str = _MODEL

    def __post_init__(self) -> None:
        if (
            self.model not in {_MODEL, _M2C_MODEL}
            or type(self.identities) is not tuple
            or not self.identities
            or len(self.identities) > _MAX_CANDIDATES
            or any(type(identity) is not str or not identity for identity in self.identities)
            or len(self.identities) != len(set(self.identities))
        ):
            raise ValueError("M2a rerank result is invalid")


@dataclass(frozen=True, slots=True)
class DashScopeReranker:
    credential: str = field(repr=False)
    http_transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)

    async def rerank(self, request: RerankRequest) -> RerankResult:
        if type(request) is not RerankRequest:
            raise M2aRerankError("M2A_RERANK_DEGRADED")
        payload = _payload_for(request)
        body = await _post(
            payload=payload,
            credential=self.credential,
            transport=self.http_transport,
        )
        try:
            return _parse_response(body, request=request)
        except M2aRerankError:
            raise
        except Exception:
            raise M2aRerankError("M2A_RERANK_DEGRADED") from None


def build_dashscope_reranker(
    *,
    environ: dict[str, str] | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> DashScopeReranker:
    """Build the only M2a reranker from an already-present process credential."""

    source = os.environ if environ is None else environ
    credential = source.get(_CREDENTIAL)
    if (
        type(credential) is not str
        or not credential
        or credential != credential.strip()
        or any(not 0x21 <= ord(character) <= 0x7E for character in credential)
    ):
        raise M2aRerankError("M2A_RERANK_DEGRADED")
    return DashScopeReranker(credential=credential, http_transport=http_transport)


def _payload_for(request: RerankRequest) -> bytes:
    value = {
        "input": {
            "documents": [document.text for document in request.documents],
            "query": request.query,
        },
        "model": _MODEL,
        "parameters": {"return_documents": False},
    }
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > _MAX_REQUEST_BYTES:
        raise M2aRerankError("M2A_RERANK_DEGRADED")
    return payload


async def _post(
    *,
    payload: bytes,
    credential: str,
    transport: httpx.AsyncBaseTransport | None,
) -> bytes:
    try:
        active_transport = transport
        if active_transport is None:
            active_transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False)
        async with asyncio.timeout(_DEADLINE_SECONDS):
            async with (
                httpx.AsyncClient(
                    transport=active_transport,
                    timeout=httpx.Timeout(_DEADLINE_SECONDS),
                    follow_redirects=False,
                    trust_env=False,
                ) as client,
                client.stream(
                    "POST",
                    _RERANK_URL,
                    headers={
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                        "Authorization": f"Bearer {credential}",
                        "Content-Type": "application/json",
                    },
                    content=payload,
                ) as response,
            ):
                if response.status_code != 200 or response.headers.get("Content-Encoding") not in {
                    None,
                    "identity",
                }:
                    raise M2aRerankError("M2A_RERANK_DEGRADED")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(chunk) > _MAX_RESPONSE_BYTES - len(body):
                        raise M2aRerankError("M2A_RERANK_DEGRADED")
                    body.extend(chunk)
                return bytes(body)
    except M2aRerankError:
        raise
    except (TimeoutError, httpx.HTTPError):
        raise M2aRerankError("M2A_RERANK_DEGRADED") from None
    except Exception:
        raise M2aRerankError("M2A_RERANK_DEGRADED") from None


def _parse_response(body: bytes, *, request: RerankRequest) -> RerankResult:
    if len(body) > _MAX_RESPONSE_BYTES:
        raise M2aRerankError("M2A_RERANK_DEGRADED")
    raw = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
    if type(raw) is not dict:
        raise ValueError("rerank envelope is invalid")
    root = cast("dict[str, object]", raw)
    if root.get("model") not in {None, _MODEL}:
        raise ValueError("rerank model is invalid")
    output = root.get("output")
    if type(output) is not dict:
        raise ValueError("rerank output is invalid")
    results = cast("dict[str, object]", output).get("results")
    if type(results) is not list or len(results) != len(request.documents):
        raise ValueError("rerank result count is invalid")
    ranked: list[tuple[float, int]] = []
    seen: set[int] = set()
    for item in results:
        if type(item) is not dict:
            raise ValueError("rerank result is invalid")
        result = cast("dict[str, object]", item)
        index = result.get("index")
        score = result.get("relevance_score")
        if (
            type(index) is not int
            or isinstance(index, bool)
            or not 0 <= index < len(request.documents)
            or index in seen
            or type(score) not in {int, float}
            or isinstance(score, bool)
        ):
            raise ValueError("rerank result identity is invalid")
        seen.add(index)
        ranked.append((float(cast("float", score)), index))
    if seen != set(range(len(request.documents))):
        raise ValueError("rerank result identities are incomplete")
    ranked.sort(key=lambda item: (-item[0], request.documents[item[1]].identity))
    return RerankResult(
        identities=tuple(request.documents[index].identity for _score, index in ranked)
    )


def _unique_object(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


__all__ = [
    "DashScopeReranker",
    "M2aRerankError",
    "RerankDocument",
    "RerankRequest",
    "RerankResult",
    "build_dashscope_reranker",
]
