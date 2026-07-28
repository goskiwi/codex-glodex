"""Generate the deterministic, uncommitted 20k-product performance snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path
from typing import BinaryIO, Final

SNAPSHOT_VERSION: Final = "perf-20k-v1"
PRODUCT_COUNT: Final = 20_000
DEFAULT_SEED: Final = 20_260_705
CREATED_AT: Final = "2026-01-01T00:00:00Z"
GENERATOR_NAME: Final = "glodex-performance-fixture"
GENERATOR_VERSION: Final = "1.0.0"
MANIFEST_SCHEMA_VERSION: Final = "glodex.snapshot-manifest.v1"
EXCHANGE_RATES_SCHEMA_VERSION: Final = "glodex.exchange-rates.v1"

_PROVIDER_ID: Final = "perf-local"
_MARKET: Final = "US"
_CURRENCY: Final = "USD"
_MATCHING_PRODUCT_COUNT: Final = 3
_TITLE_WORDS: Final = ("Atlas", "Comet", "Orbit", "Vector")
_FILE_NAMES: Final = {
    "products": "products.jsonl",
    "offers": "offers.jsonl",
    "evidence": "evidence.jsonl",
    "exchange_rates": "exchange_rates.json",
}


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _write_json_line(stream: BinaryIO, value: object) -> None:
    stream.write(_json_bytes(value))


def _product_evidence(
    product_id: str,
    field_path: str,
    suffix: str,
) -> tuple[dict[str, str], dict[str, object]]:
    evidence_id = f"ev-{product_id}-{suffix}"
    return (
        {"evidence_id": evidence_id, "field_path": field_path},
        {
            "captured_at": CREATED_AT,
            "currency": None,
            "entity_type": "PRODUCT",
            "evidence_id": evidence_id,
            "field_path": field_path,
            "offer_id": None,
            "product_id": product_id,
            "provider_id": _PROVIDER_ID,
            "snapshot_version": SNAPSHOT_VERSION,
            "source_uri": f"fixture://{_PROVIDER_ID}/products/{product_id}",
        },
    )


def _offer_evidence(
    product_id: str,
    offer_id: str,
    field_path: str,
    suffix: str,
) -> tuple[dict[str, str], dict[str, object]]:
    evidence_id = f"ev-{offer_id}-{suffix}"
    return (
        {"evidence_id": evidence_id, "field_path": field_path},
        {
            "captured_at": CREATED_AT,
            "currency": None,
            "entity_type": "OFFER",
            "evidence_id": evidence_id,
            "field_path": field_path,
            "offer_id": offer_id,
            "product_id": product_id,
            "provider_id": _PROVIDER_ID,
            "snapshot_version": SNAPSHOT_VERSION,
            "source_uri": f"fixture://{_PROVIDER_ID}/offers/{offer_id}",
        },
    )


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_products_and_evidence(snapshot_dir: Path, seed: int) -> int:
    rng = random.Random(seed)
    evidence_count = 0
    products_path = snapshot_dir / _FILE_NAMES["products"]
    evidence_path = snapshot_dir / _FILE_NAMES["evidence"]
    with products_path.open("wb") as products, evidence_path.open("wb") as evidence:
        for ordinal in range(PRODUCT_COUNT):
            product_id = f"perf-product-{ordinal:05d}"
            category = "laptop" if ordinal < _MATCHING_PRODUCT_COUNT else "tablet"
            title = f"Glodex {rng.choice(_TITLE_WORDS)} {category.title()} {ordinal:05d}"
            bindings: list[dict[str, str]] = []
            for field_path, suffix in (
                ("product.title", "title"),
                ("product.category", "category"),
                ("product.entity_kind", "entity-kind"),
            ):
                binding, reference = _product_evidence(product_id, field_path, suffix)
                bindings.append(binding)
                _write_json_line(evidence, reference)
                evidence_count += 1
            _write_json_line(
                products,
                {
                    "attributes": [],
                    "category": category,
                    "entity_kind": "PRIMARY_PRODUCT",
                    "field_evidence": bindings,
                    "product_id": product_id,
                    "provider_id": _PROVIDER_ID,
                    "snapshot_version": SNAPSHOT_VERSION,
                    "source_uri": f"fixture://{_PROVIDER_ID}/products/{product_id}",
                    "title": title,
                },
            )

        for ordinal in range(_MATCHING_PRODUCT_COUNT):
            product_id = f"perf-product-{ordinal:05d}"
            offer_id = f"perf-offer-{ordinal:05d}"
            for field_path, suffix in (
                ("offer.inventory", "inventory"),
                ("offer.market", "market"),
                ("offer.cost_components.currency", "currency"),
                ("offer.cost_components.item_price", "item-price"),
                ("offer.cost_components.shipping", "shipping"),
                ("offer.cost_components.tax", "tax"),
                ("offer.cost_components.duty", "duty"),
            ):
                _, reference = _offer_evidence(product_id, offer_id, field_path, suffix)
                _write_json_line(evidence, reference)
                evidence_count += 1

        _write_json_line(
            evidence,
            {
                "captured_at": CREATED_AT,
                "currency": _CURRENCY,
                "entity_type": "EXCHANGE_RATE",
                "evidence_id": "ev-rate-usd",
                "field_path": "exchange_rate.base_per_unit",
                "offer_id": None,
                "product_id": None,
                "provider_id": "perf-fx",
                "snapshot_version": SNAPSHOT_VERSION,
                "source_uri": "fixture://perf-fx/USD",
            },
        )
        evidence_count += 1
    return evidence_count


def _write_offers(snapshot_dir: Path) -> None:
    with (snapshot_dir / _FILE_NAMES["offers"]).open("wb") as offers:
        for ordinal in range(_MATCHING_PRODUCT_COUNT):
            product_id = f"perf-product-{ordinal:05d}"
            offer_id = f"perf-offer-{ordinal:05d}"
            amount = str(600 + ordinal * 25)
            bindings = [
                {
                    "evidence_id": f"ev-{offer_id}-{suffix}",
                    "field_path": field_path,
                }
                for field_path, suffix in (
                    ("offer.inventory", "inventory"),
                    ("offer.market", "market"),
                    ("offer.cost_components.currency", "currency"),
                    ("offer.cost_components.item_price", "item-price"),
                    ("offer.cost_components.shipping", "shipping"),
                    ("offer.cost_components.tax", "tax"),
                    ("offer.cost_components.duty", "duty"),
                )
            ]
            _write_json_line(
                offers,
                {
                    "captured_at": CREATED_AT,
                    "cost_components": {
                        "currency": _CURRENCY,
                        "duty": {
                            "amount": "0.00",
                            "evidence_id": f"ev-{offer_id}-duty",
                            "kind": "KNOWN",
                        },
                        "item_price": {
                            "amount": amount,
                            "evidence_id": f"ev-{offer_id}-item-price",
                            "kind": "KNOWN",
                        },
                        "shipping": {
                            "amount": "10",
                            "evidence_id": f"ev-{offer_id}-shipping",
                            "kind": "KNOWN",
                        },
                        "tax": {
                            "amount": "40",
                            "evidence_id": f"ev-{offer_id}-tax",
                            "kind": "KNOWN",
                        },
                    },
                    "field_evidence": bindings,
                    "market": _MARKET,
                    "offer_id": offer_id,
                    "product_id": product_id,
                    "provider_id": _PROVIDER_ID,
                    "snapshot_version": SNAPSHOT_VERSION,
                    "source_uri": f"fixture://{_PROVIDER_ID}/offers/{offer_id}",
                    "stock_status": "IN_STOCK",
                },
            )


def generate_snapshot(snapshot_dir: Path, *, seed: int = DEFAULT_SEED) -> str:
    """Write exactly 20,000 canonical products and return the manifest hash."""

    if not isinstance(snapshot_dir, Path):
        raise TypeError("snapshot_dir must be a Path")
    if type(seed) is not int:
        raise TypeError("seed must be an int")
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    evidence_count = _write_products_and_evidence(snapshot_dir, seed)
    _write_offers(snapshot_dir)

    exchange_rates = {
        "base_currency": _CURRENCY,
        "rates": [
            {
                "base_per_unit": "1",
                "currency": _CURRENCY,
                "evidence_id": "ev-rate-usd",
                "minor_units": 2,
            }
        ],
        "schema_version": EXCHANGE_RATES_SCHEMA_VERSION,
        "snapshot_version": SNAPSHOT_VERSION,
    }
    (snapshot_dir / _FILE_NAMES["exchange_rates"]).write_bytes(_json_bytes(exchange_rates))

    counts = {
        "products": PRODUCT_COUNT,
        "offers": _MATCHING_PRODUCT_COUNT,
        "evidence": evidence_count,
        "exchange_rates": 1,
    }
    manifest = {
        "base_currency": _CURRENCY,
        "categories": ["laptop", "tablet"],
        "created_at": CREATED_AT,
        "currencies": [_CURRENCY],
        "files": {
            role: {
                "path": filename,
                "record_count": counts[role],
                "sha256": _file_digest(snapshot_dir / filename),
            }
            for role, filename in _FILE_NAMES.items()
        },
        "generator": {
            "name": GENERATOR_NAME,
            "version": f"{GENERATOR_VERSION}+seed.{seed}",
        },
        "markets": [_MARKET],
        "providers": [_PROVIDER_ID],
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "snapshot_version": SNAPSHOT_VERSION,
    }
    manifest_path = snapshot_dir / "manifest.json"
    manifest_path.write_bytes(_json_bytes(manifest))
    return _file_digest(manifest_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    snapshot_hash = generate_snapshot(args.output)
    print(snapshot_hash)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
