from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import httpx

from glodex.agent.catalog import CandidateStore
from glodex.agent.contracts import (
    ItemSearchInput,
    ItemSearchRuntimeResult,
    Platform,
    PriceComparisonScope,
)
from glodex.domain.catalog import StockStatus, aggregate_catalog_batch
from glodex.domain.pricing import KnownCost
from glodex.retrieval.current_product import (
    CurrentProductGatewayIdentity,
    CurrentProductItemSource,
    build_current_product_manifest,
)
from glodex.tools.engine import PriceCompareInput, run_price_compare

_UNIT_VECTOR = (1.0, *(0.0 for _ in range(1_023)))
_PREFERENCE_VECTOR = (0.0, 1.0, *(0.0 for _ in range(1_022)))


def _request() -> ItemSearchInput:
    return ItemSearchInput(
        query="适合出差的轻薄笔记本",
        platform=Platform.AMAZON,
        category="electronics.laptop",
        min_landed_cost_cny=None,
        max_landed_cost_cny=Decimal("8000"),
        top_k=3,
    )


def test_adapter_preserves_price_inventory_shipping_and_evidence() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/hybrid-search"
        assert request.method == "POST"
        payload = json.loads(request.content)
        assert payload["query_vector"] == list(_UNIT_VECTOR)
        assert payload["preference_vector"] == list(_PREFERENCE_VECTOR)
        assert payload["min_landed_cost_cny"] is None
        assert payload["max_landed_cost_cny"] == 8000.0
        return httpx.Response(
            200,
            json={
                "schema_version": "glodex.current-product-hybrid-gateway.v12",
                "data_mode": "SYNTHETIC_INTERVIEW",
                "commerce_ruleset_version": "synthetic-multiplatform-cny-v2",
                "query": "适合出差的轻薄笔记本",
                "candidate_pool_count": 1,
                "total_recall": 1,
                "truncated": False,
                "exchange_rates": [
                    {
                        "currency": currency,
                        "base_per_unit": rate,
                        "minor_units": 2,
                        "evidence_id": "ev-synthetic-fx-" + currency.lower(),
                        "source_uri": "urn:glodex:synthetic-interview:fx:" + currency,
                        "captured_at": "2026-08-01T00:00:00Z",
                    }
                    for currency, rate in (
                        ("CNY", 1.0),
                        ("USD", 7.2),
                        ("EUR", 7.85),
                        ("SGD", 5.3),
                    )
                ],
                "results": [
                    {
                        "document_id": "doc-current-1",
                        "family_id": "family-1",
                        "source": "amazon",
                        "title": "TravelBook 14 Laptop",
                        "category": None,
                        "brand": "Glodex",
                        "color": None,
                        "attributes": {
                            "brand": "Glodex",
                            "weight": "1.25 kg",
                        },
                        "has_category": False,
                        "source_locale_raw": "en-US",
                        "source_locale_semantics": "en",
                        "item_language": "en",
                        "target_market_locale": "zh-CN",
                        "product_category": "Laptops",
                        "category_card_id": "electronics.laptop",
                        "entity_kind": "PRIMARY_PRODUCT",
                        "same_product_group_id": "sg-v1-aaaaaaaaaaaaaaaaaaaaaaaa",
                        "product_provider_id": "synthetic-interview-catalog",
                        "platform": "amazon",
                        "provider_id": "synthetic-interview-amazon",
                        "market": "US",
                        "offer_id": "offer-current-1",
                        "source_uri": "urn:glodex:synthetic-interview:product:doc-current-1",
                        "currency": "USD",
                        "item_price": 735.97,
                        "shipping": 7.36,
                        "tax": 0.0,
                        "duty": 0.0,
                        "stock_status": "IN_STOCK",
                        "delivery_days_min": 2,
                        "delivery_days_max": 10,
                        "commerce_ruleset_version": "synthetic-multiplatform-cny-v2",
                        "captured_at": "2026-08-01T00:00:00Z",
                        "hybrid_score": 0.91,
                        "rank": 1,
                    }
                ],
            },
        )

    source = CurrentProductItemSource(http_transport=httpx.MockTransport(handler))
    result = asyncio.run(
        source.search(
            _request(),
            query_vector=_UNIT_VECTOR,
            preference_vector=_PREFERENCE_VECTOR,
        )
    )

    assert result.platform is Platform.AMAZON
    assert result.total_recall == 1
    assert result.truncated is False
    assert result.candidates[0].price == Decimal("735.97")
    assert result.candidates[0].currency == "USD"
    assert result.candidates[0].same_group_id == "sg-v1-aaaaaaaaaaaaaaaaaaaaaaaa"
    product = result.platform_sub_batch.products[0]
    offer = result.platform_sub_batch.offers[0]
    assert offer.delivery_days_min == 2
    assert offer.delivery_days_max == 10
    assert {
        binding.field_path
        for binding in offer.field_evidence
        if binding.field_path.startswith("offer.delivery_days_")
    } == {"offer.delivery_days_min", "offer.delivery_days_max"}
    assert product.category == "laptop"
    assert tuple((item.name, item.value) for item in product.attributes) == (
        ("brand", "Glodex"),
        ("weight", "1.25 kg"),
    )
    assert tuple((item.name, item.value) for item in result.candidates[0].attributes) == (
        ("brand", "Glodex"),
        ("weight", "1.25 kg"),
    )
    assert {binding.field_path for binding in product.field_evidence} == {
        "product.title",
        "product.category",
        "product.entity_kind",
    }
    aggregation = aggregate_catalog_batch(result.platform_sub_batch)
    assert tuple(
        (attribute.name, attribute.value) for attribute in aggregation.products[0].attributes
    ) == (("brand", "Glodex"), ("weight", "1.25 kg"))
    assert offer.stock_status is StockStatus.IN_STOCK
    assert isinstance(offer.cost_components.shipping, KnownCost)
    assert offer.cost_components.shipping.amount == Decimal("7.36")
    assert result.platform_sub_batch.exchange_rates is not None
    assert result.platform_sub_batch.exchange_rates.base_currency == "CNY"
    assert tuple(rate.currency for rate in result.platform_sub_batch.exchange_rates.rates) == (
        "CNY",
        "USD",
        "EUR",
        "SGD",
    )
    assert len(result.platform_sub_batch.evidence) == 18


