from __future__ import annotations

from itertools import permutations

import pytest

from glodex.domain.catalog import (
    CanonicalProduct,
    CatalogAggregationResult,
    EntityKind,
    OfferIdentity,
    ProductAttribute,
    aggregate_catalog,
    aggregate_catalog_batch,
)
from glodex.domain.evidence import FieldEvidence
from glodex.domain.issues import IssueCode
from tests.builders import build_catalog_batch, build_offer, build_product

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec("GLO-P0-004"),
]


def _product(
    *,
    provider_id: str,
    ordinal: int,
    product_id: str = "product-1",
    title: str = "TravelBook 14",
    category: str = "laptop",
    entity_kind: EntityKind = EntityKind.PRIMARY_PRODUCT,
    weight: str = "1.2kg",
) -> object:
    prefix = provider_id
    return build_product(
        product_id=product_id,
        provider_id=provider_id,
        source_uri=f"fixture://{provider_id}/products/{product_id}",
        title=title,
        category=category,
        entity_kind=entity_kind,
        snapshot_ordinal=ordinal,
        attributes=(
            ProductAttribute(
                name="weight",
                value=weight,
                evidence_id=f"ev-{prefix}-weight",
            ),
        ),
        field_evidence=(
            FieldEvidence(
                field_path="product.title",
                evidence_id=f"ev-{prefix}-title",
            ),
            FieldEvidence(
                field_path="product.category",
                evidence_id=f"ev-{prefix}-category",
            ),
            FieldEvidence(
                field_path="product.entity_kind",
                evidence_id=f"ev-{prefix}-kind",
            ),
        ),
    )


def test_same_product_is_canonicalized_once_and_preserves_all_source_evidence() -> None:
    later = _product(provider_id="provider-b", ordinal=9)
    earlier = _product(provider_id="provider-a", ordinal=3)
    offers = (
        build_offer(
            provider_id="provider-b",
            offer_id="offer-b",
            source_uri="fixture://provider-b/offers/offer-b",
            snapshot_ordinal=8,
        ),
        build_offer(
            provider_id="provider-a",
            offer_id="offer-a",
            source_uri="fixture://provider-a/offers/offer-a",
            snapshot_ordinal=4,
        ),
    )

    result = aggregate_catalog((later, earlier), offers)  # type: ignore[arg-type]

    assert isinstance(result, CatalogAggregationResult)
    assert len(result.products) == 1
    canonical = result.products[0]
    assert isinstance(canonical, CanonicalProduct)
    assert canonical.product_id == "product-1"
    assert canonical.snapshot_ordinal == 3
    assert tuple(source.provider_id for source in canonical.sources) == (
        "provider-a",
        "provider-b",
    )
    assert {source.provider_id: source.evidence_ids for source in canonical.sources} == {
        "provider-a": (
            "ev-provider-a-category",
            "ev-provider-a-kind",
            "ev-provider-a-title",
            "ev-provider-a-weight",
        ),
        "provider-b": (
            "ev-provider-b-category",
            "ev-provider-b-kind",
            "ev-provider-b-title",
            "ev-provider-b-weight",
        ),
    }
    title_evidence = tuple(
        binding.evidence_id
        for binding in canonical.field_evidence
        if binding.field_path == "product.title"
    )
    assert title_evidence == ("ev-provider-a-title", "ev-provider-b-title")
    assert canonical.attributes[0].evidence_ids == (
        "ev-provider-a-weight",
        "ev-provider-b-weight",
    )
    assert tuple(offer.offer_id for offer in result.offers) == ("offer-a", "offer-b")
    assert result.legal_input_offer_identities == (
        OfferIdentity(provider_id="provider-a", offer_id="offer-a"),
        OfferIdentity(provider_id="provider-b", offer_id="offer-b"),
    )
    assert result.offers_conserved


@pytest.mark.parametrize(
    ("changed_field", "changed_value"),
    [
        ("title", "Different laptop"),
        ("category", "tablet"),
        ("entity_kind", EntityKind.ACCESSORY),
    ],
)
def test_core_identity_conflict_quarantines_the_whole_product_group(
    changed_field: str,
    changed_value: object,
) -> None:
    first = _product(provider_id="provider-a", ordinal=1)
    changed = {
        "provider_id": "provider-b",
        "ordinal": 2,
        changed_field: changed_value,
    }
    second = _product(**changed)  # type: ignore[arg-type]
    offer = build_offer(snapshot_ordinal=3)

    result = aggregate_catalog((first, second), (offer,))  # type: ignore[arg-type]

    assert result.products == ()
    assert result.offers == ()
    assert result.legal_input_offer_identities == ()
    assert [issue.code for issue in result.quarantine_issues] == [
        IssueCode.IDENTITY_CONFLICT,
        IssueCode.IDENTITY_CONFLICT,
    ]
    assert result.quarantine_issues[0].entity_ref == "offer:provider-a/offer-1@3"
    assert result.quarantine_issues[1].entity_ref == "product:product-1"


