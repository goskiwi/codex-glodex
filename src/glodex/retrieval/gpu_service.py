"""Private fixed-model GPU service for the semantic retrieval path."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Protocol, cast

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from glodex.retrieval.contracts import (
    RETRIEVAL_MODEL_DIMENSION,
    RETRIEVAL_MODEL_EMBEDDING_MODEL,
    RETRIEVAL_MODEL_MAX_EMBEDDING_TEXTS,
    RETRIEVAL_MODEL_MAX_QUERY_CHARACTERS,
    RETRIEVAL_MODEL_MAX_RERANK_DOCUMENTS,
    RETRIEVAL_MODEL_MAX_TEXT_CHARACTERS,
    RETRIEVAL_MODEL_RERANKER_MODEL,
    RetrievalModelIdentity,
)

_PRIVATE_MANIFEST_SCHEMA = "glodex.retrieval-model-private-manifest.v1"
_MAX_REQUEST_BYTES = 32 * 1024
_MAX_RESPONSE_BYTES = 128 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_WEIGHTS_FILENAME = "model.safetensors"
_READINESS_PROBE = "glodex retrieval model readiness"


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _EmbedRequest(_StrictRequest):
    texts: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=RETRIEVAL_MODEL_MAX_TEXT_CHARACTERS)]],
        Field(min_length=1, max_length=RETRIEVAL_MODEL_MAX_EMBEDDING_TEXTS),
    ]

    @model_validator(mode="after")
    def texts_are_safe(self) -> _EmbedRequest:
        if len(self.texts) != len(set(self.texts)) or any("\0" in text for text in self.texts):
            raise ValueError("embed texts are invalid")
        return self


class _RerankRequest(_StrictRequest):
    query: Annotated[str, Field(min_length=1, max_length=RETRIEVAL_MODEL_MAX_QUERY_CHARACTERS)]
    documents: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=RETRIEVAL_MODEL_MAX_TEXT_CHARACTERS)]],
        Field(min_length=1, max_length=RETRIEVAL_MODEL_MAX_RERANK_DOCUMENTS),
    ]

    @model_validator(mode="after")
    def text_is_safe(self) -> _RerankRequest:
        if "\0" in self.query or any("\0" in document for document in self.documents):
            raise ValueError("rerank input is invalid")
        return self


class _RetrievalModelRequestBodyLimitMiddleware:
    """Minimal private ASGI body bound that does not import the application API."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                await self._app(scope, receive, send)
                return
            chunk = message.get("body", b"")
            if type(chunk) is not bytes or len(chunk) > self._max_bytes - len(body):
                await JSONResponse(
                    status_code=413,
                    content={"error": {"code": "REQUEST_TOO_LARGE"}},
                )(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        replayed = False

        async def replay_receive() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self._app(scope, replay_receive, send)


@dataclass(frozen=True, slots=True)
class PrivateModelManifest:
    """GPU-private model material; paths and full hashes never leave this process."""

    embedding_model_path: Path
    reranker_model_path: Path
    embedding_weights_sha256: str
    reranker_weights_sha256: str
    service_build_digest: str
    max_sequence_length: int

    @classmethod
    def load(cls, path: Path) -> PrivateModelManifest:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("private model manifest is unavailable") from error
        if type(payload) is not dict:
            raise ValueError("private model manifest is invalid")
        source = cast("dict[str, object]", payload)
        expected = {
            "embeddingModel",
            "embeddingModelPath",
            "embeddingWeightsSha256",
            "maxSequenceLength",
            "rerankerModel",
            "rerankerModelPath",
            "rerankerWeightsSha256",
            "schemaVersion",
            "serviceBuildDigest",
        }
        if set(source) != expected:
            raise ValueError("private model manifest keys are invalid")
        if (
            source["schemaVersion"] != _PRIVATE_MANIFEST_SCHEMA
            or source["embeddingModel"] != RETRIEVAL_MODEL_EMBEDDING_MODEL
            or source["rerankerModel"] != RETRIEVAL_MODEL_RERANKER_MODEL
        ):
            raise ValueError("private model manifest identity is invalid")
        embedding_path = _private_path(source["embeddingModelPath"])
        reranker_path = _private_path(source["rerankerModelPath"])
        embedding_hash = _private_hash(source["embeddingWeightsSha256"])
        reranker_hash = _private_hash(source["rerankerWeightsSha256"])
        service_build_digest = _private_hash(source["serviceBuildDigest"])
        max_sequence_length = source["maxSequenceLength"]
        if (
            type(max_sequence_length) is not int
            or isinstance(max_sequence_length, bool)
            or not 16 <= max_sequence_length <= 2_000
        ):
            raise ValueError("private model manifest sequence length is invalid")
        return cls(
            embedding_model_path=embedding_path,
            reranker_model_path=reranker_path,
            embedding_weights_sha256=embedding_hash,
            reranker_weights_sha256=reranker_hash,
            service_build_digest=service_build_digest,
            max_sequence_length=max_sequence_length,
        )

    @property
    def public_digest(self) -> str:
        material = {
            "embeddingModel": RETRIEVAL_MODEL_EMBEDDING_MODEL,
            "embeddingWeightsSha256": self.embedding_weights_sha256,
            "maxSequenceLength": self.max_sequence_length,
            "rerankerModel": RETRIEVAL_MODEL_RERANKER_MODEL,
            "rerankerWeightsSha256": self.reranker_weights_sha256,
            "schemaVersion": _PRIVATE_MANIFEST_SCHEMA,
            "serviceBuildDigest": self.service_build_digest,
        }
        payload = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def verify_runtime_material(self, *, service_source_path: Path | None = None) -> None:
        """Reject any model or service source that differs from this private manifest."""

        source_path = service_source_path if service_source_path is not None else Path(__file__)
        expected = (
            (self.embedding_model_path / _WEIGHTS_FILENAME, self.embedding_weights_sha256),
            (self.reranker_model_path / _WEIGHTS_FILENAME, self.reranker_weights_sha256),
            (source_path, self.service_build_digest),
        )
        if any(_file_sha256(path) != digest for path, digest in expected):
            raise ValueError("private model material does not match its manifest")


class GpuRuntime(Protocol):
    """The small runtime seam that lets default tests avoid CUDA imports."""

    @property
    def identity(self) -> RetrievalModelIdentity: ...

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]: ...

    async def rerank(self, query: str, documents: tuple[str, ...]) -> tuple[float, ...]: ...


