from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from glodex.domain.catalog import (
    CatalogBatch,
    CostComponents,
    EntityKind,
    Offer,
    Product,
    ProductAttribute,
    StockStatus,
)
from glodex.domain.evidence import FieldEvidence
from glodex.domain.issues import (
    CatalogIssue,
    IssueCode,
    IssueDisposition,
    IssueStage,
)
from glodex.domain.pricing import KnownCost, UnknownCost
from tests.builders import build_catalog_batch, build_offer, build_product

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec("GLO-P0-003", "GLO-P0-004", "GLO-P0-008"),
]


def test_catalog_models_are_frozen_and_keep_stable_ordinals() -> None:
    product = build_product(snapshot_ordinal=7)
    offer = build_offer(snapshot_ordinal=11)
    batch = build_catalog_batch()

    assert product.snapshot_ordinal == 7
    assert offer.snapshot_ordinal == 11
    assert isinstance(product.attributes, tuple)
    assert isinstance(product.field_evidence, tuple)
    assert isinstance(batch.products, tuple)
    assert isinstance(batch.offers, tuple)
    assert isinstance(batch.evidence, tuple)
    with pytest.raises(FrozenInstanceError):
        product.title = "mutated"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        offer.market = "CN"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("product_id", ""),
        ("product_id", "   "),
        ("provider_id", ""),
        ("provider_id", "   "),
        ("source_uri", ""),
        ("source_uri", "   "),
    ],
)
def test_product_rejects_empty_identity_provider_and_source(field: str, value: str) -> None:
    values = {
        "snapshot_version": "m0-v1",
        "product_id": "product-1",
        "provider_id": "provider-a",
        "source_uri": "fixture://provider-a/product-1",
        "title": "TravelBook 14",
        "category": "laptop",
        "entity_kind": EntityKind.PRIMARY_PRODUCT,
        "snapshot_ordinal": 0,
        "attributes": (),
        "field_evidence": (),
    }
    values[field] = value

    with pytest.raises(ValueError):
        Product(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("offer_id", ""),
        ("product_id", ""),
        ("provider_id", ""),
        ("source_uri", ""),
    ],
)
def test_offer_rejects_empty_identity_provider_and_source(field: str, value: str) -> None:
    values = {
        "snapshot_version": "m0-v1",
        "offer_id": "offer-1",
        "product_id": "product-1",
        "provider_id": "provider-a",
        "source_uri": "fixture://provider-a/offer-1",
        "market": "US",
        "stock_status": StockStatus.IN_STOCK,
        "cost_components": CostComponents(
            currency="USD",
            item_price=UnknownCost(reason="NOT_DISCLOSED"),
            shipping=UnknownCost(reason="NOT_DISCLOSED"),
            tax=UnknownCost(reason="NOT_DISCLOSED"),
            duty=UnknownCost(reason="NOT_DISCLOSED"),
        ),
        "captured_at": build_offer().captured_at,
        "snapshot_ordinal": 0,
        "field_evidence": (
            FieldEvidence(
                field_path="offer.inventory",
                evidence_id="ev-offer-inventory",
            ),
        ),
    }
    values[field] = value

    with pytest.raises(ValueError):
        Offer(**values)  # type: ignore[arg-type]


def test_inventory_and_product_entity_are_closed_enums() -> None:
    assert tuple(StockStatus) == (
        StockStatus.IN_STOCK,
        StockStatus.OUT_OF_STOCK,
        StockStatus.UNKNOWN,
    )
    assert tuple(EntityKind) == (
        EntityKind.PRIMARY_PRODUCT,
        EntityKind.ACCESSORY,
        EntityKind.REPLACEMENT_PART,
        EntityKind.DECORATION,
        EntityKind.UNKNOWN,
    )

    with pytest.raises(TypeError):
        build_offer(stock_status="BACKORDERED")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        build_product(entity_kind="PRIMARY_PRODUCT")  # type: ignore[arg-type]


def test_raw_costs_distinguish_unknown_from_known_zero() -> None:
    costs = CostComponents(
        currency="USD",
        item_price=KnownCost(amount=Decimal("0.00"), evidence_id="ev-zero"),
        shipping=UnknownCost(reason="CARRIER_NOT_DISCLOSED"),
        tax=KnownCost(amount=Decimal("4.50"), evidence_id="ev-tax"),
        duty=UnknownCost(reason="TARIFF_NOT_PUBLISHED"),
    )

    assert costs.item_price == KnownCost(amount=Decimal("0.00"), evidence_id="ev-zero")
    assert costs.shipping == UnknownCost(reason="CARRIER_NOT_DISCLOSED")


@pytest.mark.parametrize("legacy_value", [None, Decimal("1"), 1.5])
def test_raw_costs_reject_legacy_none_decimal_and_float_inputs(
    legacy_value: object,
) -> None:
    with pytest.raises(TypeError):
        CostComponents(
            currency="USD",
            item_price=legacy_value,  # type: ignore[arg-type]
            shipping=UnknownCost(reason="NOT_DISCLOSED"),
            tax=UnknownCost(reason="NOT_DISCLOSED"),
            duty=UnknownCost(reason="NOT_DISCLOSED"),
        )


