"""Pure eBay item-summary mapping into the existing evidence-closed catalog."""

from __future__ import annotations

import base64
import hashlib
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, DecimalException
from typing import cast
from urllib.parse import SplitResult, urlsplit, urlunsplit

from glodex.capture.config import EBAY_CAPTURE_PROFILE
from glodex.capture.contracts import CaptureId, CaptureIssueCode
from glodex.capture.ports import EbayProviderFailure, EbaySearchPage
from glodex.domain.catalog import (
    CatalogBatch,
    CostComponents,
    ExchangeRate,
    ExchangeRateTable,
    Offer,
    Product,
    StockStatus,
)
from glodex.domain.evidence import EvidenceEntityType, EvidenceRef, FieldEvidence
from glodex.domain.issues import (
    CatalogIssue,
    IssueCode,
    IssueDetail,
    IssueDisposition,
    IssueStage,
)
from glodex.domain.pricing import KnownCost, UnknownCost, canonical_exact_amount

_PROVIDER_ID = "ebay-browse"
_UNKNOWN_NOT_DISCLOSED = "NOT_DISCLOSED"
_UNKNOWN_AMBIGUOUS = "AMBIGUOUS"
_FX_EVIDENCE_ID = "ev-glodex-identity-fx-usd"
_FX_PROVIDER_ID = "glodex-system"
_FX_SOURCE_URI = "urn:glodex:identity-fx:USD"
_uncached_urlsplit = cast(
    Callable[[str], SplitResult],
    getattr(urlsplit, "__wrapped__", urlsplit),
)


@dataclass(frozen=True, slots=True)
class _MappedItem:
    product_id: str
    offer_id: str
    evidence_prefix: str
    source_uri: str
    title: str
    market: str
    item_price: Decimal
    shipping: Decimal | None
    shipping_unknown_reason: str


type EbayMappingResult = CatalogBatch | EbayProviderFailure


def map_ebay_search_page(
    page: EbaySearchPage,
    *,
    snapshot_version: CaptureId,
    captured_at: datetime,
) -> EbayMappingResult:
    """Map one complete page, quarantining records before domain construction."""

    if type(page) is not EbaySearchPage:
        raise TypeError("page must be an EbaySearchPage")

    raw_item_ids = tuple(
        item_id for item in page.item_summaries if (item_id := _item_id(item)) is not None
    )
    duplicate_item_ids = {item_id for item_id, count in Counter(raw_item_ids).items() if count > 1}

    mapped_items: list[_MappedItem] = []
    quarantine_issues: list[CatalogIssue] = []
    for input_ordinal, item in enumerate(page.item_summaries):
        item_id = _item_id(item)
        if item_id is not None and item_id in duplicate_item_ids:
            quarantine_issues.append(_quarantine_issue(input_ordinal, reason="duplicate-item-id"))
            continue
        mapped = _map_item(item)
        if mapped is None:
            quarantine_issues.append(_quarantine_issue(input_ordinal, reason="invalid-item"))
            continue
        mapped_items.append(mapped)

    if page.received_record_count > 0 and not mapped_items:
        return EbayProviderFailure(
            issue_code=CaptureIssueCode.PROVIDER_RESPONSE_INVALID,
            request_count=2,
            received_record_count=page.received_record_count,
        )

    products: list[Product] = []
    offers: list[Offer] = []
    evidence: list[EvidenceRef] = []
    for snapshot_ordinal, mapped in enumerate(
        sorted(mapped_items, key=lambda item: item.product_id)
    ):
        product, offer, item_evidence = _build_domain_item(
            mapped,
            snapshot_version=snapshot_version,
            captured_at=captured_at,
            snapshot_ordinal=snapshot_ordinal,
        )
        products.append(product)
        offers.append(offer)
        evidence.extend(item_evidence)

    fx_evidence = EvidenceRef(
        evidence_id=_FX_EVIDENCE_ID,
        snapshot_version=snapshot_version,
        entity_type=EvidenceEntityType.EXCHANGE_RATE,
        product_id=None,
        offer_id=None,
        currency=EBAY_CAPTURE_PROFILE.currency,
        field_path="exchange_rate.base_per_unit",
        provider_id=_FX_PROVIDER_ID,
        source_uri=_FX_SOURCE_URI,
        captured_at=captured_at,
    )
    evidence.append(fx_evidence)
    exchange_rates = ExchangeRateTable(
        snapshot_version=snapshot_version,
        base_currency=EBAY_CAPTURE_PROFILE.currency,
        rates=(
            ExchangeRate(
                snapshot_version=snapshot_version,
                currency=EBAY_CAPTURE_PROFILE.currency,
                base_per_unit=Decimal("1"),
                minor_units=2,
                evidence_id=_FX_EVIDENCE_ID,
                snapshot_ordinal=0,
            ),
        ),
    )
    return CatalogBatch(
        snapshot_version=snapshot_version,
        products=tuple(products),
        offers=tuple(offers),
        evidence=tuple(evidence),
        exchange_rates=exchange_rates,
        quarantine_issues=tuple(quarantine_issues),
    )


