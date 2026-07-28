from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from glodex.domain.catalog import CatalogBatch
from glodex.domain.evidence import EvidenceEntityType, EvidenceRef, FieldEvidence
from glodex.domain.issues import CatalogIssue, IssueDetail
from tests.builders import (
    build_catalog_batch,
    build_evidence_ref,
    build_offer,
    build_product,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec("GLO-P0-003", "GLO-P0-004", "GLO-P0-008"),
]


def test_evidence_ref_is_frozen_and_uses_explicit_entity_type() -> None:
    evidence = build_evidence_ref()

    assert evidence.entity_type is EvidenceEntityType.PRODUCT
    assert evidence.product_id == "product-1"
    assert evidence.offer_id is None
    assert evidence.source == evidence.source_uri
    with pytest.raises(FrozenInstanceError):
        evidence.field_path = "product.category"  # type: ignore[misc]


@pytest.mark.parametrize("field", ["evidence_id", "snapshot_version", "provider_id", "source_uri"])
def test_evidence_rejects_empty_identity_provider_and_source(field: str) -> None:
    values = {
        "evidence_id": "ev-product-title",
        "snapshot_version": "m0-v1",
        "entity_type": EvidenceEntityType.PRODUCT,
        "product_id": "product-1",
        "offer_id": None,
        "currency": None,
        "field_path": "product.title",
        "provider_id": "provider-a",
        "source_uri": "fixture://provider-a/product-1",
        "captured_at": build_evidence_ref().captured_at,
    }
    values[field] = ""

    with pytest.raises(ValueError):
        EvidenceRef(**values)  # type: ignore[arg-type]


def test_evidence_entity_shape_is_fail_closed() -> None:
    with pytest.raises(ValueError, match="offer"):
        build_evidence_ref(
            entity_type=EvidenceEntityType.OFFER,
            offer_id=None,
            field_path="offer.inventory",
        )
    with pytest.raises(ValueError, match="product"):
        build_evidence_ref(
            entity_type=EvidenceEntityType.EXCHANGE_RATE,
            product_id="product-1",
            currency="USD",
            field_path="exchange_rate.base_per_unit",
        )
    with pytest.raises(TypeError):
        build_evidence_ref(entity_type="PRODUCT")  # type: ignore[arg-type]


def test_field_evidence_and_issue_details_reject_mutable_values() -> None:
    binding = FieldEvidence(field_path="product.title", evidence_id="ev-title")
    detail = IssueDetail(key="record", value="products:1")

    assert binding.field_path == "product.title"
    with pytest.raises(FrozenInstanceError):
        binding.evidence_id = "other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        IssueDetail(key="paths", value=["one", "two"])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        CatalogIssue(  # type: ignore[call-arg]
            code="catalog.invalid-record",
            stage="products",
            disposition="quarantine",
            message="invalid",
            details={"record": "1"},
        )
    assert detail.value == "products:1"


def test_catalog_batch_rejects_cross_snapshot_entities_and_evidence() -> None:
    valid = build_catalog_batch()
    cross_snapshot = build_evidence_ref(snapshot_version="m0-v2")

    with pytest.raises(ValueError, match=r"evidence.*snapshot"):
        CatalogBatch(
            snapshot_version=valid.snapshot_version,
            products=valid.products,
            offers=valid.offers,
            evidence=(*valid.evidence[1:], cross_snapshot),
            exchange_rates=valid.exchange_rates,
        )
    with pytest.raises(ValueError, match=r"product.*snapshot"):
        CatalogBatch(
            snapshot_version=valid.snapshot_version,
            products=(build_product(snapshot_version="m0-v2"),),
        )
    with pytest.raises(ValueError, match=r"offer.*snapshot"):
        CatalogBatch(
            snapshot_version=valid.snapshot_version,
            products=valid.products,
            offers=(build_offer(snapshot_version="m0-v2"),),
        )


def test_catalog_batch_rejects_missing_or_wrong_entity_evidence() -> None:
    valid = build_catalog_batch()
    without_title = tuple(ref for ref in valid.evidence if ref.evidence_id != "ev-product-title")

    with pytest.raises(ValueError, match="unknown evidence"):
        CatalogBatch(
            snapshot_version=valid.snapshot_version,
            products=valid.products,
            offers=valid.offers,
            evidence=without_title,
            exchange_rates=valid.exchange_rates,
        )

    wrong_entity = build_evidence_ref(
        evidence_id="ev-product-title",
        product_id="product-other",
    )
    replaced = tuple(
        wrong_entity if ref.evidence_id == wrong_entity.evidence_id else ref
        for ref in valid.evidence
    )
    with pytest.raises(ValueError, match="entity"):
        CatalogBatch(
            snapshot_version=valid.snapshot_version,
            products=valid.products,
            offers=valid.offers,
            evidence=replaced,
            exchange_rates=valid.exchange_rates,
        )


@pytest.mark.parametrize(
    "evidence_id",
    [
        "ev-offer-market",
        "ev-offer-currency",
    ],
)
def test_offer_market_and_currency_reject_wrongly_bound_evidence(
    evidence_id: str,
) -> None:
    valid = build_catalog_batch()
    wrongly_bound = tuple(
        replace(ref, field_path="offer.inventory") if ref.evidence_id == evidence_id else ref
        for ref in valid.evidence
    )

    with pytest.raises(ValueError, match="offer evidence entity or field mismatch"):
        CatalogBatch(
            snapshot_version=valid.snapshot_version,
            products=valid.products,
            offers=valid.offers,
            evidence=wrongly_bound,
            exchange_rates=valid.exchange_rates,
        )


def test_catalog_batch_rejects_duplicate_evidence_ids() -> None:
    evidence = build_evidence_ref()

    with pytest.raises(ValueError, match="duplicate evidence"):
        CatalogBatch(
            snapshot_version="m0-v1",
            evidence=(evidence, evidence),
        )
