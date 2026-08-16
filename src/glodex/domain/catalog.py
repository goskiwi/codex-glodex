"""Immutable raw catalog, offer, FX, and evidence-closed batch models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from glodex.domain._validation import (
    require_currency as _require_currency,
)
from glodex.domain._validation import (
    require_text as _require_text,
)
from glodex.domain._validation import (
    require_utc as _require_utc,
)
from glodex.domain.evidence import EvidenceEntityType, EvidenceRef, FieldEvidence
from glodex.domain.issues import (
    CatalogIssue,
    IssueCode,
    IssueDetail,
    IssueDisposition,
    IssueStage,
)
from glodex.domain.pricing import CostBreakdown, KnownCost, UnknownCost


class StockStatus(StrEnum):
    IN_STOCK = "IN_STOCK"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    UNKNOWN = "UNKNOWN"


class EntityKind(StrEnum):
    PRIMARY_PRODUCT = "PRIMARY_PRODUCT"
    ACCESSORY = "ACCESSORY"
    REPLACEMENT_PART = "REPLACEMENT_PART"
    DECORATION = "DECORATION"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ProductAttribute:
    name: str
    value: str
    evidence_id: str

    def __post_init__(self) -> None:
        _require_text(self.name, "attribute name", maximum=128)
        _require_text(self.value, "attribute value", maximum=16_384)
        _require_text(self.evidence_id, "attribute evidence ID", maximum=128)


@dataclass(frozen=True, slots=True)
class CostComponents(CostBreakdown):
    """Nominal catalog bridge for lossless known/unknown pricing components."""


@dataclass(frozen=True, slots=True)
class Product:
    snapshot_version: str
    product_id: str
    provider_id: str
    source_uri: str
    title: str
    category: str
    entity_kind: EntityKind
    snapshot_ordinal: int
    attributes: tuple[ProductAttribute, ...] = ()
    field_evidence: tuple[FieldEvidence, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.snapshot_version, "snapshot version", maximum=128)
        _require_text(self.product_id, "product ID", maximum=128)
        _require_text(self.provider_id, "provider ID", maximum=128)
        _require_text(self.source_uri, "source URI", maximum=4_096)
        _require_text(self.title, "product title", maximum=16_384)
        _require_text(self.category, "product category", maximum=128)
        if type(self.entity_kind) is not EntityKind:
            raise TypeError("entity_kind must be an EntityKind")
        _require_ordinal(self.snapshot_ordinal, "product snapshot ordinal")
        _require_tuple(self.attributes, ProductAttribute, "product attributes")
        _require_tuple(self.field_evidence, FieldEvidence, "product field evidence")
        _require_unique(
            tuple(attribute.name for attribute in self.attributes),
            "product attribute names",
        )
        _require_unique(
            tuple(binding.field_path for binding in self.field_evidence),
            "product field evidence paths",
        )
        required_paths = {
            "product.title",
            "product.category",
            "product.entity_kind",
        }
        actual_paths = {binding.field_path for binding in self.field_evidence}
        if not required_paths.issubset(actual_paths):
            raise ValueError("product core fields require field evidence")


@dataclass(frozen=True, slots=True)
class Offer:
    snapshot_version: str
    offer_id: str
    product_id: str
    provider_id: str
    source_uri: str
    market: str
    stock_status: StockStatus
    cost_components: CostComponents
    captured_at: datetime
    snapshot_ordinal: int
    field_evidence: tuple[FieldEvidence, ...] = ()
    delivery_days_min: int | None = None
    delivery_days_max: int | None = None

    def __post_init__(self) -> None:
        _require_text(self.snapshot_version, "snapshot version", maximum=128)
        _require_text(self.offer_id, "offer ID", maximum=128)
        _require_text(self.product_id, "product ID", maximum=128)
        _require_text(self.provider_id, "provider ID", maximum=128)
        _require_text(self.source_uri, "source URI", maximum=4_096)
        _require_text(self.market, "offer market", maximum=128)
        if type(self.stock_status) is not StockStatus:
            raise TypeError("stock_status must be a StockStatus")
        if type(self.cost_components) is not CostComponents:
            raise TypeError("cost_components must be CostComponents")
        CostBreakdown.__post_init__(self.cost_components)
        _require_utc(self.captured_at, "offer captured_at")
        _require_ordinal(self.snapshot_ordinal, "offer snapshot ordinal")
        _require_tuple(self.field_evidence, FieldEvidence, "offer field evidence")
        paths = tuple(binding.field_path for binding in self.field_evidence)
        _require_unique(paths, "offer field evidence paths")
        evidence_by_path = {
            binding.field_path: binding.evidence_id for binding in self.field_evidence
        }
        required_paths = {
            "offer.inventory",
            "offer.market",
            "offer.cost_components.currency",
        }
        missing_paths = required_paths.difference(paths)
        if missing_paths:
            missing = ", ".join(sorted(missing_paths))
            raise ValueError(f"offer fields require field evidence: {missing}")
        for component, cost in self.cost_components.items():
            path = f"offer.cost_components.{component}"
            if type(cost) is KnownCost:
                if evidence_by_path.get(path) != cost.evidence_id:
                    raise ValueError(f"known {component} requires matching field evidence")
            elif type(cost) is UnknownCost and path in evidence_by_path:
                raise ValueError(f"unknown {component} cannot have field evidence")
        delivery_values = (self.delivery_days_min, self.delivery_days_max)
        if (self.delivery_days_min is None) is not (self.delivery_days_max is None):
            raise ValueError("offer delivery bounds must both be present or absent")
        if any(
            type(value) is not int or isinstance(value, bool) or value < 0
            for value in delivery_values
            if value is not None
        ):
            raise ValueError("offer delivery bounds must be non-negative integers")
        if (
            self.delivery_days_min is not None
            and self.delivery_days_max is not None
            and self.delivery_days_max < self.delivery_days_min
        ):
            raise ValueError("offer delivery maximum cannot precede minimum")
        delivery_paths = {
            "offer.delivery_days_min",
            "offer.delivery_days_max",
        }
        if self.delivery_days_min is None:
            if delivery_paths.intersection(evidence_by_path):
                raise ValueError("unknown offer delivery cannot have field evidence")
        elif not delivery_paths.issubset(evidence_by_path):
            raise ValueError("known offer delivery requires matching field evidence")


@dataclass(frozen=True, slots=True)
class ExchangeRate:
    snapshot_version: str
    currency: str
    base_per_unit: Decimal
    minor_units: int
    evidence_id: str
    snapshot_ordinal: int

    def __post_init__(self) -> None:
        _require_text(self.snapshot_version, "snapshot version", maximum=128)
        _require_currency(self.currency)
        if (
            type(self.base_per_unit) is not Decimal
            or not self.base_per_unit.is_finite()
            or self.base_per_unit <= 0
        ):
            raise ValueError("base_per_unit must be a finite positive Decimal")
        if (
            type(self.minor_units) is not int
            or isinstance(self.minor_units, bool)
            or not 0 <= self.minor_units <= 4
        ):
            raise ValueError("minor_units must be an integer from zero to four")
        _require_text(self.evidence_id, "exchange-rate evidence ID", maximum=128)
        _require_ordinal(self.snapshot_ordinal, "exchange-rate snapshot ordinal")


@dataclass(frozen=True, slots=True)
class ExchangeRateTable:
    snapshot_version: str
    base_currency: str
    rates: tuple[ExchangeRate, ...]

    def __post_init__(self) -> None:
        _require_text(self.snapshot_version, "snapshot version", maximum=128)
        _require_currency(self.base_currency)
        _require_tuple(self.rates, ExchangeRate, "exchange rates")
        currencies = tuple(rate.currency for rate in self.rates)
        _require_unique(currencies, "exchange-rate currencies")
        if self.base_currency not in currencies:
            raise ValueError("exchange rates must include the base currency")
        if any(rate.snapshot_version != self.snapshot_version for rate in self.rates):
            raise ValueError("exchange-rate snapshot mismatch")
        base_rate = next(rate for rate in self.rates if rate.currency == self.base_currency)
        if base_rate.base_per_unit != Decimal(1):
            raise ValueError("base currency rate must equal one")

    @property
    def supported_currencies(self) -> frozenset[str]:
        return frozenset(rate.currency for rate in self.rates)


@dataclass(frozen=True, slots=True)
class CatalogBatch:
    snapshot_version: str
    products: tuple[Product, ...] = ()
    offers: tuple[Offer, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()
    exchange_rates: ExchangeRateTable | None = None
    quarantine_issues: tuple[CatalogIssue, ...] = ()
    fatal_issues: tuple[CatalogIssue, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.snapshot_version, "snapshot version", maximum=128)
        _require_tuple(self.products, Product, "catalog products")
        _require_tuple(self.offers, Offer, "catalog offers")
        _require_tuple(self.evidence, EvidenceRef, "catalog evidence")
        _require_tuple(
            self.quarantine_issues,
            CatalogIssue,
            "catalog quarantine issues",
        )
        _require_tuple(self.fatal_issues, CatalogIssue, "catalog fatal issues")
        if self.exchange_rates is not None and type(self.exchange_rates) is not ExchangeRateTable:
            raise TypeError("exchange_rates must be an ExchangeRateTable or None")
        if any(
            issue.disposition is not IssueDisposition.QUARANTINE for issue in self.quarantine_issues
        ):
            raise ValueError("quarantine issues must use the quarantine disposition")
        if any(issue.disposition is not IssueDisposition.FATAL for issue in self.fatal_issues):
            raise ValueError("fatal issues must use the fatal disposition")
        if self.fatal_issues and (
            self.products or self.offers or self.evidence or self.exchange_rates is not None
        ):
            raise ValueError("fatal catalog outcome cannot contain partial data")

        self._validate_snapshots()
        self._validate_evidence_closure()

    def _validate_snapshots(self) -> None:
        if any(product.snapshot_version != self.snapshot_version for product in self.products):
            raise ValueError("product snapshot mismatch")
        if any(offer.snapshot_version != self.snapshot_version for offer in self.offers):
            raise ValueError("offer snapshot mismatch")
        if any(evidence.snapshot_version != self.snapshot_version for evidence in self.evidence):
            raise ValueError("evidence snapshot mismatch")
        if (
            self.exchange_rates is not None
            and self.exchange_rates.snapshot_version != self.snapshot_version
        ):
            raise ValueError("exchange-rate table snapshot mismatch")

    def _validate_evidence_closure(self) -> None:
        evidence_ids = tuple(item.evidence_id for item in self.evidence)
        _require_unique(evidence_ids, "duplicate evidence IDs")
        by_id = {item.evidence_id: item for item in self.evidence}

        product_ids = {product.product_id for product in self.products}
        for offer in self.offers:
            if offer.product_id not in product_ids:
                raise ValueError("offer references an unknown product")

        for product in self.products:
            bindings = (
                *product.field_evidence,
                *(
                    FieldEvidence(
                        field_path=f"product.attributes.{attribute.name}",
                        evidence_id=attribute.evidence_id,
                    )
                    for attribute in product.attributes
                ),
            )
            for binding in bindings:
                evidence = _known_evidence(by_id, binding.evidence_id)
                if (
                    evidence.entity_type is not EvidenceEntityType.PRODUCT
                    or evidence.product_id != product.product_id
                    or evidence.provider_id != product.provider_id
                    or evidence.source_uri != product.source_uri
                    or evidence.field_path != binding.field_path
                ):
                    raise ValueError("product evidence entity or field mismatch")

        for offer in self.offers:
            for binding in offer.field_evidence:
                evidence = _known_evidence(by_id, binding.evidence_id)
                if (
                    evidence.entity_type is not EvidenceEntityType.OFFER
                    or evidence.product_id != offer.product_id
                    or evidence.offer_id != offer.offer_id
                    or evidence.provider_id != offer.provider_id
                    or evidence.source_uri != offer.source_uri
                    or evidence.field_path != binding.field_path
                ):
                    raise ValueError("offer evidence entity or field mismatch")

        if self.exchange_rates is not None:
            for rate in self.exchange_rates.rates:
                evidence = _known_evidence(by_id, rate.evidence_id)
                if (
                    evidence.entity_type is not EvidenceEntityType.EXCHANGE_RATE
                    or evidence.currency != rate.currency
                    or evidence.field_path != "exchange_rate.base_per_unit"
                ):
                    raise ValueError("exchange-rate evidence entity or field mismatch")


@dataclass(frozen=True, slots=True)
class ProductSource:
    """One raw source retained by a canonical product."""

    provider_id: str
    source_uri: str
    snapshot_ordinal: int
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.provider_id, "provider ID", maximum=128)
        _require_text(self.source_uri, "source URI", maximum=4_096)
        _require_ordinal(self.snapshot_ordinal, "product source snapshot ordinal")
        _require_tuple(self.evidence_ids, str, "product source evidence IDs")
        for evidence_id in self.evidence_ids:
            _require_text(evidence_id, "product source evidence ID", maximum=128)
        _require_unique(self.evidence_ids, "product source evidence IDs")


@dataclass(frozen=True, slots=True)
class CanonicalAttribute:
    """One non-conflicting attribute with every supporting evidence ID."""

    name: str
    value: str
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_text(self.name, "canonical attribute name", maximum=128)
        _require_text(self.value, "canonical attribute value", maximum=16_384)
        _require_tuple(self.evidence_ids, str, "canonical attribute evidence IDs")
        if not self.evidence_ids:
            raise ValueError("canonical attribute requires evidence")
        for evidence_id in self.evidence_ids:
            _require_text(evidence_id, "attribute evidence ID", maximum=128)
        _require_unique(self.evidence_ids, "canonical attribute evidence IDs")


@dataclass(frozen=True, slots=True)
class CanonicalProduct:
    """A deterministic merge of all non-conflicting raw records for a product ID."""

    snapshot_version: str
    product_id: str
    title: str
    category: str
    entity_kind: EntityKind
    snapshot_ordinal: int
    sources: tuple[ProductSource, ...]
    attributes: tuple[CanonicalAttribute, ...]
    field_evidence: tuple[FieldEvidence, ...]

    def __post_init__(self) -> None:
        _require_text(self.snapshot_version, "snapshot version", maximum=128)
        _require_text(self.product_id, "product ID", maximum=128)
        _require_text(self.title, "product title", maximum=16_384)
        _require_text(self.category, "product category", maximum=128)
        if type(self.entity_kind) is not EntityKind:
            raise TypeError("entity_kind must be an EntityKind")
        _require_ordinal(self.snapshot_ordinal, "canonical product snapshot ordinal")
        _require_tuple(self.sources, ProductSource, "canonical product sources")
        _require_tuple(self.attributes, CanonicalAttribute, "canonical product attributes")
        _require_tuple(
            self.field_evidence,
            FieldEvidence,
            "canonical product field evidence",
        )
        if not self.sources:
            raise ValueError("canonical product requires at least one source")
        _require_unique(
            tuple(attribute.name for attribute in self.attributes),
            "canonical product attribute names",
        )
        evidence_pairs = tuple(
            f"{binding.field_path}\0{binding.evidence_id}" for binding in self.field_evidence
        )
        _require_unique(evidence_pairs, "canonical product field evidence bindings")
        canonical_evidence_ids = (
            *(binding.evidence_id for binding in self.field_evidence),
            *(
                evidence_id
                for attribute in self.attributes
                for evidence_id in attribute.evidence_ids
            ),
        )
        _require_unique(canonical_evidence_ids, "canonical product evidence IDs")
        source_evidence_ids = tuple(
            evidence_id for source in self.sources for evidence_id in source.evidence_ids
        )
        if source_evidence_ids:
            if any(not source.evidence_ids for source in self.sources):
                raise ValueError("canonical product provenance must cover every source")
            _require_unique(source_evidence_ids, "canonical source evidence ownership")
            if set(source_evidence_ids) != set(canonical_evidence_ids):
                raise ValueError(
                    "canonical source evidence ownership must exactly cover product evidence"
                )
        required_paths = {
            "product.title",
            "product.category",
            "product.entity_kind",
        }
        actual_paths = {binding.field_path for binding in self.field_evidence}
        if not required_paths.issubset(actual_paths):
            raise ValueError("canonical product core fields require field evidence")


@dataclass(frozen=True, slots=True, order=True)
class OfferIdentity:
    """The provider-scoped identity that must be conserved by aggregation."""

    provider_id: str
    offer_id: str

    def __post_init__(self) -> None:
        _require_text(self.provider_id, "provider ID", maximum=128)
        _require_text(self.offer_id, "offer ID", maximum=128)


@dataclass(frozen=True, slots=True)
class CatalogAggregationResult:
    """Canonical products, conserved legal offers, and stable quarantines."""

    products: tuple[CanonicalProduct, ...]
    offers: tuple[Offer, ...]
    quarantine_issues: tuple[CatalogIssue, ...]
    legal_input_offer_identities: tuple[OfferIdentity, ...]
    source_offer_count: int
    quarantined_offer_count: int

    def __post_init__(self) -> None:
        _require_tuple(self.products, CanonicalProduct, "canonical products")
        _require_tuple(self.offers, Offer, "aggregated offers")
        _require_tuple(
            self.quarantine_issues,
            CatalogIssue,
            "aggregation quarantine issues",
        )
        _require_tuple(
            self.legal_input_offer_identities,
            OfferIdentity,
            "legal input offer identities",
        )
        _require_ordinal(self.source_offer_count, "source offer count")
        _require_ordinal(self.quarantined_offer_count, "quarantined offer count")
        if any(
            issue.disposition is not IssueDisposition.QUARANTINE for issue in self.quarantine_issues
        ):
            raise ValueError("aggregation issues must use the quarantine disposition")
        _require_unique(
            tuple(product.product_id for product in self.products),
            "canonical product IDs",
        )
        _require_unique(
            tuple(
                f"{identity.provider_id}\0{identity.offer_id}"
                for identity in self.legal_input_offer_identities
            ),
            "legal input offer identities",
        )
        if self.source_offer_count != len(self.offers) + self.quarantined_offer_count:
            raise ValueError("source offers must equal conserved plus quarantined offers")
        if not self.offers_conserved:
            raise ValueError("legal offer identities were not conserved")

    @property
    def output_offer_identities(self) -> tuple[OfferIdentity, ...]:
        return tuple(
            sorted(
                OfferIdentity(provider_id=offer.provider_id, offer_id=offer.offer_id)
                for offer in self.offers
            )
        )

    @property
    def offers_conserved(self) -> bool:
        return self.output_offer_identities == self.legal_input_offer_identities


def aggregate_catalog(
    products: tuple[Product, ...],
    offers: tuple[Offer, ...],
) -> CatalogAggregationResult:
    """Purely merge raw products and quarantine unsafe product/offer records."""

    _require_tuple(products, Product, "raw products")
    _require_tuple(offers, Offer, "raw offers")
    snapshot_versions = {
        *(product.snapshot_version for product in products),
        *(offer.snapshot_version for offer in offers),
    }
    if len(snapshot_versions) > 1:
        raise ValueError("cannot aggregate records from multiple snapshot versions")

    product_groups: dict[str, list[Product]] = {}
    for product in products:
        product_groups.setdefault(product.product_id, []).append(product)

    canonical_products: list[CanonicalProduct] = []
    conflicted_product_ids: set[str] = set()
    issues: list[CatalogIssue] = []
    for product_id in sorted(product_groups):
        group = tuple(sorted(product_groups[product_id], key=_product_source_sort_key))
        conflict_fields = _product_conflict_fields(group)
        if conflict_fields:
            conflicted_product_ids.add(product_id)
            issues.append(
                CatalogIssue(
                    code=IssueCode.IDENTITY_CONFLICT,
                    stage=IssueStage.AGGREGATION,
                    disposition=IssueDisposition.QUARANTINE,
                    message="product identity fields conflict across sources",
                    entity_ref=f"product:{product_id}",
                    details=(IssueDetail(key="fields", value=",".join(conflict_fields)),),
                )
            )
            continue
        canonical_products.append(_canonicalize_product_group(group))

    canonical_products.sort(key=lambda product: (product.snapshot_ordinal, product.product_id))
    canonical_product_ids = {product.product_id for product in canonical_products}
    raw_product_ids = set(product_groups)

    offer_groups: dict[OfferIdentity, list[Offer]] = {}
    for offer in offers:
        identity = OfferIdentity(provider_id=offer.provider_id, offer_id=offer.offer_id)
        offer_groups.setdefault(identity, []).append(offer)
    duplicate_identities = {
        identity for identity, identity_offers in offer_groups.items() if len(identity_offers) > 1
    }
    legal_input_identities = tuple(
        sorted(
            identity
            for identity, identity_offers in offer_groups.items()
            if len(identity_offers) == 1
            and identity_offers[0].product_id in raw_product_ids
            and identity_offers[0].product_id not in conflicted_product_ids
        )
    )

    kept_offers: list[Offer] = []
    quarantined_offer_count = 0
    for offer in sorted(offers, key=_offer_sort_key):
        identity = OfferIdentity(provider_id=offer.provider_id, offer_id=offer.offer_id)
        issue = _offer_quarantine_issue(
            offer,
            duplicate=identity in duplicate_identities,
            parent_exists=offer.product_id in raw_product_ids,
            parent_conflicted=offer.product_id in conflicted_product_ids,
        )
        if issue is not None:
            issues.append(issue)
            quarantined_offer_count += 1
            continue
        if offer.product_id not in canonical_product_ids:
            raise AssertionError("non-conflicting offer parent was not canonicalized")
        kept_offers.append(offer)

    kept_offers.sort(key=_offer_sort_key)
    issues.sort(key=_issue_sort_key)
    return CatalogAggregationResult(
        products=tuple(canonical_products),
        offers=tuple(kept_offers),
        quarantine_issues=tuple(issues),
        legal_input_offer_identities=legal_input_identities,
        source_offer_count=len(offers),
        quarantined_offer_count=quarantined_offer_count,
    )


def aggregate_catalog_batch(batch: CatalogBatch) -> CatalogAggregationResult:
    """Aggregate a validated batch while retaining adapter quarantine warnings."""

    if type(batch) is not CatalogBatch:
        raise TypeError("batch must be a CatalogBatch")
    if batch.fatal_issues:
        raise ValueError("cannot aggregate a fatal catalog batch")
    result = aggregate_catalog(batch.products, batch.offers)
    combined_issues = tuple(
        sorted((*batch.quarantine_issues, *result.quarantine_issues), key=_issue_sort_key)
    )
    return CatalogAggregationResult(
        products=result.products,
        offers=result.offers,
        quarantine_issues=combined_issues,
        legal_input_offer_identities=result.legal_input_offer_identities,
        source_offer_count=result.source_offer_count,
        quarantined_offer_count=result.quarantined_offer_count,
    )


def _product_conflict_fields(products: tuple[Product, ...]) -> tuple[str, ...]:
    conflicts: list[str] = []
    if len({product.title for product in products}) > 1:
        conflicts.append("title")
    if len({product.category for product in products}) > 1:
        conflicts.append("category")
    if len({product.entity_kind for product in products}) > 1:
        conflicts.append("entity_kind")

    attribute_values: dict[str, set[str]] = {}
    for product in products:
        for attribute in product.attributes:
            attribute_values.setdefault(attribute.name, set()).add(attribute.value)
    conflicts.extend(
        f"attribute:{name}" for name in sorted(attribute_values) if len(attribute_values[name]) > 1
    )
    return tuple(conflicts)


def _canonicalize_product_group(products: tuple[Product, ...]) -> CanonicalProduct:
    first = products[0]
    attribute_values: dict[str, str] = {}
    attribute_evidence: dict[str, set[str]] = {}
    for product in products:
        for attribute in product.attributes:
            attribute_values[attribute.name] = attribute.value
            attribute_evidence.setdefault(attribute.name, set()).add(attribute.evidence_id)

    attributes = tuple(
        CanonicalAttribute(
            name=name,
            value=attribute_values[name],
            evidence_ids=tuple(sorted(attribute_evidence[name])),
        )
        for name in sorted(attribute_values)
    )
    evidence_pairs = {
        (binding.field_path, binding.evidence_id)
        for product in products
        for binding in product.field_evidence
    }
    field_evidence = tuple(
        FieldEvidence(field_path=field_path, evidence_id=evidence_id)
        for field_path, evidence_id in sorted(evidence_pairs)
    )
    sources = tuple(
        ProductSource(
            provider_id=product.provider_id,
            source_uri=product.source_uri,
            snapshot_ordinal=product.snapshot_ordinal,
            evidence_ids=tuple(
                sorted(
                    (
                        *(binding.evidence_id for binding in product.field_evidence),
                        *(attribute.evidence_id for attribute in product.attributes),
                    )
                )
            ),
        )
        for product in sorted(products, key=_product_source_sort_key)
    )
    return CanonicalProduct(
        snapshot_version=first.snapshot_version,
        product_id=first.product_id,
        title=first.title,
        category=first.category,
        entity_kind=first.entity_kind,
        snapshot_ordinal=min(product.snapshot_ordinal for product in products),
        sources=sources,
        attributes=attributes,
        field_evidence=field_evidence,
    )


def _offer_quarantine_issue(
    offer: Offer,
    *,
    duplicate: bool,
    parent_exists: bool,
    parent_conflicted: bool,
) -> CatalogIssue | None:
    entity_ref = f"offer:{offer.provider_id}/{offer.offer_id}@{offer.snapshot_ordinal}"
    details = (
        IssueDetail(key="product_id", value=offer.product_id),
        IssueDetail(key="snapshot_ordinal", value=offer.snapshot_ordinal),
    )
    if duplicate:
        return CatalogIssue(
            code=IssueCode.DUPLICATE_OFFER,
            stage=IssueStage.AGGREGATION,
            disposition=IssueDisposition.QUARANTINE,
            message="duplicate provider-scoped offer identity",
            entity_ref=entity_ref,
            details=details,
        )
    if not parent_exists:
        return CatalogIssue(
            code=IssueCode.ORPHAN_OFFER,
            stage=IssueStage.AGGREGATION,
            disposition=IssueDisposition.QUARANTINE,
            message="offer references a missing product",
            entity_ref=entity_ref,
            details=details,
        )
    if parent_conflicted:
        return CatalogIssue(
            code=IssueCode.IDENTITY_CONFLICT,
            stage=IssueStage.AGGREGATION,
            disposition=IssueDisposition.QUARANTINE,
            message="offer parent was quarantined for an identity conflict",
            entity_ref=entity_ref,
            details=details,
        )
    return None


def _product_source_sort_key(product: Product) -> tuple[object, ...]:
    return (
        product.snapshot_ordinal,
        product.provider_id,
        product.source_uri,
        product.title,
        product.category,
        product.entity_kind.value,
        tuple(
            (attribute.name, attribute.value, attribute.evidence_id)
            for attribute in product.attributes
        ),
        tuple((binding.field_path, binding.evidence_id) for binding in product.field_evidence),
    )


def _offer_sort_key(offer: Offer) -> tuple[object, ...]:
    return (
        offer.snapshot_ordinal,
        offer.provider_id,
        offer.offer_id,
        offer.product_id,
    )


def _issue_sort_key(issue: CatalogIssue) -> tuple[object, ...]:
    return (
        issue.entity_ref or "",
        issue.code.value,
        issue.stage.value,
        issue.message,
        tuple((detail.key, str(detail.value)) for detail in issue.details),
    )


def _known_evidence(
    by_id: dict[str, EvidenceRef],
    evidence_id: str,
) -> EvidenceRef:
    try:
        return by_id[evidence_id]
    except KeyError as error:
        raise ValueError(f"unknown evidence ID: {evidence_id}") from error


def _require_ordinal(value: object, name: str) -> int:
    if type(value) is not int or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _require_tuple(value: object, item_type: type[object], name: str) -> tuple[object, ...]:
    if type(value) is not tuple or any(type(item) is not item_type for item in value):
        raise TypeError(f"{name} must be a tuple of {item_type.__name__}")
    return value


def _require_unique(values: tuple[str, ...], name: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"{name} must be unique")


__all__ = [
    "CanonicalAttribute",
    "CanonicalProduct",
    "CatalogAggregationResult",
    "CatalogBatch",
    "CostComponents",
    "EntityKind",
    "ExchangeRate",
    "ExchangeRateTable",
    "Offer",
    "OfferIdentity",
    "Product",
    "ProductAttribute",
    "ProductSource",
    "StockStatus",
    "aggregate_catalog",
    "aggregate_catalog_batch",
]
