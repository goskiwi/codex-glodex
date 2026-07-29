from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from pydantic import TypeAdapter

from glodex.capture.ebay_mapping import map_ebay_search_page
from glodex.capture.ports import EbaySearchPage
from glodex.contracts import Identifier
from glodex.domain.catalog import (
    CatalogBatch,
    EntityKind,
    StockStatus,
    aggregate_catalog_batch,
)
from glodex.domain.evidence import EvidenceEntityType
from glodex.domain.pricing import KnownCost, UnknownCost

pytestmark = [
    pytest.mark.contract,
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

_FIXTURES = Path(__file__).parents[1] / "fixtures"
_SNAPSHOT_VERSION = "capture-0123456789abcdef0123456789abcdef"
_CAPTURED_AT = datetime(2026, 7, 29, 6, 30, tzinfo=UTC)
_IDENTIFIER_ADAPTER = TypeAdapter(Identifier)


def _fixture_page(name: str) -> EbaySearchPage:
    payload = json.loads((_FIXTURES / name).read_text(encoding="utf-8"))
    assert type(payload) is dict
    items = payload["itemSummaries"]
    assert type(items) is list
    return EbaySearchPage(item_summaries=tuple(items))


def test_success_fixture_maps_to_evidence_closed_domain_batch() -> None:
    result = map_ebay_search_page(
        _fixture_page("ebay_search_success.json"),
        snapshot_version=_SNAPSHOT_VERSION,
        captured_at=_CAPTURED_AT,
    )

    assert isinstance(result, CatalogBatch)
    assert len(result.products) == len(result.offers) == 2
    assert result.quarantine_issues == ()
    assert result.fatal_issues == ()
    assert tuple(product.snapshot_ordinal for product in result.products) == (0, 1)
    assert tuple(offer.snapshot_ordinal for offer in result.offers) == (0, 1)
    assert tuple(product.product_id for product in result.products) == tuple(
        sorted(product.product_id for product in result.products)
    )
    for product in result.products:
        assert _IDENTIFIER_ADAPTER.validate_python(product.product_id) == product.product_id
        assert product.provider_id == "ebay-browse"
        assert product.category == "phone"
        assert product.entity_kind is EntityKind.PRIMARY_PRODUCT
        assert product.attributes == ()
        assert "?" not in product.source_uri

    products_by_title = {product.title: product for product in result.products}
    offers_by_product = {offer.product_id: offer for offer in result.offers}
    alpha_offer = offers_by_product[products_by_title["Synthetic Phone Alpha"].product_id]
    beta_offer = offers_by_product[products_by_title["Synthetic Phone Beta"].product_id]
    assert alpha_offer.market == beta_offer.market == "EBAY_US"
    assert alpha_offer.stock_status is beta_offer.stock_status is StockStatus.UNKNOWN
    assert type(alpha_offer.cost_components.item_price) is KnownCost
    assert type(beta_offer.cost_components.item_price) is KnownCost
    assert alpha_offer.cost_components.item_price.amount == Decimal("199.99")
    assert beta_offer.cost_components.item_price.amount == Decimal("249.50")
    assert type(alpha_offer.cost_components.shipping) is KnownCost
    assert alpha_offer.cost_components.shipping.amount == Decimal("0.00")
    assert alpha_offer.cost_components.shipping.amount.as_tuple().exponent == -2
    assert type(beta_offer.cost_components.shipping) is UnknownCost
    for offer in result.offers:
        assert offer.captured_at == _CAPTURED_AT
        assert type(offer.cost_components.tax) is UnknownCost
        assert type(offer.cost_components.duty) is UnknownCost
        assert offer.cost_components.tax.reason == "NOT_DISCLOSED"
        assert offer.cost_components.duty.reason == "NOT_DISCLOSED"

    assert result.exchange_rates is not None
    assert result.exchange_rates.base_currency == "USD"
    assert result.exchange_rates.rates[0].currency == "USD"
    assert result.exchange_rates.rates[0].base_per_unit == Decimal("1")
    assert result.exchange_rates.rates[0].minor_units == 2
    fx_evidence = next(
        evidence
        for evidence in result.evidence
        if evidence.entity_type is EvidenceEntityType.EXCHANGE_RATE
    )
    assert fx_evidence.source_uri == "urn:glodex:identity-fx:USD"
    assert fx_evidence.provider_id == "glodex-system"
    assert all(evidence.captured_at == _CAPTURED_AT for evidence in result.evidence)
    evidence_ids = tuple(evidence.evidence_id for evidence in result.evidence)
    assert len(evidence_ids) == len(set(evidence_ids))
    assert all(len(evidence_id) <= 128 for evidence_id in evidence_ids)

    aggregated = aggregate_catalog_batch(result)
    assert aggregated.quarantine_issues == ()
    assert len(aggregated.products) == len(aggregated.offers) == 2
    assert aggregated.offers_conserved


def test_empty_fixture_is_a_publishable_batch_with_identity_fx() -> None:
    result = map_ebay_search_page(
        _fixture_page("ebay_search_empty.json"),
        snapshot_version=_SNAPSHOT_VERSION,
        captured_at=_CAPTURED_AT,
    )

    assert isinstance(result, CatalogBatch)
    assert result.products == ()
    assert result.offers == ()
    assert result.quarantine_issues == ()
    assert result.fatal_issues == ()
    assert result.exchange_rates is not None
    assert result.exchange_rates.supported_currencies == frozenset({"USD"})
    assert len(result.evidence) == 1
    assert result.evidence[0].entity_type is EvidenceEntityType.EXCHANGE_RATE
    assert result.evidence[0].captured_at == _CAPTURED_AT
    aggregated = aggregate_catalog_batch(result)
    assert aggregated.products == ()
    assert aggregated.offers == ()
    assert aggregated.quarantine_issues == ()
    assert cast(int, aggregated.source_offer_count) == 0