def test_every_known_cost_including_zero_requires_field_evidence() -> None:
    offer = build_offer()
    missing_shipping = tuple(
        binding
        for binding in offer.field_evidence
        if binding.field_path != "offer.cost_components.shipping"
    )

    with pytest.raises(ValueError, match="shipping"):
        build_offer(field_evidence=missing_shipping)


def test_known_cost_evidence_id_must_match_field_binding() -> None:
    offer = build_offer()
    mismatched = tuple(
        FieldEvidence(field_path=binding.field_path, evidence_id="wrong-evidence")
        if binding.field_path == "offer.cost_components.shipping"
        else binding
        for binding in offer.field_evidence
    )

    with pytest.raises(ValueError, match="shipping"):
        build_offer(field_evidence=mismatched)


def test_unknown_cost_must_not_have_a_field_evidence_binding() -> None:
    offer = build_offer()
    unknown_costs = CostComponents(
        currency="USD",
        item_price=offer.cost_components.item_price,
        shipping=UnknownCost(reason="CARRIER_NOT_DISCLOSED"),
        tax=offer.cost_components.tax,
        duty=offer.cost_components.duty,
    )

    with pytest.raises(ValueError, match="shipping"):
        build_offer(cost_components=unknown_costs)


def test_offer_delivery_requires_one_ordered_evidence_closed_pair() -> None:
    offer = build_offer(delivery_days_min=2, delivery_days_max=6)

    assert offer.delivery_days_min == 2
    assert offer.delivery_days_max == 6
    assert {
        binding.field_path
        for binding in offer.field_evidence
        if binding.field_path.startswith("offer.delivery_days_")
    } == {"offer.delivery_days_min", "offer.delivery_days_max"}

    with pytest.raises(ValueError, match="both be present"):
        build_offer(delivery_days_min=2)
    with pytest.raises(ValueError, match="cannot precede"):
        build_offer(delivery_days_min=6, delivery_days_max=2)
    with pytest.raises(ValueError, match="requires matching field evidence"):
        build_offer(
            delivery_days_min=2,
            delivery_days_max=6,
            field_evidence=build_offer().field_evidence,
        )
    with pytest.raises(ValueError, match="unknown offer delivery"):
        build_offer(field_evidence=offer.field_evidence)


@pytest.mark.parametrize(
    "required_path",
    [
        "offer.market",
        "offer.cost_components.currency",
    ],
)
def test_offer_market_and_currency_require_independent_evidence(
    required_path: str,
) -> None:
    offer = build_offer()
    without_required_binding = tuple(
        binding for binding in offer.field_evidence if binding.field_path != required_path
    )

    with pytest.raises(ValueError, match=required_path):
        build_offer(field_evidence=without_required_binding)


def test_models_reject_mutable_nested_collections_and_extra_fields() -> None:
    with pytest.raises(TypeError):
        build_product(attributes=[])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ProductAttribute(  # type: ignore[call-arg]
            name="weight",
            value="1kg",
            evidence_id="ev-weight",
            extra=True,
        )
    with pytest.raises(TypeError):
        CatalogBatch(  # type: ignore[arg-type]
            snapshot_version="m0-v1",
            products=[build_product()],
        )


def test_product_rejects_duplicate_attribute_and_evidence_paths() -> None:
    duplicate_attributes = (
        ProductAttribute(name="weight", value="1kg", evidence_id="ev-weight-1"),
        ProductAttribute(name="weight", value="2kg", evidence_id="ev-weight-2"),
    )
    duplicate_paths = (
        FieldEvidence(field_path="product.title", evidence_id="ev-title-1"),
        FieldEvidence(field_path="product.title", evidence_id="ev-title-2"),
    )

    with pytest.raises(ValueError, match="attribute"):
        build_product(attributes=duplicate_attributes)
    with pytest.raises(ValueError, match="field evidence"):
        build_product(field_evidence=duplicate_paths)


def test_batch_preserves_quarantine_warnings_but_fatal_has_no_partial_data() -> None:
    warning = CatalogIssue(
        code=IssueCode.INVALID_RECORD,
        stage=IssueStage.PRODUCTS,
        disposition=IssueDisposition.QUARANTINE,
        message="invalid product record",
        entity_ref="products:7",
    )
    fatal = CatalogIssue(
        code=IssueCode.MANIFEST_INVALID,
        stage=IssueStage.MANIFEST,
        disposition=IssueDisposition.FATAL,
        message="manifest schema unsupported",
    )
    batch = CatalogBatch(snapshot_version="m0-v1", quarantine_issues=(warning,))

    assert batch.quarantine_issues == (warning,)
    assert batch.fatal_issues == ()
    assert warning.is_fatal is False
    assert fatal.is_fatal is True
    with pytest.raises(ValueError, match="partial"):
        CatalogBatch(
            snapshot_version="m0-v1",
            products=(build_product(),),
            fatal_issues=(fatal,),
        )


def test_catalog_issue_rejects_wrong_issue_partition() -> None:
    fatal = CatalogIssue(
        code=IssueCode.HASH_MISMATCH,
        stage=IssueStage.MANIFEST,
        disposition=IssueDisposition.FATAL,
        message="hash mismatch",
    )

    with pytest.raises(ValueError, match="quarantine"):
        CatalogBatch(snapshot_version="m0-v1", quarantine_issues=(fatal,))
