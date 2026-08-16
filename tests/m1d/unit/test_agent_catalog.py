from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from glodex.agent.catalog import (
    CandidateManifest,
    CandidateStore,
    InMemoryCatalogGateway,
    ManifestRecord,
    build_fx_evaluation_view,
    rebind_selected_candidates,
)
from glodex.agent.contracts import (
    Candidate,
    CandidateAttribute,
    DataMode,
    ItemSearchRuntimeResult,
    Platform,
)
from glodex.domain.catalog import (
    CatalogBatch,
    CostComponents,
    Offer,
    OfferIdentity,
    Product,
    ProductAttribute,
)
from glodex.domain.evidence import EvidenceEntityType, FieldEvidence
from glodex.domain.pricing import KnownCost
from tests.builders import (
    build_catalog_batch,
    build_evidence_ref,
    build_offer,
    build_product,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec("GLO-M1D-P0-004", "GLO-M1D-NFR-003"),
]


def _candidate(*, candidate_id: str = "amazon.product-1") -> Candidate:
    return Candidate(
        candidate_id=candidate_id,
        item_id="item-1",
        platform=Platform.AMAZON,
        title="TravelBook 14",
        price=Decimal("699.00"),
        currency="USD",
        attributes=(CandidateAttribute(name="weight", value="1.2kg"),),
        source_ref="offer-1",
        record_ref="amazon-item-1",
    )


def _manifest(*, snapshot_version: str = "m0-v1") -> CandidateManifest:
    return CandidateManifest(
        data_mode=DataMode.SYNTHETIC_INTERVIEW,
        snapshot_version=snapshot_version,
        records=(
            ManifestRecord(
                record_ref="amazon-item-1",
                source_ref="offer-1",
                item_id="item-1",
                platform=Platform.AMAZON,
                product_id="product-1",
                offer_identities=(OfferIdentity(provider_id="provider-a", offer_id="offer-1"),),
                provider_ids=("provider-a",),
            ),
        ),
    )


def _result(*, snapshot_version: str = "m0-v1") -> ItemSearchRuntimeResult:
    return ItemSearchRuntimeResult(
        platform=Platform.AMAZON,
        target_query="test query",
        retrieval_query="test query",
        candidates=(_candidate(),),
        platform_sub_batch=build_catalog_batch(snapshot_version=snapshot_version),
        total_recall=1,
        returned_before_semantic_filter=1,
        truncated=False,
    )


def _record_product(
    *,
    product_id: str,
    provider_id: str,
    title: str,
    attributes: tuple[ProductAttribute, ...] = (),
) -> Product:
    return build_product(
        product_id=product_id,
        provider_id=provider_id,
        source_uri=f"fixture://{provider_id}/products/{product_id}",
        title=title,
        attributes=attributes,
        field_evidence=(
            FieldEvidence(
                field_path="product.title",
                evidence_id=f"ev-{product_id}-title",
            ),
            FieldEvidence(
                field_path="product.category",
                evidence_id=f"ev-{product_id}-category",
            ),
            FieldEvidence(
                field_path="product.entity_kind",
                evidence_id=f"ev-{product_id}-kind",
            ),
        ),
    )


def _record_offer(
    *,
    offer_id: str,
    product_id: str,
    provider_id: str,
    amount: str,
) -> Offer:
    prefix = f"ev-{provider_id}-{offer_id}"
    costs = CostComponents(
        currency="USD",
        item_price=KnownCost(
            amount=Decimal(amount),
            evidence_id=f"{prefix}-item-price",
        ),
        shipping=KnownCost(
            amount=Decimal("0.00"),
            evidence_id=f"{prefix}-shipping",
        ),
        tax=KnownCost(
            amount=Decimal("0.00"),
            evidence_id=f"{prefix}-tax",
        ),
        duty=KnownCost(
            amount=Decimal("0.00"),
            evidence_id=f"{prefix}-duty",
        ),
    )
    return build_offer(
        offer_id=offer_id,
        product_id=product_id,
        provider_id=provider_id,
        source_uri=f"fixture://{provider_id}/offers/{offer_id}",
        cost_components=costs,
        field_evidence=(
            FieldEvidence(
                field_path="offer.inventory",
                evidence_id=f"{prefix}-inventory",
            ),
            FieldEvidence(
                field_path="offer.market",
                evidence_id=f"{prefix}-market",
            ),
            FieldEvidence(
                field_path="offer.cost_components.currency",
                evidence_id=f"{prefix}-currency",
            ),
            FieldEvidence(
                field_path="offer.cost_components.item_price",
                evidence_id=f"{prefix}-item-price",
            ),
            FieldEvidence(
                field_path="offer.cost_components.shipping",
                evidence_id=f"{prefix}-shipping",
            ),
            FieldEvidence(
                field_path="offer.cost_components.tax",
                evidence_id=f"{prefix}-tax",
            ),
            FieldEvidence(
                field_path="offer.cost_components.duty",
                evidence_id=f"{prefix}-duty",
            ),
        ),
    )


