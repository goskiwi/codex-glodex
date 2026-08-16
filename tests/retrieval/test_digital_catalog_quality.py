from __future__ import annotations

import json
from pathlib import Path

from glodex.retrieval.catalog_quality import (
    CatalogAdmissionReason,
    CatalogPolicy,
    admission_reason,
)
from glodex.retrieval.digital_catalog import DigitalCatalogItemSource

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "data/digital-interview-v1/products.json"


def _policy() -> CatalogPolicy:
    payload = json.loads(CATALOG.read_text(encoding="utf-8"))
    policy = CatalogPolicy.from_payload(payload["catalog_policy"])
    assert policy is not None
    return policy


def _product(release_date: str) -> dict[str, object]:
    return {
        "attributes": {
            "brand": "Example",
            "model": "Example One",
            "release_date": release_date,
        },
        "specification_source": {
            "source_type": "MANUFACTURER_SPECIFICATION",
            "url": "https://example.com/spec",
        },
    }


def test_five_year_policy_rejects_vague_and_out_of_window_dates() -> None:
    policy = _policy()
    vague_date = "2023" + chr(0x2013) + "2026在售系列"

    assert admission_reason(_product(vague_date), policy) is (
        CatalogAdmissionReason.INVALID_RELEASE_DATE
    )
    assert admission_reason(_product("2021-08-14"), policy) is (
        CatalogAdmissionReason.BEFORE_WINDOW
    )
    assert admission_reason(_product("2021-08-15"), policy) is CatalogAdmissionReason.ADMITTED
    assert admission_reason(_product("2026-08-16"), policy) is (
        CatalogAdmissionReason.AFTER_WINDOW
    )


def test_runtime_only_loads_records_admitted_by_the_five_year_policy() -> None:
    source = DigitalCatalogItemSource(CATALOG)

    assert source.quality_report.rejected > 0
    assert len(source._products) == source.quality_report.admitted
    assert all(
        len(product.attributes["release_date"]) == 10 for product in source._products
    )
