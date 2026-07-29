from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import urlsplit

import pytest
from pydantic import TypeAdapter

from glodex.capture.contracts import CaptureIssueCode
from glodex.capture.ebay_mapping import map_ebay_search_page
from glodex.capture.ports import EbayProviderFailure, EbaySearchPage
from glodex.contracts import Identifier
from glodex.domain.catalog import CatalogBatch, aggregate_catalog_batch
from glodex.domain.issues import IssueCode, IssueDisposition
from glodex.domain.pricing import KnownCost, UnknownCost

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec(
        "GLO-M1B-P0-003",
        "GLO-M1B-P0-004",
        "GLO-M1B-P0-005",
        "M1B-AC-002",
        "M1B-AC-003",
        "M1B-AC-004",
        "GLO-M1B-NFR-002",
        "GLO-M1B-NFR-003",
        "GLO-M1B-NFR-005",
    ),
]

_SNAPSHOT_VERSION = "capture-0123456789abcdef0123456789abcdef"
_CAPTURED_AT = datetime(2026, 7, 29, 6, 30, tzinfo=UTC)
_IDENTIFIER_ADAPTER = TypeAdapter(Identifier)


def _item(item_id: str = "synthetic|phone|variant") -> dict[str, object]:
    return {
        "itemId": item_id,
        "title": "Synthetic Phone",
        "itemWebUrl": "https://www.ebay.com/itm/synthetic-phone?tracking=discard",
        "listingMarketplaceId": "EBAY_US",
        "price": {"value": "99.95", "currency": "USD"},
        "shippingOptions": [{"shippingCost": {"value": "3.00", "currency": "USD"}}],
    }


def _map(*items: object) -> CatalogBatch | EbayProviderFailure:
    return map_ebay_search_page(
        EbaySearchPage(item_summaries=items),
        snapshot_version=_SNAPSHOT_VERSION,
        captured_at=_CAPTURED_AT,
    )


def test_item_identity_is_lossless_urlsafe_and_identifier_compatible() -> None:
    item_id = "v1|手机/蓝色?variant=海"

    result = _map(_item(item_id))

    assert isinstance(result, CatalogBatch)
    product_id = result.products[0].product_id
    offer_id = result.offers[0].offer_id
    assert _IDENTIFIER_ADAPTER.validate_python(product_id) == product_id
    assert _IDENTIFIER_ADAPTER.validate_python(offer_id) == offer_id
    encoded = product_id.removeprefix("ebay-browse:product:")
    padding = "=" * (-len(encoded) % 4)
    assert base64.urlsafe_b64decode(encoded + padding).decode() == item_id
    assert "=" not in encoded
    evidence_prefix = f"ev-ebay-{hashlib.sha256(item_id.encode()).hexdigest()[:24]}-"
    assert all(
        evidence.evidence_id.startswith(evidence_prefix)
        for evidence in result.evidence
        if evidence.product_id == result.products[0].product_id
    )

    too_long = _map(_item("界" * 100))
    assert too_long == EbayProviderFailure(
        issue_code=CaptureIssueCode.PROVIDER_RESPONSE_INVALID,
        request_count=2,
        received_record_count=1,
    )


def test_duplicate_item_ids_quarantine_every_occurrence_and_keep_unrelated_items() -> None:
    duplicate_id = "synthetic|duplicate|variant"
    alpha = _item("synthetic|alpha|variant")
    alpha["title"] = "Alpha"
    zeta = _item("synthetic|zeta|variant")
    zeta["title"] = "Zeta"

    result = _map(_item(duplicate_id), zeta, _item(duplicate_id), alpha)

    assert isinstance(result, CatalogBatch)
    assert {product.title for product in result.products} == {"Alpha", "Zeta"}
    assert tuple(product.snapshot_ordinal for product in result.products) == (0, 1)
    assert tuple(offer.snapshot_ordinal for offer in result.offers) == (0, 1)
    assert len(result.quarantine_issues) == 2
    assert tuple(issue.entity_ref for issue in result.quarantine_issues) == (
        "provider-item@0",
        "provider-item@2",
    )
    assert all(
        issue.code is IssueCode.INVALID_RECORD and issue.disposition is IssueDisposition.QUARANTINE
        for issue in result.quarantine_issues
    )
    assert all(
        duplicate_id not in f"{issue.entity_ref}{issue.message}{issue.details!r}"
        for issue in result.quarantine_issues
    )
    aggregated = aggregate_catalog_batch(result)
    assert aggregated.quarantine_issues == result.quarantine_issues