def _record_batch(
    products: tuple[Product, ...],
    offers: tuple[Offer, ...],
) -> CatalogBatch:
    product_evidence = tuple(
        build_evidence_ref(
            evidence_id=binding.evidence_id,
            product_id=product.product_id,
            field_path=binding.field_path,
            provider_id=product.provider_id,
            source_uri=product.source_uri,
        )
        for product in products
        for binding in (
            *product.field_evidence,
            *(
                FieldEvidence(
                    field_path=f"product.attributes.{attribute.name}",
                    evidence_id=attribute.evidence_id,
                )
                for attribute in product.attributes
            ),
        )
    )
    offer_evidence = tuple(
        build_evidence_ref(
            evidence_id=binding.evidence_id,
            entity_type=EvidenceEntityType.OFFER,
            product_id=offer.product_id,
            offer_id=offer.offer_id,
            field_path=binding.field_path,
            provider_id=offer.provider_id,
            source_uri=offer.source_uri,
        )
        for offer in offers
        for binding in offer.field_evidence
    )
    return CatalogBatch(
        snapshot_version="m0-v1",
        products=products,
        offers=offers,
        evidence=(*product_evidence, *offer_evidence),
    )


def _record(
    *,
    suffix: str,
    product_id: str,
    provider_id: str,
    offer_ids: tuple[str, ...],
    source_ref: str | None = None,
) -> ManifestRecord:
    return ManifestRecord(
        record_ref=f"record-{suffix}",
        source_ref=offer_ids[0] if source_ref is None else source_ref,
        item_id=f"item-{suffix}",
        platform=Platform.AMAZON,
        product_id=product_id,
        offer_identities=tuple(
            OfferIdentity(provider_id=provider_id, offer_id=offer_id) for offer_id in offer_ids
        ),
        provider_ids=(provider_id,),
    )


def _projected_candidate(
    *,
    suffix: str,
    record: ManifestRecord,
    product: Product,
    priced_offer: Offer,
) -> Candidate:
    item_price = priced_offer.cost_components.item_price
    assert type(item_price) is KnownCost
    by_name = {attribute.name: attribute.value for attribute in product.attributes}
    return Candidate(
        candidate_id=f"amazon.{suffix}",
        item_id=record.item_id,
        platform=Platform.AMAZON,
        title=product.title,
        price=item_price.amount,
        currency=priced_offer.cost_components.currency,
        attributes=tuple(
            CandidateAttribute(name=attribute.name, value=attribute.value)
            for attribute in product.attributes
        ),
        source_ref=record.source_ref,
        record_ref=record.record_ref,
        pack_size=(None if "pack_size" not in by_name else Decimal(by_name["pack_size"])),
        pack_note=by_name.get("pack_note"),
    )


def _multi_result(
    *,
    candidates: tuple[Candidate, ...],
    batch: CatalogBatch,
) -> ItemSearchRuntimeResult:
    return ItemSearchRuntimeResult(
        platform=Platform.AMAZON,
        target_query="test query",
        retrieval_query="test query",
        candidates=candidates,
        platform_sub_batch=batch,
        total_recall=len(candidates),
        returned_before_semantic_filter=len(candidates),
        truncated=False,
    )


def test_candidate_store_validates_manifest_and_materializes_one_closed_batch() -> None:
    store = CandidateStore(_manifest())

    pool = store.merge((_result(),))

    assert pool.candidates == (_candidate(),)
    assert pool.evaluation_batch == build_catalog_batch()
    assert pool.records == _manifest().records
    assert store.byte_count > 0
    with pytest.raises(FrozenInstanceError):
        pool.candidates = ()  # type: ignore[misc]

    store.clear()
    assert store.byte_count == 0
    assert store.is_cleared


