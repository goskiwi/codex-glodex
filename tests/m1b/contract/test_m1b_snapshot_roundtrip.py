from __future__ import annotations

import asyncio
import hashlib
import json
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from glodex.adapters.local_snapshot import LocalSnapshotCatalog
from glodex.capture.ebay_mapping import map_ebay_search_page
from glodex.capture.ports import EbaySearchPage, SnapshotPublishSuccess
from glodex.capture.snapshot_publisher import LocalSnapshotPublisher
from glodex.domain.catalog import CatalogBatch, aggregate_catalog_batch

pytestmark = [
    pytest.mark.contract,
    pytest.mark.spec(
        "GLO-M1B-P0-003",
        "GLO-M1B-P0-005",
        "M1B-AC-002",
        "M1B-AC-003",
        "M1B-AC-004",
        "GLO-M1B-NFR-002",
        "GLO-M1B-NFR-003",
        "GLO-M1B-NFR-004",
    ),
]

_FIXTURES = Path(__file__).parents[1] / "fixtures"
_CAPTURED_AT = datetime(2026, 7, 29, 6, 30, tzinfo=UTC)
_FILES = {
    "manifest.json",
    "products.jsonl",
    "offers.jsonl",
    "evidence.jsonl",
    "exchange_rates.json",
}


def _root(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    return root


def _items(name: str) -> list[object]:
    payload = json.loads((_FIXTURES / name).read_text(encoding="utf-8"))
    assert type(payload) is dict
    items = payload["itemSummaries"]
    assert type(items) is list
    return cast(list[object], items)


def _batch(kind: str, capture_id: str) -> tuple[CatalogBatch, int]:
    items = _items("ebay_search_empty.json" if kind == "empty" else "ebay_search_success.json")
    expected_quarantine = 0
    if kind == "mixed":
        items.append(
            {
                "itemId": "synthetic|invalid|must-not-persist",
                "title": "sentinel-raw-invalid-item",
                "itemWebUrl": "https://example.invalid/secret",
                "price": {"value": "1.00", "currency": "USD"},
            }
        )
        expected_quarantine = 1
    result = map_ebay_search_page(
        EbaySearchPage(item_summaries=tuple(items)),
        snapshot_version=capture_id,
        captured_at=_CAPTURED_AT,
    )
    assert isinstance(result, CatalogBatch)
    assert len(result.quarantine_issues) == expected_quarantine
    return result, expected_quarantine


def _load(root: Path, capture_id: str) -> CatalogBatch:
    return asyncio.run(
        LocalSnapshotCatalog(root).load(
            capture_id,
            display_currency="USD",
        )
    )


@pytest.mark.parametrize(
    ("kind", "number", "expected_count"),
    [("success", 1, 2), ("empty", 2, 0), ("mixed", 3, 2)],
)
def test_published_snapshot_round_trips_without_new_quarantine(
    tmp_path: Path,
    kind: str,
    number: int,
    expected_count: int,
) -> None:
    capture_id = f"capture-{number:032x}"
    root = _root(tmp_path, kind)
    batch, _mapping_quarantine = _batch(kind, capture_id)

    result = LocalSnapshotPublisher().publish(
        output_root=root,
        batch=batch,
        captured_at=_CAPTURED_AT,
    )

    assert result == SnapshotPublishSuccess(
        product_count=expected_count,
        offer_count=expected_count,
    )
    final = root / capture_id
    assert {path.name for path in final.iterdir()} == _FILES
    assert stat.S_IMODE(final.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in final.iterdir())
    loaded = _load(root, capture_id)
    assert loaded.fatal_issues == ()
    assert loaded.quarantine_issues == ()
    aggregated = aggregate_catalog_batch(loaded)
    assert aggregated.quarantine_issues == ()
    assert len(aggregated.products) == len(aggregated.offers) == expected_count
    assert aggregated.offers_conserved
    assert loaded.exchange_rates is not None
    assert loaded.exchange_rates.supported_currencies == frozenset({"USD"})
    all_bytes = b"".join(path.read_bytes() for path in final.iterdir())
    assert b"sentinel-raw-invalid-item" not in all_bytes
    assert b"must-not-persist" not in all_bytes


def test_manifest_hashes_counts_inventories_and_canonical_bytes_are_exact(
    tmp_path: Path,
) -> None:
    capture_id = "capture-11111111111111111111111111111111"
    first_root = _root(tmp_path, "first")
    second_root = _root(tmp_path, "second")
    batch, _quarantine = _batch("success", capture_id)

    first = LocalSnapshotPublisher().publish(
        output_root=first_root,
        batch=batch,
        captured_at=_CAPTURED_AT,
    )
    second = LocalSnapshotPublisher().publish(
        output_root=second_root,
        batch=batch,
        captured_at=_CAPTURED_AT,
    )

    assert first == second == SnapshotPublishSuccess(product_count=2, offer_count=2)
    first_dir = first_root / capture_id
    second_dir = second_root / capture_id
    for name in _FILES:
        assert (first_dir / name).read_bytes() == (second_dir / name).read_bytes()

    manifest = json.loads((first_dir / "manifest.json").read_bytes())
    assert manifest["schema_version"] == "glodex.snapshot-manifest.v1"
    assert manifest["snapshot_version"] == capture_id
    assert manifest["created_at"] == "2026-07-29T06:30:00Z"
    assert manifest["base_currency"] == "USD"
    assert manifest["generator"] == {
        "name": "glodex-m1b-ebay-capture",
        "version": "1.0.0",
    }
    assert manifest["providers"] == ["ebay-browse"]
    assert manifest["markets"] == ["EBAY_US"]
    assert manifest["categories"] == ["phone"]
    assert manifest["currencies"] == ["USD"]

    files = manifest["files"]
    assert type(files) is dict
    for role, filename in (
        ("products", "products.jsonl"),
        ("offers", "offers.jsonl"),
        ("evidence", "evidence.jsonl"),
        ("exchange_rates", "exchange_rates.json"),
    ):
        spec = files[role]
        assert type(spec) is dict
        payload = (first_dir / filename).read_bytes()
        assert spec["path"] == filename
        assert spec["sha256"] == hashlib.sha256(payload).hexdigest()
        if role == "exchange_rates":
            decoded = json.loads(payload)
            assert type(decoded) is dict
            actual_count = len(decoded["rates"])
        else:
            actual_count = len(payload.splitlines())
            for line in payload.splitlines():
                decoded_line = json.loads(line)
                assert (
                    line
                    == json.dumps(
                        decoded_line,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                )
        assert spec["record_count"] == actual_count
        assert payload.endswith(b"\n")