def _item_id(item: object) -> str | None:
    if type(item) is not dict:
        return None
    value = cast(dict[str, object], item).get("itemId")
    return value if type(value) is str else None


def _map_item(item: object) -> _MappedItem | None:
    if type(item) is not dict:
        return None
    root = cast(dict[str, object], item)
    item_id = _item_id(root)
    identity = _map_identity(item_id)
    if identity is None:
        return None
    product_id, offer_id, evidence_prefix = identity

    title = root.get("title")
    if (
        type(title) is not str
        or not title.strip()
        or len(title) > 2_000
        or _has_invalid_scalar(title)
    ):
        return None

    source_uri = _normalized_source_uri(root.get("itemWebUrl"))
    if source_uri is None:
        return None

    if "listingMarketplaceId" not in root:
        market = EBAY_CAPTURE_PROFILE.marketplace
    else:
        raw_market = root["listingMarketplaceId"]
        if type(raw_market) is not str or raw_market != EBAY_CAPTURE_PROFILE.marketplace:
            return None
        market = raw_market

    price = _required_price(root.get("price"))
    if price is None:
        return None
    shipping, shipping_reason = _shipping(root.get("shippingOptions"))
    return _MappedItem(
        product_id=product_id,
        offer_id=offer_id,
        evidence_prefix=evidence_prefix,
        source_uri=source_uri,
        title=title,
        market=market,
        item_price=price,
        shipping=shipping,
        shipping_unknown_reason=shipping_reason,
    )


def _map_identity(item_id: str | None) -> tuple[str, str, str] | None:
    if (
        item_id is None
        or not item_id
        or len(item_id) > 81
        or item_id != item_id.strip()
        or _has_invalid_scalar(item_id)
    ):
        return None
    try:
        raw_item_id = item_id.encode()
    except UnicodeError:
        return None
    encoded = base64.urlsafe_b64encode(raw_item_id).decode("ascii").rstrip("=")
    padding = "=" * (-len(encoded) % 4)
    if base64.urlsafe_b64decode(encoded + padding).decode() != item_id:
        return None

    product_id = f"{_PROVIDER_ID}:product:{encoded}"
    offer_id = f"{_PROVIDER_ID}:offer:{encoded}"
    if len(product_id) > 128 or len(offer_id) > 128:
        return None
    digest = hashlib.sha256(raw_item_id).hexdigest()[:24]
    return product_id, offer_id, f"ev-ebay-{digest}"


def _normalized_source_uri(value: object) -> str | None:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or _has_invalid_scalar(value)
        or any(character.isspace() for character in value)
    ):
        return None
    try:
        split = _uncached_urlsplit(value)
        hostname = split.hostname
        port = split.port
        username = split.username
        password = split.password
    except ValueError:
        return None
    if (
        split.scheme.lower() != "https"
        or hostname not in {"ebay.com", "www.ebay.com"}
        or username is not None
        or password is not None
        or split.fragment
        or not split.path
        or not split.path.startswith("/")
        or port not in {None, 443}
    ):
        return None
    normalized = urlunsplit(("https", hostname, split.path, "", ""))
    return normalized if len(normalized) <= 4_096 else None


def _required_price(value: object) -> Decimal | None:
    if type(value) is not dict:
        return None
    root = cast(dict[str, object], value)
    if root.get("currency") != EBAY_CAPTURE_PROFILE.currency:
        return None
    return _decimal_amount(root.get("value"), allow_zero=False)


def _shipping(value: object) -> tuple[Decimal | None, str]:
    if type(value) is not list:
        return None, _UNKNOWN_NOT_DISCLOSED
    options = cast(list[object], value)
    if len(options) != 1:
        reason = _UNKNOWN_NOT_DISCLOSED if not options else _UNKNOWN_AMBIGUOUS
        return None, reason
    option = options[0]
    if type(option) is not dict:
        return None, _UNKNOWN_NOT_DISCLOSED
    option_root = cast(dict[str, object], option)
    cost_type = option_root.get("shippingCostType")
    if cost_type is not None and cost_type != "FIXED":
        return None, _UNKNOWN_AMBIGUOUS
    cost = option_root.get("shippingCost")
    if type(cost) is not dict:
        return None, _UNKNOWN_NOT_DISCLOSED
    cost_root = cast(dict[str, object], cost)
    if cost_root.get("currency") != EBAY_CAPTURE_PROFILE.currency:
        return None, _UNKNOWN_NOT_DISCLOSED
    amount = _decimal_amount(cost_root.get("value"), allow_zero=True)
    if amount is None:
        return None, _UNKNOWN_NOT_DISCLOSED
    return amount, _UNKNOWN_NOT_DISCLOSED


def _decimal_amount(value: object, *, allow_zero: bool) -> Decimal | None:
    if type(value) is not str or not value or value != value.strip() or _has_invalid_scalar(value):
        return None
    try:
        amount = Decimal(value)
    except (DecimalException, ValueError):
        return None
    if not amount.is_finite() or amount.is_signed():
        return None
    exponent = amount.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -2:
        return None
    if amount.is_zero():
        return Decimal("0.00") if allow_zero else None
    try:
        canonical_exact_amount(amount)
    except (TypeError, ValueError):
        return None
    return amount


