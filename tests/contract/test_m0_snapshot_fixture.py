"""Contract for the committed, deterministic m0-v1 acceptance snapshot."""

from __future__ import annotations

import asyncio
import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from glodex.adapters.local_snapshot import LocalSnapshotCatalog
from glodex.domain.catalog import aggregate_catalog_batch
from glodex.domain.issues import IssueCode
from scripts.generate_m0_snapshot import SNAPSHOT_VERSION, generate_snapshot

pytestmark = [
    pytest.mark.contract,
    pytest.mark.spec(
        "GLO-P0-003",
        "GLO-P0-004",
        "GLO-P0-005",
        "GLO-P0-006",
        "GLO-P0-008",
    ),
]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_ROOT = PROJECT_ROOT / "data" / "snapshots"
SNAPSHOT_DIR = SNAPSHOT_ROOT / SNAPSHOT_VERSION
FILE_ROLES = ("products", "offers", "evidence", "exchange_rates")
COMMITTED_FILES = (
    "manifest.json",
    "products.jsonl",
    "offers.jsonl",
    "evidence.jsonl",
    "exchange_rates.json",
)


def _json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _jsonl(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = json.loads(line)
        assert isinstance(value, dict)
        records.append(value)
    return records


def _file_spec(manifest: dict[str, object], role: str) -> dict[str, object]:
    files = manifest["files"]
    assert isinstance(files, dict)
    spec = files[role]
    assert isinstance(spec, dict)
    return spec


def _cost_total(offer: dict[str, object]) -> Decimal | None:
    raw = offer["cost_components"]
    assert isinstance(raw, dict)
    total = Decimal(0)
    for name in ("item_price", "shipping", "tax", "duty"):
        component = raw[name]
        assert isinstance(component, dict)
        if component["kind"] == "UNKNOWN":
            return None
        assert component["kind"] == "KNOWN"
        total += Decimal(str(component["amount"]))
    return total


def test_generator_is_byte_deterministic_and_matches_committed_snapshot(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first" / SNAPSHOT_VERSION
    second = tmp_path / "second" / SNAPSHOT_VERSION

    generate_snapshot(first)
    generate_snapshot(second)

    for name in COMMITTED_FILES:
        first_bytes = (first / name).read_bytes()
        assert (second / name).read_bytes() == first_bytes
        assert (SNAPSHOT_DIR / name).read_bytes() == first_bytes


def test_manifest_hashes_counts_and_inventory_are_exact() -> None:
    manifest = _json(SNAPSHOT_DIR / "manifest.json")

    assert manifest["schema_version"] == "glodex.snapshot-manifest.v1"
    assert manifest["snapshot_version"] == SNAPSHOT_VERSION
    assert manifest["created_at"] == "2026-01-01T00:00:00Z"
    assert manifest["base_currency"] == "USD"
    assert manifest["generator"] == {
        "name": "glodex-m0-fixture",
        "version": "1.2.0",
    }
    for role in FILE_ROLES:
        spec = _file_spec(manifest, role)
        relative_path = spec["path"]
        assert isinstance(relative_path, str)
        assert not Path(relative_path).is_absolute()
        assert ".." not in Path(relative_path).parts
        payload = (SNAPSHOT_DIR / relative_path).read_bytes()
        assert spec["sha256"] == hashlib.sha256(payload).hexdigest()
        if role == "exchange_rates":
            rate_payload = json.loads(payload)
            assert isinstance(rate_payload, dict)
            rates = rate_payload["rates"]
            assert isinstance(rates, list)
            actual_count = len(rates)
        else:
            actual_count = len(payload.splitlines())
        assert spec["record_count"] == actual_count

    assert len(manifest["providers"]) >= 3  # type: ignore[arg-type]
    assert len(manifest["markets"]) >= 3  # type: ignore[arg-type]
    assert len(manifest["categories"]) >= 3  # type: ignore[arg-type]
    assert len(manifest["currencies"]) >= 3  # type: ignore[arg-type]


def test_fixture_has_30_canonical_products_and_three_provider_cross_source_product() -> None:
    products = _jsonl(SNAPSHOT_DIR / "products.jsonl")
    valid = [
        product
        for product in products
        if isinstance(product.get("product_id"), str)
        and product["product_id"]
        and isinstance(product.get("source_uri"), str)
        and product["source_uri"]
        and not str(product["product_id"]).startswith("dirty-")
    ]
    canonical_ids = {str(product["product_id"]) for product in valid}
    primary_ids = {
        str(product["product_id"])
        for product in valid
        if product["entity_kind"] == "PRIMARY_PRODUCT"
    }

    assert len(canonical_ids) >= 30
    assert len(primary_ids) >= 30
    assert len({str(product["category"]) for product in valid}) >= 3

    cross_source_records = [
        product for product in valid if product["product_id"] == "prod-cross-market-travelbook"
    ]
    assert len(cross_source_records) == 3
    assert {str(product["provider_id"]) for product in cross_source_records} == {
        "shop-us",
        "shop-eu",
        "shop-cn",
    }
    assert len({str(product["source_uri"]) for product in cross_source_records}) == 3
    assert {
        (product["title"], product["category"], product["entity_kind"])
        for product in cross_source_records
    } == {("Glodex TravelBook Cross", "laptop", "PRIMARY_PRODUCT")}


def test_fixture_contains_all_price_stock_entity_and_tie_sentinels() -> None:
    products = {
        str(product["product_id"]): product
        for product in _jsonl(SNAPSHOT_DIR / "products.jsonl")
        if product.get("product_id")
    }
    offers = {
        str(offer["offer_id"]): offer
        for offer in _jsonl(SNAPSHOT_DIR / "offers.jsonl")
        if offer.get("offer_id")
    }

    assert _cost_total(offers["offer-budget-exact-800"]) == Decimal("800.00")
    assert _cost_total(offers["offer-budget-over-800-01"]) == Decimal("800.01")
    assert offers["offer-out-of-stock"]["stock_status"] == "OUT_OF_STOCK"
    assert _cost_total(offers["offer-unknown-duty"]) is None
    assert offers["offer-unknown-duty"]["cost_components"]["duty"] == {  # type: ignore[index]
        "kind": "UNKNOWN",
        "reason": "NOT_DISCLOSED",
    }
    assert products["prod-accessory-stand"]["entity_kind"] == "ACCESSORY"
    assert products["prod-replacement-battery"]["entity_kind"] == "REPLACEMENT_PART"
    assert products["prod-decoration-sticker"]["entity_kind"] == "DECORATION"
    assert products["prod-unknown-entity"]["entity_kind"] == "UNKNOWN"
    assert "laptop" in str(products["prod-decoration-sticker"]["title"]).lower()

    tie_alpha = offers["offer-tie-alpha"]
    tie_beta = offers["offer-tie-beta"]
    assert tie_alpha["cost_components"]["currency"] == "USD"  # type: ignore[index]
    assert tie_beta["cost_components"]["currency"] == "USD"  # type: ignore[index]
    assert _cost_total(tie_alpha) == _cost_total(tie_beta)
    assert _cost_total(tie_alpha) is not None
    assert (tie_alpha["provider_id"], tie_alpha["offer_id"]) < (
        tie_beta["provider_id"],
        tie_beta["offer_id"],
    )


def test_fixture_has_three_provider_market_currency_coverage() -> None:
    products = _jsonl(SNAPSHOT_DIR / "products.jsonl")
    offers = _jsonl(SNAPSHOT_DIR / "offers.jsonl")

    providers = {
        str(product.get("provider_id")) for product in products if product.get("provider_id")
    }
    markets = {str(offer.get("market")) for offer in offers if offer.get("market")}
    assert len(providers) >= 3
    assert len(markets) >= 3
    currencies = {
        str(costs["currency"])
        for offer in offers
        if isinstance((costs := offer.get("cost_components")), dict)
    }
    assert currencies >= {"USD", "EUR", "CNY", "GBP"}


def test_cost_components_use_strict_known_unknown_discriminants() -> None:
    offers = _jsonl(SNAPSHOT_DIR / "offers.jsonl")

    for offer in offers:
        costs = offer.get("cost_components")
        if not isinstance(costs, dict):
            continue
        bindings = {
            binding["field_path"]: binding["evidence_id"]
            for binding in offer.get("field_evidence", [])
            if isinstance(binding, dict)
        }
        assert set(costs) == {"currency", "item_price", "shipping", "tax", "duty"}
        for name in ("item_price", "shipping", "tax", "duty"):
            component = costs[name]
            assert isinstance(component, dict)
            if component["kind"] == "KNOWN":
                assert set(component) == {"kind", "amount", "evidence_id"}
                assert isinstance(component["amount"], str)
                Decimal(component["amount"])
                assert bindings[f"offer.cost_components.{name}"] == component["evidence_id"]
                if Decimal(component["amount"]).is_zero():
                    assert component["amount"] == "0.00"
            else:
                assert component == {
                    "kind": "UNKNOWN",
                    "reason": "NOT_DISCLOSED",
                }


def test_dirty_records_cover_missing_identity_source_evidence_and_orphan() -> None:
    products = _jsonl(SNAPSHOT_DIR / "products.jsonl")
    offers = _jsonl(SNAPSHOT_DIR / "offers.jsonl")
    evidence_ids = {
        str(record["evidence_id"])
        for record in _jsonl(SNAPSHOT_DIR / "evidence.jsonl")
        if record.get("evidence_id")
    }
    product_ids = {
        str(product["product_id"])
        for product in products
        if product.get("product_id") and product.get("source_uri")
    }

    assert any(not product.get("product_id") for product in products)
    assert any(not product.get("source_uri") for product in products)
    assert any(not offer.get("offer_id") for offer in offers)
    assert any(not offer.get("source_uri") for offer in offers)
    assert any(
        offer.get("product_id") not in product_ids for offer in offers if offer.get("product_id")
    )
    referenced_ids = {
        str(binding["evidence_id"])
        for record in (*products, *offers)
        for binding in record.get("field_evidence", [])
        if isinstance(binding, dict) and binding.get("evidence_id")
    }
    assert "ev-dirty-missing-reference" in referenced_ids - evidence_ids


def test_every_clean_output_field_has_evidence_and_evidence_ids_are_unique() -> None:
    products = _jsonl(SNAPSHOT_DIR / "products.jsonl")
    offers = _jsonl(SNAPSHOT_DIR / "offers.jsonl")
    evidence = _jsonl(SNAPSHOT_DIR / "evidence.jsonl")
    evidence_ids = [str(record["evidence_id"]) for record in evidence]

    assert len(evidence_ids) == len(set(evidence_ids))
    available = set(evidence_ids)
    for product in products:
        product_id = product.get("product_id")
        if not product_id or str(product_id).startswith("dirty-"):
            continue
        field_paths = {
            binding["field_path"]: binding["evidence_id"]
            for binding in product["field_evidence"]  # type: ignore[union-attr]
        }
        assert {"product.title", "product.category", "product.entity_kind"} <= set(field_paths)
        assert set(field_paths.values()) <= available
        for attribute in product["attributes"]:  # type: ignore[union-attr]
            assert attribute["evidence_id"] in available

    for offer in offers:
        offer_id = offer.get("offer_id")
        if not offer_id or str(offer_id).startswith("dirty-"):
            continue
        if not offer.get("source_uri") or offer.get("product_id") == "prod-orphan":
            continue
        field_paths = {
            binding["field_path"]: binding["evidence_id"]
            for binding in offer["field_evidence"]  # type: ignore[union-attr]
        }
        assert {
            "offer.inventory",
            "offer.market",
            "offer.cost_components.currency",
        } <= set(field_paths)
        assert set(field_paths.values()) <= available


def test_committed_fixture_is_accepted_by_the_strict_snapshot_boundary() -> None:
    batch = asyncio.run(
        LocalSnapshotCatalog(SNAPSHOT_ROOT).load(
            SNAPSHOT_VERSION,
            display_currency="USD",
            budget_currency="EUR",
        )
    )

    assert batch.fatal_issues == ()
    assert batch.exchange_rates is not None
    assert batch.exchange_rates.supported_currencies == frozenset({"USD", "EUR", "CNY", "GBP"})
    assert len(batch.products) == 36
    assert len(batch.offers) == 37
    assert len(batch.evidence) == 480
    assert tuple(issue.code for issue in batch.quarantine_issues) == (
        IssueCode.MISSING_IDENTITY,
        IssueCode.MISSING_SOURCE,
        IssueCode.EVIDENCE_NOT_FOUND,
        IssueCode.MISSING_IDENTITY,
        IssueCode.MISSING_SOURCE,
        IssueCode.ORPHAN_OFFER,
        IssueCode.EVIDENCE_NOT_FOUND,
    )
    aggregated = aggregate_catalog_batch(batch)
    assert len(aggregated.products) == 34
    assert len(aggregated.offers) == 37
    assert aggregated.offers_conserved
    cross_market = next(
        product
        for product in aggregated.products
        if product.product_id == "prod-cross-market-travelbook"
    )
    assert len(cross_market.sources) == 3
    assert {source.provider_id for source in cross_market.sources} == {
        "shop-us",
        "shop-eu",
        "shop-cn",
    }


def test_fixture_contains_no_secret_shaped_content_or_user_data() -> None:
    rendered = "\n".join(
        (SNAPSHOT_DIR / name).read_text(encoding="utf-8") for name in COMMITTED_FILES
    ).lower()

    for forbidden in ("api_key", "password", "bearer ", "private_key", "sk-"):
        assert forbidden not in rendered