class BgeGpuRuntime:
    """The only CUDA owner; imported and constructed only by the GPU deployment entrypoint."""

    def __init__(self, manifest: PrivateModelManifest) -> None:
        manifest.verify_runtime_material()
        try:
            import torch  # type: ignore[import-not-found]
            from sentence_transformers import (  # type: ignore[import-not-found]
                CrossEncoder,
                SentenceTransformer,
            )
        except Exception as error:
            raise RuntimeError("GPU model dependencies are unavailable") from error
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA is unavailable")
        device_name = str(torch.cuda.get_device_name(0))
        if "A100" not in device_name.upper():
            raise RuntimeError("unsupported GPU class")
        self._embedding = SentenceTransformer(str(manifest.embedding_model_path), device="cuda")
        self._embedding.max_seq_length = manifest.max_sequence_length
        dimension = self._embedding.get_sentence_embedding_dimension()
        if dimension != RETRIEVAL_MODEL_DIMENSION:
            raise RuntimeError("embedding dimension is invalid")
        self._reranker = CrossEncoder(
            str(manifest.reranker_model_path),
            device="cuda",
            max_length=manifest.max_sequence_length,
        )
        self._verify_model_readiness()
        self._lock = asyncio.Lock()
        self._identity = RetrievalModelIdentity(
            manifest_digest=manifest.public_digest,
            embedding_model=RETRIEVAL_MODEL_EMBEDDING_MODEL,
            reranker_model=RETRIEVAL_MODEL_RERANKER_MODEL,
            dimension=RETRIEVAL_MODEL_DIMENSION,
            max_embedding_texts=RETRIEVAL_MODEL_MAX_EMBEDDING_TEXTS,
            max_text_characters=RETRIEVAL_MODEL_MAX_TEXT_CHARACTERS,
            max_rerank_documents=RETRIEVAL_MODEL_MAX_RERANK_DOCUMENTS,
            max_query_characters=RETRIEVAL_MODEL_MAX_QUERY_CHARACTERS,
            device_class="cuda",
            gpu_model_class="a100",
        )

    def _verify_model_readiness(self) -> None:
        vectors = self._embedding.encode(
            [_READINESS_PROBE],
            batch_size=1,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        vector = tuple(float(value) for value in vectors.tolist()[0])
        if not _normalized(vector):
            raise RuntimeError("embedding readiness check failed")
        raw_scores = self._reranker.predict(
            [[_READINESS_PROBE, _READINESS_PROBE]],
            batch_size=1,
            show_progress_bar=False,
        )
        values = raw_scores.tolist() if hasattr(raw_scores, "tolist") else list(raw_scores)
        if type(values) is not list or len(values) != 1 or not math.isfinite(float(values[0])):
            raise RuntimeError("reranker readiness check failed")

    @property
    def identity(self) -> RetrievalModelIdentity:
        return self._identity

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        async with self._lock:
            values = await asyncio.to_thread(
                self._embedding.encode,
                list(texts),
                batch_size=len(texts),
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
        vectors = tuple(
            tuple(float(format(float(value), ".8g")) for value in vector)
            for vector in values.tolist()
        )
        if len(vectors) != len(texts) or any(not _normalized(vector) for vector in vectors):
            raise RuntimeError("embedding response is invalid")
        return vectors

    async def rerank(self, query: str, documents: tuple[str, ...]) -> tuple[float, ...]:
        pairs = [[query, document] for document in documents]
        async with self._lock:
            values = await asyncio.to_thread(
                self._reranker.predict,
                pairs,
                batch_size=len(documents),
                show_progress_bar=False,
            )
        raw = values.tolist() if hasattr(values, "tolist") else list(values)
        scores = tuple(float(value) for value in raw)
        if len(scores) != len(documents) or any(not math.isfinite(value) for value in scores):
            raise RuntimeError("reranker response is invalid")
        return scores


def create_private_gpu_app(runtime: GpuRuntime) -> FastAPI:
    """Create the private fixed service without reading a manifest or importing CUDA."""

    if not isinstance(runtime.identity, RetrievalModelIdentity):
        raise TypeError("GPU runtime identity is invalid")
    app = FastAPI(
        title="Glodex Retrieval Model Service",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(_RetrievalModelRequestBodyLimitMiddleware, max_bytes=_MAX_REQUEST_BYTES)

    @app.get("/v1/health")
    async def health() -> dict[str, object]:
        return _health_payload(runtime.identity)

    @app.post("/v1/embed", response_model=None)
    async def embed(request: _EmbedRequest) -> dict[str, object] | JSONResponse:
        try:
            vectors = await runtime.embed(tuple(request.texts))
            _validate_vectors(vectors=vectors, expected=len(request.texts))
        except Exception:
            return _service_error()
        payload: dict[str, object] = {
            "embeddings": [list(vector) for vector in vectors],
            "manifestDigest": runtime.identity.manifest_digest,
        }
        return payload if _response_is_bounded(payload) else _service_error()

    @app.post("/v1/rerank", response_model=None)
    async def rerank(request: _RerankRequest) -> dict[str, object] | JSONResponse:
        try:
            scores = await runtime.rerank(request.query, tuple(request.documents))
            if len(scores) != len(request.documents) or any(
                not math.isfinite(score) for score in scores
            ):
                raise ValueError("scores are invalid")
        except Exception:
            return _service_error()
        payload: dict[str, object] = {
            "manifestDigest": runtime.identity.manifest_digest,
            "scores": list(scores),
        }
        return payload if _response_is_bounded(payload) else _service_error()

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(
        _request: object,
        _error: Exception,
    ) -> JSONResponse:
        return JSONResponse(status_code=422, content={"error": {"code": "REQUEST_REJECTED"}})

    return app


def serve_private_gpu_service(*, manifest_path: Path) -> int:
    """Load one private manifest and serve only the fixed GPU loopback surface."""

    try:
        runtime = BgeGpuRuntime(PrivateModelManifest.load(manifest_path))
    except Exception:
        return 1
    try:
        import uvicorn

        uvicorn.run(
            create_private_gpu_app(runtime),
            host="127.0.0.1",
            port=18000,
            access_log=False,
            log_config=None,
        )
        return 0
    except Exception:
        return 1


def _health_payload(identity: RetrievalModelIdentity) -> dict[str, object]:
    return {
        "deviceClass": identity.device_class,
        "dimension": identity.dimension,
        "embeddingModel": identity.embedding_model,
        "gpuModelClass": identity.gpu_model_class,
        "manifestDigest": identity.manifest_digest,
        "maxEmbeddingTexts": identity.max_embedding_texts,
        "maxQueryCharacters": identity.max_query_characters,
        "maxRerankDocuments": identity.max_rerank_documents,
        "maxTextCharacters": identity.max_text_characters,
        "rerankerModel": identity.reranker_model,
        "schemaVersion": identity.schema_version,
    }


def _service_error() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"error": {"code": "RETRIEVAL_MODEL_SERVICE_UNAVAILABLE"}},
    )


def _response_is_bounded(payload: dict[str, object]) -> bool:
    return (
        len(
            json.dumps(
                payload,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        <= _MAX_RESPONSE_BYTES
    )


def _validate_vectors(*, vectors: tuple[tuple[float, ...], ...], expected: int) -> None:
    if len(vectors) != expected or any(not _normalized(vector) for vector in vectors):
        raise ValueError("vectors are invalid")


def _normalized(vector: tuple[float, ...]) -> bool:
    return (
        len(vector) == RETRIEVAL_MODEL_DIMENSION
        and all(type(value) is float and math.isfinite(value) for value in vector)
        and math.isclose(math.sqrt(sum(value * value for value in vector)), 1.0, abs_tol=1e-6)
    )


def _private_path(value: object) -> Path:
    if type(value) is not str or not value or len(value) > 4_096 or "\0" in value:
        raise ValueError("private model path is invalid")
    return Path(value)


def _private_hash(value: object) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError("private model hash is invalid")
    return value


def _file_sha256(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        raise ValueError("private model material is unavailable")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while block := source.read(1024 * 1024):
                digest.update(block)
    except OSError as error:
        raise ValueError("private model material is unavailable") from error
    return digest.hexdigest()


__all__ = [
    "BgeGpuRuntime",
    "GpuRuntime",
    "PrivateModelManifest",
    "create_private_gpu_app",
    "serve_private_gpu_service",
]
