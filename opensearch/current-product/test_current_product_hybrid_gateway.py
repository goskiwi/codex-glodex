"""Offline contract tests for the default-off current-product Hybrid gateway."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import math
import sqlite3
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

pytestmark = [
    pytest.mark.contract,
    pytest.mark.spec(
        "GLO-RETRIEVAL_MODEL-P0-003",
        "GLO-RETRIEVAL_MODEL-P0-005",
        "GLO-RETRIEVAL_MODEL-P0-006",
        "GLO-RETRIEVAL_MODEL-NFR-003",
    ),
]

MODULE_PATH = Path(__file__).with_name("current_product_hybrid_gateway.py")
SPEC = importlib.util.spec_from_file_location("current_product_hybrid_gateway", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
gateway = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gateway
SPEC.loader.exec_module(gateway)


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def _fixture_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    catalog = tmp_path / "current-product-catalog.sqlite3"
    connection = sqlite3.connect(catalog)
    try:
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute(
            "CREATE TABLE documents (document_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO documents(document_id, payload_json) VALUES (?, ?)",
            (
                "doc-current-1",
                json.dumps(
                    {
                        "document_id": "doc-current-1",
                        "brand": "Apple",
                        "color": None,
                        "attribute_names": ["brand", "screen_size"],
                        "search_text": (
                            "iPad Air 11-inch\n"
                            "brand: Apple\n"
                            "screen_size: 11 in\n"
                            "Title: iPad Air 11-inch"
                        ),
                    }
                ),
            ),
        )
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            (
                ("source_binding_sha256", "a" * 64),
                ("expected_document_count", "926710"),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    vector_manifest = tmp_path / "current-item-vectors-manifest.json"
    _write_json(
        vector_manifest,
        {
            "schema_version": "current_catalog_item_vectors_v1",
            "catalog": {
                "path": str(catalog),
                "document_count": 926710,
                "source_binding_sha256": "a" * 64,
                "document_id_key": "current canonical document_id",
            },
            "model": {
                "source": "current_catalog_finetuned",
                "vector_dimension": 1024,
                "weights_sha256": "4" * 64,
            },
            "item_text": {"template_version": "current-item-text-v1"},
            "outputs": {
                "item_vectors": {
                    "file": "item-vectors.npy",
                    "sha256": "5" * 64,
                    "bytes": 1,
                    "dtype": "float16",
                    "shape": [926710, 1024],
                    "normalized": True,
                },
                "item_ids": {
                    "file": "item-ids.jsonl.gz",
                    "sha256": "6" * 64,
                    "bytes": 1,
                    "row_count": 926710,
                },
            },
            "legacy_qrels_used": False,
        },
    )
    binding = tmp_path / "current_product_binding.json"
    _write_json(
        binding,
        {
            "schema_version": gateway.BINDING_SCHEMA_VERSION,
            "purpose": "offline current-product Hybrid gateway fixture",
            "current_product_contract": {
                "canonical_join_key": "document_id",
                "expected_document_count": 926710,
                "identity_policy": "canonical_document_id_only",
            },
            "current_catalog": {
                "sqlite_path": str(catalog),
                "expected_document_count": 926710,
                "source_binding_sha256": "a" * 64,
            },
            "current_item_vectors": {
                "manifest_path": str(vector_manifest),
                "manifest_sha256": hashlib.sha256(vector_manifest.read_bytes()).hexdigest(),
                "expected_schema_version": "current_catalog_item_vectors_v1",
            },
            "opensearch": {
                "endpoint": "http://127.0.0.1:9200",
                "index_alias": "glodex-current-product-hybrid",
                "physical_index": "glodex-current-product-hybrid-19ada6773d8d",
                "pipeline_id": "glodex-current-product-hybrid-v1",
                "vector_field": "item_vector",
                "embedding_dimension": 1024,
                "primary_shards": 1,
                "vector_engine": "lucene_hnsw_cosinesimil",
                "hybrid_weights": {"semantic": 0.7, "lexical": 0.3},
            },
            "schema_policy": {},
        },
    )
    manifest = tmp_path / "retrieval_model-manifest.json"
    _write_json(
        manifest,
        {
            "embeddingModel": "glodex-bge-m3-v1",
            "embeddingModelPath": "/data3/sybai/glodex/current-model/embedding/final",
            "embeddingWeightsSha256": "1" * 64,
            "maxSequenceLength": 256,
            "rerankerModel": "glodex-bge-reranker-v1",
            "rerankerModelPath": "/data3/sybai/glodex/current-model/reranker/final",
            "rerankerWeightsSha256": "2" * 64,
            "schemaVersion": gateway.RETRIEVAL_MODEL_MANIFEST_SCHEMA_VERSION,
            "serviceBuildDigest": "3" * 64,
        },
    )
    return binding, catalog, manifest


class _FakeLoopback:
    def __init__(self, *, binding: Any, identity: Any) -> None:
        self.binding = binding
        self.identity = identity
        self.calls: list[dict[str, object]] = []
        self.alias_target: object = binding.physical_index
        self.mapping_meta: dict[str, object] = {
            "schema_version": gateway.INDEX_SCHEMA_VERSION,
            "current_catalog_binding_sha256": self.binding.catalog_binding_sha256,
            "current_item_vectors_manifest_sha256": self.binding.item_vectors_manifest_sha256,
            "canonical_join_key": "document_id",
            "expected_document_count": self.binding.expected_document_count,
            "identity_policy": "canonical_document_id_only",
        }

    def __call__(
        self,
        *,
        base_url: str,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        self.calls.append(
            {"base_url": base_url, "method": method, "path": path, "payload": payload}
        )
        if base_url.endswith(":18000") and path == "/v1/health":
            return {
                "deviceClass": "cuda",
                "dimension": 1024,
                "embeddingModel": "glodex-bge-m3-v1",
                "gpuModelClass": "a100",
                "manifestDigest": self.identity.manifest_digest,
                "maxEmbeddingTexts": 8,
                "maxQueryCharacters": 512,
                "maxRerankDocuments": 50,
                "maxTextCharacters": 2000,
                "rerankerModel": "glodex-bge-reranker-v1",
                "schemaVersion": gateway.RETRIEVAL_MODEL_SERVICE_SCHEMA_VERSION,
            }
        if base_url.endswith(":18000") and path == "/v1/embed":
            return {
                "manifestDigest": self.identity.manifest_digest,
                "embeddings": [[1.0] + [0.0] * 1023],
            }
        if base_url.endswith(":18000") and path == "/v1/rerank":
            assert payload is not None
            documents = payload["documents"]
            assert isinstance(documents, list)
            return {
                "manifestDigest": self.identity.manifest_digest,
                "scores": [0.91 for _document in documents],
            }
        if path.startswith("/_cluster/health"):
            return {"status": "yellow"}
        if path.startswith("/_alias/"):
            if self.alias_target == "two":
                return {
                    self.binding.physical_index: {"aliases": {self.binding.alias: {}}},
                    self.binding.physical_index + "-other": {"aliases": {self.binding.alias: {}}},
                }
            return {str(self.alias_target): {"aliases": {self.binding.alias: {}}}}
        if path.endswith("/_mapping"):
            return {
                self.binding.physical_index: {
                    "mappings": {
                        "_meta": self.mapping_meta,
                        "properties": {
                            "document_id": {"type": "keyword"},
                            "search_text": {"type": "text"},
                            "item_vector": {"type": "knn_vector", "dimension": 1024},
                            "category_card_id": {"type": "keyword"},
                            "item_price": {"type": "scaled_float"},
                            "shipping": {"type": "scaled_float"},
                            "stock_status": {"type": "keyword"},
                            "attributes": {"type": "object", "enabled": False},
                        },
                    }
                }
            }
        if path.endswith("/_count"):
            return {"count": self.binding.expected_document_count}
        if path.endswith("/_settings"):
            return {
                self.binding.physical_index: {"settings": {"index": {"blocks": {"write": "true"}}}}
            }
        if path.startswith("/_search/pipeline/"):
            return {
                self.binding.pipeline: {
                    "phase_results_processors": [
                        {
                            "normalization-processor": {
                                "normalization": {"technique": "min_max"},
                                "combination": {
                                    "technique": "arithmetic_mean",
                                    "parameters": {"weights": [0.7, 0.3]},
                                },
                            }
                        }
                    ]
                }
            }
        if path.startswith("/glodex-current-product-hybrid/_search?"):
            return {
                "hits": {
                    "hits": [
                        {
                            "_index": self.binding.physical_index,
                            "_id": "doc-current-1",
                            "_score": 0.875,
                            "_source": {
                                "document_id": "doc-current-1",
                                "family_id": "family-current-1",
                                "source": "wands",
                                "title": "iPad Air 11-inch",
                                "category": "tablets",
                                "brand": "Apple",
                                "color": None,
                                "attributes": {
                                    "brand": "Apple",
                                    "screen_size": "11 in",
                                },
                                "attribute_names": ["brand", "screen_size"],
                                "search_text": (
                                    "iPad Air 11-inch\n"
                                    "brand: Apple\n"
                                    "screen_size: 11 in\n"
                                    "Title: iPad Air 11-inch"
                                ),
                                "has_category": True,
                                "source_locale_raw": "en-US",
                                "source_locale_semantics": "en",
                                "item_language": "en",
                                "target_market_locale": "en-US",
                                "category_card_id": "electronics.tablet",
                                "entity_kind": "PRIMARY_PRODUCT",
                                "platform": "amazon",
                                "provider_id": "current-product-amazon",
                                "market": "CN",
                                "offer_id": "offer-current-1",
                                "source_uri": "urn:glodex:current-product:doc-current-1",
                                "currency": "CNY",
                                "item_price": 4299.0,
                                "shipping": 0.0,
                                "tax": 0.0,
                                "duty": 0.0,
                                "stock_status": "IN_STOCK",
                                "delivery_days_min": 2,
                                "delivery_days_max": 10,
                                "commerce_ruleset_version": "current-commerce-v1",
                                "captured_at": "2026-08-01T00:00:00Z",
                            },
                        }
                    ]
                }
            }
        raise AssertionError("unexpected loopback request: " + base_url + path)


def _runtime(tmp_path: Path) -> tuple[Any, _FakeLoopback]:
    binding_path, _, manifest_path = _fixture_paths(tmp_path)
    binding = gateway.load_binding(binding_path)
    item_vectors = gateway.load_current_item_vectors_identity(binding)
    identity = gateway.load_retrieval_model_identity(manifest_path)
    fake = _FakeLoopback(binding=binding, identity=identity)
    retrieval_model = gateway.RetrievalModelClient("http://127.0.0.1:18000", identity, request=fake)
    opensearch = gateway.OpenSearchClient(binding, request=fake)
    retrieval_model.verify_health()
    opensearch.verify_startup()
    return (
        gateway.GatewayRuntime(
            binding=binding,
            item_vectors=item_vectors,
            retrieval_model=retrieval_model,
            opensearch=opensearch,
            catalog_attributes=gateway.CurrentCatalogAttributeStore(binding.catalog_path),
        ),
        fake,
    )


def test_startup_validation_accepts_one_current_alias_and_pipeline(tmp_path: Path) -> None:
    runtime, fake = _runtime(tmp_path)
    assert runtime.binding.alias == "glodex-current-product-hybrid"
    paths = [str(call["path"]) for call in fake.calls]
    assert "/_alias/glodex-current-product-hybrid" in paths
    assert "/glodex-current-product-hybrid-19ada6773d8d/_mapping" in paths
    assert "/_search/pipeline/glodex-current-product-hybrid-v1" in paths
    assert "/glodex-current-product-hybrid-19ada6773d8d/_settings" in paths


def test_startup_validation_rejects_multiple_alias_targets(tmp_path: Path) -> None:
    binding_path, _, manifest_path = _fixture_paths(tmp_path)
    binding = gateway.load_binding(binding_path)
    identity = gateway.load_retrieval_model_identity(manifest_path)
    fake = _FakeLoopback(binding=binding, identity=identity)
    fake.alias_target = "two"
    with pytest.raises(gateway.HybridGatewayError, match="exactly"):
        gateway.OpenSearchClient(binding, request=fake).verify_startup()


def test_startup_validation_rejects_retired_mapping_metadata(tmp_path: Path) -> None:
    binding_path, _, manifest_path = _fixture_paths(tmp_path)
    binding = gateway.load_binding(binding_path)
    identity = gateway.load_retrieval_model_identity(manifest_path)
    fake = _FakeLoopback(binding=binding, identity=identity)
    fake.mapping_meta = {
        "schema_version": gateway.INDEX_SCHEMA_VERSION,
        "source_item_search_binding_sha256": binding.catalog_binding_sha256,
        "canonical_join_key": "document_id",
        "expected_document_count": binding.expected_document_count,
        "legacy_v8_used": False,
    }
    with pytest.raises(gateway.HybridGatewayError, match="not bound"):
        gateway.OpenSearchClient(binding, request=fake).verify_startup()


def test_binding_rejects_retired_local_input_cache(tmp_path: Path) -> None:
    binding_path, _, _ = _fixture_paths(tmp_path)
    payload = json.loads(binding_path.read_text(encoding="utf-8"))
    payload["local_input_cache"] = {"root": "/retired"}
    _write_json(binding_path, payload)
    with pytest.raises(gateway.HybridGatewayError, match="retired field local_input_cache"):
        gateway.load_binding(binding_path)


def test_gateway_strict_request_and_hybrid_query_shape(tmp_path: Path) -> None:
    runtime, fake = _runtime(tmp_path)
    app = gateway.create_app(runtime)
    query_vector = [1.0] + [0.0] * 1023

    async def exercise() -> tuple[httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            valid = await client.post(
                "/v1/hybrid-search",
                json={
                    "query": " ipad air ",
                    "top_k": 3,
                    "platform": "amazon",
                    "min_landed_cost_cny": 4000.0,
                    "max_landed_cost_cny": 5000.0,
                    "query_vector": query_vector,
                    "preference_vector": None,
                },
            )
            rejected = await client.post(
                "/v1/hybrid-search",
                json={
                    "query": "ipad air",
                    "top_k": 3,
                    "platform": "amazon",
                    "min_landed_cost_cny": 4000.0,
                    "max_landed_cost_cny": 5000.0,
                    "query_vector": query_vector,
                    "preference_vector": None,
                    "category_card_id": "electronics.tablet",
                },
            )
            return valid, rejected

    valid, rejected = asyncio.run(exercise())
    assert valid.status_code == 200
    assert rejected.status_code == 422
    payload = valid.json()
    assert payload["query"] == "ipad air"
    assert payload["results"][0]["document_id"] == "doc-current-1"
    assert payload["results"][0]["rank"] == 1
    assert payload["results"][0]["hybrid_score"] == 0.875
    assert payload["data_mode"] == "SYNTHETIC_INTERVIEW"
    assert payload["commerce_ruleset_version"] == "synthetic-multiplatform-cny-v2"
    assert payload["results"][0]["item_price"] == 4299.0
    assert payload["results"][0]["currency"] == "CNY"
    assert payload["results"][0]["platform"] == "amazon"
    assert payload["results"][0]["same_product_group_id"].startswith("sg-v1-")
    assert payload["results"][0]["attributes"] == {
        "brand": "Apple",
        "screen_size": "11 in",
    }
    assert [rate["currency"] for rate in payload["exchange_rates"]] == [
        "CNY",
        "USD",
        "EUR",
        "SGD",
    ]
    assert payload["results"][0]["stock_status"] == "IN_STOCK"
    assert payload["results"][0]["shipping"] == 0.0
    embed_calls = [call for call in fake.calls if call["path"] == "/v1/embed"]
    assert embed_calls == []
    rerank_call = next(call for call in fake.calls if call["path"] == "/v1/rerank")
    assert rerank_call["payload"] == {
        "query": "ipad air",
        "documents": ["iPad Air 11-inch\nbrand: Apple\nscreen_size: 11 in"],
    }
    search_call = next(
        call
        for call in reversed(fake.calls)
        if str(call["path"]).endswith("_search?search_pipeline=glodex-current-product-hybrid-v1")
    )
    body = search_call["payload"]
    assert isinstance(body, dict)
    hybrid = body["query"]["hybrid"]
    # Pipeline weights are positional: semantic is 0.7 and lexical is 0.3.
    # Keeping KNN first is therefore part of the retrieval contract.
    semantic_bool = hybrid["queries"][0]["bool"]
    lexical_bool = hybrid["queries"][1]["bool"]
    assert semantic_bool["must"][0]["knn"]["item_vector"]["k"] == 200
    assert semantic_bool["must"][0]["knn"]["item_vector"]["vector"] == query_vector
    assert lexical_bool["must"] == [{"match": {"search_text": {"query": "ipad air"}}}]
    assert semantic_bool["filter"] == lexical_bool["filter"]
    assert semantic_bool["filter"][0] == {"term": {"platform": "amazon"}}
    script = semantic_bool["filter"][1]["script"]["script"]
    assert script["params"] == {
        "cny": 1.0,
        "usd": 7.2,
        "eur": 7.85,
        "sgd": 5.3,
        "minimum": pytest.approx(3999.89),
        "maximum": pytest.approx(5000.11),
    }


def test_user_preference_vector_changes_the_recall_vector() -> None:
    query = (1.0, 0.0)
    preference = (0.0, 1.0)

    assert gateway._fuse_query_preference_vectors(query, None) == query
    fused = gateway._fuse_query_preference_vectors(query, preference)
    assert fused[0] > fused[1] > 0.0
    assert math.isclose(math.sqrt(sum(value * value for value in fused)), 1.0)


def test_gateway_recalls_query_and_personalized_channels_concurrently_then_reranks_once(
    tmp_path: Path,
) -> None:
    runtime, fake = _runtime(tmp_path)
    app = gateway.create_app(runtime)
    query_vector = [1.0] + [0.0] * 1023
    preference_vector = [0.0, 1.0] + [0.0] * 1022

    async def exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/v1/hybrid-search",
                json={
                    "query": "ipad air",
                    "top_k": 3,
                    "platform": "amazon",
                    "min_landed_cost_cny": None,
                    "max_landed_cost_cny": None,
                    "query_vector": query_vector,
                    "preference_vector": preference_vector,
                },
            )

    response = asyncio.run(exercise())
    assert response.status_code == 200
    search_calls = [
        call
        for call in fake.calls
        if str(call["path"]).endswith(
            "_search?search_pipeline=glodex-current-product-hybrid-v1"
        )
    ]
    assert len(search_calls) == 2
    recall_vectors = {
        tuple(call["payload"]["query"]["hybrid"]["queries"][0]["bool"]["must"][0]["knn"]["item_vector"]["vector"])
        for call in search_calls
    }
    assert tuple(query_vector) in recall_vectors
    personalized = gateway._fuse_query_preference_vectors(query_vector, preference_vector)
    assert tuple(personalized) in recall_vectors
    rerank_calls = [call for call in fake.calls if call["path"] == "/v1/rerank"]
    assert len(rerank_calls) == 1
    payload = response.json()
    assert payload["candidate_pool_count"] == 1
    assert payload["total_recall"] == 1


def test_gateway_has_no_default_port_and_preserves_und_language() -> None:
    with pytest.raises(SystemExit):
        gateway.parse_args(["serve", "--retrieval-model-manifest", "manifest.json"])
    assert gateway.detect_query_language("无线耳机") == "und"
    assert "import faiss" not in MODULE_PATH.read_text(encoding="utf-8").casefold()
