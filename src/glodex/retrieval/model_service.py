"""Fixed-loopback client adapters for the private retrieval model service."""

from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass, field
from typing import Final, cast
from urllib.parse import SplitResult, urlsplit

import httpx

from glodex._json import compact_bytes, loads_unique
from glodex.agent.contracts import EmbeddingBatch, EmbeddingResult, ToolFailureCode
from glodex.agent.ports import EmbeddingPort, ToolPortError
from glodex.application.rerank_contracts import RerankRequest, RerankResult
from glodex.observability.runtime import (
    M6CircuitOpen,
    M6Operation,
    M6OperationLease,
    M6OperationRecorder,
)
from glodex.retrieval.contracts import (
    RETRIEVAL_MODEL_DIMENSION,
    RETRIEVAL_MODEL_EMBEDDING_MODEL,
    RETRIEVAL_MODEL_ENDPOINT,
    RETRIEVAL_MODEL_MAX_EMBEDDING_TEXTS,
    RETRIEVAL_MODEL_MAX_RERANK_DOCUMENTS,
    RETRIEVAL_MODEL_MAX_TEXT_CHARACTERS,
    RETRIEVAL_MODEL_RERANKER_MODEL,
    RETRIEVAL_MODEL_SERVICE_SCHEMA,
    RetrievalModelIdentity,
)

RETRIEVAL_MODEL_HEALTH_DEADLINE_SECONDS: Final = 2.0
RETRIEVAL_MODEL_INFERENCE_DEADLINE_SECONDS: Final = 15.0
_MAX_HEALTH_RESPONSE_BYTES: Final = 64 * 1024
_MAX_INFERENCE_RESPONSE_BYTES: Final = 128 * 1024
_MAX_EMBED_REQUEST_BYTES: Final = 16 * 1024
_MAX_RERANK_REQUEST_BYTES: Final = 32 * 1024
_LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "::1"})


class RetrievalModelError(RuntimeError):
    """A stable RetrievalModel transport/schema failure with no GPU detail."""

    code: Final[str] = "RETRIEVAL_MODEL_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__(self.code)


def validate_retrieval_model_endpoint(value: object) -> str:
    """Accept exactly the existing private loopback tunnel endpoint."""

    if type(value) is not str or not value or len(value) > 128:
        raise ValueError("retrieval model endpoint is invalid")
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
        raise ValueError("retrieval model endpoint must be the fixed loopback tunnel")
    return f"http://{_render_host(parsed)}:18000"


def _render_host(parsed: SplitResult) -> str:
    if parsed.hostname == "::1":
        return "[::1]"
    if parsed.hostname == "127.0.0.1":
        return "127.0.0.1"
    raise ValueError("retrieval model endpoint host is invalid")


