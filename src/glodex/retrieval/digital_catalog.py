"""Reviewed recent-digital catalog overlay for the live shopping Agent."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from glodex.agent.contracts import (
    ItemSearchInput,
    ItemSearchRuntimeResult,
    Platform,
    ToolFailureCode,
)
from glodex.agent.ports import ToolPortError
from glodex.application.rerank_contracts import RerankDocument, RerankRequest
from glodex.domain.catalog import CatalogBatch
from glodex.retrieval.catalog_quality import (
    CatalogPolicy,
    CatalogQualityReport,
    audit_catalog,
)
from glodex.retrieval.current_product import (
    CurrentProductGatewayIdentity,
    CurrentProductItemSource,
    _materialize_exchange_rates,
    _materialize_hit,
)
from glodex.retrieval.model_service import RetrievalModelError, RetrievalReranker

_RULESET = "synthetic-multiplatform-cny-v2"
_SNAPSHOT = "synthetic-interview-commerce-v1"
_RERANK_WINDOW = 30


@dataclass(frozen=True, slots=True)
class _DigitalProduct:
    product_id: str
    title: str
    category: str
    price_cny: Decimal
    attributes: dict[str, str]
    source_uri: str
    captured_at: datetime


class DigitalCatalogItemSource:
    """Search the reviewed recent catalog by trusted category and budget facts."""

    def __init__(self, path: Path, *, reranker: RetrievalReranker | None = None) -> None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw_products = payload.get("products") if isinstance(payload, dict) else None
        raw_categories = payload.get("categories") if isinstance(payload, dict) else None
        if not isinstance(raw_products, list) or not raw_products:
            raise ValueError("digital catalog products are invalid")
        if not isinstance(raw_categories, dict):
            raise ValueError("digital catalog categories are invalid")
        policy = CatalogPolicy.from_payload(payload.get("catalog_policy"))
        if policy is not None:
            raw_products, quality_report = audit_catalog(raw_products, policy)
            if not raw_products:
                raise ValueError("digital catalog has no products admitted by policy")
        else:
            quality_report = CatalogQualityReport(
                total=len(raw_products),
                admitted=len(raw_products),
                rejected=0,
                reasons={"ADMITTED": len(raw_products)},
            )
        category_by_alias: dict[str, str] = {}
        for category, aliases in raw_categories.items():
            if not isinstance(category, str) or not isinstance(aliases, list):
                raise ValueError("digital catalog category aliases are invalid")
            category_by_alias.setdefault(_normalized(category), category)
            for alias in aliases:
                if not isinstance(alias, str):
                    raise ValueError("digital catalog category alias is invalid")
                category_by_alias.setdefault(_normalized(alias), category)
        products: list[_DigitalProduct] = []
        for raw in raw_products:
            if not isinstance(raw, dict):
                raise ValueError("digital catalog product is invalid")
            spec = raw.get("specification_source")
            attributes = raw.get("attributes")
            if not isinstance(spec, dict) or not isinstance(attributes, dict):
                raise ValueError("digital catalog evidence is invalid")
            captured_at = datetime.fromisoformat(str(spec["captured_at"]).replace("Z", "+00:00"))
            products.append(
                _DigitalProduct(
                    product_id=str(raw["product_id"]),
                    title=str(raw["title"]),
                    category=str(raw["category"]),
                    price_cny=Decimal(str(raw["reference_price_cny"])),
                    attributes={str(key): str(value) for key, value in attributes.items()},
                    source_uri=str(spec["url"]),
                    captured_at=captured_at.astimezone(UTC),
                )
            )
        self._products = tuple(products)
        self._category_by_alias = category_by_alias
        self._reranker = reranker
        self._quality_report = quality_report

    @property
    def quality_report(self) -> CatalogQualityReport:
        return self._quality_report

    async def search(
        self,
        request: ItemSearchInput,
        *,
        query_vector: tuple[float, ...] | None,
        preference_vector: tuple[float, ...] | None,
    ) -> ItemSearchRuntimeResult:
        del preference_vector
        if query_vector is None:
            raise ToolPortError(ToolFailureCode.ITEM_SOURCE_INVALID)
        category = self._canonical_category(request.category)
        matches = [
            product
            for product in self._products
            if product.category == category and self._within_budget(product, request)
        ]
        matches.sort(key=lambda product: self._rank_key(product, request))
        coarse = _interleave_models(matches)[:_RERANK_WINDOW]
        ordered = await self._rerank(request.query, coarse)
        selected = ordered[: request.top_k]
        materialized = tuple(
            _materialize_hit(
                self._hit(product, platform=request.platform),
                expected_platform=request.platform,
                ordinal=ordinal,
            )
            for ordinal, product in enumerate(selected)
        )
        exchange_rates, fx_evidence = _materialize_exchange_rates(_exchange_rates())
        return ItemSearchRuntimeResult(
            platform=request.platform,
            target_query=request.query,
            retrieval_query=request.query,
            candidates=tuple(item[0] for item in materialized),
            platform_sub_batch=CatalogBatch(
                snapshot_version=_SNAPSHOT,
                products=tuple(item[1] for item in materialized),
                offers=tuple(item[2] for item in materialized),
                evidence=(
                    *(ref for item in materialized for ref in item[3]),
                    *fx_evidence,
                ),
                exchange_rates=exchange_rates,
            ),
            total_recall=len(matches),
            returned_before_semantic_filter=len(materialized),
            truncated=len(matches) > len(materialized),
        )

    async def _rerank(
        self,
        query: str,
        products: list[_DigitalProduct],
    ) -> list[_DigitalProduct]:
        """Cross-encode the bounded coarse pool while preserving membership."""

        if self._reranker is None or len(products) < 2:
            return products
        identities = tuple(f"recent-{index}" for index in range(len(products)))
        documents = tuple(
            RerankDocument(identity=identity, text=_rerank_text(product))
            for identity, product in zip(identities, products, strict=True)
        )
        try:
            result = await self._reranker.rerank(
                RerankRequest(query=query[:512], documents=documents)
            )
        except RetrievalModelError:
            return products
        if set(result.identities) != set(identities):
            return products
        by_identity = dict(zip(identities, products, strict=True))
        return [by_identity[identity] for identity in result.identities]

    @staticmethod
    def _within_budget(product: _DigitalProduct, request: ItemSearchInput) -> bool:
        return not (
            (
                request.min_landed_cost_cny is not None
                and product.price_cny < request.min_landed_cost_cny
            )
            or (
                request.max_landed_cost_cny is not None
                and product.price_cny > request.max_landed_cost_cny
            )
        )

    @staticmethod
    def _rank_key(product: _DigitalProduct, request: ItemSearchInput) -> tuple[int, Decimal, str]:
        if request.min_landed_cost_cny is not None and request.max_landed_cost_cny is not None:
            target = (request.min_landed_cost_cny + request.max_landed_cost_cny) / 2
        elif request.max_landed_cost_cny is not None:
            target = request.max_landed_cost_cny
        else:
            target = product.price_cny
        evidence_text = _normalized(" ".join((product.title, *product.attributes.values())))
        evidence_matches = sum(term in evidence_text for term in _query_terms(request.query))
        return -evidence_matches, abs(product.price_cny - target), product.product_id

    def _canonical_category(self, value: str) -> str:
        normalized = _normalized(value)
        exact = self._category_by_alias.get(normalized)
        if exact is not None:
            return exact
        contained = [
            (len(alias), category)
            for alias, category in self._category_by_alias.items()
            if alias and (alias in normalized or normalized in alias)
        ]
        return max(contained, default=(0, value))[1]

    @staticmethod
    def _hit(product: _DigitalProduct, *, platform: Platform) -> dict[str, Any]:
        document_id = "dg-" + hashlib.sha256(product.product_id.encode()).hexdigest()[:24]
        group = hashlib.sha256(product.product_id.encode()).hexdigest()[:24]
        offer_digest = hashlib.sha256(
            (platform.value + "\x1f" + product.product_id).encode()
        ).hexdigest()[:24]
        return {
            "document_id": document_id,
            "title": product.title,
            "product_category": product.category,
            "category_card_id": "digital." + product.category,
            "entity_kind": "PRIMARY_PRODUCT",
            "platform": platform.value,
            "product_provider_id": "public-product-spec-reviewed-digital-catalog",
            "provider_id": "catalog-reference-price-reviewed-digital-" + platform.value,
            "offer_id": "offer-" + offer_digest,
            "source_uri": product.source_uri,
            "market": "CN",
            "currency": "CNY",
            "item_price": float(product.price_cny),
            "shipping": 0.0,
            "tax": 0.0,
            "duty": 0.0,
            "stock_status": "IN_STOCK",
            "delivery_days_min": 1,
            "delivery_days_max": 7,
            "commerce_ruleset_version": _RULESET,
            "captured_at": product.captured_at.isoformat().replace("+00:00", "Z"),
            "same_product_group_id": "sg-v1-" + group,
            "attributes": dict(list(product.attributes.items())[:16]),
        }


class DigitalFirstItemSource:
    """Use reviewed recent products when covered, otherwise retain the 92万 catalog."""

    def __init__(self, *, recent: DigitalCatalogItemSource, historical: CurrentProductItemSource):
        self._recent = recent
        self._historical = historical

    async def health(self) -> CurrentProductGatewayIdentity:
        return await self._historical.health()

    async def search(self, request: ItemSearchInput, **kwargs: Any) -> ItemSearchRuntimeResult:
        recent = await self._recent.search(request, **kwargs)
        if recent.candidates:
            return recent
        return await self._historical.search(request, **kwargs)


def _interleave_models(products: list[_DigitalProduct]) -> list[_DigitalProduct]:
    first: list[_DigitalProduct] = []
    seen: set[str] = set()
    for product in products:
        model = product.attributes.get("model", product.title).casefold()
        if model in seen:
            continue
        first.append(product)
        seen.add(model)
    return first


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())


def _query_terms(value: str) -> tuple[str, ...]:
    normalized = _normalized(value)
    terms = set(re.findall(r"[a-z][a-z0-9.+-]{1,}|[0-9]+(?:\.[0-9]+)?", normalized))
    for sequence in re.findall(r"[\u3400-\u9fff]+", normalized):
        terms.update(sequence[index : index + 2] for index in range(len(sequence) - 1))
    return tuple(sorted(terms))


def _rerank_text(product: _DigitalProduct) -> str:
    text = " | ".join(
        (
            product.title,
            f"category: {product.category}",
            *(f"{key}: {value}" for key, value in product.attributes.items()),
        )
    )
    return text[:2_000]


def _exchange_rates() -> list[dict[str, object]]:
    rates = (("CNY", 1.0), ("USD", 7.2), ("EUR", 7.85), ("SGD", 5.3))
    return [
        {
            "currency": currency,
            "base_per_unit": value,
            "minor_units": 2,
            "evidence_id": "ev-synthetic-fx-" + currency.casefold(),
            "source_uri": "urn:glodex:synthetic-interview:fx:" + currency,
            "captured_at": "2026-08-01T00:00:00Z",
        }
        for currency, value in rates
    ]


__all__ = ["DigitalCatalogItemSource", "DigitalFirstItemSource"]
