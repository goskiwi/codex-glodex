from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import httpx
import pytest

from glodex.agent.contracts import CategoryInsightInput, InsightDepth, InsightStatus
from glodex.agent.ports import ToolPortError
from glodex.interview_catalog.runtime import CategoryKnowledgeInsight


def test_category_knowledge_adapter_uses_natural_category_and_returns_structured_insight() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/category-insight"
        assert json.loads(request.content) == {"category": "轻薄本", "depth": "deep"}
        return httpx.Response(
            200,
            json={
                "schema_version": "glodex.category-insight-gateway.v1",
                "insight": {
                    "status": "FOUND",
                    "category": "轻薄本",
                    "components": ["轻薄办公本", "商务笔记本"],
                    "bestsellers": [
                        {
                            "name": "主流轻薄办公本",
                            "typical_price_cny": "5500",
                            "why_popular": "便携、续航和办公性能较均衡",
                        }
                    ],
                    "attributes": [
                        {
                            "name": "内存容量",
                            "distribution": {"16GB": "0.7", "32GB": "0.3"},
                        }
                    ],
                    "price_tiers": [
                        {
                            "tier": "mid",
                            "range_cny": ["5500", "15000"],
                            "notes": "主流预算段",
                        }
                    ],
                    "confidence": "0.86",
                },
            },
        )

    adapter = CategoryKnowledgeInsight(http_transport=httpx.MockTransport(handler))
    result = asyncio.run(
        adapter.retrieve(CategoryInsightInput(category="轻薄本", depth=InsightDepth.DEEP))
    )

    assert result.status is InsightStatus.FOUND
    assert result.category == "轻薄本"
    assert result.components == ("轻薄办公本", "商务笔记本")
    assert result.attributes[0].distribution["16GB"] == Decimal("0.7")
    assert not hasattr(result, "candidates")


def test_category_knowledge_health_proves_independent_index_identity() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/health"
        return httpx.Response(
            200,
            json={
                "schema_version": "glodex.category-insight-gateway.v1",
                "status": "ready",
                "index_alias": "glodex-category-knowledge",
                "index_version": "knowledge-v1",
                "document_count": 6,
            },
        )

    identity = asyncio.run(
        CategoryKnowledgeInsight(http_transport=httpx.MockTransport(handler)).health()
    )

    assert identity.index_alias == "glodex-category-knowledge"
    assert identity.document_count == 6


def test_category_knowledge_adapter_fails_closed_on_old_card_candidate_shape() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "schema_version": "glodex.category-insight-gateway.v1",
                "insight": {"status": "FOUND", "candidates": [{"card_id": "laptop"}]},
            },
        )

    adapter = CategoryKnowledgeInsight(http_transport=httpx.MockTransport(handler))
    with pytest.raises(ToolPortError):
        asyncio.run(
            adapter.retrieve(CategoryInsightInput(category="轻薄本", depth=InsightDepth.QUICK))
        )
