"""Fixed-loopback client adapters for the explicit M2c GPU model service."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final, cast
from urllib.parse import SplitResult, urlsplit

import httpx

from glodex.adapters.dashscope_rerank import RerankRequest, RerankResult
from glodex.application.agent.contracts import EmbeddingBatch, EmbeddingResult, ToolFailureCode
from glodex.application.agent.ports import EmbeddingPort, ToolPortError
from glodex.m2c_contract import (
    M2C_DIMENSION,
    M2C_EMBEDDING_MODEL,
    M2C_ENDPOINT,
    M2C_MAX_EMBEDDING_TEXTS,
    M2C_MAX_RERANK_DOCUMENTS,
    M2C_MAX_TEXT_CHARACTERS,
    M2C_RERANKER_MODEL,
    M2C_SERVICE_SCHEMA,
    M2cModelIdentity,
)

M2C_HEALTH_DEADLINE_SECONDS: Final = 2.0
M2C_INFERENCE_DEADLINE_SECONDS: Final = 15.0
_MAX_HEALTH_RESPONSE_BYTES: Final = 64 * 1024
_MAX_INFERENCE_RESPONSE_BYTES: Final = 128 * 1024
_MAX_EMBED_REQUEST_BYTES: Final = 16 * 1024
_MAX_RERANK_REQUEST_BYTES: Final = 32 * 1024
_LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "::1"})


class M2cModelServiceError(RuntimeError):
    """A stable M2c transport/schema failure with no GPU detail."""

    code: Final[str] = "M2C_MODEL_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__(self.code)


def validate_m2c_endpoint(value: object) -> str:
    """Accept exactly the existing private loopback tunnel endpoint."""

    if type(value) is not str or not value or len(value) > 128:
        raise ValueError("M2c model endpoint is invalid")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in _LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.port != 18000
    ):
        raise ValueError("M2c model endpoint must be the fixed loopback tunnel")
    return f"http://{_render_host(parsed)}:18000"


def _render_host(parsed: SplitResult) -> str:
    if parsed.hostname == "::1":
        return "[::1]"
    if parsed.hostname == "127.0.0.1":
        return "127.0.0.1"
    raise ValueError("M2c model endpoint host is invalid")


@dataclass(frozen=True, slots=True)
class M2cModelServiceClient:
    """One bounded, no-proxy client for the fixed local SSH tunnel."""

    http_transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)

    async def health(self) -> M2cModelIdentity:
        body = await self._request(
            method="GET",
            path="/v1/health",
            payload=None,
            deadline_seconds=M2C_HEALTH_DEADLINE_SECONDS,
            response_limit=_MAX_HEALTH_RESPONSE_BYTES,
        )
        try:
            return _identity_from_health(_json_object(body))
        except Exception:
            raise M2cModelServiceError() from None

    async def embed_texts(
        self,
        *,
        texts: tuple[str, ...],
        identity: M2cModelIdentity,
    ) -> tuple[tuple[float, ...], ...]:
        _validate_texts(texts, maximum=M2C_MAX_EMBEDDING_TEXTS)
        payload = _json_request({"texts": list(texts)}, maximum=_MAX_EMBED_REQUEST_BYTES)
        body = await self._request(
            method="POST",
            path="/v1/embed",
            payload=payload,
            deadline_seconds=M2C_INFERENCE_DEADLINE_SECONDS,
            response_limit=_MAX_INFERENCE_RESPONSE_BYTES,
        )
        try:
            root = _json_object(body)
            if root.get("manifestDigest") != identity.manifest_digest:
                raise ValueError("manifest mismatch")
            raw_vectors = root.get("embeddings")
            if type(raw_vectors) is not list or len(raw_vectors) != len(texts):
                raise ValueError("embedding count is invalid")
            vectors = tuple(_vector(value) for value in raw_vectors)
            return vectors
        except Exception:
            raise M2cModelServiceError() from None

    async def rerank_scores(
        self,
        *,
        request: RerankRequest,
        identity: M2cModelIdentity,
    ) -> tuple[float, ...]:
        if type(request) is not RerankRequest:
            raise M2cModelServiceError()
        documents = tuple(document.text for document in request.documents)
        _validate_texts(documents, maximum=M2C_MAX_RERANK_DOCUMENTS)
        payload = _json_request(
            {"documents": list(documents), "query": request.query},
            maximum=_MAX_RERANK_REQUEST_BYTES,
        )
        body = await self._request(
            method="POST",
            path="/v1/rerank",
            payload=payload,
            deadline_seconds=M2C_INFERENCE_DEADLINE_SECONDS,
            response_limit=_MAX_INFERENCE_RESPONSE_BYTES,
        )
        try:
            root = _json_object(body)
            if root.get("manifestDigest") != identity.manifest_digest:
                raise ValueError("manifest mismatch")
            raw_scores = root.get("scores")
            if type(raw_scores) is not list or len(raw_scores) != len(request.documents):
                raise ValueError("rerank count is invalid")
            scores = tuple(_score(value) for value in raw_scores)
            return scores
        except Exception:
            raise M2cModelServiceError() from None

    async def _request(
        self,
        *,
        method: str,
        path: str,
        payload: bytes | None,
        deadline_seconds: float,
        response_limit: int,
    ) -> bytes:
        if method not in {"GET", "POST"} or path not in {
            "/v1/health",
            "/v1/embed",
            "/v1/rerank",
        }:
            raise M2cModelServiceError()
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            transport = self.http_transport or httpx.AsyncHTTPTransport(retries=0, trust_env=False)
            async with asyncio.timeout(deadline_seconds):
                async with (
                    httpx.AsyncClient(
                        transport=transport,
                        timeout=httpx.Timeout(deadline_seconds),
                        follow_redirects=False,
                        trust_env=False,
                    ) as client,
                    client.stream(
                        method,
                        f"{M2C_ENDPOINT}{path}",
                        headers=headers,
                        content=payload,
                    ) as response,
                ):
                    if response.status_code != 200 or response.headers.get(
                        "Content-Encoding"
                    ) not in {
                        None,
                        "identity",
                    }:
                        raise M2cModelServiceError()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(chunk) > response_limit - len(body):
                            raise M2cModelServiceError()
                        body.extend(chunk)
                    return bytes(body)
        except M2cModelServiceError:
            raise
        except (TimeoutError, httpx.HTTPError):
            raise M2cModelServiceError() from None
        except Exception:
            raise M2cModelServiceError() from None


@dataclass(frozen=True, slots=True)
class M2cEmbedding(EmbeddingPort):
    """The M2c implementation of the existing narrow EmbeddingPort."""

    client: M2cModelServiceClient
    identity: M2cModelIdentity

    async def embed(self, request: EmbeddingBatch) -> EmbeddingResult:
        if type(request) is not EmbeddingBatch:
            raise ToolPortError(ToolFailureCode.M2C_QUERY_EMBEDDING_FAILED)
        try:
            vectors = await self.client.embed_texts(texts=request.texts, identity=self.identity)
            return EmbeddingResult(vectors=vectors)
        except M2cModelServiceError:
            raise ToolPortError(ToolFailureCode.M2C_QUERY_EMBEDDING_FAILED) from None


@dataclass(frozen=True, slots=True)
class M2cReranker:
    """Translate bounded M2c cross-encoder scores into the existing identity result."""

    client: M2cModelServiceClient
    identity: M2cModelIdentity

    async def rerank(self, request: RerankRequest) -> RerankResult:
        try:
            scores = await self.client.rerank_scores(request=request, identity=self.identity)
        except M2cModelServiceError:
            raise M2cModelServiceError() from None
        ranked = sorted(
            zip(scores, request.documents, strict=True),
            key=lambda item: (-item[0], item[1].identity),
        )
        try:
            return RerankResult(
                identities=tuple(document.identity for _score_value, document in ranked),
                model=M2C_RERANKER_MODEL,
            )
        except ValueError:
            raise M2cModelServiceError() from None


def _identity_from_health(root: dict[str, object]) -> M2cModelIdentity:
    expected = {
        "deviceClass",
        "dimension",
        "embeddingModel",
        "gpuModelClass",
        "manifestDigest",
        "maxEmbeddingTexts",
        "maxQueryCharacters",
        "maxRerankDocuments",
        "maxTextCharacters",
        "rerankerModel",
        "schemaVersion",
    }
    if set(root) != expected:
        raise ValueError("health keys are invalid")
    return M2cModelIdentity(
        manifest_digest=_string(root["manifestDigest"]),
        embedding_model=_string(root["embeddingModel"]),
        reranker_model=_string(root["rerankerModel"]),
        dimension=_integer(root["dimension"]),
        max_embedding_texts=_integer(root["maxEmbeddingTexts"]),
        max_text_characters=_integer(root["maxTextCharacters"]),
        max_rerank_documents=_integer(root["maxRerankDocuments"]),
        max_query_characters=_integer(root["maxQueryCharacters"]),
        device_class=_string(root["deviceClass"]),
        gpu_model_class=_string(root["gpuModelClass"]),
        schema_version=_string(root["schemaVersion"]),
    )


def _json_request(value: object, *, maximum: int) -> bytes:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > maximum:
        raise M2cModelServiceError()
    return payload


def _json_object(body: bytes) -> dict[str, object]:
    root = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
    if type(root) is not dict:
        raise ValueError("JSON root is invalid")
    return cast("dict[str, object]", root)


def _unique_object(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _validate_texts(texts: tuple[str, ...], *, maximum: int) -> None:
    if (
        type(texts) is not tuple
        or not texts
        or len(texts) > maximum
        or any(
            type(text) is not str
            or not text.strip()
            or len(text) > M2C_MAX_TEXT_CHARACTERS
            or "\0" in text
            for text in texts
        )
    ):
        raise M2cModelServiceError()


def _vector(value: object) -> tuple[float, ...]:
    if type(value) is not list or len(value) != M2C_DIMENSION:
        raise ValueError("vector dimension is invalid")
    if any(type(item) not in (int, float) for item in value):
        raise ValueError("vector value is invalid")
    vector = tuple(float(cast("int | float", item)) for item in value)
    norm = math.sqrt(sum(item * item for item in vector))
    if not math.isfinite(norm) or not math.isclose(norm, 1.0, rel_tol=0.0, abs_tol=1e-6):
        raise ValueError("vector normalization is invalid")
    return vector


def _score(value: object) -> float:
    if type(value) is int:
        score = float(value)
    elif type(value) is float:
        score = value
    else:
        raise ValueError("rerank score is invalid")
    if not math.isfinite(score):
        raise ValueError("rerank score is invalid")
    return score


def _string(value: object) -> str:
    if type(value) is not str:
        raise ValueError("string is invalid")
    return value


def _integer(value: object) -> int:
    if type(value) is not int or isinstance(value, bool):
        raise ValueError("integer is invalid")
    return value


__all__ = [
    "M2C_DIMENSION",
    "M2C_EMBEDDING_MODEL",
    "M2C_ENDPOINT",
    "M2C_RERANKER_MODEL",
    "M2C_SERVICE_SCHEMA",
    "M2cEmbedding",
    "M2cModelIdentity",
    "M2cModelServiceClient",
    "M2cModelServiceError",
    "M2cReranker",
    "validate_m2c_endpoint",
]
