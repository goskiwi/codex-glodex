from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from glodex.adapters.dashscope_rerank import DashScopeReranker, RerankDocument, RerankRequest
from glodex.adapters.m2a_indexes import (
    M2A_PIPELINE_ID,
    card_mapping,
    hybrid_pipeline_body,
    manifest_for,
    product_mapping,
)
from glodex.adapters.m2a_opensearch import M2aOpenSearch, validate_loopback_endpoint
from glodex.adapters.m2a_retrieval import (
    card_hybrid_body,
    parse_ranked_ids,
    product_hybrid_body,
    product_user_ann_body,
)
from glodex.application.m2a_profile import (
    M2aProfileEntry,
    profile_conflicts_with_current_query,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec(
        "GLO-M2A-P0-001",
        "GLO-M2A-P0-002",
        "GLO-M2A-P0-003",
        "GLO-M2A-P0-004",
        "GLO-M2A-P0-005",
        "GLO-M2A-P0-006",
        "GLO-M2A-NFR-001",
        "GLO-M2A-NFR-002",
        "GLO-M2A-NFR-003",
        "GLO-M2A-NFR-004",
        "GLO-M2A-NFR-006",
    ),
]

_ROOT = Path(__file__).parents[3]


def _vector() -> tuple[float, ...]:
    return (1.0, *(0.0 for _ in range(1_023)))


def test_loopback_endpoint_rejects_every_non_fixed_target() -> None:
    assert validate_loopback_endpoint("http://127.0.0.1:9200") == "http://127.0.0.1:9200"
    assert validate_loopback_endpoint("http://[::1]:9200") == "http://[::1]:9200"
    for value in (
        "https://127.0.0.1:9200",
        "http://localhost:9200",
        "http://127.0.0.1:9201",
        "http://127.0.0.1:9200/path",
        "http://127.0.0.1:9200?url=http://remote",
    ):
        with pytest.raises(ValueError):
            validate_loopback_endpoint(value)


def test_manifest_mapping_and_pipeline_are_fixed_to_validated_m1d_assets() -> None:
    from glodex.adapters.agent_indexes import load_agent_indexes

    indexes = asyncio.run(
        load_agent_indexes(
            snapshot_root=_ROOT / "data" / "snapshots",
            agent_root=_ROOT / "data" / "agent",
        )
    )
    manifest = manifest_for(indexes)
    product = product_mapping(manifest)
    card = card_mapping(manifest)

    assert manifest.fingerprint == manifest_for(indexes).fingerprint
    assert len(manifest.product_ids) == 8
    assert len(manifest.card_ids) == 8
    assert product["mappings"]["_meta"]["asset_fingerprint"] == manifest.fingerprint
    assert product["mappings"]["properties"]["item_vector"]["dimension"] == 1_024
    assert card["mappings"]["properties"]["card_vector"]["method"]["engine"] == "lucene"
    pipeline = hybrid_pipeline_body()
    processor = pipeline["phase_results_processors"][0]["normalization-processor"]
    assert processor["normalization"]["technique"] == "min_max"
    assert processor["combination"]["parameters"]["weights"] == [0.7, 0.3]


def test_hybrid_and_user_ann_bodies_are_bounded_and_share_hard_filters() -> None:
    query = product_hybrid_body(
        query="phone",
        query_vector=_vector(),
        platform="amazon",
    )
    hybrid = query["query"]["hybrid"]
    assert query["size"] == 30
    assert query["search_pipeline"] == M2A_PIPELINE_ID
    assert len(hybrid["queries"]) == 2
    lexical = hybrid["queries"][0]["bool"]
    semantic = hybrid["queries"][1]["knn"]["item_vector"]
    assert lexical["filter"] == semantic["filter"]["bool"]["filter"]
    assert semantic["k"] == 30
    user = product_user_ann_body(vector=_vector(), platform="amazon")
    assert user["size"] == 10
    assert user["query"]["knn"]["item_vector"]["k"] == 10
    card = card_hybrid_body(category="phone", query="phone", query_vector=_vector())
    assert card["size"] == 30
    assert card["search_pipeline"] == M2A_PIPELINE_ID


def test_opensearch_adapter_translates_the_hybrid_pipeline_to_search_api_params() -> None:
    captured: dict[str, object] = {}

    class FakeClient:
        async def search(
            self,
            *,
            index: str,
            body: dict[str, object],
            params: dict[str, str] | None,
        ) -> dict[str, object]:
            captured.update({"index": index, "body": body, "params": params})
            return {"hits": {"hits": []}}

    adapter = M2aOpenSearch()
    adapter._client = FakeClient()
    response = asyncio.run(
        adapter.search(
            alias="glodex-m2a-product",
            body={"search_pipeline": M2A_PIPELINE_ID, "query": {"match_all": {}}},
        )
    )

    assert response == {"hits": {"hits": []}}
    assert captured == {
        "index": "glodex-m2a-product",
        "body": {"query": {"match_all": {}}},
        "params": {"search_pipeline": M2A_PIPELINE_ID},
    }


def test_key_only_parser_rejects_injected_or_duplicate_identities() -> None:
    valid = {
        "hits": {
            "hits": [
                {"_id": "a", "_score": 0.5, "_source": {"record_key": "a"}},
                {"_id": "b", "_score": 0.5, "_source": {"record_key": "b"}},
            ]
        }
    }
    assert parse_ranked_ids(valid, identity_field="record_key", maximum=30) == ("a", "b")
    for mutated in (
        {"hits": {"hits": [{"_id": "a", "_score": 1.0, "_source": {"record_key": "x"}}]}},
        {
            "hits": {
                "hits": [
                    {"_id": "a", "_score": 1.0, "_source": {"record_key": "a"}},
                    {"_id": "a", "_score": 0.1, "_source": {"record_key": "a"}},
                ]
            }
        },
    ):
        with pytest.raises(ValueError):
            parse_ranked_ids(mutated, identity_field="record_key", maximum=30)


def test_profile_is_explicit_typed_soft_memory_and_never_becomes_required() -> None:
    entry = M2aProfileEntry(
        profile_id="local-demo",
        entry_id="pref-01",
        scope="soft",
        kind="preference",
        value="轻薄",
        user_vector=_vector(),
    )
    assert not profile_conflicts_with_current_query(entry=entry, query="推荐一台轻薄笔记本")
    assert profile_conflicts_with_current_query(entry=entry, query="不要轻薄, 优先性能")
    with pytest.raises(ValueError):
        M2aProfileEntry(
            profile_id="local-demo",
            entry_id="pref-01",
            scope="hard",
            kind="preference",
            value="轻薄",
            user_vector=_vector(),
        )


def test_reranker_requires_complete_identity_bijection_and_uses_fixed_model() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "qwen3-rerank",
                "output": {
                    "results": [
                        {"index": 0, "relevance_score": 0.1},
                        {"index": 1, "relevance_score": 0.9},
                    ]
                },
            },
        )

    reranker = DashScopeReranker(
        credential="test-credential",
        http_transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        reranker.rerank(
            RerankRequest(
                query="phone",
                documents=(
                    RerankDocument(identity="a", text="trusted item a"),
                    RerankDocument(identity="b", text="trusted item b"),
                ),
            )
        )
    )
    assert captured["model"] == "qwen3-rerank"
    assert result.identities == ("b", "a")