def test_candidate_store_accepts_verified_empty_search_results() -> None:
    manifest = CandidateManifest(
        data_mode=DataMode.SYNTHETIC_INTERVIEW,
        snapshot_version="m1d-demo-v1",
        records=(),
    )
    result = ItemSearchRuntimeResult(
        platform=Platform.AMAZON,
        target_query="commuter backpack",
        retrieval_query="commuter backpack",
        candidates=(),
        platform_sub_batch=CatalogBatch(snapshot_version="m1d-demo-v1"),
        total_recall=0,
        returned_before_semantic_filter=0,
        truncated=False,
    )

    pool = CandidateStore(manifest).merge((result,))

    assert pool.candidates == ()
    assert pool.records == ()
    assert pool.evaluation_batch.products == ()


@pytest.mark.parametrize(
    "update",
    (
        {"title": "Forged title"},
        {"attributes": (CandidateAttribute(name="weight", value="99kg"),)},
        {"pack_size": Decimal("12")},
        {"pack_note": "forged pack"},
        {"rating": Decimal("5")},
        {"sales": 1_000_000},
        {"image_url": "https://example.test/forged.jpg"},
    ),
)
def test_candidate_store_rejects_candidate_projection_drift(
    update: dict[str, object],
) -> None:
    forged = _candidate(candidate_id="amazon.forged").model_copy(update=update)
    result = ItemSearchRuntimeResult(
        platform=Platform.AMAZON,
        target_query="test query",
        retrieval_query="test query",
        candidates=(forged,),
        platform_sub_batch=build_catalog_batch(),
        total_recall=1,
        returned_before_semantic_filter=1,
        truncated=False,
    )

    with pytest.raises(ValueError, match="Catalog projection"):
        CandidateStore(_manifest()).merge((result,))


def test_candidate_store_rejects_same_product_group_drift() -> None:
    forged = _candidate().model_copy(update={"same_group_id": "sg-v1-aaaaaaaaaaaaaaaaaaaaaaaa"})
    result = ItemSearchRuntimeResult(
        platform=Platform.AMAZON,
        target_query="test query",
        retrieval_query="test query",
        candidates=(forged,),
        platform_sub_batch=build_catalog_batch(),
        total_recall=1,
        returned_before_semantic_filter=1,
        truncated=False,
    )

    with pytest.raises(ValueError, match="candidate ownership"):
        CandidateStore(_manifest()).merge((result,))


def test_candidate_store_accepts_exact_pack_projection() -> None:
    product = _record_product(
        product_id="product-pack",
        provider_id="provider-a",
        title="Six-pack",
        attributes=(
            ProductAttribute(
                name="pack_size",
                value="6",
                evidence_id="ev-product-pack-size",
            ),
            ProductAttribute(
                name="pack_note",
                value="six units",
                evidence_id="ev-product-pack-note",
            ),
        ),
    )
    offer = _record_offer(
        offer_id="offer-pack",
        product_id=product.product_id,
        provider_id="provider-a",
        amount="24.00",
    )
    record = _record(
        suffix="pack",
        product_id=product.product_id,
        provider_id="provider-a",
        offer_ids=(offer.offer_id,),
    )
    candidate = _projected_candidate(
        suffix="pack",
        record=record,
        product=product,
        priced_offer=offer,
    )
    manifest = CandidateManifest(
        data_mode=DataMode.SYNTHETIC_INTERVIEW,
        snapshot_version="m0-v1",
        records=(record,),
    )

    pool = CandidateStore(manifest).merge(
        (_multi_result(candidates=(candidate,), batch=_record_batch((product,), (offer,))),)
    )

    assert pool.candidates == (candidate,)


def test_candidate_store_prices_only_from_source_ref_offer() -> None:
    product = _record_product(
        product_id="product-source",
        provider_id="provider-a",
        title="Source-bound",
    )
    source_offer = _record_offer(
        offer_id="offer-source",
        product_id=product.product_id,
        provider_id="provider-a",
        amount="699.00",
    )
    alternate_offer = _record_offer(
        offer_id="offer-alternate",
        product_id=product.product_id,
        provider_id="provider-a",
        amount="1.00",
    )
    record = _record(
        suffix="source",
        product_id=product.product_id,
        provider_id="provider-a",
        offer_ids=(source_offer.offer_id, alternate_offer.offer_id),
    )
    manifest = CandidateManifest(
        data_mode=DataMode.SYNTHETIC_INTERVIEW,
        snapshot_version="m0-v1",
        records=(record,),
    )
    batch = _record_batch((product,), (source_offer, alternate_offer))
    substituted = _projected_candidate(
        suffix="source",
        record=record,
        product=product,
        priced_offer=alternate_offer,
    )

    with pytest.raises(ValueError, match="source offer"):
        CandidateStore(manifest).merge((_multi_result(candidates=(substituted,), batch=batch),))

    expected = _projected_candidate(
        suffix="source",
        record=record,
        product=product,
        priced_offer=source_offer,
    )
    pool = CandidateStore(manifest).merge((_multi_result(candidates=(expected,), batch=batch),))
    assert pool.candidates == (expected,)