@dataclass(frozen=True, slots=True)
class RetrievalModelClient:
    """One bounded, no-proxy client for the fixed local SSH tunnel."""

    http_transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)

    async def health(self) -> RetrievalModelIdentity:
        body = await self._request(
            method="GET",
            path="/v1/health",
            payload=None,
            deadline_seconds=RETRIEVAL_MODEL_HEALTH_DEADLINE_SECONDS,
            response_limit=_MAX_HEALTH_RESPONSE_BYTES,
        )
        try:
            return _identity_from_health(_json_object(body))
        except Exception:
            raise RetrievalModelError() from None

    async def embed_texts(
        self,
        *,
        texts: tuple[str, ...],
        identity: RetrievalModelIdentity,
    ) -> tuple[tuple[float, ...], ...]:
        _validate_texts(texts, maximum=RETRIEVAL_MODEL_MAX_EMBEDDING_TEXTS)
        payload = _json_request({"texts": list(texts)}, maximum=_MAX_EMBED_REQUEST_BYTES)
        body = await self._request(
            method="POST",
            path="/v1/embed",
            payload=payload,
            deadline_seconds=RETRIEVAL_MODEL_INFERENCE_DEADLINE_SECONDS,
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
            raise RetrievalModelError() from None

    async def rerank_scores(
        self,
        *,
        request: RerankRequest,
        identity: RetrievalModelIdentity,
    ) -> tuple[float, ...]:
        if type(request) is not RerankRequest:
            raise RetrievalModelError()
        documents = tuple(document.text for document in request.documents)
        _validate_texts(documents, maximum=RETRIEVAL_MODEL_MAX_RERANK_DOCUMENTS)
        payload = _json_request(
            {"documents": list(documents), "query": request.query},
            maximum=_MAX_RERANK_REQUEST_BYTES,
        )
        body = await self._request(
            method="POST",
            path="/v1/rerank",
            payload=payload,
            deadline_seconds=RETRIEVAL_MODEL_INFERENCE_DEADLINE_SECONDS,
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
            raise RetrievalModelError() from None

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
            raise RetrievalModelError()
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
                        f"{RETRIEVAL_MODEL_ENDPOINT}{path}",
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
                        raise RetrievalModelError()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(chunk) > response_limit - len(body):
                            raise RetrievalModelError()
                        body.extend(chunk)
                    return bytes(body)
        except RetrievalModelError:
            raise
        except (TimeoutError, httpx.HTTPError):
            raise RetrievalModelError() from None
        except Exception:
            raise RetrievalModelError() from None


@dataclass(frozen=True, slots=True)
class RetrievalEmbedding(EmbeddingPort):
    """The RetrievalModel implementation of the existing narrow EmbeddingPort."""

    client: RetrievalModelClient
    identity: RetrievalModelIdentity
    m6_recorder: M6OperationRecorder | None = None

    def __post_init__(self) -> None:
        if (
            type(self.client) is not RetrievalModelClient
            or type(self.identity) is not RetrievalModelIdentity
            or (self.m6_recorder is not None and type(self.m6_recorder) is not M6OperationRecorder)
        ):
            raise TypeError("RetrievalModel embedding inputs are invalid")

    async def embed(self, request: EmbeddingBatch) -> EmbeddingResult:
        if type(request) is not EmbeddingBatch:
            raise ToolPortError(ToolFailureCode.RETRIEVAL_MODEL_QUERY_EMBEDDING_FAILED)
        lease: M6OperationLease | None = None
        try:
            lease = await _m6_acquire(
                recorder=self.m6_recorder,
                operation=M6Operation.BGE_EMBEDDING,
            )
        except M6CircuitOpen:
            raise ToolPortError(ToolFailureCode.CIRCUIT_OPEN) from None
        try:
            vectors = await self.client.embed_texts(
                texts=tuple(_tag_query_language(text) for text in request.texts),
                identity=self.identity,
            )
            result = EmbeddingResult(vectors=vectors)
        except RetrievalModelError:
            await _m6_failure(
                recorder=self.m6_recorder,
                lease=lease,
                safe_code="RETRIEVAL_MODEL_UNAVAILABLE",
                version=self.identity.embedding_model,
            )
            raise ToolPortError(ToolFailureCode.RETRIEVAL_MODEL_QUERY_EMBEDDING_FAILED) from None
        await _m6_success(
            recorder=self.m6_recorder,
            lease=lease,
            version=self.identity.embedding_model,
        )
        return result


def _tag_query_language(text: str) -> str:
    """Match the deployed current-product index query-vector contract exactly."""

    if any("\u3040" <= char <= "\u30ff" or "\uff66" <= char <= "\uff9f" for char in text):
        language = "ja"
    else:
        lowered = text.casefold()
        words = set(re.findall(r"[a-záéíóúüñ]+", lowered))
        spanish_markers = {
            "de",
            "del",
            "para",
            "con",
            "sin",
            "mujer",
            "hombre",
            "niño",
            "niña",
            "coche",
            "zapatos",
            "vestido",
            "camiseta",
        }
        if (
            any(marker in lowered for marker in ("á", "é", "í", "ó", "ú", "ü", "ñ", "¿", "¡"))
            or words & spanish_markers
        ):
            language = "es"
        elif re.search(r"[a-z]", lowered):
            language = "en"
        else:
            language = "und"
    return f"[query_language={language}] {text}"


@dataclass(frozen=True, slots=True)
class RetrievalReranker:
    """Translate bounded RetrievalModel cross-encoder scores into the existing identity result."""

    client: RetrievalModelClient
    identity: RetrievalModelIdentity
    m6_recorder: M6OperationRecorder | None = None

    def __post_init__(self) -> None:
        if (
            type(self.client) is not RetrievalModelClient
            or type(self.identity) is not RetrievalModelIdentity
            or (self.m6_recorder is not None and type(self.m6_recorder) is not M6OperationRecorder)
        ):
            raise TypeError("RetrievalModel reranker inputs are invalid")

    async def rerank(self, request: RerankRequest) -> RerankResult:
        lease = await _m6_acquire(
            recorder=self.m6_recorder,
            operation=M6Operation.BGE_RERANK,
        )
        try:
            scores = await self.client.rerank_scores(request=request, identity=self.identity)
        except RetrievalModelError:
            await _m6_failure(
                recorder=self.m6_recorder,
                lease=lease,
                safe_code="RETRIEVAL_MODEL_UNAVAILABLE",
                version=self.identity.reranker_model,
            )
            raise RetrievalModelError() from None
        ranked = sorted(
            zip(scores, request.documents, strict=True),
            key=lambda item: (-item[0], item[1].identity),
        )
        try:
            result = RerankResult(
                identities=tuple(document.identity for _score_value, document in ranked),
                model=RETRIEVAL_MODEL_RERANKER_MODEL,
            )
        except ValueError:
            await _m6_failure(
                recorder=self.m6_recorder,
                lease=lease,
                safe_code="RETRIEVAL_MODEL_UNAVAILABLE",
                version=self.identity.reranker_model,
            )
            raise RetrievalModelError() from None
        await _m6_success(
            recorder=self.m6_recorder,
            lease=lease,
            version=self.identity.reranker_model,
        )
        return result


async def _m6_acquire(
    *, recorder: M6OperationRecorder | None, operation: M6Operation
) -> M6OperationLease | None:
    """Only an OPEN M6 circuit blocks an existing local model call."""

    if recorder is None:
        return None
    try:
        return await recorder.acquire(operation=operation)
    except M6CircuitOpen:
        raise
    except Exception:
        return None


async def _m6_success(
    *,
    recorder: M6OperationRecorder | None,
    lease: M6OperationLease | None,
    version: str,
) -> None:
    if recorder is None or lease is None:
        return
    try:
        await recorder.success(lease=lease, version=version)
    except Exception:
        return


async def _m6_failure(
    *,
    recorder: M6OperationRecorder | None,
    lease: M6OperationLease | None,
    safe_code: str,
    version: str,
) -> None:
    if recorder is None or lease is None:
        return
    try:
        await recorder.failure(
            lease=lease,
            safe_code=safe_code,
            retryable_failure=True,
            version=version,
        )
    except Exception:
        return


def _identity_from_health(root: dict[str, object]) -> RetrievalModelIdentity:
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
    return RetrievalModelIdentity(
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
    payload = compact_bytes(value)
    if len(payload) > maximum:
        raise RetrievalModelError()
    return payload


def _json_object(body: bytes) -> dict[str, object]:
    root = loads_unique(body)
    if type(root) is not dict:
        raise ValueError("JSON root is invalid")
    return cast("dict[str, object]", root)


def _validate_texts(texts: tuple[str, ...], *, maximum: int) -> None:
    if (
        type(texts) is not tuple
        or not texts
        or len(texts) > maximum
        or any(
            type(text) is not str
            or not text.strip()
            or len(text) > RETRIEVAL_MODEL_MAX_TEXT_CHARACTERS
            or "\0" in text
            for text in texts
        )
    ):
        raise RetrievalModelError()


def _vector(value: object) -> tuple[float, ...]:
    if type(value) is not list or len(value) != RETRIEVAL_MODEL_DIMENSION:
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
    "RETRIEVAL_MODEL_DIMENSION",
    "RETRIEVAL_MODEL_EMBEDDING_MODEL",
    "RETRIEVAL_MODEL_ENDPOINT",
    "RETRIEVAL_MODEL_RERANKER_MODEL",
    "RETRIEVAL_MODEL_SERVICE_SCHEMA",
    "RetrievalEmbedding",
    "RetrievalModelClient",
    "RetrievalModelError",
    "RetrievalModelIdentity",
    "RetrievalReranker",
    "validate_retrieval_model_endpoint",
]
