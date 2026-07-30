"""Generate, verify, or operator-build the M1d demo assets.

The committed vectors are deterministic fixture vectors so the offline contract
suite never needs credentials or a network connection.  The manifest records
that provenance explicitly while freezing the request-time embedding model and
dimensions required by M1d.  A separate explicit operator mode may replace only
the agent assets with one validated DashScope embedding build.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from glodex.adapters.agent_indexes import AgentIndexes, load_agent_indexes
from glodex.adapters.agent_live_http import build_dashscope_embedding
from glodex.adapters.local_snapshot import (
    EXCHANGE_RATES_SCHEMA_VERSION,
    MANIFEST_SCHEMA_VERSION,
)
from glodex.application.agent.contracts import (
    EmbeddingBatch,
    EmbeddingResult,
    ToolFailureCode,
)
from glodex.application.agent.ports import EmbeddingPort, ToolPortError

SNAPSHOT_VERSION: Final = "m1d-demo-v1"
INDEX_VERSION: Final = "m1d-demo-index-v1"
RULESET_VERSION: Final = "m1d-cn-shipping-v1"
AGENT_MANIFEST_SCHEMA: Final = "glodex.agent-assets-manifest.v1"
CATEGORY_CARD_SCHEMA: Final = "glodex.category-card.v1"
CATEGORY_EMBEDDING_SCHEMA: Final = "glodex.category-embedding.v1"
ITEM_EMBEDDING_SCHEMA: Final = "glodex.item-embedding.v1"
SHIPPING_RULES_SCHEMA: Final = "glodex.shipping-rules.v1"
EMBEDDING_MODEL: Final = "text-embedding-v4"
EMBEDDING_DIMENSIONS: Final = 1024
CREATED_AT: Final = "2026-07-29T00:00:00Z"
CAPTURED_AT: Final = "2026-07-01T00:00:00Z"
GENERATOR_NAME: Final = "glodex-m1d-demo-assets"
GENERATOR_VERSION: Final = "1.0.0"
DETERMINISTIC_EMBEDDING_PROVENANCE: Final = "DETERMINISTIC_DEMO_FIXTURE"
DASHSCOPE_EMBEDDING_PROVENANCE: Final = "DASHSCOPE_OPERATOR_BUILD"

PLATFORMS: Final = ("amazon", "shopee", "aliexpress", "ebay")
PROVIDERS: Final = (
    "amazon-demo",
    "shopee-demo",
    "aliexpress-demo",
    "ebay-demo",
)
MARKETS: Final = ("US", "CN")
CURRENCIES: Final = ("CNY", "USD")
PRODUCT_CATEGORIES: Final = ("phone", "laptop", "tablet")
CATEGORY_COVERAGE: Final = (
    "laptop",
    "tablet",
    "phone",
    "laptop-accessory",
    "laptop-part",
    "laptop-decoration",
)

_PLATFORM_DIMENSION: Final = {
    "amazon": 100,
    "shopee": 101,
    "aliexpress": 102,
    "ebay": 103,
}
_CATEGORY_DIMENSION: Final = {
    "phone": 0,
    "laptop": 1,
    "tablet": 2,
    "laptop-accessory": 3,
    "laptop-part": 4,
    "laptop-decoration": 5,
}


@dataclass(frozen=True, slots=True)
class _ProductSpec:
    platform: str
    slug: str
    title: str
    category: str
    currency: str
    market: str
    item_price: str
    shipping: str
    tax: str
    duty: str
    highlight: str

    @property
    def provider_id(self) -> str:
        return f"{self.platform}-demo"

    @property
    def product_id(self) -> str:
        return f"{self.platform}-{self.slug}"

    @property
    def offer_id(self) -> str:
        return f"{self.platform}-offer-{self.slug}"

    @property
    def product_uri(self) -> str:
        return f"https://demo-data.glodex.example/{self.platform}/products/{self.slug}"

    @property
    def offer_uri(self) -> str:
        return f"https://demo-data.glodex.example/{self.platform}/offers/{self.slug}"


@dataclass(frozen=True, slots=True)
class _CardSpec:
    card_id: str
    category: str
    summary: str
    components: tuple[str, ...]
    bestsellers: tuple[str, ...]
    attributes: tuple[str, ...]
    price_tiers: tuple[str, ...]
    confidence: str

    @property
    def source_url(self) -> str:
        return f"https://demo-guides.glodex.example/cards/{self.card_id}"


_PRODUCTS: Final = (
    _ProductSpec(
        "amazon",
        "phone-air",
        "Amazon Demo Phone Air",
        "phone",
        "USD",
        "US",
        "599",
        "18",
        "36",
        "0.00",
        "long_battery",
    ),
    _ProductSpec(
        "amazon",
        "laptop-travel",
        "Amazon Demo Travel Laptop",
        "laptop",
        "USD",
        "US",
        "899",
        "25",
        "54",
        "0.00",
        "portable",
    ),
    _ProductSpec(
        "shopee",
        "phone-lite",
        "Shopee Demo Phone Lite",
        "phone",
        "CNY",
        "CN",
        "3299",
        "20",
        "99",
        "0.00",
        "lightweight",
    ),
    _ProductSpec(
        "shopee",
        "tablet-go",
        "Shopee Demo Tablet Go",
        "tablet",
        "CNY",
        "CN",
        "2199",
        "18",
        "66",
        "0.00",
        "portable",
    ),
    _ProductSpec(
        "aliexpress",
        "phone-max",
        "AliExpress Demo Phone Max",
        "phone",
        "USD",
        "US",
        "449",
        "12",
        "27",
        "0.00",
        "long_battery",
    ),
    _ProductSpec(
        "aliexpress",
        "laptop-mini",
        "AliExpress Demo Laptop Mini",
        "laptop",
        "USD",
        "US",
        "699",
        "22",
        "42",
        "0.00",
        "lightweight",
    ),
    _ProductSpec(
        "ebay",
        "phone-reference",
        "eBay Demo Phone Reference",
        "phone",
        "USD",
        "US",
        "399",
        "15",
        "24",
        "0.00",
        "portable",
    ),
    _ProductSpec(
        "ebay",
        "laptop-reference",
        "eBay Demo Laptop Reference",
        "laptop",
        "USD",
        "US",
        "749",
        "24",
        "45",
        "0.00",
        "travel_ready",
    ),
)

_CARDS: Final = (
    _CardSpec(
        "card-phone-core",
        "phone",
        "手机选购关注续航、屏幕、存储与网络制式。",
        ("battery", "display", "storage", "network"),
        ("balanced_phone", "camera_phone"),
        ("long_battery", "portable", "storage_capacity"),
        ("entry", "mid", "premium"),
        "0.920",
    ),
    _CardSpec(
        "card-phone-travel",
        "phone",
        "旅行手机应关注重量、漫游频段、快充和耐用性。",
        ("radio", "charging", "battery"),
        ("travel_phone",),
        ("lightweight", "long_battery", "durable"),
        ("mid", "premium"),
        "0.880",
    ),
    _CardSpec(
        "card-laptop-core",
        "laptop",
        "轻薄本选购关注处理器、内存、续航、重量和接口。",
        ("processor", "memory", "battery", "ports"),
        ("travel_laptop", "productivity_laptop"),
        ("lightweight", "long_battery", "portable"),
        ("entry", "mid", "premium"),
        "0.930",
    ),
    _CardSpec(
        "card-laptop-travel",
        "laptop",
        "出差笔记本应兼顾重量、续航、充电器尺寸和可靠性。",
        ("battery", "charger", "chassis"),
        ("business_ultraportable",),
        ("travel_ready", "lightweight", "durable"),
        ("mid", "premium"),
        "0.900",
    ),
    _CardSpec(
        "card-tablet-core",
        "tablet",
        "平板电脑选购关注屏幕、手写输入、续航和配件生态。",
        ("display", "stylus", "battery"),
        ("media_tablet", "productivity_tablet"),
        ("portable", "long_battery"),
        ("entry", "mid", "premium"),
        "0.890",
    ),
    _CardSpec(
        "card-laptop-accessory",
        "laptop-accessory",
        "笔记本配件包含扩展坞、保护包、鼠标和电源适配器。",
        ("dock", "bag", "mouse", "charger"),
        ("travel_dock",),
        ("compatibility", "portable"),
        ("entry", "mid"),
        "0.860",
    ),
    _CardSpec(
        "card-laptop-part",
        "laptop-part",
        "笔记本替换件需要核对型号、接口、电压和保修边界。",
        ("battery_pack", "keyboard", "display_panel"),
        ("replacement_battery",),
        ("compatibility", "model_specific"),
        ("entry", "mid"),
        "0.840",
    ),
    _CardSpec(
        "card-laptop-decoration",
        "laptop-decoration",
        "笔记本装饰类商品不应被当作主机或功能配件推荐。",
        ("sticker", "skin", "decorative_cover"),
        ("protective_skin",),
        ("decorative_only",),
        ("entry",),
        "0.810",
    ),
)


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def _jsonl_bytes(records: tuple[dict[str, object], ...]) -> bytes:
    return b"".join(_json_bytes(record) for record in records)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _binding(field_path: str, evidence_id: str) -> dict[str, object]:
    return {"evidence_id": evidence_id, "field_path": field_path}


def _evidence(
    *,
    evidence_id: str,
    entity_type: str,
    field_path: str,
    provider_id: str,
    source_uri: str,
    product_id: str | None = None,
    offer_id: str | None = None,
    currency: str | None = None,
) -> dict[str, object]:
    return {
        "captured_at": CAPTURED_AT,
        "currency": currency,
        "entity_type": entity_type,
        "evidence_id": evidence_id,
        "field_path": field_path,
        "offer_id": offer_id,
        "product_id": product_id,
        "provider_id": provider_id,
        "snapshot_version": SNAPSHOT_VERSION,
        "source_uri": source_uri,
    }


def _known_cost(amount: str, evidence_id: str) -> dict[str, object]:
    return {
        "amount": amount,
        "evidence_id": evidence_id,
        "kind": "KNOWN",
    }


def _product_records(
    spec: _ProductSpec,
) -> tuple[dict[str, object], dict[str, object], tuple[dict[str, object], ...]]:
    product_bindings: list[dict[str, object]] = []
    attributes: list[dict[str, object]] = []
    evidence: list[dict[str, object]] = []
    for suffix, field_path in (
        ("title", "product.title"),
        ("category", "product.category"),
        ("kind", "product.entity_kind"),
    ):
        evidence_id = f"ev-{spec.product_id}-{suffix}"
        product_bindings.append(_binding(field_path, evidence_id))
        evidence.append(
            _evidence(
                evidence_id=evidence_id,
                entity_type="PRODUCT",
                field_path=field_path,
                provider_id=spec.provider_id,
                source_uri=spec.product_uri,
                product_id=spec.product_id,
            )
        )
    for name, value in (
        ("pack_size", "1"),
        ("pack_note", "single unit"),
        ("verified_signal", spec.highlight),
    ):
        field_path = f"product.attributes.{name}"
        evidence_id = f"ev-{spec.product_id}-attr-{name}"
        attributes.append(
            {
                "evidence_id": evidence_id,
                "name": name,
                "value": value,
            }
        )
        evidence.append(
            _evidence(
                evidence_id=evidence_id,
                entity_type="PRODUCT",
                field_path=field_path,
                provider_id=spec.provider_id,
                source_uri=spec.product_uri,
                product_id=spec.product_id,
            )
        )

    offer_bindings: list[dict[str, object]] = []
    costs: dict[str, object] = {"currency": spec.currency}
    offer_fields = (
        ("market", "offer.market"),
        ("currency", "offer.cost_components.currency"),
        ("inventory", "offer.inventory"),
        ("item_price", "offer.cost_components.item_price"),
        ("shipping", "offer.cost_components.shipping"),
        ("tax", "offer.cost_components.tax"),
        ("duty", "offer.cost_components.duty"),
    )
    amounts = {
        "item_price": spec.item_price,
        "shipping": spec.shipping,
        "tax": spec.tax,
        "duty": spec.duty,
    }
    for suffix, field_path in offer_fields:
        evidence_id = f"ev-{spec.offer_id}-{suffix}"
        offer_bindings.append(_binding(field_path, evidence_id))
        evidence.append(
            _evidence(
                evidence_id=evidence_id,
                entity_type="OFFER",
                field_path=field_path,
                provider_id=spec.provider_id,
                source_uri=spec.offer_uri,
                product_id=spec.product_id,
                offer_id=spec.offer_id,
            )
        )
        if suffix in amounts:
            costs[suffix] = _known_cost(amounts[suffix], evidence_id)

    product: dict[str, object] = {
        "attributes": attributes,
        "category": spec.category,
        "entity_kind": "PRIMARY_PRODUCT",
        "field_evidence": product_bindings,
        "product_id": spec.product_id,
        "provider_id": spec.provider_id,
        "snapshot_version": SNAPSHOT_VERSION,
        "source_uri": spec.product_uri,
        "title": spec.title,
    }
    offer: dict[str, object] = {
        "captured_at": CAPTURED_AT,
        "cost_components": costs,
        "field_evidence": offer_bindings,
        "market": spec.market,
        "offer_id": spec.offer_id,
        "product_id": spec.product_id,
        "provider_id": spec.provider_id,
        "snapshot_version": SNAPSHOT_VERSION,
        "source_uri": spec.offer_uri,
        "stock_status": "IN_STOCK",
    }
    return product, offer, tuple(evidence)


def _exchange_rates() -> tuple[dict[str, object], tuple[dict[str, object], ...]]:
    rates: tuple[dict[str, object], ...] = (
        {
            "base_per_unit": "1",
            "currency": "CNY",
            "evidence_id": "ev-m1d-fx-cny",
            "minor_units": 2,
        },
        {
            "base_per_unit": "7.2",
            "currency": "USD",
            "evidence_id": "ev-m1d-fx-usd",
            "minor_units": 2,
        },
    )
    evidence = tuple(
        _evidence(
            evidence_id=str(rate["evidence_id"]),
            entity_type="EXCHANGE_RATE",
            field_path="exchange_rate.base_per_unit",
            provider_id="m1d-demo-fx",
            source_uri=f"https://demo-data.glodex.example/fx/{rate['currency']}",
            currency=str(rate["currency"]),
        )
        for rate in rates
    )
    result: dict[str, object] = {
        "base_currency": "CNY",
        "rates": list(rates),
        "schema_version": EXCHANGE_RATES_SCHEMA_VERSION,
        "snapshot_version": SNAPSHOT_VERSION,
    }
    return result, evidence


def _snapshot_payloads() -> tuple[dict[str, bytes], dict[str, object]]:
    products: list[dict[str, object]] = []
    offers: list[dict[str, object]] = []
    evidence: list[dict[str, object]] = []
    for spec in _PRODUCTS:
        product, offer, item_evidence = _product_records(spec)
        products.append(product)
        offers.append(offer)
        evidence.extend(item_evidence)
    exchange_rates, fx_evidence = _exchange_rates()
    evidence.extend(fx_evidence)
    raw_rates = exchange_rates["rates"]
    if not isinstance(raw_rates, list):
        raise TypeError("exchange-rate records must be a list")
    payloads = {
        "products": _jsonl_bytes(tuple(products)),
        "offers": _jsonl_bytes(tuple(offers)),
        "evidence": _jsonl_bytes(tuple(evidence)),
        "exchange_rates": _json_bytes(exchange_rates),
    }
    manifest: dict[str, object] = {
        "base_currency": "CNY",
        "categories": list(PRODUCT_CATEGORIES),
        "created_at": CREATED_AT,
        "currencies": list(CURRENCIES),
        "files": {
            role: {
                "path": filename,
                "record_count": (
                    len(raw_rates) if role == "exchange_rates" else len(payloads[role].splitlines())
                ),
                "sha256": _sha256(payloads[role]),
            }
            for role, filename in (
                ("products", "products.jsonl"),
                ("offers", "offers.jsonl"),
                ("evidence", "evidence.jsonl"),
                ("exchange_rates", "exchange_rates.json"),
            )
        },
        "generator": {
            "name": GENERATOR_NAME,
            "version": GENERATOR_VERSION,
        },
        "markets": list(MARKETS),
        "providers": list(PROVIDERS),
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "snapshot_version": SNAPSHOT_VERSION,
    }
    return payloads, manifest


def _unit_vector(primary: int, secondary: int) -> list[float]:
    values = [0.0] * EMBEDDING_DIMENSIONS
    values[primary] = 0.92
    values[secondary] = 0.38
    norm = math.sqrt(sum(value * value for value in values))
    return [round(value / norm, 15) for value in values]


def _card_record(spec: _CardSpec) -> dict[str, object]:
    return {
        "attributes": list(spec.attributes),
        "bestsellers": list(spec.bestsellers),
        "captured_at": CAPTURED_AT,
        "card_id": spec.card_id,
        "category": spec.category,
        "components": list(spec.components),
        "price_tiers": list(spec.price_tiers),
        "schema_version": CATEGORY_CARD_SCHEMA,
        "source_confidence": spec.confidence,
        "source_domain": "demo-guides.glodex.example",
        "source_url": spec.source_url,
        "summary": spec.summary,
    }


def _card_text(card: dict[str, object]) -> str:
    values: list[str] = [
        str(card["category"]),
        str(card["summary"]),
        str(card["source_domain"]),
    ]
    for name in ("components", "bestsellers", "attributes", "price_tiers"):
        raw = card[name]
        if not isinstance(raw, list):
            raise TypeError("card fact collection must be a list")
        values.extend(str(item) for item in raw)
    return "\n".join(values)


def _item_text(spec: _ProductSpec) -> str:
    return "\n".join(
        (
            spec.title,
            spec.category,
            spec.platform,
            spec.highlight,
            "single unit",
        )
    )


def _shipping_rules() -> dict[str, object]:
    return {
        "currency": "CNY",
        "destination_country": "CN",
        "effective_date": "2026-07-01",
        "rules": [
            {
                "base_shipping": base_shipping,
                "duty_rate": duty_rate,
                "duty_threshold": "5000",
                "eta_max_days": eta_max,
                "eta_min_days": eta_min,
                "platform": platform,
            }
            for platform, base_shipping, duty_rate, eta_min, eta_max in (
                ("amazon", "80", "0.10", 7, 14),
                ("shopee", "20", "0.06", 3, 8),
                ("aliexpress", "45", "0.08", 8, 20),
                ("ebay", "90", "0.10", 9, 18),
            )
        ],
        "ruleset_version": RULESET_VERSION,
        "schema_version": SHIPPING_RULES_SCHEMA,
    }


def _agent_payloads(
    snapshot_payloads: dict[str, bytes],
    snapshot_manifest: dict[str, object],
    *,
    category_vectors: tuple[tuple[float, ...], ...] | None = None,
    item_vectors: tuple[tuple[float, ...], ...] | None = None,
    embedding_provenance: str = DETERMINISTIC_EMBEDDING_PROVENANCE,
) -> tuple[dict[str, bytes], dict[str, object]]:
    cards = tuple(_card_record(spec) for spec in _CARDS)
    if (category_vectors is None) is not (item_vectors is None):
        raise ValueError("category and item vectors must be supplied together")
    if category_vectors is None:
        active_category_vectors = tuple(
            tuple(
                _unit_vector(
                    _CATEGORY_DIMENSION[spec.category],
                    200 + ordinal,
                )
            )
            for ordinal, spec in enumerate(_CARDS)
        )
        active_item_vectors = tuple(
            tuple(
                _unit_vector(
                    _CATEGORY_DIMENSION[spec.category],
                    _PLATFORM_DIMENSION[spec.platform],
                )
            )
            for spec in _PRODUCTS
        )
    else:
        if (
            item_vectors is None
            or len(category_vectors) != len(_CARDS)
            or len(item_vectors) != len(_PRODUCTS)
        ):
            raise ValueError("embedding vectors must cover every source exactly once")
        active_category_vectors = category_vectors
        active_item_vectors = item_vectors
    category_embeddings: tuple[dict[str, object], ...] = tuple(
        {
            "card_id": spec.card_id,
            "checksum": _sha256(_card_text(card).encode()),
            "schema_version": CATEGORY_EMBEDDING_SCHEMA,
            "vector": list(active_category_vectors[ordinal]),
        }
        for ordinal, (spec, card) in enumerate(zip(_CARDS, cards, strict=True))
    )
    item_embeddings: tuple[dict[str, object], ...] = tuple(
        {
            "checksum": _sha256(_item_text(spec).encode()),
            "record_key": spec.product_id,
            "schema_version": ITEM_EMBEDDING_SCHEMA,
            "vector": list(active_item_vectors[ordinal]),
        }
        for ordinal, spec in enumerate(_PRODUCTS)
    )
    shipping_rules = _shipping_rules()
    payloads = {
        "category_cards": _jsonl_bytes(cards),
        "category_embeddings": _jsonl_bytes(category_embeddings),
        "item_embeddings": _jsonl_bytes(item_embeddings),
        "shipping_rules": _json_bytes(shipping_rules),
    }
    snapshot_manifest_payload = _json_bytes(snapshot_manifest)
    raw_snapshot_files = snapshot_manifest["files"]
    if not isinstance(raw_snapshot_files, dict):
        raise TypeError("snapshot manifest files must be an object")
    snapshot_file_specs = cast("dict[str, dict[str, object]]", raw_snapshot_files)
    snapshot_files = {
        "manifest": {
            "path": "manifest.json",
            "record_count": 1,
            "sha256": _sha256(snapshot_manifest_payload),
        },
        **{
            role: {
                "path": str(snapshot_file_specs[role]["path"]),
                "record_count": cast("int", snapshot_file_specs[role]["record_count"]),
                "sha256": str(snapshot_file_specs[role]["sha256"]),
            }
            for role in ("products", "offers", "evidence", "exchange_rates")
        },
    }
    platform_records: dict[str, object] = {
        platform: {
            "provider_ids": [f"{platform}-demo"],
            "record_keys": [spec.product_id for spec in _PRODUCTS if spec.platform == platform],
        }
        for platform in PLATFORMS
    }
    raw_shipping_rules = shipping_rules["rules"]
    if not isinstance(raw_shipping_rules, list):
        raise TypeError("shipping rules must be a list")
    manifest: dict[str, object] = {
        "agent_files": {
            role: {
                "path": filename,
                "record_count": (
                    len(payloads[role].splitlines())
                    if role != "shipping_rules"
                    else len(raw_shipping_rules)
                ),
                "sha256": _sha256(payloads[role]),
            }
            for role, filename in (
                ("category_cards", "category_cards.jsonl"),
                ("category_embeddings", "category_embeddings.jsonl"),
                ("item_embeddings", "item_embeddings.jsonl"),
                ("shipping_rules", "shipping_rules.json"),
            )
        },
        "card_sources": [
            {
                "captured_at": card["captured_at"],
                "card_id": card["card_id"],
                "source_confidence": card["source_confidence"],
                "source_domain": card["source_domain"],
                "source_url": card["source_url"],
            }
            for card in cards
        ],
        "category_coverage": list(CATEGORY_COVERAGE),
        "created_at": CREATED_AT,
        "data_mode": "DEMO_SNAPSHOT",
        "embedding": {
            "dimensions": EMBEDDING_DIMENSIONS,
            "model": EMBEDDING_MODEL,
            "provenance": embedding_provenance,
        },
        "generator": {
            "name": GENERATOR_NAME,
            "version": GENERATOR_VERSION,
        },
        "index_version": INDEX_VERSION,
        "non_live_statement": (
            "Versioned deterministic demo data; not a real-time marketplace API."
        ),
        "platforms": platform_records,
        "reducer": {
            "bm25_b": "0.75",
            "bm25_k1": "1.2",
            "lexical_top_k": 30,
            "normalization": "NFKC_ASCII_LOWER_CJK_BIGRAM",
            "rrf_k": 60,
            "vector_top_k": 30,
        },
        "ruleset_version": RULESET_VERSION,
        "schema_version": AGENT_MANIFEST_SCHEMA,
        "snapshot_files": snapshot_files,
        "snapshot_version": SNAPSHOT_VERSION,
    }
    return payloads, manifest


def _expected_assets() -> tuple[dict[str, bytes], dict[str, bytes]]:
    snapshot_payloads, snapshot_manifest = _snapshot_payloads()
    agent_payloads, agent_manifest = _agent_payloads(
        snapshot_payloads,
        snapshot_manifest,
    )
    return (
        {
            **snapshot_payloads,
            "manifest": _json_bytes(snapshot_manifest),
        },
        {
            **agent_payloads,
            "manifest": _json_bytes(agent_manifest),
        },
    )


def _write_assets(
    directory: Path,
    payloads: dict[str, bytes],
    filenames: dict[str, str],
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for role, filename in filenames.items():
        (directory / filename).write_bytes(payloads[role])


def generate_assets(snapshot_dir: Path, agent_dir: Path) -> None:
    """Write both hash-closed M1d asset directories."""

    if not isinstance(snapshot_dir, Path) or not isinstance(agent_dir, Path):
        raise TypeError("asset output directories must be Paths")
    snapshot_payloads, agent_payloads = _expected_assets()
    _write_assets(
        snapshot_dir,
        snapshot_payloads,
        {
            "products": "products.jsonl",
            "offers": "offers.jsonl",
            "evidence": "evidence.jsonl",
            "exchange_rates": "exchange_rates.json",
            "manifest": "manifest.json",
        },
    )
    _write_assets(
        agent_dir,
        agent_payloads,
        {
            "category_cards": "category_cards.jsonl",
            "category_embeddings": "category_embeddings.jsonl",
            "item_embeddings": "item_embeddings.jsonl",
            "shipping_rules": "shipping_rules.json",
            "manifest": "manifest.json",
        },
    )


async def build_dashscope_agent_assets(
    snapshot_dir: Path,
    agent_dir: Path,
    *,
    embedding_port: EmbeddingPort | None = None,
) -> None:
    """Build all agent vectors through DashScope and publish only after validation."""

    if not isinstance(snapshot_dir, Path) or not isinstance(agent_dir, Path):
        raise TypeError("asset directories must be Paths")
    if not snapshot_dir.is_dir():
        raise ValueError("snapshot input directory is unavailable")
    if snapshot_dir.resolve() == agent_dir.resolve():
        raise ValueError("snapshot and agent directories must be distinct")
    if agent_dir.is_symlink() or (agent_dir.exists() and not agent_dir.is_dir()):
        raise ValueError("agent output must be a directory, not a symlink")

    cards = tuple(_card_record(spec) for spec in _CARDS)
    category_texts = tuple(_card_text(card) for card in cards)
    item_texts = tuple(_item_text(spec) for spec in _PRODUCTS)
    active_port = build_dashscope_embedding() if embedding_port is None else embedding_port
    all_vectors = await _embed_all(
        active_port,
        texts=(*category_texts, *item_texts),
    )
    category_vectors = all_vectors[: len(category_texts)]
    item_vectors = all_vectors[len(category_texts) :]

    snapshot_payloads, snapshot_manifest = _snapshot_payloads()
    agent_payloads, agent_manifest = _agent_payloads(
        snapshot_payloads,
        snapshot_manifest,
        category_vectors=category_vectors,
        item_vectors=item_vectors,
        embedding_provenance=DASHSCOPE_EMBEDDING_PROVENANCE,
    )
    complete_agent_payloads = {
        **agent_payloads,
        "manifest": _json_bytes(agent_manifest),
    }

    agent_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".glodex-m1d-dashscope-",
        dir=agent_dir.parent,
    ) as raw:
        staging_root = Path(raw)
        validation_snapshot_root = staging_root / "snapshots"
        validation_agent_root = staging_root / "agent"
        staged_snapshot = validation_snapshot_root / SNAPSHOT_VERSION
        staged_agent = validation_agent_root / SNAPSHOT_VERSION
        shutil.copytree(snapshot_dir, staged_snapshot)
        _write_assets(
            staged_agent,
            complete_agent_payloads,
            {
                "category_cards": "category_cards.jsonl",
                "category_embeddings": "category_embeddings.jsonl",
                "item_embeddings": "item_embeddings.jsonl",
                "shipping_rules": "shipping_rules.json",
                "manifest": "manifest.json",
            },
        )
        await load_agent_indexes(
            snapshot_root=validation_snapshot_root,
            agent_root=validation_agent_root,
        )
        _publish_validated_directory(
            staged=staged_agent,
            destination=agent_dir,
            backup=staging_root / "previous-agent-output",
        )


async def _embed_all(
    port: EmbeddingPort,
    *,
    texts: tuple[str, ...],
) -> tuple[tuple[float, ...], ...]:
    vectors: list[tuple[float, ...]] = []
    for offset in range(0, len(texts), 2):
        batch = EmbeddingBatch(texts=texts[offset : offset + 2])
        try:
            result = await port.embed(batch)
        except ToolPortError:
            raise
        except Exception:
            raise ToolPortError(ToolFailureCode.PROVIDER_UNAVAILABLE) from None
        if type(result) is not EmbeddingResult or len(result.vectors) != len(batch.texts):
            raise ToolPortError(ToolFailureCode.PROVIDER_RESPONSE_INVALID)
        vectors.extend(result.vectors)
    if len(vectors) != len(texts):
        raise ToolPortError(ToolFailureCode.PROVIDER_RESPONSE_INVALID)
    return tuple(vectors)


def _publish_validated_directory(
    *,
    staged: Path,
    destination: Path,
    backup: Path,
) -> None:
    had_previous = destination.exists()
    if had_previous:
        destination.replace(backup)
    try:
        staged.replace(destination)
    except BaseException:
        if had_previous:
            backup.replace(destination)
        raise
    if had_previous:
        shutil.rmtree(backup)


def verify_assets(snapshot_dir: Path, agent_dir: Path) -> None:
    """Verify deterministic or operator-built assets without any live call."""

    with tempfile.TemporaryDirectory(prefix="glodex-m1d-assets-") as raw:
        root = Path(raw)
        expected_snapshot = root / "snapshots" / SNAPSHOT_VERSION
        expected_agent = root / "agent" / SNAPSHOT_VERSION
        generate_assets(expected_snapshot, expected_agent)
        _require_equal_asset_directories(
            snapshot_dir,
            expected_snapshot,
            mismatch_message="snapshot bytes do not match the deterministic build",
        )
        indexes = asyncio.run(
            _load_copied_indexes(
                snapshot_dir,
                agent_dir,
                validation_root=root / "validation",
            )
        )
        if indexes.embedding_provenance == DETERMINISTIC_EMBEDDING_PROVENANCE:
            _require_equal_asset_directories(
                agent_dir,
                expected_agent,
                mismatch_message="agent bytes do not match the deterministic build",
            )
            return
        if indexes.embedding_provenance != DASHSCOPE_EMBEDDING_PROVENANCE:
            raise ValueError("unsupported embedding provenance")

        snapshot_payloads, snapshot_manifest = _snapshot_payloads()
        category_vectors = _read_embedding_vectors(agent_dir / "category_embeddings.jsonl")
        item_vectors = _read_embedding_vectors(agent_dir / "item_embeddings.jsonl")
        operator_payloads, operator_manifest = _agent_payloads(
            snapshot_payloads,
            snapshot_manifest,
            category_vectors=category_vectors,
            item_vectors=item_vectors,
            embedding_provenance=DASHSCOPE_EMBEDDING_PROVENANCE,
        )
        expected_operator = root / "operator" / SNAPSHOT_VERSION
        _write_assets(
            expected_operator,
            {**operator_payloads, "manifest": _json_bytes(operator_manifest)},
            {
                "category_cards": "category_cards.jsonl",
                "category_embeddings": "category_embeddings.jsonl",
                "item_embeddings": "item_embeddings.jsonl",
                "shipping_rules": "shipping_rules.json",
                "manifest": "manifest.json",
            },
        )
        _require_equal_asset_directories(
            agent_dir,
            expected_operator,
            mismatch_message="agent bytes do not match the operator build contract",
        )


async def _load_copied_indexes(
    snapshot_dir: Path,
    agent_dir: Path,
    *,
    validation_root: Path,
) -> AgentIndexes:
    snapshot_root = validation_root / "snapshots"
    agent_root = validation_root / "agent"
    shutil.copytree(snapshot_dir, snapshot_root / SNAPSHOT_VERSION)
    shutil.copytree(agent_dir, agent_root / SNAPSHOT_VERSION)
    return await load_agent_indexes(
        snapshot_root=snapshot_root,
        agent_root=agent_root,
    )


def _read_embedding_vectors(path: Path) -> tuple[tuple[float, ...], ...]:
    vectors: list[tuple[float, ...]] = []
    for line in path.read_bytes().splitlines():
        record = json.loads(line)
        if type(record) is not dict:
            raise ValueError("embedding record must be an object")
        vector = cast("dict[str, object]", record).get("vector")
        if type(vector) is not list:
            raise ValueError("embedding vector must be a list")
        vectors.append(cast("tuple[float, ...]", tuple(vector)))
    return tuple(vectors)


def _require_equal_asset_directories(
    actual: Path,
    expected: Path,
    *,
    mismatch_message: str,
) -> None:
    actual_names = {path.name for path in actual.iterdir() if path.is_file()}
    expected_names = {path.name for path in expected.iterdir() if path.is_file()}
    if actual_names != expected_names:
        raise ValueError("asset file inventory does not match the expected build")
    for name in sorted(expected_names):
        if (actual / name).read_bytes() != (expected / name).read_bytes():
            raise ValueError(mismatch_message)


def main(argv: list[str] | None = None) -> int:
    repository_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot-output",
        type=Path,
        default=repository_root / "data" / "snapshots" / SNAPSHOT_VERSION,
    )
    parser.add_argument(
        "--agent-output",
        type=Path,
        default=repository_root / "data" / "agent" / SNAPSHOT_VERSION,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="verify existing assets instead of replacing them",
    )
    mode.add_argument(
        "--build-dashscope-embeddings",
        action="store_true",
        help=(
            "operator-only: build every agent embedding through DashScope, "
            "validate, then replace only --agent-output"
        ),
    )
    args = parser.parse_args(argv)
    if args.check:
        verify_assets(args.snapshot_output, args.agent_output)
    elif args.build_dashscope_embeddings:
        asyncio.run(
            build_dashscope_agent_assets(
                args.snapshot_output,
                args.agent_output,
            )
        )
    else:
        generate_assets(args.snapshot_output, args.agent_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
