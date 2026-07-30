"""Fixed, bounded Tavily Search and DashScope embedding adapters for M1d."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Never, cast
from urllib.parse import urlsplit

import httpx

from glodex.application.agent.contracts import (
    EmbeddingBatch,
    EmbeddingResult,
    EvidenceKind,
    ToolFailureCode,
    WebEvidence,
    WebSearchInput,
    WebSearchOutput,
)
from glodex.application.agent.ports import (
    EmbeddingPort,
    ToolPortError,
    WebSearchPort,
)

TAVILY_TOTAL_DEADLINE_SECONDS: Final = 12.0
DASHSCOPE_TOTAL_DEADLINE_SECONDS: Final = 15.0

_TAVILY_URL: Final = "https://api.tavily.com/search"
_DASHSCOPE_URL: Final = "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"
_TAVILY_CREDENTIAL: Final = "TAVILY_API_KEY"
_DASHSCOPE_CREDENTIAL: Final = "DASHSCOPE_API_KEY"
_MAX_REQUEST_BYTES: Final = 16 * 1024
_TAVILY_MAX_RESPONSE_BYTES: Final = 256 * 1024
_DASHSCOPE_MAX_RESPONSE_BYTES: Final = 128 * 1024
_TAVILY_MAX_RESULTS: Final = 8
_EMBEDDING_MODEL: Final = "text-embedding-v4"
_EMBEDDING_DIMENSIONS: Final = 1_024
_CONTENT_LENGTH = re.compile(r"[0-9]+\Z")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_DASHSCOPE_ROOT_KEYS: Final = frozenset(
    {
        "data",
        "id",
        "model",
        "object",
        "usage",
    }
)
_DASHSCOPE_ITEM_KEYS: Final = frozenset({"embedding", "index", "object"})


@dataclass(frozen=True, slots=True)
class _TavilyWebSearch:
    credential: str = field(repr=False)
    http_transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)

    async def search(self, request: WebSearchInput) -> WebSearchOutput:
        if type(request) is not WebSearchInput:
            raise ToolPortError(ToolFailureCode.PROVIDER_RESPONSE_INVALID)
        payload = _json_request(
            {
                "query": request.query,
                "topic": "general",
                "search_depth": "basic",
                "auto_parameters": False,
                "include_answer": False,
                "include_raw_content": False,
                "max_results": _TAVILY_MAX_RESULTS,
            }
        )
        response = await _post_bounded(
            url=_TAVILY_URL,
            credential=self.credential,
            payload=payload,
            deadline_seconds=TAVILY_TOTAL_DEADLINE_SECONDS,
            response_limit=_TAVILY_MAX_RESPONSE_BYTES,
            http_transport=self.http_transport,
        )
        try:
            return _parse_tavily_response(response, request=request)
        except ToolPortError:
            raise
        except Exception:
            raise ToolPortError(ToolFailureCode.PROVIDER_RESPONSE_INVALID) from None


@dataclass(frozen=True, slots=True)
class _DashScopeEmbedding:
    credential: str = field(repr=False)
    http_transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)

    async def embed(self, request: EmbeddingBatch) -> EmbeddingResult:
        if type(request) is not EmbeddingBatch:
            raise ToolPortError(ToolFailureCode.PROVIDER_RESPONSE_INVALID)
        payload = _json_request(
            {
                "model": _EMBEDDING_MODEL,
                "input": list(request.texts),
                "dimensions": _EMBEDDING_DIMENSIONS,
            }
        )
        response = await _post_bounded(
            url=_DASHSCOPE_URL,
            credential=self.credential,
            payload=payload,
            deadline_seconds=DASHSCOPE_TOTAL_DEADLINE_SECONDS,
            response_limit=_DASHSCOPE_MAX_RESPONSE_BYTES,
            http_transport=self.http_transport,
        )
        try:
            return _parse_dashscope_response(response, expected_count=len(request.texts))
        except ToolPortError:
            raise
        except Exception:
            raise ToolPortError(ToolFailureCode.PROVIDER_RESPONSE_INVALID) from None


def build_tavily_web_search(
    *,
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> WebSearchPort:
    """Build the only approved Tavily WebSearchPort from process credentials."""

    credential = _process_credential(
        _TAVILY_CREDENTIAL,
        missing_code=ToolFailureCode.WEB_SEARCH_NOT_ENABLED,
    )
    return _TavilyWebSearch(
        credential=credential,
        http_transport=http_transport,
    )


def build_dashscope_embedding(
    *,
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> EmbeddingPort:
    """Build the only approved DashScope EmbeddingPort from process credentials."""

    credential = _process_credential(
        _DASHSCOPE_CREDENTIAL,
        missing_code=ToolFailureCode.PROVIDER_UNAVAILABLE,
    )
    return _DashScopeEmbedding(
        credential=credential,
        http_transport=http_transport,
    )


async def _post_bounded(
    *,
    url: str,
    credential: str,
    payload: bytes,
    deadline_seconds: float,
    response_limit: int,
    http_transport: httpx.AsyncBaseTransport | None,
) -> bytes:
    """Perform one identity-encoded POST and collect at most ``response_limit`` bytes."""

    if len(payload) > _MAX_REQUEST_BYTES:
        raise ToolPortError(ToolFailureCode.PROVIDER_RESPONSE_INVALID)
    try:
        async with asyncio.timeout(deadline_seconds):
            active_transport = http_transport
            if active_transport is None:
                active_transport = httpx.AsyncHTTPTransport(
                    retries=0,
                    trust_env=False,
                )
            async with (
                httpx.AsyncClient(
                    transport=active_transport,
                    timeout=httpx.Timeout(deadline_seconds),
                    follow_redirects=False,
                    trust_env=False,
                ) as client,
                client.stream(
                    "POST",
                    url,
                    headers={
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                        "Authorization": f"Bearer {credential}",
                        "Content-Type": "application/json",
                    },
                    content=payload,
                ) as response,
            ):
                if 300 <= response.status_code < 400:
                    raise ToolPortError(ToolFailureCode.PROVIDER_RESPONSE_INVALID)
                if response.status_code != 200:
                    raise ToolPortError(ToolFailureCode.PROVIDER_UNAVAILABLE)
                content_encoding = response.headers.get("Content-Encoding")
                if content_encoding is not None and content_encoding.lower() != "identity":
                    raise ToolPortError(ToolFailureCode.PROVIDER_RESPONSE_INVALID)
                declared_length = _declared_length(
                    response.headers.get("Content-Length"),
                    limit=response_limit,
                )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(chunk) > response_limit - len(body):
                        raise ToolPortError(ToolFailureCode.PROVIDER_RESPONSE_INVALID)
                    if declared_length is not None and len(chunk) > declared_length - len(body):
                        raise ToolPortError(ToolFailureCode.PROVIDER_RESPONSE_INVALID)
                    body.extend(chunk)
                if declared_length is not None and len(body) != declared_length:
                    raise ToolPortError(ToolFailureCode.PROVIDER_RESPONSE_INVALID)
                return bytes(body)
    except ToolPortError:
        raise
    except (TimeoutError, httpx.HTTPError):
        raise ToolPortError(ToolFailureCode.PROVIDER_UNAVAILABLE) from None
    except Exception:
        raise ToolPortError(ToolFailureCode.PROVIDER_UNAVAILABLE) from None


def _declared_length(raw_value: str | None, *, limit: int) -> int | None:
    if raw_value is None:
        return None
    if _CONTENT_LENGTH.fullmatch(raw_value) is None:
        raise ToolPortError(ToolFailureCode.PROVIDER_RESPONSE_INVALID)
    normalized = raw_value.lstrip("0") or "0"
    limit_text = str(limit)
    if len(normalized) > len(limit_text) or (
        len(normalized) == len(limit_text) and normalized > limit_text
    ):
        raise ToolPortError(ToolFailureCode.PROVIDER_RESPONSE_INVALID)
    return int(normalized)


def _process_credential(
    name: str,
    *,
    missing_code: ToolFailureCode,
) -> str:
    value: object = os.environ.get(name)
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or any(not 0x21 <= ord(character) <= 0x7E for character in value)
    ):
        raise ToolPortError(missing_code)
    return value


def _json_request(value: object) -> bytes:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    if len(payload) > _MAX_REQUEST_BYTES:
        raise ToolPortError(ToolFailureCode.PROVIDER_RESPONSE_INVALID)
    return payload


def _parse_tavily_response(
    response: bytes,
    *,
    request: WebSearchInput,
) -> WebSearchOutput:
    root = _json_object(response)
    # Tavily may add envelope metadata.  It is intentionally not trusted or
    # projected; only the bounded result fields below affect WebEvidence.
    if "results" not in root:
        raise ValueError("invalid Tavily root")
    raw_results = root["results"]
    if (
        type(raw_results) is not list
        or len(raw_results) > _TAVILY_MAX_RESULTS
        or len(raw_results) > request.max_results
    ):
        raise ValueError("invalid Tavily result collection")
    evidence = tuple(
        _parse_tavily_result(item, source_type=request.evidence_kind) for item in raw_results
    )
    return WebSearchOutput(evidence=evidence)


def _parse_tavily_result(
    value: object,
    *,
    source_type: EvidenceKind,
) -> WebEvidence:
    item = _object(value)
    if not {"title", "url", "content"}.issubset(item):
        raise ValueError("invalid Tavily result")
    # Provider snippets are not an API contract for our public DTO.  Bound
    # them while projecting so a valid longer response cannot become an Agent
    # failure, and never expose the discarded text.
    title = _provider_projection_text(item["title"], maximum=256)
    snippet = _provider_projection_text(item["content"], maximum=280)
    source_url, domain = _source_identity(item["url"])
    published_at = _published_at(item.get("published_date"))
    return WebEvidence(
        source_id=f"web.{hashlib.sha256(source_url.encode()).hexdigest()[:24]}",
        title=title,
        url_domain=domain,
        published_at=published_at,
        snippet=snippet,
        source_type=source_type,
    )


def _source_identity(value: object) -> tuple[str, str]:
    raw_url = _provider_text(value, maximum=2_048)
    parsed = urlsplit(raw_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("unsafe source URL")
    domain = parsed.hostname.lower()
    if not domain or len(domain) > 128:
        raise ValueError("source domain is invalid")
    port = f":{parsed.port}" if parsed.port is not None and parsed.port != 443 else ""
    path = parsed.path or "/"
    return f"https://{domain}{port}{path}", domain


def _published_at(value: object) -> datetime | None:
    if value is None:
        return None
    text = _provider_text(value, maximum=64)
    if _DATE.fullmatch(text) is not None:
        return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC)
    normalized = f"{text[:-1]}+00:00" if text.endswith("Z") else text
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("published timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _parse_dashscope_response(
    response: bytes,
    *,
    expected_count: int,
) -> EmbeddingResult:
    root = _json_object(response)
    if not frozenset(root).issubset(_DASHSCOPE_ROOT_KEYS) or "data" not in root:
        raise ValueError("invalid DashScope root")
    if "model" in root and root["model"] != _EMBEDDING_MODEL:
        raise ValueError("unexpected embedding model")
    if "object" in root and root["object"] != "list":
        raise ValueError("unexpected embedding envelope type")
    raw_data = root["data"]
    if type(raw_data) is not list or len(raw_data) != expected_count:
        raise ValueError("embedding count mismatch")
    by_index: dict[int, tuple[float, ...]] = {}
    for value in raw_data:
        item = _object(value)
        if not frozenset(item).issubset(_DASHSCOPE_ITEM_KEYS) or not {
            "embedding",
            "index",
        }.issubset(item):
            raise ValueError("invalid embedding item")
        if "object" in item and item["object"] != "embedding":
            raise ValueError("invalid embedding object")
        index = item["index"]
        if (
            type(index) is not int
            or isinstance(index, bool)
            or not 0 <= index < expected_count
            or index in by_index
        ):
            raise ValueError("invalid embedding index")
        by_index[index] = _normalized_vector(item["embedding"])
    if set(by_index) != set(range(expected_count)):
        raise ValueError("embedding indices are incomplete")
    return EmbeddingResult(vectors=tuple(by_index[index] for index in range(expected_count)))


def _normalized_vector(value: object) -> tuple[float, ...]:
    if type(value) is not list or len(value) != _EMBEDDING_DIMENSIONS:
        raise ValueError("embedding dimensions are invalid")
    vector: list[float] = []
    for item in value:
        if isinstance(item, bool) or type(item) not in {int, float}:
            raise ValueError("embedding value is invalid")
        converted = float(item)
        if not math.isfinite(converted):
            raise ValueError("embedding value is not finite")
        vector.append(converted)
    scale = max(abs(item) for item in vector)
    if scale <= 0:
        raise ValueError("embedding vector is zero or invalid")
    scaled = tuple(item / scale for item in vector)
    norm = math.sqrt(math.fsum(item * item for item in scaled))
    return tuple(item / norm for item in scaled)


def _json_object(payload: bytes) -> dict[str, object]:
    value = json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )
    return _object(value)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> Never:
    raise ValueError("non-standard JSON number")


def _object(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError("provider JSON value must be an object")
    return cast("dict[str, object]", value)


def _provider_text(value: object, *, maximum: int) -> str:
    if type(value) is not str:
        raise ValueError("provider text must be a string")
    text = value.strip()
    if (
        not text
        or len(text) > maximum
        or "\0" in text
        or any(0xD800 <= ord(character) <= 0xDFFF for character in text)
    ):
        raise ValueError("provider text is invalid")
    return text


def _provider_projection_text(value: object, *, maximum: int) -> str:
    """Validate provider text then retain only the public projection bound."""

    if type(value) is not str:
        raise ValueError("provider text must be a string")
    text = value.strip()
    if not text or "\0" in text or any(0xD800 <= ord(character) <= 0xDFFF for character in text):
        raise ValueError("provider text is invalid")
    return text[:maximum].rstrip()


__all__ = [
    "DASHSCOPE_TOTAL_DEADLINE_SECONDS",
    "TAVILY_TOTAL_DEADLINE_SECONDS",
    "build_dashscope_embedding",
    "build_tavily_web_search",
]