def _build_domain_item(
    mapped: _MappedItem,
    *,
    snapshot_version: CaptureId,
    captured_at: datetime,
    snapshot_ordinal: int,
) -> tuple[Product, Offer, tuple[EvidenceRef, ...]]:
    product_fields = (
        ("product.title", "product-title"),
        ("product.category", "product-category"),
        ("product.entity_kind", "product-entity-kind"),
    )
    offer_fields = [
        ("offer.inventory", "offer-inventory"),
        ("offer.market", "offer-market"),
        ("offer.cost_components.currency", "offer-currency"),
        ("offer.cost_components.item_price", "offer-item-price"),
    ]
    if mapped.shipping is not None:
        offer_fields.append(("offer.cost_components.shipping", "offer-shipping"))

    product_bindings = tuple(
        FieldEvidence(
            field_path=field_path,
            evidence_id=f"{mapped.evidence_prefix}-{suffix}",
        )
        for field_path, suffix in product_fields
    )
    offer_bindings = tuple(
        FieldEvidence(
            field_path=field_path,
            evidence_id=f"{mapped.evidence_prefix}-{suffix}",
        )
        for field_path, suffix in offer_fields
    )
    item_price = KnownCost(
        amount=mapped.item_price,
        evidence_id=f"{mapped.evidence_prefix}-offer-item-price",
    )
    if mapped.shipping is None:
        shipping: KnownCost | UnknownCost = UnknownCost(reason=mapped.shipping_unknown_reason)
    else:
        shipping = KnownCost(
            amount=mapped.shipping,
            evidence_id=f"{mapped.evidence_prefix}-offer-shipping",
        )
    costs = CostComponents(
        currency=EBAY_CAPTURE_PROFILE.currency,
        item_price=item_price,
        shipping=shipping,
        tax=UnknownCost(reason=_UNKNOWN_NOT_DISCLOSED),
        duty=UnknownCost(reason=_UNKNOWN_NOT_DISCLOSED),
    )
    product = Product(
        snapshot_version=snapshot_version,
        product_id=mapped.product_id,
        provider_id=_PROVIDER_ID,
        source_uri=mapped.source_uri,
        title=mapped.title,
        category=EBAY_CAPTURE_PROFILE.canonical_category,
        entity_kind=EBAY_CAPTURE_PROFILE.entity_kind,
        snapshot_ordinal=snapshot_ordinal,
        attributes=(),
        field_evidence=product_bindings,
    )
    offer = Offer(
        snapshot_version=snapshot_version,
        offer_id=mapped.offer_id,
        product_id=mapped.product_id,
        provider_id=_PROVIDER_ID,
        source_uri=mapped.source_uri,
        market=mapped.market,
        stock_status=StockStatus.UNKNOWN,
        cost_components=costs,
        captured_at=captured_at,
        snapshot_ordinal=snapshot_ordinal,
        field_evidence=offer_bindings,
    )
    evidence = (
        *(
            _product_evidence(
                binding,
                product=product,
                captured_at=captured_at,
            )
            for binding in product_bindings
        ),
        *(
            _offer_evidence(
                binding,
                offer=offer,
                captured_at=captured_at,
            )
            for binding in offer_bindings
        ),
    )
    return product, offer, evidence


def _product_evidence(
    binding: FieldEvidence,
    *,
    product: Product,
    captured_at: datetime,
) -> EvidenceRef:
    return EvidenceRef(
        evidence_id=binding.evidence_id,
        snapshot_version=product.snapshot_version,
        entity_type=EvidenceEntityType.PRODUCT,
        product_id=product.product_id,
        offer_id=None,
        currency=None,
        field_path=binding.field_path,
        provider_id=product.provider_id,
        source_uri=product.source_uri,
        captured_at=captured_at,
    )


def _offer_evidence(
    binding: FieldEvidence,
    *,
    offer: Offer,
    captured_at: datetime,
) -> EvidenceRef:
    return EvidenceRef(
        evidence_id=binding.evidence_id,
        snapshot_version=offer.snapshot_version,
        entity_type=EvidenceEntityType.OFFER,
        product_id=offer.product_id,
        offer_id=offer.offer_id,
        currency=None,
        field_path=binding.field_path,
        provider_id=offer.provider_id,
        source_uri=offer.source_uri,
        captured_at=captured_at,
    )


def _quarantine_issue(input_ordinal: int, *, reason: str) -> CatalogIssue:
    return CatalogIssue(
        code=IssueCode.INVALID_RECORD,
        stage=IssueStage.PRODUCTS,
        disposition=IssueDisposition.QUARANTINE,
        message="invalid provider item",
        entity_ref=f"provider-item@{input_ordinal}",
        details=(IssueDetail(key="reason", value=reason),),
    )


def _has_invalid_scalar(value: str) -> bool:
    return any(
        ord(character) < 0x20 or ord(character) == 0x7F or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    )


__all__ = ["EbayMappingResult", "map_ebay_search_page"]