def test_gateway_health_returns_exact_serving_identity() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/health"
        return httpx.Response(
            200,
            json={
                "schema_version": "glodex.current-product-hybrid-gateway.v12",
                "status": "ready",
                "data_mode": "SYNTHETIC_INTERVIEW",
                "commerce_ruleset_version": "synthetic-multiplatform-cny-v2",
                "catalog_binding_sha256": "a" * 64,
                "item_vectors_manifest_sha256": "b" * 64,
                "retrieval_model_manifest_digest": "c" * 64,
                "candidate_engine": "opensearch_hybrid_rerank_fusion",
                "index_alias": "current-products",
                "search_pipeline": "hybrid-v1",
            },
        )

    source = CurrentProductItemSource(http_transport=httpx.MockTransport(handler))

    assert asyncio.run(source.health()) == CurrentProductGatewayIdentity(
        catalog_binding_sha256="a" * 64,
        item_vectors_manifest_sha256="b" * 64,
        retrieval_model_manifest_digest="c" * 64,
        index_alias="current-products",
        search_pipeline="hybrid-v1",
        data_mode="SYNTHETIC_INTERVIEW",
        commerce_ruleset_version="synthetic-multiplatform-cny-v2",
    )


def test_same_synthetic_product_compares_across_source_currencies_in_cny() -> None:
    platform_facts = {
        "amazon": ("USD", 597.08, 7.20),
        "ebay": ("EUR", 566.56, 7.85),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        platform = body["platform"]
        currency, item_price, _rate = platform_facts[platform]
        shared = {
            "document_id": "doc-shared-1",
            "title": "TravelBook 14 Laptop",
            "product_category": "Laptops",
            "category_card_id": "electronics.laptop",
            "entity_kind": "PRIMARY_PRODUCT",
            "attributes": {},
            "same_product_group_id": "sg-v1-aaaaaaaaaaaaaaaaaaaaaaaa",
            "product_provider_id": "synthetic-interview-catalog",
            "platform": platform,
            "provider_id": "synthetic-interview-" + platform,
            "market": "US" if platform == "amazon" else "GLOBAL",
            "offer_id": "offer-" + platform + "-doc-shared-1",
            "source_uri": "urn:glodex:synthetic-interview:product:doc-shared-1",
            "currency": currency,
            "item_price": item_price,
            "shipping": 2.50,
            "tax": 0.0,
            "duty": 0.0,
            "stock_status": "IN_STOCK",
            "delivery_days_min": 4,
            "delivery_days_max": 12,
            "commerce_ruleset_version": "synthetic-multiplatform-cny-v2",
            "captured_at": "2026-08-01T00:00:00Z",
        }
        # The same product deliberately ranks first on Amazon and second on
        # eBay. Retrieval order must not become conflicting product facts.
        results = [shared]
        if platform == "ebay":
            results.insert(
                0,
                {
                    **shared,
                    "document_id": "doc-ebay-filler",
                    "title": "Unrelated filler",
                    "same_product_group_id": "sg-v1-bbbbbbbbbbbbbbbbbbbbbbbb",
                    "offer_id": "offer-ebay-filler",
                    "source_uri": ("urn:glodex:synthetic-interview:product:doc-ebay-filler"),
                },
            )
        return httpx.Response(
            200,
            json={
                "schema_version": "glodex.current-product-hybrid-gateway.v12",
                "data_mode": "SYNTHETIC_INTERVIEW",
                "commerce_ruleset_version": "synthetic-multiplatform-cny-v2",
                "query": body["query"],
                "candidate_pool_count": len(results),
                "total_recall": len(results),
                "truncated": False,
                "exchange_rates": [
                    {
                        "currency": code,
                        "base_per_unit": rate,
                        "minor_units": 2,
                        "evidence_id": "ev-synthetic-fx-" + code.lower(),
                        "source_uri": "urn:glodex:synthetic-interview:fx:" + code,
                        "captured_at": "2026-08-01T00:00:00Z",
                    }
                    for code, rate in (
                        ("CNY", 1.0),
                        ("USD", 7.2),
                        ("EUR", 7.85),
                        ("SGD", 5.3),
                    )
                ],
                "results": results,
            },
        )

    source = CurrentProductItemSource(http_transport=httpx.MockTransport(handler))
    amazon_request = _request().model_copy(update={"platform": Platform.AMAZON})
    ebay_request = _request().model_copy(update={"platform": Platform.EBAY})

    async def exercise() -> tuple[ItemSearchRuntimeResult, ...]:
        return await asyncio.gather(
            source.search(
                amazon_request,
                query_vector=_UNIT_VECTOR,
                preference_vector=None,
            ),
            source.search(
                ebay_request,
                query_vector=_UNIT_VECTOR,
                preference_vector=None,
            ),
        )

    item_results = tuple(asyncio.run(exercise()))
    manifest = build_current_product_manifest(item_results)
    pool = CandidateStore(manifest).merge(item_results)
    prices = run_price_compare(PriceCompareInput(pool=pool))

    assert manifest.data_mode.value == "SYNTHETIC_INTERVIEW"
    assert tuple(candidate.record_ref for candidate in pool.candidates) == (
        "amazon.doc-shared-1",
        "ebay.doc-ebay-filler",
        "ebay.doc-shared-1",
    )
    assert prices.comparison_scope is PriceComparisonScope.ALL_RETRIEVED
    assert prices.compared_group_ids == ("sg-v1-aaaaaaaaaaaaaaaaaaaaaaaa",)
    point_by_id = {point.candidate_id: point for point in prices.ranked}
    assert set(point_by_id) == {
        "amazon.doc-shared-1",
        "ebay.doc-ebay-filler",
        "ebay.doc-shared-1",
    }
    assert point_by_id["amazon.doc-shared-1"].source_currency == "USD"
    assert point_by_id["amazon.doc-shared-1"].base_amount == Decimal("4298.976")
    assert point_by_id["ebay.doc-shared-1"].source_currency == "EUR"
    assert point_by_id["ebay.doc-shared-1"].base_amount == Decimal("4447.4960")
