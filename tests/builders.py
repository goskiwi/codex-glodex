"""Adapter-independent builders for immutable domain test data."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from glodex.domain.catalog import (
    CatalogBatch,
    CostComponents,
    EntityKind,
    ExchangeRate,
    ExchangeRateTable,
    Offer,
    Product,
    ProductAttribute,
    StockStatus,
)
from glodex.domain.evidence import EvidenceEntityType, EvidenceRef, FieldEvidence
from glodex.domain.pricing import KnownCost

SNAPSHOT_VERSION = "m0-v1"
CAPTURED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def build_product(
    *,
    snapshot_version: str = SNAPSHOT_VERSION,
    product_id: str = "product-1",
    provider_id: str = "provider-a",
    source_uri: str = "fixture://provider-a/products/product-1",
    title: str = "TravelBook 14",
    category: str = "laptop",
    entity_kind: EntityKind = EntityKind.PRIMARY_PRODUCT,
    snapshot_ordinal: int = 0,
    attributes: tuple[ProductAttribute, ...] | None = None,
    field_evidence: tuple[FieldEvidence, ...] | None = None,
) -> Product:
    """Build one valid raw product record without depending on an adapter."""

    if attributes is None:
        attributes = (
            ProductAttribute(name="weight", value="1.2kg", evidence_id="ev-product-weight"),
        )
    if field_evidence is None:
        field_evidence = (
            FieldEvidence(field_path="product.title", evidence_id="ev-product-title"),
            FieldEvidence(field_path="product.category", evidence_id="ev-product-category"),
            FieldEvidence(field_path="product.entity_kind", evidence_id="ev-product-kind"),
        )
    return Product(
        snapshot_version=snapshot_version,
        product_id=product_id,
        provider_id=provider_id,
        source_uri=source_uri,
        title=title,
        category=category,
        entity_kind=entity_kind,
        snapshot_ordinal=snapshot_ordinal,
        attributes=attributes,
        field_evidence=field_evidence,
    )


def build_offer(
    *,
    snapshot_version: str = SNAPSHOT_VERSION,
    offer_id: str = "offer-1",
    product_id: str = "product-1",
    provider_id: str = "provider-a",
    source_uri: str = "fixture://provider-a/offers/offer-1",
    market: str = "US",
    stock_status: StockStatus = StockStatus.IN_STOCK,
    cost_components: CostComponents | None = None,
    captured_at: datetime = CAPTURED_AT,
    snapshot_ordinal: int = 0,
    field_evidence: tuple[FieldEvidence, ...] | None = None,
) -> Offer:
    """Build one valid offer with explicit raw cost component provenance."""

    if cost_components is None:
        cost_components = CostComponents(
            currency="USD",
            item_price=KnownCost(
                amount=Decimal("699.00"),
                evidence_id="ev-offer-item-price",
            ),
            shipping=KnownCost(
                amount=Decimal("0.00"),
                evidence_id="ev-offer-shipping",
            ),
            tax=KnownCost(
                amount=Decimal("55.92"),
                evidence_id="ev-offer-tax",
            ),
            duty=KnownCost(
                amount=Decimal("0.00"),
                evidence_id="ev-offer-duty",
            ),
        )
    if field_evidence is None:
        field_evidence = (
            FieldEvidence(field_path="offer.inventory", evidence_id="ev-offer-inventory"),
            FieldEvidence(field_path="offer.market", evidence_id="ev-offer-market"),
            FieldEvidence(
                field_path="offer.cost_components.currency",
                evidence_id="ev-offer-currency",
            ),
            FieldEvidence(
                field_path="offer.cost_components.item_price",
                evidence_id="ev-offer-item-price",
            ),
            FieldEvidence(
                field_path="offer.cost_components.shipping",
                evidence_id="ev-offer-shipping",
            ),
            FieldEvidence(field_path="offer.cost_components.tax", evidence_id="ev-offer-tax"),
            FieldEvidence(field_path="offer.cost_components.duty", evidence_id="ev-offer-duty"),
        )
    return Offer(
        snapshot_version=snapshot_version,
        offer_id=offer_id,
        product_id=product_id,
        provider_id=provider_id,
        source_uri=source_uri,
        market=market,
        stock_status=stock_status,
        cost_components=cost_components,
        captured_at=captured_at,
        snapshot_ordinal=snapshot_ordinal,
        field_evidence=field_evidence,
    )


def build_evidence_ref(
    *,
    evidence_id: str = "ev-product-title",
    snapshot_version: str = SNAPSHOT_VERSION,
    entity_type: EvidenceEntityType = EvidenceEntityType.PRODUCT,
    product_id: str | None = "product-1",
    offer_id: str | None = None,
    currency: str | None = None,
    field_path: str = "product.title",
    provider_id: str = "provider-a",
    source_uri: str = "fixture://provider-a/products/product-1",
    captured_at: datetime = CAPTURED_AT,
) -> EvidenceRef:
    """Build one valid evidence record for the selected entity type."""

    return EvidenceRef(
        evidence_id=evidence_id,
        snapshot_version=snapshot_version,
        entity_type=entity_type,
        product_id=product_id,
        offer_id=offer_id,
        currency=currency,
        field_path=field_path,
        provider_id=provider_id,
        source_uri=source_uri,
        captured_at=captured_at,
    )


def build_exchange_rates(
    *,
    snapshot_version: str = SNAPSHOT_VERSION,
) -> ExchangeRateTable:
    """Build the minimal immutable FX table required by a complete fixture."""

    return ExchangeRateTable(
        snapshot_version=snapshot_version,
        base_currency="USD",
        rates=(
            ExchangeRate(
                snapshot_version=snapshot_version,
                currency="USD",
                base_per_unit=Decimal("1"),
                minor_units=2,
                evidence_id="ev-rate-usd",
                snapshot_ordinal=0,
            ),
        ),
    )


def build_catalog_batch(
    *,
    snapshot_version: str = SNAPSHOT_VERSION,
) -> CatalogBatch:
    """Build a complete, evidence-closed single-product catalog batch."""

    product = build_product(snapshot_version=snapshot_version)
    offer = build_offer(snapshot_version=snapshot_version)
    product_refs = (
        build_evidence_ref(snapshot_version=snapshot_version),
        build_evidence_ref(
            evidence_id="ev-product-category",
            snapshot_version=snapshot_version,
            field_path="product.category",
        ),
        build_evidence_ref(
            evidence_id="ev-product-kind",
            snapshot_version=snapshot_version,
            field_path="product.entity_kind",
        ),
        build_evidence_ref(
            evidence_id="ev-product-weight",
            snapshot_version=snapshot_version,
            field_path="product.attributes.weight",
        ),
    )
    offer_refs = tuple(
        build_evidence_ref(
            evidence_id=binding.evidence_id,
            snapshot_version=snapshot_version,
            entity_type=EvidenceEntityType.OFFER,
            product_id=offer.product_id,
            offer_id=offer.offer_id,
            field_path=binding.field_path,
            provider_id=offer.provider_id,
            source_uri=offer.source_uri,
        )
        for binding in offer.field_evidence
    )
    rate_ref = build_evidence_ref(
        evidence_id="ev-rate-usd",
        snapshot_version=snapshot_version,
        entity_type=EvidenceEntityType.EXCHANGE_RATE,
        product_id=None,
        currency="USD",
        field_path="exchange_rate.base_per_unit",
        provider_id="fixture-fx",
        source_uri="fixture://fx/USD",
    )
    return CatalogBatch(
        snapshot_version=snapshot_version,
        products=(product,),
        offers=(offer,),
        evidence=(*product_refs, *offer_refs, rate_ref),
        exchange_rates=build_exchange_rates(snapshot_version=snapshot_version),
    )
