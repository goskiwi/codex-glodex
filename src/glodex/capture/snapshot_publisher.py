"""Canonical, reverse-validated, atomically published Capture snapshots."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from glodex.adapters.local_snapshot import (
    EXCHANGE_RATES_SCHEMA_VERSION,
    MANIFEST_SCHEMA_VERSION,
    LocalSnapshotCatalog,
)
from glodex.capture.config import EBAY_CAPTURE_PROFILE
from glodex.capture.contracts import CaptureIssueCode
from glodex.capture.ports import (
    SnapshotPublishFailure,
    SnapshotPublishResult,
    SnapshotPublishSuccess,
)
from glodex.domain.catalog import (
    CatalogBatch,
    ExchangeRate,
    Offer,
    Product,
    ProductAttribute,
    aggregate_catalog_batch,
)
from glodex.domain.evidence import EvidenceRef, FieldEvidence
from glodex.domain.pricing import KnownCost, UnknownCost, canonical_exact_amount

_CAPTURE_ID = re.compile(r"capture-[0-9a-f]{32}\Z")
_GENERATOR_NAME: Final = "glodex-m1b-ebay-capture"
_GENERATOR_VERSION: Final = "1.0.0"
_DATA_FILES: Final = (
    ("products", "products.jsonl"),
    ("offers", "offers.jsonl"),
    ("evidence", "evidence.jsonl"),
    ("exchange_rates", "exchange_rates.json"),
)


class LocalSnapshotPublisher:
    """Publish one validated immutable snapshot below an approved output root."""

    def publish(
        self,
        *,
        output_root: Path,
        batch: CatalogBatch,
        captured_at: datetime,
    ) -> SnapshotPublishResult:
        """Write, reverse-validate, then atomically reveal one snapshot."""

        try:
            data_payloads, manifest_payload = _snapshot_payloads(
                batch,
                captured_at=captured_at,
            )
        except Exception:
            return _failure(CaptureIssueCode.SNAPSHOT_VALIDATION_FAILED)

        staging_parent: Path | None = None
        try:
            if not isinstance(output_root, Path):
                raise TypeError("output_root must be a Path")
            staging_parent = Path(
                tempfile.mkdtemp(
                    prefix=".glodex-staging-",
                    dir=output_root,
                )
            )
            staging_parent.chmod(0o700)
            staging_snapshot = staging_parent / batch.snapshot_version
            staging_snapshot.mkdir(mode=0o700)
            staging_snapshot.chmod(0o700)
            for role, filename in _DATA_FILES:
                _write_private_file(
                    staging_snapshot / filename,
                    data_payloads[role],
                )
            _write_private_file(
                staging_snapshot / "manifest.json",
                manifest_payload,
            )
        except Exception:
            if staging_parent is not None:
                _best_effort_cleanup(staging_parent)
            return _failure(CaptureIssueCode.SNAPSHOT_PUBLISH_FAILED)

        try:
            success = _validate_staged_snapshot(
                staging_parent,
                batch=batch,
                captured_at=captured_at,
            )
        except Exception:
            _best_effort_cleanup(staging_parent)
            return _failure(CaptureIssueCode.SNAPSHOT_VALIDATION_FAILED)

        final_snapshot = output_root / batch.snapshot_version
        try:
            final_snapshot.lstat()
        except FileNotFoundError:
            pass
        except Exception:
            _best_effort_cleanup(staging_parent)
            return _failure(CaptureIssueCode.SNAPSHOT_PUBLISH_FAILED)
        else:
            _best_effort_cleanup(staging_parent)
            return _failure(CaptureIssueCode.SNAPSHOT_PUBLISH_FAILED)

        try:
            _rename_snapshot(
                staging_parent / batch.snapshot_version,
                final_snapshot,
            )
        except Exception:
            _best_effort_cleanup(staging_parent)
            return _failure(CaptureIssueCode.SNAPSHOT_PUBLISH_FAILED)

        _best_effort_cleanup(staging_parent)
        return success


def _snapshot_payloads(
    batch: CatalogBatch,
    *,
    captured_at: datetime,
) -> tuple[dict[str, bytes], bytes]:
    if type(batch) is not CatalogBatch:
        raise TypeError("batch must be a CatalogBatch")
    if _CAPTURE_ID.fullmatch(batch.snapshot_version) is None:
        raise ValueError("snapshot version must be a Capture ID")
    if batch.fatal_issues:
        raise ValueError("fatal batch cannot be published")
    captured_text = _utc_text(captured_at)
    if any(offer.captured_at != captured_at for offer in batch.offers):
        raise ValueError("offer capture time mismatch")
    if any(item.captured_at != captured_at for item in batch.evidence):
        raise ValueError("evidence capture time mismatch")
    if batch.exchange_rates is None:
        raise ValueError("snapshot requires exchange rates")

    products = tuple(sorted(batch.products, key=lambda item: item.product_id))
    offers = tuple(sorted(batch.offers, key=lambda item: item.offer_id))
    evidence = tuple(sorted(batch.evidence, key=lambda item: item.evidence_id))
    rates = tuple(
        sorted(
            batch.exchange_rates.rates,
            key=lambda item: item.currency,
        )
    )
    data_payloads = {
        "products": _jsonl_bytes(tuple(_product_record(item) for item in products)),
        "offers": _jsonl_bytes(tuple(_offer_record(item) for item in offers)),
        "evidence": _jsonl_bytes(tuple(_evidence_record(item) for item in evidence)),
        "exchange_rates": _json_bytes(
            {
                "base_currency": batch.exchange_rates.base_currency,
                "rates": [_rate_record(item) for item in rates],
                "schema_version": EXCHANGE_RATES_SCHEMA_VERSION,
                "snapshot_version": batch.snapshot_version,
            }
        ),
    }
    record_counts = {
        "products": len(products),
        "offers": len(offers),
        "evidence": len(evidence),
        "exchange_rates": len(rates),
    }
    nonempty = bool(products or offers)
    manifest = {
        "base_currency": EBAY_CAPTURE_PROFILE.currency,
        "categories": [EBAY_CAPTURE_PROFILE.canonical_category] if nonempty else [],
        "created_at": captured_text,
        "currencies": [EBAY_CAPTURE_PROFILE.currency],
        "files": {
            role: {
                "path": filename,
                "record_count": record_counts[role],
                "sha256": hashlib.sha256(data_payloads[role]).hexdigest(),
            }
            for role, filename in _DATA_FILES
        },
        "generator": {
            "name": _GENERATOR_NAME,
            "version": _GENERATOR_VERSION,
        },
        "markets": [EBAY_CAPTURE_PROFILE.marketplace] if nonempty else [],
        "providers": [EBAY_CAPTURE_PROFILE.provider_id] if nonempty else [],
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "snapshot_version": batch.snapshot_version,
    }
    return data_payloads, _json_bytes(manifest)


def _validate_staged_snapshot(
    staging_parent: Path,
    *,
    batch: CatalogBatch,
    captured_at: datetime,
) -> SnapshotPublishSuccess:
    expected_rates = batch.exchange_rates
    if expected_rates is None:
        raise ValueError("snapshot requires exchange rates")

    loaded = _load_staged_snapshot(staging_parent, batch.snapshot_version)
    if (
        type(loaded) is not CatalogBatch
        or loaded.snapshot_version != batch.snapshot_version
        or loaded.fatal_issues
        or loaded.quarantine_issues
        or loaded.exchange_rates is None
        or loaded.exchange_rates.snapshot_version != batch.snapshot_version
        or any(item.snapshot_version != batch.snapshot_version for item in loaded.products)
        or any(item.snapshot_version != batch.snapshot_version for item in loaded.offers)
        or any(item.snapshot_version != batch.snapshot_version for item in loaded.evidence)
        or any(
            item.snapshot_version != batch.snapshot_version for item in loaded.exchange_rates.rates
        )
        or any(item.captured_at != captured_at for item in loaded.offers)
        or any(item.captured_at != captured_at for item in loaded.evidence)
        or len(loaded.products) != len(batch.products)
        or len(loaded.offers) != len(batch.offers)
        or len(loaded.evidence) != len(batch.evidence)
        or len(loaded.exchange_rates.rates) != len(expected_rates.rates)
    ):
        raise ValueError("staged snapshot failed reverse validation")

    aggregation = aggregate_catalog_batch(loaded)
    if (
        aggregation.quarantine_issues
        or not aggregation.offers_conserved
        or len(aggregation.products) != len(batch.products)
        or len(aggregation.offers) != len(batch.offers)
    ):
        raise ValueError("staged snapshot failed aggregation validation")
    return SnapshotPublishSuccess(
        product_count=len(aggregation.products),
        offer_count=len(aggregation.offers),
    )


def _load_staged_snapshot(staging_parent: Path, capture_id: str) -> CatalogBatch:
    return asyncio.run(
        LocalSnapshotCatalog(staging_parent).load(
            capture_id,
            display_currency=EBAY_CAPTURE_PROFILE.currency,
        )
    )


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def _jsonl_bytes(records: tuple[dict[str, object], ...]) -> bytes:
    return b"".join(_json_bytes(record) for record in records)


def _utc_text(value: datetime) -> str:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("captured_at must be a UTC datetime")
    return value.isoformat().replace("+00:00", "Z")


def _binding_record(value: FieldEvidence) -> dict[str, object]:
    return {
        "evidence_id": value.evidence_id,
        "field_path": value.field_path,
    }


def _attribute_record(value: ProductAttribute) -> dict[str, object]:
    return {
        "evidence_id": value.evidence_id,
        "name": value.name,
        "value": value.value,
    }


def _product_record(value: Product) -> dict[str, object]:
    return {
        "attributes": [
            _attribute_record(item)
            for item in sorted(
                value.attributes,
                key=lambda item: (item.name, item.evidence_id),
            )
        ],
        "category": value.category,
        "entity_kind": value.entity_kind.value,
        "field_evidence": [
            _binding_record(item)
            for item in sorted(
                value.field_evidence,
                key=lambda item: (item.field_path, item.evidence_id),
            )
        ],
        "product_id": value.product_id,
        "provider_id": value.provider_id,
        "snapshot_version": value.snapshot_version,
        "source_uri": value.source_uri,
        "title": value.title,
    }


def _cost_record(value: KnownCost | UnknownCost) -> dict[str, object]:
    if type(value) is KnownCost:
        amount = "0.00" if value.amount.is_zero() else canonical_exact_amount(value.amount)
        return {
            "amount": amount,
            "evidence_id": value.evidence_id,
            "kind": value.kind,
        }
    if type(value) is UnknownCost:
        return {
            "kind": value.kind,
            "reason": value.reason,
        }
    raise TypeError("unsupported cost value")


def _offer_record(value: Offer) -> dict[str, object]:
    return {
        "captured_at": _utc_text(value.captured_at),
        "cost_components": {
            "currency": value.cost_components.currency,
            "duty": _cost_record(value.cost_components.duty),
            "item_price": _cost_record(value.cost_components.item_price),
            "shipping": _cost_record(value.cost_components.shipping),
            "tax": _cost_record(value.cost_components.tax),
        },
        "field_evidence": [
            _binding_record(item)
            for item in sorted(
                value.field_evidence,
                key=lambda item: (item.field_path, item.evidence_id),
            )
        ],
        "market": value.market,
        "offer_id": value.offer_id,
        "product_id": value.product_id,
        "provider_id": value.provider_id,
        "snapshot_version": value.snapshot_version,
        "source_uri": value.source_uri,
        "stock_status": value.stock_status.value,
    }


def _evidence_record(value: EvidenceRef) -> dict[str, object]:
    return {
        "captured_at": _utc_text(value.captured_at),
        "currency": value.currency,
        "entity_type": value.entity_type.value,
        "evidence_id": value.evidence_id,
        "field_path": value.field_path,
        "offer_id": value.offer_id,
        "product_id": value.product_id,
        "provider_id": value.provider_id,
        "snapshot_version": value.snapshot_version,
        "source_uri": value.source_uri,
    }


def _rate_record(value: ExchangeRate) -> dict[str, object]:
    return {
        "base_per_unit": canonical_exact_amount(value.base_per_unit),
        "currency": value.currency,
        "evidence_id": value.evidence_id,
        "minor_units": value.minor_units,
    }


def _write_private_file(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _rename_snapshot(source: Path, target: Path) -> None:
    os.rename(source, target)


def _cleanup_staging(staging_parent: Path) -> None:
    shutil.rmtree(staging_parent)


def _best_effort_cleanup(staging_parent: Path) -> None:
    with suppress(Exception):
        _cleanup_staging(staging_parent)


def _failure(issue_code: CaptureIssueCode) -> SnapshotPublishFailure:
    return SnapshotPublishFailure(issue_code=issue_code)


__all__ = ["LocalSnapshotPublisher"]
