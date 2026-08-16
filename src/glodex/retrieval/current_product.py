"""Current-product gateway adapter for the shopping Agent."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlsplit

import httpx

from glodex.agent.catalog import CandidateManifest, ManifestRecord
from glodex.agent.contracts import (
    Candidate,
    CandidateAttribute,
    DataMode,
    EmbeddingResult,
    ItemSearchInput,
    ItemSearchRuntimeResult,
    Platform,
    ToolFailureCode,
)
from glodex.agent.ports import ToolPortError
from glodex.domain.catalog import (
    CatalogBatch,
    CostComponents,
    EntityKind,
    ExchangeRate,
    ExchangeRateTable,
    Offer,
    OfferIdentity,
    Product,
    ProductAttribute,
    StockStatus,
)
from glodex.domain.evidence import EvidenceEntityType, EvidenceRef, FieldEvidence
from glodex.domain.pricing import KnownCost

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_GATEWAY_SCHEMA = "glodex.current-product-hybrid-gateway.v12"
_SYNTHETIC_DATA_MODE = "SYNTHETIC_INTERVIEW"
_SYNTHETIC_RULESET = "synthetic-multiplatform-cny-v2"
_SYNTHETIC_SNAPSHOT = "synthetic-interview-commerce-v1"
_SYNTHETIC_CURRENCIES = ("CNY", "USD", "EUR", "SGD")
_CANONICAL_CATEGORY_BY_CARD = {
    "electronics.laptop": "laptop",
    "electronics.tablet": "tablet",
    "electronics.phone": "phone",
    "electronics.headphone": "headphone",
    "electronics.laptop-accessory": "laptop-accessory",
    "electronics.laptop-part": "laptop-part",
}


@dataclass(frozen=True, slots=True)
class CurrentProductGatewayIdentity:
    """Safe identity returned by the loopback current-product gateway."""

    catalog_binding_sha256: str
    item_vectors_manifest_sha256: str
    retrieval_model_manifest_digest: str
    index_alias: str
    search_pipeline: str
    data_mode: str
    commerce_ruleset_version: str

    def __post_init__(self) -> None:
        if any(
            type(value) is not str or _DIGEST.fullmatch(value) is None
            for value in (
                self.catalog_binding_sha256,
                self.item_vectors_manifest_sha256,
                self.retrieval_model_manifest_digest,
            )
        ):
            raise ValueError("current-product gateway digest is invalid")
        if (
            type(self.index_alias) is not str
            or not self.index_alias
            or type(self.search_pipeline) is not str
            or not self.search_pipeline
            or self.data_mode != _SYNTHETIC_DATA_MODE
            or self.commerce_ruleset_version != _SYNTHETIC_RULESET
        ):
            raise ValueError("current-product gateway index identity is invalid")


class CurrentProductItemSource:
    """Convert complete gateway hits without regenerating market facts."""

    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:18085",
        *,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.port is None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("current-product endpoint must be a loopback HTTP URL")
        self._endpoint = "http://127.0.0.1:" + str(parsed.port)
        self._transport = http_transport

    async def health(self) -> CurrentProductGatewayIdentity:
        """Read and strictly validate the serving identity before API bind."""

        try:
            async with httpx.AsyncClient(
                base_url=self._endpoint,
                transport=self._transport,
                timeout=httpx.Timeout(2.0),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.get(
                    "/v1/health",
                    headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                )
            if (
                response.status_code != 200
                or response.headers.get("Content-Encoding") not in {None, "identity"}
                or len(response.content) > 64 * 1024
            ):
                raise ValueError("current-product gateway health failed")
            body = json.loads(response.content)
            if (
                type(body) is not dict
                or set(body)
                != {
                    "schema_version",
                    "status",
                    "data_mode",
                    "commerce_ruleset_version",
                    "catalog_binding_sha256",
                    "item_vectors_manifest_sha256",
                    "retrieval_model_manifest_digest",
                    "candidate_engine",
                    "index_alias",
                    "search_pipeline",
                }
                or body["schema_version"] != _GATEWAY_SCHEMA
                or body["status"] != "ready"
                or body["data_mode"] != _SYNTHETIC_DATA_MODE
                or body["commerce_ruleset_version"] != _SYNTHETIC_RULESET
                or body["candidate_engine"] != "opensearch_hybrid_rerank_fusion"
            ):
                raise ValueError("current-product gateway health identity is invalid")
            return CurrentProductGatewayIdentity(
                catalog_binding_sha256=body["catalog_binding_sha256"],
                item_vectors_manifest_sha256=body["item_vectors_manifest_sha256"],
                retrieval_model_manifest_digest=body["retrieval_model_manifest_digest"],
                index_alias=body["index_alias"],
                search_pipeline=body["search_pipeline"],
                data_mode=body["data_mode"],
                commerce_ruleset_version=body["commerce_ruleset_version"],
            )
        except Exception as error:
            raise ToolPortError(ToolFailureCode.ITEM_SOURCE_INVALID) from error

    async def search(
        self,
        request: ItemSearchInput,
        *,
        query_vector: tuple[float, ...] | None,
        preference_vector: tuple[float, ...] | None,
    ) -> ItemSearchRuntimeResult:
        if type(request) is not ItemSearchInput or query_vector is None:
            raise ToolPortError(ToolFailureCode.ITEM_SOURCE_INVALID)
        try:
            EmbeddingResult(vectors=(query_vector,))
            if preference_vector is not None:
                EmbeddingResult(vectors=(preference_vector,))
                if len(preference_vector) != len(query_vector):
                    raise ValueError("current-product vector dimensions differ")
        except (TypeError, ValueError) as error:
            raise ToolPortError(ToolFailureCode.ITEM_SOURCE_INVALID) from error
        try:
            payload = {
                "query": request.query,
                "top_k": request.top_k,
                "platform": request.platform.value,
                "min_landed_cost_cny": (
                    None
                    if request.min_landed_cost_cny is None
                    else float(request.min_landed_cost_cny)
                ),
                "max_landed_cost_cny": (
                    None
                    if request.max_landed_cost_cny is None
                    else float(request.max_landed_cost_cny)
                ),
                "query_vector": list(query_vector),
                "preference_vector": (
                    None if preference_vector is None else list(preference_vector)
                ),
            }
            async with httpx.AsyncClient(
                base_url=self._endpoint,
                transport=self._transport,
                timeout=httpx.Timeout(15.0),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.post("/v1/hybrid-search", json=payload)
            if response.status_code != 200 or len(response.content) > 8 * 1024 * 1024:
                raise ValueError("current-product gateway rejected the request")
            body = response.json()
            if (
                type(body) is not dict
                or body.get("schema_version") != _GATEWAY_SCHEMA
                or body.get("data_mode") != _SYNTHETIC_DATA_MODE
                or body.get("commerce_ruleset_version") != _SYNTHETIC_RULESET
            ):
                raise ValueError("current-product gateway schema is invalid")
            raw_results = body.get("results")
            if type(raw_results) is not list or len(raw_results) > request.top_k:
                raise ValueError("current-product gateway result list is invalid")
            candidate_pool_count = body.get("candidate_pool_count")
            total_recall = body.get("total_recall")
            truncated = body.get("truncated")
            if (
                type(candidate_pool_count) is not int
                or isinstance(candidate_pool_count, bool)
                or candidate_pool_count < len(raw_results)
                or type(total_recall) is not int
                or isinstance(total_recall, bool)
                or total_recall < candidate_pool_count
                or type(truncated) is not bool
                or truncated is not (total_recall > len(raw_results))
            ):
                raise ValueError("current-product gateway recall metadata is invalid")
            materialized = tuple(
                _materialize_hit(hit, expected_platform=request.platform, ordinal=ordinal)
                for ordinal, hit in enumerate(raw_results)
            )
            candidates = tuple(item[0] for item in materialized)
            products = tuple(item[1] for item in materialized)
            offers = tuple(item[2] for item in materialized)
            evidence = tuple(ref for item in materialized for ref in item[3])
            exchange_rates, fx_evidence = _materialize_exchange_rates(body.get("exchange_rates"))
            batch = CatalogBatch(
                snapshot_version=_SYNTHETIC_SNAPSHOT,
                products=products,
                offers=offers,
                evidence=(*evidence, *fx_evidence),
                exchange_rates=exchange_rates,
            )
            return ItemSearchRuntimeResult(
                platform=request.platform,
                target_query=request.query,
                retrieval_query=request.query,
                candidates=candidates,
                platform_sub_batch=batch,
                total_recall=total_recall,
                returned_before_semantic_filter=len(candidates),
                truncated=truncated,
            )
        except ToolPortError:
            raise
        except Exception:
            raise ToolPortError(ToolFailureCode.ITEM_SOURCE_INVALID) from None


def build_current_product_manifest(
    results: tuple[ItemSearchRuntimeResult, ...],
) -> CandidateManifest:
    """Derive the candidate authority manifest from exact returned facts only."""

    if (
        type(results) is not tuple
        or not results
        or any(type(result) is not ItemSearchRuntimeResult for result in results)
    ):
        raise ValueError("current-product manifest requires item results")
    records_by_ref: dict[str, ManifestRecord] = {}
    snapshots: set[str] = set()
    for result in results:
        batch = result.platform_sub_batch
        snapshots.add(batch.snapshot_version)
        products = {product.product_id: product for product in batch.products}
        offers_by_product: dict[str, list[Offer]] = {}
        for offer in batch.offers:
            offers_by_product.setdefault(offer.product_id, []).append(offer)
        for candidate in result.candidates:
            product = products.get(candidate.item_id)
            offers = offers_by_product.get(candidate.item_id, [])
            if product is None or not offers:
                raise ValueError("current-product manifest facts are incomplete")
            if candidate.source_ref not in {offer.offer_id for offer in offers}:
                raise ValueError("current-product candidate source is not an offer")
            provider_ids = tuple(
                dict.fromkeys((product.provider_id, *(offer.provider_id for offer in offers)))
            )
            record = ManifestRecord(
                record_ref=candidate.record_ref,
                source_ref=candidate.source_ref,
                item_id=candidate.item_id,
                platform=candidate.platform,
                product_id=product.product_id,
                offer_identities=tuple(
                    OfferIdentity(provider_id=offer.provider_id, offer_id=offer.offer_id)
                    for offer in offers
                ),
                provider_ids=provider_ids,
                same_group_id=candidate.same_group_id,
            )
            previous = records_by_ref.get(record.record_ref)
            if previous is not None and previous != record:
                raise ValueError("current-product manifest record facts conflict")
            records_by_ref[record.record_ref] = record
    if len(snapshots) != 1:
        raise ValueError("current-product manifest snapshots differ")
    return CandidateManifest(
        data_mode=DataMode.SYNTHETIC_INTERVIEW,
        snapshot_version=next(iter(snapshots)),
        records=tuple(records_by_ref.values()),
    )


def _materialize_hit(
    value: object,
    *,
    expected_platform: Platform,
    ordinal: int,
) -> tuple[Candidate, Product, Offer, tuple[EvidenceRef, ...]]:
    if type(value) is not dict:
        raise ValueError("current-product hit must be an object")
    hit = value
    document_id = _text(hit.get("document_id"), "document_id", maximum=128)
    title = _text(hit.get("title"), "title", maximum=16_384)[:256]
    source_category = _text(hit.get("product_category"), "product_category", maximum=128)
    category_card_id = _text(hit.get("category_card_id"), "category_card_id", maximum=128)
    category = _CANONICAL_CATEGORY_BY_CARD.get(category_card_id, source_category)
    same_group_id = _text(hit.get("same_product_group_id"), "same_product_group_id", maximum=30)
    product_provider_id = _text(hit.get("product_provider_id"), "product_provider_id", maximum=128)
    provider_id = _text(hit.get("provider_id"), "provider_id", maximum=128)
    offer_id = _text(hit.get("offer_id"), "offer_id", maximum=128)
    source_uri = _text(hit.get("source_uri"), "source_uri", maximum=4_096)
    market = _text(hit.get("market"), "market", maximum=128)
    currency = _text(hit.get("currency"), "currency", maximum=3)
    eta_days_min = _non_negative_integer(hit.get("delivery_days_min"), "delivery_days_min")
    eta_days_max = _non_negative_integer(hit.get("delivery_days_max"), "delivery_days_max")
    if eta_days_max < eta_days_min:
        raise ValueError("delivery ETA maximum cannot precede minimum")
    platform = Platform(_text(hit.get("platform"), "platform", maximum=32))
    if (
        platform is not expected_platform
        or currency not in _SYNTHETIC_CURRENCIES
        or hit.get("commerce_ruleset_version") != _SYNTHETIC_RULESET
    ):
        raise ValueError("current-product hit lineage differs from the request")
    captured_at = _captured_at(hit)
    snapshot_version = _SYNTHETIC_SNAPSHOT
    record_ref = platform.value + "." + document_id
    raw_attributes = hit.get("attributes")
    if type(raw_attributes) is not dict or len(raw_attributes) > 16:
        raise ValueError("current-product attributes are invalid")
    attribute_values: list[tuple[str, str]] = []
    for raw_name, raw_value in raw_attributes.items():
        name = _text(raw_name, "attribute name", maximum=64)
        value = _text(raw_value, "attribute value", maximum=256)
        attribute_values.append((name, value))
    product_paths = (
        "product.title",
        "product.category",
        "product.entity_kind",
    )
    offer_paths = (
        "offer.inventory",
        "offer.market",
        "offer.cost_components.currency",
        "offer.cost_components.item_price",
        "offer.cost_components.shipping",
        "offer.cost_components.tax",
        "offer.cost_components.duty",
        "offer.delivery_days_min",
        "offer.delivery_days_max",
    )
    product_bindings = tuple(
        FieldEvidence(field_path=path, evidence_id=_evidence_id(document_id, path))
        for path in product_paths
    )
    offer_bindings = tuple(
        FieldEvidence(field_path=path, evidence_id=_evidence_id(record_ref, path))
        for path in offer_paths
    )
    entity_kind = EntityKind(_text(hit.get("entity_kind"), "entity_kind", maximum=32))
    product_attributes = tuple(
        ProductAttribute(
            name=name,
            value=value,
            evidence_id=_evidence_id(document_id, f"product.attributes.{name}"),
        )
        for name, value in attribute_values
    )
    # Search rank belongs to this one retrieval observation.  It is not a
    # product or offer fact: the same product may rank differently for each
    # platform/query and must still merge into one cross-platform comparison.
    # Bind snapshot ordinals to stable entity identities instead.
    product_ordinal = _stable_snapshot_ordinal("product", document_id)
    offer_ordinal = _stable_snapshot_ordinal("offer", provider_id, offer_id)
    product = Product(
        snapshot_version=snapshot_version,
        product_id=document_id,
        provider_id=product_provider_id,
        source_uri=source_uri,
        title=title,
        category=category,
        entity_kind=entity_kind,
        snapshot_ordinal=product_ordinal,
        attributes=product_attributes,
        field_evidence=product_bindings,
    )
    costs = {
        name: KnownCost(
            amount=_amount(hit.get(name), name, positive=name == "item_price"),
            evidence_id=_evidence_id(record_ref, "offer.cost_components." + name),
        )
        for name in ("item_price", "shipping", "tax", "duty")
    }
    offer = Offer(
        snapshot_version=snapshot_version,
        offer_id=offer_id,
        product_id=document_id,
        provider_id=provider_id,
        source_uri=source_uri,
        market=market,
        stock_status=StockStatus(_text(hit.get("stock_status"), "stock_status", maximum=32)),
        cost_components=CostComponents(currency=currency, **costs),
        captured_at=captured_at,
        snapshot_ordinal=offer_ordinal,
        field_evidence=offer_bindings,
        delivery_days_min=eta_days_min,
        delivery_days_max=eta_days_max,
    )
    evidence = (
        tuple(
            EvidenceRef(
                evidence_id=binding.evidence_id,
                snapshot_version=snapshot_version,
                entity_type=EvidenceEntityType.PRODUCT,
                product_id=document_id,
                offer_id=None,
                currency=None,
                field_path=binding.field_path,
                provider_id=product_provider_id,
                source_uri=source_uri,
                captured_at=captured_at,
            )
            for binding in product_bindings
        )
        + tuple(
            EvidenceRef(
                evidence_id=attribute.evidence_id,
                snapshot_version=snapshot_version,
                entity_type=EvidenceEntityType.PRODUCT,
                product_id=document_id,
                offer_id=None,
                currency=None,
                field_path=f"product.attributes.{attribute.name}",
                provider_id=product_provider_id,
                source_uri=source_uri,
                captured_at=captured_at,
            )
            for attribute in product_attributes
        )
        + tuple(
            EvidenceRef(
                evidence_id=binding.evidence_id,
                snapshot_version=snapshot_version,
                entity_type=EvidenceEntityType.OFFER,
                product_id=document_id,
                offer_id=offer_id,
                currency=None,
                field_path=binding.field_path,
                provider_id=provider_id,
                source_uri=source_uri,
                captured_at=captured_at,
            )
            for binding in offer_bindings
        )
    )
    candidate = Candidate(
        candidate_id=record_ref,
        item_id=document_id,
        platform=platform,
        title=title,
        price=costs["item_price"].amount,
        currency=currency,
        attributes=tuple(
            CandidateAttribute(name=attribute.name, value=attribute.value)
            for attribute in product_attributes
        ),
        source_ref=offer_id,
        record_ref=record_ref,
        same_group_id=same_group_id,
    )
    return candidate, product, offer, evidence


def _stable_snapshot_ordinal(*identity: str) -> int:
    """Return a deterministic non-negative ordinal for one synthetic entity."""

    material = "\x1f".join(identity).encode("utf-8")
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big") >> 1


def _materialize_exchange_rates(
    value: object,
) -> tuple[ExchangeRateTable, tuple[EvidenceRef, ...]]:
    if type(value) is not list or len(value) != len(_SYNTHETIC_CURRENCIES):
        raise ValueError("synthetic exchange-rate table is invalid")
    rates: list[ExchangeRate] = []
    evidence: list[EvidenceRef] = []
    currencies: list[str] = []
    for ordinal, raw in enumerate(value):
        if type(raw) is not dict or set(raw) != {
            "currency",
            "base_per_unit",
            "minor_units",
            "evidence_id",
            "source_uri",
            "captured_at",
        }:
            raise ValueError("synthetic exchange-rate row is invalid")
        currency = _text(raw.get("currency"), "FX currency", maximum=3)
        if currency not in _SYNTHETIC_CURRENCIES:
            raise ValueError("synthetic FX currency is invalid")
        rate = _amount(raw.get("base_per_unit"), "FX base_per_unit", positive=True)
        evidence_id = _text(raw.get("evidence_id"), "FX evidence_id", maximum=128)
        source_uri = _text(raw.get("source_uri"), "FX source_uri", maximum=4_096)
        captured_at = _captured_at({"captured_at": raw.get("captured_at")})
        if raw.get("minor_units") != 2:
            raise ValueError("synthetic FX minor units are invalid")
        currencies.append(currency)
        rates.append(
            ExchangeRate(
                snapshot_version=_SYNTHETIC_SNAPSHOT,
                currency=currency,
                base_per_unit=rate,
                minor_units=2,
                evidence_id=evidence_id,
                snapshot_ordinal=ordinal,
            )
        )
        evidence.append(
            EvidenceRef(
                evidence_id=evidence_id,
                snapshot_version=_SYNTHETIC_SNAPSHOT,
                entity_type=EvidenceEntityType.EXCHANGE_RATE,
                product_id=None,
                offer_id=None,
                currency=currency,
                field_path="exchange_rate.base_per_unit",
                provider_id="synthetic-interview-fx",
                source_uri=source_uri,
                captured_at=captured_at,
            )
        )
    if tuple(currencies) != _SYNTHETIC_CURRENCIES:
        raise ValueError("synthetic exchange-rate order is invalid")
    return (
        ExchangeRateTable(
            snapshot_version=_SYNTHETIC_SNAPSHOT,
            base_currency="CNY",
            rates=tuple(rates),
        ),
        tuple(evidence),
    )


def _text(value: object, name: str, *, maximum: int) -> str:
    if type(value) is not str or not value or len(value) > maximum or "\0" in value:
        raise ValueError(name + " is invalid")
    return value


def _amount(value: object, name: str, *, positive: bool) -> Decimal:
    if type(value) not in (int, float, str) or isinstance(value, bool):
        raise ValueError(name + " is invalid")
    amount = Decimal(str(value)).quantize(Decimal("0.01"))
    if not amount.is_finite() or amount < 0 or (positive and amount == 0):
        raise ValueError(name + " is invalid")
    return amount


def _non_negative_integer(value: object, name: str) -> int:
    if type(value) is not int or isinstance(value, bool) or value < 0:
        raise ValueError(name + " is invalid")
    return value


def _captured_at(hit: object) -> datetime:
    if type(hit) is not dict:
        raise ValueError("current-product hit is invalid")
    raw = _text(hit.get("captured_at"), "captured_at", maximum=64)
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    offset = value.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise ValueError("captured_at must be UTC")
    return value


def _evidence_id(document_id: str, field_path: str) -> str:
    digest = hashlib.sha256((document_id + "\x1f" + field_path).encode("utf-8")).hexdigest()[:24]
    return "ev-current-" + digest


__all__ = [
    "CurrentProductGatewayIdentity",
    "CurrentProductItemSource",
    "build_current_product_manifest",
]
