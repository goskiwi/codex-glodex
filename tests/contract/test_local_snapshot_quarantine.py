"""Record-isolation contract for the local snapshot adapter."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from glodex.adapters.local_snapshot import (
    EXCHANGE_RATES_SCHEMA_VERSION,
    MANIFEST_SCHEMA_VERSION,
    LocalSnapshotCatalog,
)
from glodex.domain.catalog import CatalogBatch
from glodex.domain.issues import IssueCode, IssueDisposition
from glodex.domain.pricing import KnownCost, UnknownCost

SNAPSHOT_VERSION = "m0-v1"


def _json_line(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _binding(field_path: str, evidence_id: str) -> dict[str, str]:
    return {"evidence_id": evidence_id, "field_path": field_path}


def _product(
    *,
    product_id: object = "product-1",
    provider_id: object = "provider-a",
    source_uri: object = "fixture://provider-a/products/product-1",
) -> dict[str, object]:
    return {
        "attributes": [
            {
                "evidence_id": "ev-product-weight",
                "name": "weight",
                "value": "1.2kg",
            }
        ],
        "category": "laptop",
        "entity_kind": "PRIMARY_PRODUCT",
        "field_evidence": [
            _binding("product.title", "ev-product-title"),
            _binding("product.category", "ev-product-category"),
            _binding("product.entity_kind", "ev-product-kind"),
        ],
        "product_id": product_id,
        "provider_id": provider_id,
        "snapshot_version": SNAPSHOT_VERSION,
        "source_uri": source_uri,
        "title": "TravelBook 14",
    }


def _known(amount: object, evidence_id: object) -> dict[str, object]:
    return {"amount": amount, "evidence_id": evidence_id, "kind": "KNOWN"}


def _unknown(reason: object = "NOT_DISCLOSED") -> dict[str, object]:
    return {"kind": "UNKNOWN", "reason": reason}


def _offer(
    *,
    offer_id: object = "offer-1",
    product_id: object = "product-1",
    provider_id: object = "provider-a",
    source_uri: object = "fixture://provider-a/offers/offer-1",
) -> dict[str, object]:
    return {
        "captured_at": "2026-01-01T00:00:00Z",
        "cost_components": {
            "currency": "USD",
            "duty": _unknown(),
            "item_price": _known("699", "ev-offer-item"),
            "shipping": _known("0.00", "ev-offer-shipping"),
            "tax": _unknown(),
        },
        "field_evidence": [
            _binding("offer.market", "ev-offer-market"),
            _binding("offer.cost_components.currency", "ev-offer-currency"),
            _binding("offer.inventory", "ev-offer-inventory"),
            _binding("offer.cost_components.item_price", "ev-offer-item"),
            _binding("offer.cost_components.shipping", "ev-offer-shipping"),
        ],
        "market": "US",
        "offer_id": offer_id,
        "product_id": product_id,
        "provider_id": provider_id,
        "snapshot_version": SNAPSHOT_VERSION,
        "source_uri": source_uri,
        "stock_status": "IN_STOCK",
    }


def _evidence(
    evidence_id: str,
    field_path: str,
    *,
    entity_type: str = "PRODUCT",
    product_id: str | None = "product-1",
    offer_id: str | None = None,
    currency: str | None = None,
    provider_id: str = "provider-a",
    source_uri: str = "fixture://provider-a/products/product-1",
) -> dict[str, object]:
    return {
        "captured_at": "2026-01-01T00:00:00Z",
        "currency": currency,
        "entity_type": entity_type,
        "evidence_id": evidence_id,
        "field_path": field_path,
        "offer_id": offer_id,
        "product_id": product_id,
        "provider_id": provider_id,
        "snapshot_version": SNAPSHOT_VERSION,
        "source_uri": source_uri,
    }


def _valid_evidence() -> list[dict[str, object]]:
    product_source = "fixture://provider-a/products/product-1"
    offer_source = "fixture://provider-a/offers/offer-1"
    product = [
        _evidence("ev-product-title", "product.title"),
        _evidence("ev-product-category", "product.category"),
        _evidence("ev-product-kind", "product.entity_kind"),
        _evidence("ev-product-weight", "product.attributes.weight"),
    ]
    offer = [
        _evidence(
            "ev-offer-market",
            "offer.market",
            entity_type="OFFER",
            offer_id="offer-1",
            source_uri=offer_source,
        ),
        _evidence(
            "ev-offer-currency",
            "offer.cost_components.currency",
            entity_type="OFFER",
            offer_id="offer-1",
            source_uri=offer_source,
        ),
        _evidence(
            "ev-offer-inventory",
            "offer.inventory",
            entity_type="OFFER",
            offer_id="offer-1",
            source_uri=offer_source,
        ),
        _evidence(
            "ev-offer-item",
            "offer.cost_components.item_price",
            entity_type="OFFER",
            offer_id="offer-1",
            source_uri=offer_source,
        ),
        _evidence(
            "ev-offer-shipping",
            "offer.cost_components.shipping",
            entity_type="OFFER",
            offer_id="offer-1",
            source_uri=offer_source,
        ),
    ]
    rate = [
        _evidence(
            "ev-rate-usd",
            "exchange_rate.base_per_unit",
            entity_type="EXCHANGE_RATE",
            product_id=None,
            currency="USD",
            provider_id="fixture-fx",
            source_uri="fixture://fx/USD",
        )
    ]
    assert product_source == product[0]["source_uri"]
    return [*product, *offer, *rate]


def _inventory(
    products: list[dict[str, object]],
    offers: list[dict[str, object]],
) -> tuple[list[str], list[str], list[str]]:
    providers = {
        value
        for record in [*products, *offers]
        if isinstance((value := record.get("provider_id")), str) and value
    }
    markets = {
        value for record in offers if isinstance((value := record.get("market")), str) and value
    }
    categories = {
        value for record in products if isinstance((value := record.get("category")), str) and value
    }
    return sorted(providers), sorted(markets), sorted(categories)


def _write_snapshot(
    root: Path,
    *,
    products: list[dict[str, object]] | None = None,
    offers: list[dict[str, object]] | None = None,
    evidence: list[dict[str, object]] | None = None,
    raw_override: tuple[str, bytes] | None = None,
    manifest_mutation: tuple[str, object] | None = None,
) -> Path:
    product_records = [_product()] if products is None else products
    offer_records = [_offer()] if offers is None else offers
    evidence_records = _valid_evidence() if evidence is None else evidence
    payloads = {
        "products": b"".join(_json_line(record) for record in product_records),
        "offers": b"".join(_json_line(record) for record in offer_records),
        "evidence": b"".join(_json_line(record) for record in evidence_records),
        "exchange_rates": _json_line(
            {
                "base_currency": "USD",
                "rates": [
                    {
                        "base_per_unit": "1",
                        "currency": "USD",
                        "evidence_id": "ev-rate-usd",
                        "minor_units": 2,
                    }
                ],
                "schema_version": EXCHANGE_RATES_SCHEMA_VERSION,
                "snapshot_version": SNAPSHOT_VERSION,
            }
        ),
    }
    if raw_override is not None:
        role, raw = raw_override
        payloads[role] = raw
    providers, markets, categories = _inventory(product_records, offer_records)
    counts = {
        "products": len(payloads["products"].splitlines()),
        "offers": len(payloads["offers"].splitlines()),
        "evidence": len(payloads["evidence"].splitlines()),
        "exchange_rates": 1,
    }
    manifest: dict[str, object] = {
        "base_currency": "USD",
        "categories": categories,
        "created_at": "2026-01-01T00:00:00Z",
        "currencies": ["USD"],
        "files": {
            role: {
                "path": f"{role}.json" if role == "exchange_rates" else f"{role}.jsonl",
                "record_count": counts[role],
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            for role, payload in payloads.items()
        },
        "generator": {"name": "glodex-test-fixture", "version": "1"},
        "markets": markets,
        "providers": providers,
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "snapshot_version": SNAPSHOT_VERSION,
    }
    if manifest_mutation is not None:
        key, value = manifest_mutation
        manifest[key] = value
    snapshot = root / SNAPSHOT_VERSION
    snapshot.mkdir(parents=True)
    for role, payload in payloads.items():
        suffix = "json" if role == "exchange_rates" else "jsonl"
        (snapshot / f"{role}.{suffix}").write_bytes(payload)
    (snapshot / "manifest.json").write_bytes(_json_line(manifest))
    return snapshot


def _load(root: Path) -> CatalogBatch:
    return asyncio.run(
        LocalSnapshotCatalog(root).load(
            SNAPSHOT_VERSION,
            display_currency="USD",
        )
    )


def _codes(batch: CatalogBatch) -> list[IssueCode]:
    return [issue.code for issue in batch.quarantine_issues]


@pytest.mark.contract
@pytest.mark.spec("GLO-P0-003", "GLO-P0-004", "AC-010")
def test_valid_records_materialize_an_evidence_closed_catalog(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"
    _write_snapshot(root)

    batch = _load(root)

    assert batch.fatal_issues == ()
    assert batch.quarantine_issues == ()
    assert len(batch.products) == 1
    assert len(batch.offers) == 1
    assert len(batch.evidence) == 10
    assert batch.products[0].snapshot_ordinal == 0
    assert batch.offers[0].snapshot_ordinal == 0
    costs = batch.offers[0].cost_components
    assert costs.item_price == KnownCost(
        amount=Decimal("699"),
        evidence_id="ev-offer-item",
    )
    assert costs.shipping == KnownCost(
        amount=Decimal("0.00"),
        evidence_id="ev-offer-shipping",
    )
    assert costs.tax == UnknownCost(reason="NOT_DISCLOSED")
    assert costs.duty == UnknownCost(reason="NOT_DISCLOSED")
    bindings = {item.field_path: item.evidence_id for item in batch.offers[0].field_evidence}
    assert bindings["offer.cost_components.item_price"] == "ev-offer-item"
    assert bindings["offer.cost_components.shipping"] == "ev-offer-shipping"


@pytest.mark.contract
@pytest.mark.spec("GLO-P0-003", "GLO-P0-005", "AC-005")
def test_unknown_cost_reason_survives_wire_to_catalog_without_normalization(
    tmp_path: Path,
) -> None:
    root = tmp_path / "snapshots"
    offer = _offer()
    costs = offer["cost_components"]
    assert isinstance(costs, dict)
    costs["duty"] = _unknown("TARIFF_NOT_PUBLISHED/2026-Q1")
    costs["tax"] = _unknown("UPSTREAM:TAX_PENDING")
    _write_snapshot(root, offers=[offer])

    batch = _load(root)

    parsed = batch.offers[0].cost_components
    assert parsed.duty == UnknownCost(reason="TARIFF_NOT_PUBLISHED/2026-Q1")
    assert parsed.tax == UnknownCost(reason="UPSTREAM:TAX_PENDING")


@pytest.mark.contract
@pytest.mark.spec("GLO-P0-003", "AC-010")
@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("product_id", "", IssueCode.MISSING_IDENTITY),
        ("provider_id", "", IssueCode.MISSING_SOURCE),
        ("source_uri", "", IssueCode.MISSING_SOURCE),
    ],
)
def test_missing_product_identity_or_source_is_quarantined(
    tmp_path: Path,
    field: str,
    value: object,
    code: IssueCode,
) -> None:
    root = tmp_path / "snapshots"
    dirty = _product()
    dirty[field] = value
    _write_snapshot(root, products=[_product(), dirty])

    batch = _load(root)

    assert batch.fatal_issues == ()
    assert len(batch.products) == 1
    assert _codes(batch) == [code]


@pytest.mark.contract
@pytest.mark.spec("GLO-P0-003", "AC-010")
def test_legal_json_with_a_record_schema_error_is_quarantined(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"
    dirty = _product(product_id="dirty")
    dirty["unexpected"] = True
    _write_snapshot(root, products=[_product(), dirty])

    batch = _load(root)

    assert batch.fatal_issues == ()
    assert len(batch.products) == 1
    assert _codes(batch) == [IssueCode.INVALID_RECORD]


@pytest.mark.contract
@pytest.mark.spec("GLO-P0-003", "AC-010")
@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("offer_id", "", IssueCode.MISSING_IDENTITY),
        ("product_id", "", IssueCode.MISSING_IDENTITY),
        ("provider_id", "", IssueCode.MISSING_SOURCE),
        ("source_uri", "", IssueCode.MISSING_SOURCE),
    ],
)
def test_missing_offer_identity_or_source_is_quarantined(
    tmp_path: Path,
    field: str,
    value: object,
    code: IssueCode,
) -> None:
    root = tmp_path / "snapshots"
    dirty = _offer()
    dirty[field] = value
    _write_snapshot(root, offers=[_offer(), dirty])

    batch = _load(root)

    assert batch.fatal_issues == ()
    assert len(batch.products) == 1
    assert len(batch.offers) == 1
    assert _codes(batch) == [code]


@pytest.mark.contract
@pytest.mark.spec("GLO-P0-003", "AC-010")
@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("evidence_id", "", IssueCode.MISSING_IDENTITY),
        ("provider_id", "", IssueCode.MISSING_SOURCE),
        ("source_uri", "", IssueCode.MISSING_SOURCE),
    ],
)
def test_missing_unreferenced_evidence_identity_or_source_is_quarantined(
    tmp_path: Path,
    field: str,
    value: object,
    code: IssueCode,
) -> None:
    root = tmp_path / "snapshots"
    dirty = _evidence("unused", "product.title")
    dirty[field] = value
    _write_snapshot(root, evidence=[*_valid_evidence(), dirty])

    batch = _load(root)

    assert batch.fatal_issues == ()
    assert len(batch.evidence) == 10
    assert _codes(batch) == [code]


@pytest.mark.contract
@pytest.mark.spec("GLO-P0-003", "AC-010")
@pytest.mark.parametrize(
    "component",
    [
        None,
        "699",
        699.0,
        {"kind": "KNOWN", "amount": "0", "evidence_id": "ev-offer-item"},
        {"kind": "KNOWN", "amount": "1.0", "evidence_id": "ev-offer-item"},
        {"kind": "KNOWN", "amount": "1e2", "evidence_id": "ev-offer-item"},
        {"kind": "KNOWN", "amount": 1, "evidence_id": "ev-offer-item"},
        {"kind": "KNOWN", "amount": "-1", "evidence_id": "ev-offer-item"},
        {"kind": "UNKNOWN"},
        {"kind": "UNKNOWN", "reason": ""},
        {"kind": "UNKNOWN", "reason": "NO_DATA", "evidence_id": "ev"},
    ],
)
def test_invalid_cost_discriminated_union_quarantines_only_the_offer(
    tmp_path: Path,
    component: object,
) -> None:
    root = tmp_path / "snapshots"
    dirty_offer = _offer()
    costs = dirty_offer["cost_components"]
    assert isinstance(costs, dict)
    costs["item_price"] = component
    _write_snapshot(root, offers=[dirty_offer])

    batch = _load(root)

    assert batch.fatal_issues == ()
    assert len(batch.products) == 1
    assert batch.offers == ()
    assert _codes(batch) == [IssueCode.INVALID_RECORD]


@pytest.mark.contract
@pytest.mark.spec("GLO-P0-003", "AC-010")
def test_known_cost_evidence_must_match_the_field_binding(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"
    dirty_offer = _offer()
    bindings = dirty_offer["field_evidence"]
    assert isinstance(bindings, list)
    bindings[-1] = _binding("offer.cost_components.shipping", "wrong-evidence")
    _write_snapshot(root, offers=[dirty_offer])

    batch = _load(root)

    assert batch.fatal_issues == ()
    assert batch.offers == ()
    assert _codes(batch) == [IssueCode.INVALID_RECORD]


@pytest.mark.contract
@pytest.mark.spec("GLO-P0-003", "AC-010")
@pytest.mark.parametrize(
    ("evidence_id", "expected_code"),
    [
        ("missing-evidence", IssueCode.EVIDENCE_NOT_FOUND),
        ("ev-product-title", IssueCode.EVIDENCE_ENTITY_MISMATCH),
    ],
)
def test_offer_evidence_must_exist_and_bind_to_the_same_offer(
    tmp_path: Path,
    evidence_id: str,
    expected_code: IssueCode,
) -> None:
    root = tmp_path / "snapshots"
    dirty = _offer()
    bindings = dirty["field_evidence"]
    assert isinstance(bindings, list)
    bindings[0] = _binding("offer.market", evidence_id)
    _write_snapshot(root, offers=[dirty])

    batch = _load(root)

    assert batch.fatal_issues == ()
    assert len(batch.products) == 1
    assert batch.offers == ()
    assert _codes(batch) == [expected_code]


@pytest.mark.contract
@pytest.mark.spec("GLO-P0-003", "AC-010")
def test_duplicate_evidence_quarantines_every_duplicate_record(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"
    unused = _evidence("duplicate", "product.title")
    records = [*_valid_evidence(), unused, copy.deepcopy(unused)]
    _write_snapshot(root, evidence=records)

    batch = _load(root)

    assert batch.fatal_issues == ()
    assert len(batch.evidence) == 10
    assert _codes(batch) == [
        IssueCode.DUPLICATE_EVIDENCE,
        IssueCode.DUPLICATE_EVIDENCE,
    ]
    duplicate_issues = batch.quarantine_issues
    assert len({issue.entity_ref for issue in duplicate_issues}) == 2


@pytest.mark.contract
@pytest.mark.spec("GLO-P0-003", "GLO-P0-004", "AC-010")
def test_missing_product_evidence_quarantines_product_then_orphan_offer(
    tmp_path: Path,
) -> None:
    root = tmp_path / "snapshots"
    product = _product()
    bindings = product["field_evidence"]
    assert isinstance(bindings, list)
    bindings[0] = _binding("product.title", "missing-evidence")
    _write_snapshot(root, products=[product])

    batch = _load(root)

    assert batch.fatal_issues == ()
    assert batch.products == ()
    assert batch.offers == ()
    assert _codes(batch) == [
        IssueCode.EVIDENCE_NOT_FOUND,
        IssueCode.ORPHAN_OFFER,
    ]


@pytest.mark.contract
@pytest.mark.spec("GLO-P0-003", "AC-010")
def test_wrong_evidence_binding_is_quarantined(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"
    evidence = _valid_evidence()
    evidence[0]["provider_id"] = "provider-b"
    _write_snapshot(root, evidence=evidence)

    batch = _load(root)

    assert batch.fatal_issues == ()
    assert batch.products == ()
    assert _codes(batch)[0] is IssueCode.EVIDENCE_ENTITY_MISMATCH


@pytest.mark.contract
@pytest.mark.spec("GLO-P0-003", "GLO-P0-004", "AC-010")
def test_orphan_offer_is_quarantined(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"
    orphan = _offer(product_id="missing-product")
    evidence = _valid_evidence()
    for record in evidence:
        if record["entity_type"] == "OFFER":
            record["product_id"] = "missing-product"
    _write_snapshot(root, offers=[orphan], evidence=evidence)

    batch = _load(root)

    assert batch.fatal_issues == ()
    assert len(batch.products) == 1
    assert batch.offers == ()
    assert _codes(batch) == [IssueCode.ORPHAN_OFFER]


@pytest.mark.contract
@pytest.mark.spec("GLO-P0-003", "AC-010")
@pytest.mark.parametrize(
    "raw",
    [
        b"{broken\n",
        b'{"snapshot_version":"m0-v1","x":1,"x":2}\n',
        _json_line({"snapshot_version": "m0-v2"}),
    ],
)
def test_syntax_duplicate_key_and_version_mismatch_remain_fatal(
    tmp_path: Path,
    raw: bytes,
) -> None:
    root = tmp_path / "snapshots"
    _write_snapshot(root, raw_override=("products", raw))

    batch = _load(root)

    assert len(batch.fatal_issues) == 1
    assert batch.products == ()
    assert batch.offers == ()
    assert batch.evidence == ()
    assert batch.exchange_rates is None
    assert batch.quarantine_issues == ()


@pytest.mark.contract
@pytest.mark.spec("GLO-P0-003")
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("providers", ["provider-b"]),
        ("markets", ["CN"]),
        ("categories", ["camera"]),
        ("currencies", ["EUR"]),
    ],
)
def test_manifest_inventories_are_verified(
    tmp_path: Path,
    name: str,
    value: object,
) -> None:
    root = tmp_path / "snapshots"
    _write_snapshot(root, manifest_mutation=(name, value))

    batch = _load(root)

    assert len(batch.fatal_issues) == 1
    assert batch.fatal_issues[0].code in {
        IssueCode.MANIFEST_INVALID,
        IssueCode.CORE_JSON_INVALID,
    }
    assert batch.products == ()


@pytest.mark.contract
@pytest.mark.spec("GLO-P0-003", "AC-010", "GLO-NFR-009")
def test_each_quarantined_record_has_one_safe_issue(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"
    dirty_product = _product(product_id="")
    dirty_offer = _offer(offer_id="")
    unused = _evidence("duplicate", "product.title")
    records = [*_valid_evidence(), unused, copy.deepcopy(unused)]
    _write_snapshot(
        root,
        products=[_product(), dirty_product],
        offers=[_offer(), dirty_offer],
        evidence=records,
    )

    batch = _load(root)

    assert batch.fatal_issues == ()
    assert len(batch.products) == 1
    assert len(batch.offers) == 1
    assert len(batch.quarantine_issues) == 4
    assert len({issue.entity_ref for issue in batch.quarantine_issues}) == 4
    assert all(
        issue.disposition is IssueDisposition.QUARANTINE for issue in batch.quarantine_issues
    )
    assert str(root.resolve()) not in repr(batch.quarantine_issues)
    assert "Traceback" not in repr(batch.quarantine_issues)
