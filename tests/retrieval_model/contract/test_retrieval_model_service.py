"""Contract evidence for the retrieval model service and its fixed loopback client."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from glodex.agent.contracts import EmbeddingBatch, ToolFailureCode
from glodex.agent.ports import ToolPortError
from glodex.application.rerank_contracts import RerankDocument, RerankRequest
from glodex.cli import CliUsageError, _build_parser
from glodex.retrieval.gpu_service import PrivateModelManifest, create_private_gpu_app
from glodex.retrieval.model_service import (
    RETRIEVAL_MODEL_EMBEDDING_MODEL,
    RETRIEVAL_MODEL_ENDPOINT,
    RETRIEVAL_MODEL_RERANKER_MODEL,
    RetrievalEmbedding,
    RetrievalModelClient,
    RetrievalModelError,
    RetrievalModelIdentity,
    RetrievalReranker,
    validate_retrieval_model_endpoint,
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.spec(
        "GLO-RETRIEVAL_MODEL-P0-001",
        "GLO-RETRIEVAL_MODEL-P0-002",
        "GLO-RETRIEVAL_MODEL-NFR-001",
        "GLO-RETRIEVAL_MODEL-NFR-002",
        "GLO-RETRIEVAL_MODEL-NFR-004",
        "GLO-RETRIEVAL_MODEL-NFR-005",
    ),
]


def _identity() -> RetrievalModelIdentity:
    return RetrievalModelIdentity(
        manifest_digest="a" * 64,
        embedding_model=RETRIEVAL_MODEL_EMBEDDING_MODEL,
        reranker_model=RETRIEVAL_MODEL_RERANKER_MODEL,
        dimension=1024,
        max_embedding_texts=8,
        max_text_characters=2000,
        max_rerank_documents=50,
        max_query_characters=512,
        device_class="cuda",
        gpu_model_class="a100",
    )


def _health_payload() -> dict[str, object]:
    identity = _identity()
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


def _vector() -> list[float]:
    return [1.0, *([0.0] * 1023)]


def _dense_vector() -> list[float]:
    return [0.03125] * 1024


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def test_private_manifest_binds_model_weights_and_service_source(tmp_path: Path) -> None:
    embedding = tmp_path / "embedding"
    reranker = tmp_path / "reranker"
    embedding.mkdir()
    reranker.mkdir()
    embedding_weight = b"embedding weights"
    reranker_weight = b"reranker weights"
    service_source = tmp_path / "retrieval_model_gpu_service.py"
    (embedding / "model.safetensors").write_bytes(embedding_weight)
    (reranker / "model.safetensors").write_bytes(reranker_weight)
    service_source.write_bytes(b"service source")
    manifest_path = tmp_path / "retrieval_model-model-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "embeddingModel": RETRIEVAL_MODEL_EMBEDDING_MODEL,
                "embeddingModelPath": str(embedding),
                "embeddingWeightsSha256": _sha256(embedding_weight),
                "maxSequenceLength": 256,
                "rerankerModel": RETRIEVAL_MODEL_RERANKER_MODEL,
                "rerankerModelPath": str(reranker),
                "rerankerWeightsSha256": _sha256(reranker_weight),
                "schemaVersion": "glodex.retrieval-model-private-manifest.v1",
                "serviceBuildDigest": _sha256(b"service source"),
            }
        ),
        encoding="utf-8",
    )

    manifest = PrivateModelManifest.load(manifest_path)
    manifest.verify_runtime_material(service_source_path=service_source)

    (embedding / "model.safetensors").write_bytes(b"replaced")
    with pytest.raises(ValueError):
        manifest.verify_runtime_material(service_source_path=service_source)


def test_client_uses_exact_fixed_loopback_contract_and_stable_rerank_order() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path == "/v1/health":
            return httpx.Response(200, json=_health_payload())
        if request.url.path == "/v1/embed":
            assert json.loads(request.content) == {"texts": ["[query_language=en] probe"]}
            return httpx.Response(
                200,
                json={"embeddings": [_vector()], "manifestDigest": "a" * 64},
            )
        assert request.url.path == "/v1/rerank"
        return httpx.Response(
            200,
            json={"manifestDigest": "a" * 64, "scores": [0.2, 0.9]},
        )

    async def scenario() -> None:
        client = RetrievalModelClient(http_transport=httpx.MockTransport(handler))
        identity = await client.health()
        embedding = await RetrievalEmbedding(client, identity).embed(
            EmbeddingBatch(texts=("probe",))
        )
        reranked = await RetrievalReranker(client, identity).rerank(
            RerankRequest(
                query="phone",
                documents=(
                    RerankDocument(identity="a", text="trusted a"),
                    RerankDocument(identity="b", text="trusted b"),
                ),
            )
        )
        assert embedding.vectors == (tuple(_vector()),)
        assert reranked.identities == ("b", "a")
        assert reranked.model == RETRIEVAL_MODEL_RERANKER_MODEL

    asyncio.run(scenario())
    assert [str(request.url).removesuffix(request.url.path) for request in captured] == [
        RETRIEVAL_MODEL_ENDPOINT,
        RETRIEVAL_MODEL_ENDPOINT,
        RETRIEVAL_MODEL_ENDPOINT,
    ]
    assert all(request.headers["accept-encoding"] == "identity" for request in captured)
    assert all("authorization" not in request.headers for request in captured)


def test_client_rejects_any_non_fixed_target_and_malformed_model_identity() -> None:
    assert validate_retrieval_model_endpoint(RETRIEVAL_MODEL_ENDPOINT) == RETRIEVAL_MODEL_ENDPOINT
    for target in (
        "http://localhost:18000",
        "https://127.0.0.1:18000",
        "http://127.0.0.1:8000",
        "http://127.0.0.1:18000/path",
        "http://127.0.0.1:18000?target=remote",
    ):
        with pytest.raises(ValueError):
            validate_retrieval_model_endpoint(target)

    def handler(_request: httpx.Request) -> httpx.Response:
        payload = _health_payload()
        payload["gpuModelClass"] = "unknown"
        return httpx.Response(200, json=payload)

    with pytest.raises(RetrievalModelError):
        asyncio.run(RetrievalModelClient(http_transport=httpx.MockTransport(handler)).health())


def test_embedding_maps_model_failure_to_retrieval_model_safe_tool_code() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"code": "hidden"}})

    with pytest.raises(ToolPortError) as raised:
        asyncio.run(
            RetrievalEmbedding(
                RetrievalModelClient(http_transport=httpx.MockTransport(handler)),
                _identity(),
            ).embed(EmbeddingBatch(texts=("probe",)))
        )
    assert raised.value.code is ToolFailureCode.RETRIEVAL_MODEL_QUERY_EMBEDDING_FAILED


def test_private_service_has_no_docs_or_input_echo_and_enforces_bounds() -> None:
    class FakeRuntime:
        identity = _identity()

        async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
            return tuple(tuple(_dense_vector()) for _ in texts)

        async def rerank(self, query: str, documents: tuple[str, ...]) -> tuple[float, ...]:
            del query
            return tuple(float(index) for index, _ in enumerate(documents))

    app = create_private_gpu_app(FakeRuntime())
    with TestClient(app) as client:
        health = client.get("/v1/health")
        embedding = client.post(
            "/v1/embed",
            json={"texts": [f"private probe {index}" for index in range(8)]},
        )
        rerank = client.post(
            "/v1/rerank",
            json={"query": "private query", "documents": ["first", "second"]},
        )
        rejected = client.post("/v1/embed", json={"texts": ["x"], "extra": "secret"})
        assert client.get("/docs").status_code == 404
    assert health.json() == _health_payload()
    assert embedding.json()["manifestDigest"] == "a" * 64
    assert "private probe" not in embedding.text
    assert len(embedding.content) <= 128 * 1024
    assert rerank.json()["scores"] == [0.0, 1.0]
    assert "private query" not in rerank.text
    assert rejected.status_code == 422
    assert rejected.json() == {"error": {"code": "REQUEST_REJECTED"}}


def test_retrieval_model_operator_commands_expose_only_the_fixed_contract() -> None:
    parser = _build_parser()

    verify = parser.parse_args(("model-service", "verify", "--live"))
    assert verify.command == "model-service"
    assert verify.model_service_action == "verify"
    assert verify.live is True

    service = parser.parse_args(
        ("model-service", "serve", "--manifest", "/private/models/manifest.json")
    )
    assert service.model_service_action == "serve"
    assert service.manifest == Path("/private/models/manifest.json")

    index = parser.parse_args(("product-index", "verify", "--live"))
    assert index.command == "product-index"
    assert index.action == "verify"

    with pytest.raises(CliUsageError):
        parser.parse_args(("removed-command",))

    with pytest.raises(CliUsageError):
        parser.parse_args(("retrieval_model-model-verify", "--live"))

    with pytest.raises(CliUsageError):
        parser.parse_args(
            ("retrieval-model-gpu-service", "--manifest", "/private/models/manifest.json")
        )