def test_ordinary_invalid_item_is_isolated_once_without_retaining_raw_fields() -> None:
    raw_sentinel = "sentinel-raw-provider-field"
    invalid = _item("synthetic|invalid|variant")
    invalid["itemWebUrl"] = f"https://example.invalid/itm/x?raw={raw_sentinel}"
    invalid["title"] = ""
    valid = _item("synthetic|valid|variant")

    result = _map(invalid, valid)

    assert isinstance(result, CatalogBatch)
    assert len(result.products) == len(result.offers) == 1
    assert len(result.quarantine_issues) == 1
    assert result.quarantine_issues[0].entity_ref == "provider-item@0"
    assert raw_sentinel not in repr(result)
    aggregated = aggregate_catalog_batch(result)
    assert aggregated.quarantine_issues == result.quarantine_issues


def test_source_parser_drops_query_without_caching_the_raw_url() -> None:
    raw_sentinel = "sentinel-query-must-not-remain"
    item = _item()
    item["itemWebUrl"] = f"https://www.ebay.com/itm/synthetic?raw={raw_sentinel}"
    urlsplit.cache_clear()

    result = _map(item)

    assert isinstance(result, CatalogBatch)
    assert result.products[0].source_uri == "https://www.ebay.com/itm/synthetic"
    assert raw_sentinel not in repr(result)
    assert urlsplit.cache_info().currsize == 0


def _missing_item_id(item: dict[str, object]) -> None:
    item.pop("itemId")


def _empty_title(item: dict[str, object]) -> None:
    item["title"] = " "


def _long_title(item: dict[str, object]) -> None:
    item["title"] = "x" * 2_001


def _http_source(item: dict[str, object]) -> None:
    item["itemWebUrl"] = "http://www.ebay.com/itm/synthetic"


def _evil_source(item: dict[str, object]) -> None:
    item["itemWebUrl"] = "https://example.invalid/itm/synthetic"


def _userinfo_source(item: dict[str, object]) -> None:
    item["itemWebUrl"] = "https://user@www.ebay.com/itm/synthetic"


def _fragment_source(item: dict[str, object]) -> None:
    item["itemWebUrl"] = "https://www.ebay.com/itm/synthetic#fragment"


def _wrong_market(item: dict[str, object]) -> None:
    item["listingMarketplaceId"] = "EBAY_GB"


def _wrong_currency(item: dict[str, object]) -> None:
    item["price"] = {"value": "99.95", "currency": "EUR"}


def _float_price(item: dict[str, object]) -> None:
    item["price"] = {"value": 99.95, "currency": "USD"}


def _zero_price(item: dict[str, object]) -> None:
    item["price"] = {"value": "0.00", "currency": "USD"}


def _overprecise_price(item: dict[str, object]) -> None:
    item["price"] = {"value": "99.951", "currency": "USD"}


@pytest.mark.parametrize(
    "mutation",
    [
        _missing_item_id,
        _empty_title,
        _long_title,
        _http_source,
        _evil_source,
        _userinfo_source,
        _fragment_source,
        _wrong_market,
        _wrong_currency,
        _float_price,
        _zero_price,
        _overprecise_price,
    ],
)
def test_invalid_required_fields_quarantine_the_whole_item(
    mutation: Callable[[dict[str, object]], None],
) -> None:
    item = _item()
    mutation(item)

    result = _map(item)

    assert result == EbayProviderFailure(
        issue_code=CaptureIssueCode.PROVIDER_RESPONSE_INVALID,
        request_count=2,
        received_record_count=1,
    )


def test_non_object_item_is_a_stable_all_quarantined_failure() -> None:
    result = _map("not-an-object")

    assert result == EbayProviderFailure(
        issue_code=CaptureIssueCode.PROVIDER_RESPONSE_INVALID,
        request_count=2,
        received_record_count=1,
    )


@pytest.mark.parametrize(
    ("shipping_options", "known"),
    [
        (None, False),
        ([], False),
        (
            [
                {"shippingCost": {"value": "3.00", "currency": "USD"}},
                {"shippingCost": {"value": "4.00", "currency": "USD"}},
            ],
            False,
        ),
        (
            [
                {
                    "shippingCostType": "CALCULATED",
                    "shippingCost": {"value": "3.00", "currency": "USD"},
                }
            ],
            False,
        ),
        (
            [{"shippingCost": {"value": "3.00", "currency": "USD"}}],
            True,
        ),
    ],
)
def test_shipping_is_known_only_for_one_explicit_usd_cost(
    shipping_options: object,
    known: bool,
) -> None:
    item = _item()
    if shipping_options is None:
        item.pop("shippingOptions")
    else:
        item["shippingOptions"] = deepcopy(shipping_options)

    result = _map(item)

    assert isinstance(result, CatalogBatch)
    shipping = result.offers[0].cost_components.shipping
    assert isinstance(shipping, KnownCost) is known
    assert isinstance(shipping, UnknownCost) is not known
    if known:
        assert isinstance(shipping, KnownCost)
        assert shipping.amount == Decimal("3.00")
        evidence = next(
            evidence for evidence in result.evidence if evidence.evidence_id == shipping.evidence_id
        )
        assert evidence.field_path == "offer.cost_components.shipping"