def test_candidate_store_rejects_cross_wired_offer_parents() -> None:
    product_a = _record_product(
        product_id="product-a",
        provider_id="provider-a",
        title="Product A",
    )
    product_b = _record_product(
        product_id="product-b",
        provider_id="provider-b",
        title="Product B",
    )
    offer_a = _record_offer(
        offer_id="offer-a",
        product_id=product_b.product_id,
        provider_id="provider-a",
        amount="10.00",
    )
    offer_b = _record_offer(
        offer_id="offer-b",
        product_id=product_a.product_id,
        provider_id="provider-b",
        amount="20.00",
    )
    record_a = _record(
        suffix="a",
        product_id=product_a.product_id,
        provider_id="provider-a",
        offer_ids=(offer_a.offer_id,),
    )
    record_b = _record(
        suffix="b",
        product_id=product_b.product_id,
        provider_id="provider-b",
        offer_ids=(offer_b.offer_id,),
    )
    candidates = (
        _projected_candidate(
            suffix="a",
            record=record_a,
            product=product_a,
            priced_offer=offer_a,
        ),
        _projected_candidate(
            suffix="b",
            record=record_b,
            product=product_b,
            priced_offer=offer_b,
        ),
    )
    manifest = CandidateManifest(
        data_mode=DataMode.SYNTHETIC_INTERVIEW,
        snapshot_version="m0-v1",
        records=(record_a, record_b),
    )

    with pytest.raises(ValueError, match="parent"):
        CandidateStore(manifest).merge(
            (
                _multi_result(
                    candidates=candidates,
                    batch=_record_batch((product_a, product_b), (offer_a, offer_b)),
                ),
            )
        )


def test_candidate_store_rejects_cross_record_provider_laundering() -> None:
    product_a = _record_product(
        product_id="product-a",
        provider_id="provider-b",
        title="Product A",
    )
    product_b = _record_product(
        product_id="product-b",
        provider_id="provider-a",
        title="Product B",
    )
    offer_a = _record_offer(
        offer_id="offer-a",
        product_id=product_a.product_id,
        provider_id="provider-a",
        amount="10.00",
    )
    offer_b = _record_offer(
        offer_id="offer-b",
        product_id=product_b.product_id,
        provider_id="provider-b",
        amount="20.00",
    )
    record_a = _record(
        suffix="a",
        product_id=product_a.product_id,
        provider_id="provider-a",
        offer_ids=(offer_a.offer_id,),
    )
    record_b = _record(
        suffix="b",
        product_id=product_b.product_id,
        provider_id="provider-b",
        offer_ids=(offer_b.offer_id,),
    )
    manifest = CandidateManifest(
        data_mode=DataMode.SYNTHETIC_INTERVIEW,
        snapshot_version="m0-v1",
        records=(record_a, record_b),
    )
    candidates = (
        _projected_candidate(
            suffix="a",
            record=record_a,
            product=product_a,
            priced_offer=offer_a,
        ),
        _projected_candidate(
            suffix="b",
            record=record_b,
            product=product_b,
            priced_offer=offer_b,
        ),
    )

    with pytest.raises(ValueError, match="provider ownership"):
        CandidateStore(manifest).merge(
            (
                _multi_result(
                    candidates=candidates,
                    batch=_record_batch((product_a, product_b), (offer_a, offer_b)),
                ),
            )
        )


@pytest.mark.parametrize("duplicate", ("product", "offer"))
def test_candidate_store_rejects_duplicate_catalog_identities(duplicate: str) -> None:
    batch = build_catalog_batch()
    duplicated = CatalogBatch(
        snapshot_version=batch.snapshot_version,
        products=(
            (batch.products[0], batch.products[0]) if duplicate == "product" else batch.products
        ),
        offers=((batch.offers[0], batch.offers[0]) if duplicate == "offer" else batch.offers),
        evidence=batch.evidence,
        exchange_rates=batch.exchange_rates,
    )
    result = ItemSearchRuntimeResult(
        platform=Platform.AMAZON,
        target_query="test query",
        retrieval_query="test query",
        candidates=(_candidate(),),
        platform_sub_batch=duplicated,
        total_recall=1,
        returned_before_semantic_filter=1,
        truncated=False,
    )

    with pytest.raises(ValueError, match="exactly one"):
        CandidateStore(_manifest()).merge((result,))


