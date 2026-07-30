"""Black-box delivery evidence for M2c's explicit-only public surface."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

import glodex.esci_benchmark as esci_benchmark
from glodex.adapters.m2c_model_service import M2cModelServiceClient, M2cReranker
from glodex.cli import main
from glodex.m2c_contract import (
    M2C_EMBEDDING_MODEL,
    M2C_RERANKER_MODEL,
    M2cModelIdentity,
)
from glodex.m2c_esci_eval import comparison_summary, evaluate_m2c_rerank

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.spec(
        "M2C-AC-001",
        "M2C-AC-002",
        "M2C-AC-003",
        "M2C-AC-004",
        "M2C-AC-005",
        "M2C-AC-006",
        "GLO-M2C-P0-007",
        "GLO-M2C-NFR-001",
        "GLO-M2C-NFR-005",
        "GLO-M2C-NFR-006",
    ),
]

_ROOT = Path(__file__).parents[3]


def _identity() -> M2cModelIdentity:
    return M2cModelIdentity(
        manifest_digest="a" * 64,
        embedding_model=M2C_EMBEDDING_MODEL,
        reranker_model=M2C_RERANKER_MODEL,
        dimension=1024,
        max_embedding_texts=8,
        max_text_characters=2000,
        max_rerank_documents=40,
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


def test_m2c_commands_require_explicit_live_before_any_tunnel_or_gpu_access(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(("m2c-model-verify",)) == 2
    assert json.loads(capsys.readouterr().out) == {
        "code": "M2C_LIVE_REQUIRED",
        "status": "FAILED",
    }
    assert main(("m2c-eval-esci",)) == 2
    assert json.loads(capsys.readouterr().out) == {
        "code": "M2C_LIVE_REQUIRED",
        "status": "FAILED",
    }


def test_m2c_esci_comparison_finishes_all_reranks_before_reading_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path == "/v1/health":
            return httpx.Response(200, json=_health_payload())
        assert request.url.path == "/v1/rerank"
        payload = json.loads(request.content)
        documents = payload["documents"]
        assert isinstance(documents, list)
        calls += 1
        return httpx.Response(
            200,
            json={"manifestDigest": "a" * 64, "scores": list(range(len(documents), 0, -1))},
        )

    original_loader = esci_benchmark._load_judgement_records

    def labels_after_rerank(context: object) -> object:
        assert calls == 500
        return original_loader(context)  # type: ignore[arg-type]

    monkeypatch.setattr(esci_benchmark, "_load_judgement_records", labels_after_rerank)

    async def scenario() -> dict[str, object]:
        client = M2cModelServiceClient(http_transport=httpx.MockTransport(handler))
        reranker = M2cReranker(client=client, identity=await client.health())
        result = await evaluate_m2c_rerank(
            artifact_root=_ROOT / "data" / "benchmarks" / "esci-small-us-v1",
            reranker=reranker,
        )
        return comparison_summary(result)

    summary = asyncio.run(scenario())
    assert calls == 500
    assert set(summary) == {
        "benchmark_id",
        "coarse_metrics",
        "counts",
        "model",
        "model_manifest",
        "reranked_metrics",
        "status",
    }
    assert summary["model_manifest"] == "a" * 16
    assert "query" not in summary
    assert "labels" not in summary