def test_same_name_attribute_conflict_quarantines_group_and_parented_offers() -> None:
    first = _product(provider_id="provider-a", ordinal=1, weight="1.2kg")
    second = _product(provider_id="provider-b", ordinal=2, weight="2.0kg")
    offer = build_offer(snapshot_ordinal=5)

    result = aggregate_catalog((first, second), (offer,))  # type: ignore[arg-type]

    assert result.products == ()
    assert result.offers == ()
    product_issue = next(
        issue for issue in result.quarantine_issues if issue.entity_ref == "product:product-1"
    )
    assert product_issue.code is IssueCode.IDENTITY_CONFLICT
    assert product_issue.details[0].value == "attribute:weight"


def test_duplicate_offer_identity_is_quarantined_without_overwrite() -> None:
    product = _product(provider_id="provider-a", ordinal=1)
    first_duplicate = build_offer(offer_id="dup", snapshot_ordinal=4)
    second_duplicate = build_offer(
        offer_id="dup",
        product_id="product-1",
        market="CA",
        snapshot_ordinal=5,
    )
    valid = build_offer(
        offer_id="unique",
        provider_id="provider-b",
        source_uri="fixture://provider-b/offers/unique",
        snapshot_ordinal=6,
    )

    result = aggregate_catalog(
        (product,),  # type: ignore[arg-type]
        (second_duplicate, valid, first_duplicate),
    )

    assert tuple((offer.provider_id, offer.offer_id) for offer in result.offers) == (
        ("provider-b", "unique"),
    )
    assert result.legal_input_offer_identities == (
        OfferIdentity(provider_id="provider-b", offer_id="unique"),
    )
    duplicate_issues = [
        issue for issue in result.quarantine_issues if issue.code is IssueCode.DUPLICATE_OFFER
    ]
    assert tuple(issue.entity_ref for issue in duplicate_issues) == (
        "offer:provider-a/dup@4",
        "offer:provider-a/dup@5",
    )
    assert result.offers_conserved


def test_orphan_and_conflicted_parent_offers_are_stably_quarantined() -> None:
    conflicted_a = _product(provider_id="provider-a", ordinal=1)
    conflicted_b = _product(provider_id="provider-b", ordinal=2, category="tablet")
    good = _product(
        provider_id="provider-c",
        ordinal=3,
        product_id="product-good",
    )
    conflict_offer = build_offer(offer_id="conflict", snapshot_ordinal=5)
    orphan = build_offer(
        offer_id="orphan",
        product_id="product-missing",
        snapshot_ordinal=4,
    )
    good_offer = build_offer(
        offer_id="good",
        product_id="product-good",
        provider_id="provider-c",
        source_uri="fixture://provider-c/offers/good",
        snapshot_ordinal=6,
    )

    result = aggregate_catalog(
        (good, conflicted_b, conflicted_a),  # type: ignore[arg-type]
        (good_offer, conflict_offer, orphan),
    )

    assert tuple(product.product_id for product in result.products) == ("product-good",)
    assert tuple(offer.offer_id for offer in result.offers) == ("good",)
    by_ref = {issue.entity_ref: issue.code for issue in result.quarantine_issues}
    assert by_ref["offer:provider-a/orphan@4"] is IssueCode.ORPHAN_OFFER
    assert by_ref["offer:provider-a/conflict@5"] is IssueCode.IDENTITY_CONFLICT


def test_product_and_offer_order_is_deterministic_under_input_permutation() -> None:
    products = (
        _product(provider_id="provider-b", ordinal=7),
        _product(provider_id="provider-a", ordinal=2),
        _product(
            provider_id="provider-c",
            ordinal=4,
            product_id="product-2",
            title="TravelBook 16",
        ),
    )
    offers = (
        build_offer(offer_id="later", snapshot_ordinal=9),
        build_offer(
            offer_id="second-product",
            product_id="product-2",
            provider_id="provider-c",
            source_uri="fixture://provider-c/offers/second-product",
            snapshot_ordinal=6,
        ),
        build_offer(
            offer_id="earlier",
            provider_id="provider-b",
            source_uri="fixture://provider-b/offers/earlier",
            snapshot_ordinal=5,
        ),
    )
    expected = aggregate_catalog(products, offers)  # type: ignore[arg-type]

    for product_order in permutations(products):
        for offer_order in permutations(offers):
            assert (
                aggregate_catalog(product_order, offer_order)  # type: ignore[arg-type]
                == expected
            )

    assert tuple(product.product_id for product in expected.products) == (
        "product-1",
        "product-2",
    )
    assert tuple(offer.offer_id for offer in expected.offers) == (
        "earlier",
        "second-product",
        "later",
    )


def test_batch_entrypoint_keeps_preexisting_quarantine_issues() -> None:
    batch = build_catalog_batch()

    result = aggregate_catalog_batch(batch)

    assert tuple(product.product_id for product in result.products) == ("product-1",)
    assert tuple(offer.offer_id for offer in result.offers) == ("offer-1",)
    assert result.quarantine_issues == batch.quarantine_issues
    assert result.offers_conserved


def test_aggregation_rejects_mutable_inputs_and_mixed_snapshots() -> None:
    product = build_product()

    with pytest.raises(TypeError):
        aggregate_catalog([product], ())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="snapshot"):
        aggregate_catalog(
            (product,),
            (build_offer(snapshot_version="m0-v2"),),
        )