def test_candidate_store_rejects_unbound_evidence() -> None:
    batch = build_catalog_batch()
    extra = build_evidence_ref(
        evidence_id="ev-unbound",
        field_path="product.unbound",
        provider_id="forged-provider",
    )
    open_batch = CatalogBatch(
        snapshot_version=batch.snapshot_version,
        products=batch.products,
        offers=batch.offers,
        evidence=(*batch.evidence, extra),
        exchange_rates=batch.exchange_rates,
    )
    result = ItemSearchRuntimeResult(
        platform=Platform.AMAZON,
        target_query="test query",
        retrieval_query="test query",
        candidates=(_candidate(),),
        platform_sub_batch=open_batch,
        total_recall=1,
        returned_before_semantic_filter=1,
        truncated=False,
    )

    with pytest.raises(ValueError, match="evidence closure"):
        CandidateStore(_manifest()).merge((result,))


def test_candidate_store_rejects_cross_snapshot_and_candidate_fact_mismatch() -> None:
    store = CandidateStore(_manifest())
    with pytest.raises(ValueError, match="snapshot"):
        store.merge((_result(snapshot_version="other-v1"),))

    bad = _candidate(candidate_id="amazon.bad-price").model_copy(
        update={"price": Decimal("700.00")}
    )
    result = ItemSearchRuntimeResult(
        platform=Platform.AMAZON,
        target_query="test query",
        retrieval_query="test query",
        candidates=(bad,),
        platform_sub_batch=build_catalog_batch(),
        total_recall=1,
        returned_before_semantic_filter=1,
        truncated=False,
    )
    with pytest.raises(ValueError, match="price"):
        store.merge((result,))


def test_fx_view_preserves_source_facts_and_rebinds_only_fx_to_capture_snapshot() -> None:
    source = build_catalog_batch(snapshot_version="capture-123")
    fx_source = build_catalog_batch(snapshot_version="m0-v1")

    viewed = build_fx_evaluation_view(source, fx_source)

    assert viewed.snapshot_version == "capture-123"
    assert viewed.products == source.products
    assert viewed.offers == source.offers
    assert viewed.exchange_rates is not None
    assert viewed.exchange_rates.snapshot_version == "capture-123"
    assert viewed.exchange_rates.rates[0].evidence_id.startswith("fx.e.")
    assert viewed.exchange_rates.rates[0].evidence_id != "ev-rate-usd"


def test_rebinder_and_in_memory_gateway_enforce_final_snapshot_and_currency() -> None:
    pool = CandidateStore(_manifest()).merge((_result(),))

    rebound = rebind_selected_candidates(
        pool,
        ("amazon.product-1",),
        run_id="agent-run-1",
        fx_source_batch=build_catalog_batch(),
    )

    assert rebound.batch.snapshot_version.startswith("agent-")
    mapping = rebound.mapping.entries[0]
    assert mapping.candidate_id == "amazon.product-1"
    assert mapping.product_id.startswith("amazon.p.")
    assert mapping.offer_ids[0].startswith("amazon.o.")
    source_attribute_evidence = pool.evaluation_batch.products[0].attributes[0].evidence_id
    rebound_attribute_evidence = rebound.mapping.for_evidence(source_attribute_evidence)
    assert rebound_attribute_evidence.startswith("amazon.e.")
    assert any(
        item.evidence_id == rebound_attribute_evidence
        and item.source_uri == "fixture://provider-a/products/product-1"
        for item in rebound.batch.evidence
    )
    assert rebound.batch.products[0].source_uri == "fixture://provider-a/products/product-1"
    assert rebound.batch.offers[0].source_uri == "fixture://provider-a/offers/offer-1"

    gateway = InMemoryCatalogGateway(
        rebound.batch,
        display_currency="USD",
        budget_currency="USD",
    )
    loaded = asyncio.run(
        gateway.load(
            rebound.batch.snapshot_version,
            display_currency="USD",
            budget_currency="USD",
        )
    )
    assert loaded is rebound.batch
    with pytest.raises(ValueError, match="currency"):
        asyncio.run(
            gateway.load(
                rebound.batch.snapshot_version,
                display_currency="CNY",
                budget_currency="USD",
            )
        )
    gateway.clear()
    with pytest.raises(RuntimeError, match="cleared"):
        asyncio.run(
            gateway.load(
                rebound.batch.snapshot_version,
                display_currency="USD",
                budget_currency="USD",
            )
        )
