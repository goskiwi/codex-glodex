"""Deterministically generate the committed Glodex ``m0-v1`` snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Final

SNAPSHOT_VERSION: Final = "m0-v1"
CREATED_AT: Final = "2026-01-01T00:00:00Z"
MANIFEST_SCHEMA_VERSION: Final = "glodex.snapshot-manifest.v1"
EXCHANGE_RATES_SCHEMA_VERSION: Final = "glodex.exchange-rates.v1"
GENERATOR_NAME: Final = "glodex-m0-fixture"
GENERATOR_VERSION: Final = "1.2.0"

PROVIDERS: Final = ("shop-us", "shop-eu", "shop-cn")
MARKETS: Final = ("US", "DE", "CN", "GB")
CURRENCIES: Final = ("USD", "EUR", "CNY", "GBP")
CATEGORIES: Final = (
    "laptop",
    "tablet",
    "phone",
    "laptop-accessory",
    "laptop-part",
    "laptop-decoration",
)
_PROVIDER_MARKET_CURRENCY: Final = {
    "shop-us": ("US", "USD"),
    "shop-eu": ("DE", "EUR"),
    "shop-cn": ("CN", "CNY"),
}
_COST_NAMES: Final = ("item_price", "shipping", "tax", "duty")


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _jsonl_bytes(records: tuple[dict[str, object], ...]) -> bytes:
    return b"".join(_json_bytes(record) for record in records)


def _binding(field_path: str, evidence_id: str) -> dict[str, object]:
    return {"evidence_id": evidence_id, "field_path": field_path}


def _evidence(
    *,
    evidence_id: str,
    entity_type: str,
    field_path: str,
    provider_id: str,
    source_uri: str,
    product_id: str | None,
    offer_id: str | None = None,
    currency: str | None = None,
) -> dict[str, object]:
    return {
        "captured_at": CREATED_AT,
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


def _product_evidence_id(provider_id: str, product_id: str, suffix: str) -> str:
    return f"ev-{provider_id}-{product_id}-{suffix}"


def _offer_evidence_id(provider_id: str, offer_id: str, suffix: str) -> str:
    return f"ev-{provider_id}-{offer_id}-{suffix}"


def _make_product(
    *,
    product_id: str,
    provider_id: str,
    title: str,
    category: str,
    entity_kind: str = "PRIMARY_PRODUCT",
    attributes: tuple[tuple[str, str], ...] = (
        ("travel_ready", "true"),
        ("verified_segment", "portable"),
    ),
) -> tuple[dict[str, object], tuple[dict[str, object], ...]]:
    source_uri = f"fixture://{provider_id}/products/{product_id}"
    bindings: list[dict[str, object]] = []
    evidence: list[dict[str, object]] = []
    for suffix, field_path in (
        ("title", "product.title"),
        ("category", "product.category"),
        ("entity-kind", "product.entity_kind"),
    ):
        evidence_id = _product_evidence_id(provider_id, product_id, suffix)
        bindings.append(_binding(field_path, evidence_id))
        evidence.append(
            _evidence(
                evidence_id=evidence_id,
                entity_type="PRODUCT",
                field_path=field_path,
                provider_id=provider_id,
                source_uri=source_uri,
                product_id=product_id,
            )
        )

    raw_attributes: list[dict[str, object]] = []
    for name, value in attributes:
        evidence_id = _product_evidence_id(provider_id, product_id, f"attribute-{name}")
        raw_attributes.append(
            {
                "evidence_id": evidence_id,
                "name": name,
                "value": value,
            }
        )
        evidence.append(
            _evidence(
                evidence_id=evidence_id,
                entity_type="PRODUCT",
                field_path=f"product.attributes.{name}",
                provider_id=provider_id,
                source_uri=source_uri,
                product_id=product_id,
            )
        )

    product: dict[str, object] = {
        "attributes": raw_attributes,
        "category": category,
        "entity_kind": entity_kind,
        "field_evidence": bindings,
        "product_id": product_id,
        "provider_id": provider_id,
        "snapshot_version": SNAPSHOT_VERSION,
        "source_uri": source_uri,
        "title": title,
    }
    return product, tuple(evidence)


def _known_cost(amount: str, evidence_id: str) -> dict[str, object]:
    return {
        "amount": amount,
        "evidence_id": evidence_id,
        "kind": "KNOWN",
    }


def _unknown_cost() -> dict[str, object]:
    return {
        "kind": "UNKNOWN",
        "reason": "NOT_DISCLOSED",
    }


def _make_offer(
    *,
    offer_id: str,
    product_id: str,
    provider_id: str,
    stock_status: str = "IN_STOCK",
    item_price: str = "650",
    shipping: str | None = "10",
    tax: str | None = "40",
    duty: str | None = "0.00",
    market: str | None = None,
    currency: str | None = None,
) -> tuple[dict[str, object], tuple[dict[str, object], ...]]:
    provider_market, provider_currency = _PROVIDER_MARKET_CURRENCY[provider_id]
    market = provider_market if market is None else market
    currency = provider_currency if currency is None else currency
    source_uri = f"fixture://{provider_id}/offers/{offer_id}"
    bindings: list[dict[str, object]] = []
    evidence: list[dict[str, object]] = []

    for suffix, field_path in (
        ("inventory", "offer.inventory"),
        ("market", "offer.market"),
        ("currency", "offer.cost_components.currency"),
    ):
        evidence_id = _offer_evidence_id(provider_id, offer_id, suffix)
        bindings.append(_binding(field_path, evidence_id))
        evidence.append(
            _evidence(
                evidence_id=evidence_id,
                entity_type="OFFER",
                field_path=field_path,
                provider_id=provider_id,
                source_uri=source_uri,
                product_id=product_id,
                offer_id=offer_id,
            )
        )

    raw_amounts = {
        "item_price": item_price,
        "shipping": shipping,
        "tax": tax,
        "duty": duty,
    }
    components: dict[str, object] = {"currency": currency}
    for name in _COST_NAMES:
        amount = raw_amounts[name]
        if amount is None:
            components[name] = _unknown_cost()
            continue
        evidence_id = _offer_evidence_id(provider_id, offer_id, name)
        components[name] = _known_cost(amount, evidence_id)
        field_path = f"offer.cost_components.{name}"
        bindings.append(_binding(field_path, evidence_id))
        evidence.append(
            _evidence(
                evidence_id=evidence_id,
                entity_type="OFFER",
                field_path=field_path,
                provider_id=provider_id,
                source_uri=source_uri,
                product_id=product_id,
                offer_id=offer_id,
            )
        )

    offer: dict[str, object] = {
        "captured_at": CREATED_AT,
        "cost_components": components,
        "field_evidence": bindings,
        "market": market,
        "offer_id": offer_id,
        "product_id": product_id,
        "provider_id": provider_id,
        "snapshot_version": SNAPSHOT_VERSION,
        "source_uri": source_uri,
        "stock_status": stock_status,
    }
    return offer, tuple(evidence)


def _primary_product_specs() -> tuple[tuple[str, str, str, str], ...]:
    sentinels = (
        (
            "prod-cross-market-travelbook",
            "shop-us",
            "Glodex TravelBook Cross",
            "laptop",
        ),
        (
            "prod-budget-exact-800",
            "shop-us",
            "Glodex BoundaryBook 800",
            "laptop",
        ),
        (
            "prod-budget-over-800-01",
            "shop-us",
            "Glodex BoundaryBook 800.01",
            "laptop",
        ),
        (
            "prod-out-of-stock",
            "shop-eu",
            "Glodex SoldOut Air",
            "laptop",
        ),
        (
            "prod-unknown-fee",
            "shop-cn",
            "Glodex Unknown Duty",
            "laptop",
        ),
        (
            "prod-tie-alpha",
            "shop-us",
            "Glodex TieBook Alpha",
            "laptop",
        ),
        (
            "prod-tie-beta",
            "shop-eu",
            "Glodex TieBook Beta",
            "laptop",
        ),
    )
    generated: list[tuple[str, str, str, str]] = []
    categories = ("laptop", "tablet", "phone")
    for number in range(1, 24):
        category = categories[(number - 1) % len(categories)]
        provider = PROVIDERS[(number - 1) % len(PROVIDERS)]
        generated.append(
            (
                f"prod-{category}-{number:02d}",
                provider,
                f"Glodex {category.title()} {number:02d}",
                category,
            )
        )
    return (*sentinels, *generated)


def _build_records() -> tuple[
    tuple[dict[str, object], ...],
    tuple[dict[str, object], ...],
    tuple[dict[str, object], ...],
]:
    products: list[dict[str, object]] = []
    offers: list[dict[str, object]] = []
    evidence: list[dict[str, object]] = []

    primary_specs = _primary_product_specs()
    for index, (product_id, provider_id, title, category) in enumerate(primary_specs):
        product, product_evidence = _make_product(
            product_id=product_id,
            provider_id=provider_id,
            title=title,
            category=category,
        )
        products.append(product)
        evidence.extend(product_evidence)

        offer_provider = provider_id
        offer_id = f"offer-{product_id.removeprefix('prod-')}"
        offer_kwargs: dict[str, object] = {}
        if product_id == "prod-budget-exact-800":
            offer_id = "offer-budget-exact-800"
            offer_kwargs = {
                "item_price": "740",
                "shipping": "20",
                "tax": "40",
                "duty": "0.00",
            }
        elif product_id == "prod-budget-over-800-01":
            offer_id = "offer-budget-over-800-01"
            offer_kwargs = {
                "item_price": "740",
                "shipping": "20",
                "tax": "40.01",
                "duty": "0.00",
            }
        elif product_id == "prod-out-of-stock":
            offer_id = "offer-out-of-stock"
            offer_kwargs = {"stock_status": "OUT_OF_STOCK"}
        elif product_id == "prod-unknown-fee":
            offer_id = "offer-unknown-duty"
            offer_kwargs = {"duty": None}
        elif product_id == "prod-tie-alpha":
            offer_id = "offer-tie-alpha"
            offer_provider = "shop-us"
            offer_kwargs = {
                "item_price": "700",
                "shipping": "20",
                "tax": "30",
                "duty": "0.00",
            }
        elif product_id == "prod-tie-beta":
            offer_id = "offer-tie-beta"
            offer_provider = "shop-us"
            offer_kwargs = {
                "item_price": "700",
                "shipping": "20",
                "tax": "30",
                "duty": "0.00",
            }
        else:
            currency = _PROVIDER_MARKET_CURRENCY[provider_id][1]
            if currency == "EUR":
                offer_kwargs = {
                    "item_price": str(500 + index),
                    "shipping": "12",
                    "tax": "35",
                    "duty": "0.00",
                }
            elif currency == "CNY":
                offer_kwargs = {
                    "item_price": str(4_000 + index * 10),
                    "shipping": "80",
                    "tax": "160",
                    "duty": "0.00",
                }

        offer, offer_evidence = _make_offer(
            offer_id=offer_id,
            product_id=product_id,
            provider_id=offer_provider,
            **offer_kwargs,  # type: ignore[arg-type]
        )
        offers.append(offer)
        evidence.extend(offer_evidence)

    cross_product, cross_product_evidence = _make_product(
        product_id="prod-cross-market-travelbook",
        provider_id="shop-eu",
        title="Glodex TravelBook Cross",
        category="laptop",
    )
    products.append(cross_product)
    evidence.extend(cross_product_evidence)
    cross_offer, cross_offer_evidence = _make_offer(
        offer_id="offer-cross-market-eu",
        product_id="prod-cross-market-travelbook",
        provider_id="shop-eu",
        item_price="680",
        shipping="15",
        tax="45",
        duty="0.00",
    )
    offers.append(cross_offer)
    evidence.extend(cross_offer_evidence)

    cross_cn_product, cross_cn_product_evidence = _make_product(
        product_id="prod-cross-market-travelbook",
        provider_id="shop-cn",
        title="Glodex TravelBook Cross",
        category="laptop",
    )
    products.append(cross_cn_product)
    evidence.extend(cross_cn_product_evidence)
    cross_cn_offer, cross_cn_offer_evidence = _make_offer(
        offer_id="offer-cross-market-cn",
        product_id="prod-cross-market-travelbook",
        provider_id="shop-cn",
        item_price="4900",
        shipping="80",
        tax="260",
        duty="0.00",
    )
    offers.append(cross_cn_offer)
    evidence.extend(cross_cn_offer_evidence)

    cross_gb_offer, cross_gb_offer_evidence = _make_offer(
        offer_id="offer-cross-market-gb",
        product_id="prod-cross-market-travelbook",
        provider_id="shop-eu",
        market="GB",
        currency="GBP",
        item_price="560",
        shipping="15",
        tax="20",
        duty="0.00",
    )
    offers.append(cross_gb_offer)
    evidence.extend(cross_gb_offer_evidence)

    entity_specs = (
        (
            "prod-accessory-stand",
            "shop-us",
            "Travel laptop ergonomic stand",
            "laptop-accessory",
            "ACCESSORY",
        ),
        (
            "prod-replacement-battery",
            "shop-eu",
            "TravelBook laptop replacement battery",
            "laptop-part",
            "REPLACEMENT_PART",
        ),
        (
            "prod-decoration-sticker",
            "shop-cn",
            "Premium laptop travel edition sticker",
            "laptop-decoration",
            "DECORATION",
        ),
        (
            "prod-unknown-entity",
            "shop-us",
            "Mystery laptop bundle listing",
            "laptop",
            "UNKNOWN",
        ),
    )
    for product_id, provider_id, title, category, entity_kind in entity_specs:
        product, product_evidence = _make_product(
            product_id=product_id,
            provider_id=provider_id,
            title=title,
            category=category,
            entity_kind=entity_kind,
        )
        products.append(product)
        evidence.extend(product_evidence)
        offer, offer_evidence = _make_offer(
            offer_id=f"offer-{product_id.removeprefix('prod-')}",
            product_id=product_id,
            provider_id=provider_id,
            item_price="40",
            shipping="5",
            tax="3",
            duty="0.00",
        )
        offers.append(offer)
        evidence.extend(offer_evidence)

    dirty_product_id, dirty_product_id_evidence = _make_product(
        product_id="dirty-temporary-id",
        provider_id="shop-us",
        title="Dirty product missing ID",
        category="laptop",
    )
    dirty_product_id["product_id"] = ""
    products.append(dirty_product_id)
    evidence.extend(dirty_product_id_evidence)

    dirty_product_source, dirty_product_source_evidence = _make_product(
        product_id="dirty-missing-source",
        provider_id="shop-eu",
        title="Dirty product missing source",
        category="tablet",
    )
    dirty_product_source["source_uri"] = ""
    products.append(dirty_product_source)
    evidence.extend(dirty_product_source_evidence)

    dirty_product_evidence, _ = _make_product(
        product_id="dirty-missing-evidence",
        provider_id="shop-cn",
        title="Dirty product missing evidence",
        category="phone",
    )
    dirty_product_evidence["field_evidence"] = [
        _binding("product.title", "ev-dirty-missing-reference"),
        _binding("product.category", "ev-dirty-missing-reference-category"),
        _binding("product.entity_kind", "ev-dirty-missing-reference-kind"),
    ]
    products.append(dirty_product_evidence)

    dirty_offer_id, dirty_offer_id_evidence = _make_offer(
        offer_id="dirty-temporary-offer",
        product_id="prod-laptop-01",
        provider_id="shop-us",
    )
    dirty_offer_id["offer_id"] = ""
    offers.append(dirty_offer_id)
    evidence.extend(dirty_offer_id_evidence)

    dirty_offer_source, dirty_offer_source_evidence = _make_offer(
        offer_id="dirty-missing-source",
        product_id="prod-tablet-02",
        provider_id="shop-eu",
    )
    dirty_offer_source["source_uri"] = ""
    offers.append(dirty_offer_source)
    evidence.extend(dirty_offer_source_evidence)

    orphan_offer, orphan_offer_evidence = _make_offer(
        offer_id="dirty-orphan-offer",
        product_id="prod-orphan",
        provider_id="shop-cn",
    )
    offers.append(orphan_offer)
    evidence.extend(orphan_offer_evidence)

    missing_evidence_offer, missing_offer_evidence = _make_offer(
        offer_id="dirty-missing-evidence",
        product_id="prod-phone-03",
        provider_id="shop-us",
    )
    raw_missing_bindings = missing_evidence_offer["field_evidence"]
    if not isinstance(raw_missing_bindings, list):
        raise TypeError("fixture offer field_evidence must be a list")
    missing_bindings = list(raw_missing_bindings)
    missing_bindings[0] = _binding("offer.inventory", "ev-dirty-missing-reference")
    missing_evidence_offer["field_evidence"] = missing_bindings
    offers.append(missing_evidence_offer)
    evidence.extend(missing_offer_evidence)

    rate_evidence = tuple(
        _evidence(
            evidence_id=f"ev-rate-{currency.lower()}",
            entity_type="EXCHANGE_RATE",
            field_path="exchange_rate.base_per_unit",
            provider_id="fixture-fx",
            source_uri=f"fixture://fx/{currency}",
            product_id=None,
            currency=currency,
        )
        for currency in CURRENCIES
    )
    evidence.extend(rate_evidence)

    return tuple(products), tuple(offers), tuple(evidence)


def _exchange_rates() -> dict[str, object]:
    return {
        "base_currency": "USD",
        "rates": [
            {
                "base_per_unit": "1",
                "currency": "USD",
                "evidence_id": "ev-rate-usd",
                "minor_units": 2,
            },
            {
                "base_per_unit": "1.08",
                "currency": "EUR",
                "evidence_id": "ev-rate-eur",
                "minor_units": 2,
            },
            {
                "base_per_unit": "0.14",
                "currency": "CNY",
                "evidence_id": "ev-rate-cny",
                "minor_units": 2,
            },
            {
                "base_per_unit": "1.27",
                "currency": "GBP",
                "evidence_id": "ev-rate-gbp",
                "minor_units": 2,
            },
        ],
        "schema_version": EXCHANGE_RATES_SCHEMA_VERSION,
        "snapshot_version": SNAPSHOT_VERSION,
    }


def _manifest(payloads: dict[str, bytes], record_counts: dict[str, int]) -> dict[str, object]:
    return {
        "base_currency": "USD",
        "categories": list(CATEGORIES),
        "created_at": CREATED_AT,
        "currencies": list(CURRENCIES),
        "files": {
            role: {
                "path": filename,
                "record_count": record_counts[role],
                "sha256": hashlib.sha256(payloads[role]).hexdigest(),
            }
            for role, filename in (
                ("products", "products.jsonl"),
                ("offers", "offers.jsonl"),
                ("evidence", "evidence.jsonl"),
                ("exchange_rates", "exchange_rates.json"),
            )
        },
        "generator": {
            "name": GENERATOR_NAME,
            "version": GENERATOR_VERSION,
        },
        "markets": list(MARKETS),
        "providers": list(PROVIDERS),
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "snapshot_version": SNAPSHOT_VERSION,
    }


def generate_snapshot(snapshot_dir: Path) -> None:
    """Write a complete snapshot and a manifest computed from its exact bytes."""

    if not isinstance(snapshot_dir, Path):
        raise TypeError("snapshot_dir must be a Path")
    products, offers, evidence = _build_records()
    evidence_ids = tuple(str(item["evidence_id"]) for item in evidence)
    if len(evidence_ids) != len(set(evidence_ids)):
        raise ValueError("fixture evidence IDs must be unique")
    primary_ids = {
        str(product["product_id"])
        for product in products
        if product.get("entity_kind") == "PRIMARY_PRODUCT"
        and product.get("product_id")
        and not str(product["product_id"]).startswith("dirty-")
    }
    if len(primary_ids) < 30:
        raise ValueError("fixture must contain at least 30 canonical primary products")

    exchange_rates = _exchange_rates()
    payloads = {
        "products": _jsonl_bytes(products),
        "offers": _jsonl_bytes(offers),
        "evidence": _jsonl_bytes(evidence),
        "exchange_rates": _json_bytes(exchange_rates),
    }
    rates = exchange_rates["rates"]
    if not isinstance(rates, list):
        raise TypeError("exchange-rate fixture must contain a list")
    record_counts = {
        "products": len(products),
        "offers": len(offers),
        "evidence": len(evidence),
        "exchange_rates": len(rates),
    }
    manifest = _manifest(payloads, record_counts)

    snapshot_dir.mkdir(parents=True, exist_ok=True)
    for role, filename in (
        ("products", "products.jsonl"),
        ("offers", "offers.jsonl"),
        ("evidence", "evidence.jsonl"),
        ("exchange_rates", "exchange_rates.json"),
    ):
        (snapshot_dir / filename).write_bytes(payloads[role])
    (snapshot_dir / "manifest.json").write_bytes(_json_bytes(manifest))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "data" / "snapshots" / SNAPSHOT_VERSION,
        help="snapshot directory to create",
    )
    args = parser.parse_args(argv)
    generate_snapshot(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
