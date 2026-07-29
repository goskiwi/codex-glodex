from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import glodex.capture.snapshot_publisher as snapshot_publisher
from glodex.capture.contracts import CaptureIssueCode
from glodex.capture.ebay_mapping import map_ebay_search_page
from glodex.capture.ports import (
    EbaySearchPage,
    SnapshotPublishFailure,
    SnapshotPublishSuccess,
)
from glodex.domain.catalog import CatalogBatch
from glodex.domain.issues import (
    CatalogIssue,
    IssueCode,
    IssueDisposition,
    IssueStage,
)

pytestmark = [
    pytest.mark.unit,
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

_CAPTURE_ID = "capture-0123456789abcdef0123456789abcdef"
_CAPTURED_AT = datetime(2026, 7, 29, 6, 30, tzinfo=UTC)


def _output_root(tmp_path: Path) -> Path:
    root = tmp_path / "snapshots"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    return root


def _batch() -> CatalogBatch:
    result = map_ebay_search_page(
        EbaySearchPage(
            item_summaries=(
                {
                    "itemId": "synthetic|publisher|variant",
                    "title": "Synthetic Publisher Phone",
                    "itemWebUrl": "https://www.ebay.com/itm/synthetic-publisher",
                    "price": {"value": "99.95", "currency": "USD"},
                },
            )
        ),
        snapshot_version=_CAPTURE_ID,
        captured_at=_CAPTURED_AT,
    )
    assert isinstance(result, CatalogBatch)
    return result


def test_publish_writes_manifest_last_then_loads_aggregates_and_renames(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _output_root(tmp_path)
    writes: list[str] = []
    validations: list[str] = []
    original_write = snapshot_publisher._write_private_file
    original_catalog = snapshot_publisher.LocalSnapshotCatalog
    original_aggregate = snapshot_publisher.aggregate_catalog_batch
    original_rename = snapshot_publisher._rename_snapshot

    def recording_write(path: Path, payload: bytes) -> None:
        writes.append(path.name)
        original_write(path, payload)

    class RecordingCatalog:
        def __init__(self, catalog_root: Path) -> None:
            self._delegate = original_catalog(catalog_root)

        async def load(
            self,
            snapshot_version: str,
            *,
            display_currency: str | None = None,
            budget_currency: str | None = None,
        ) -> CatalogBatch:
            validations.append("load")
            return await self._delegate.load(
                snapshot_version,
                display_currency=display_currency,
                budget_currency=budget_currency,
            )

    def recording_aggregate(batch: CatalogBatch) -> object:
        validations.append("aggregate")
        return original_aggregate(batch)

    def recording_rename(source: Path, target: Path) -> None:
        assert source.parent.name.startswith(".glodex-staging-")
        assert source.stat().st_mode & 0o777 == 0o700
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in source.iterdir())
        original_rename(source, target)

    monkeypatch.setattr(snapshot_publisher, "_write_private_file", recording_write)
    monkeypatch.setattr(snapshot_publisher, "LocalSnapshotCatalog", RecordingCatalog)
    monkeypatch.setattr(snapshot_publisher, "aggregate_catalog_batch", recording_aggregate)
    monkeypatch.setattr(snapshot_publisher, "_rename_snapshot", recording_rename)

    result = snapshot_publisher.LocalSnapshotPublisher().publish(
        output_root=root,
        batch=_batch(),
        captured_at=_CAPTURED_AT,
    )

    assert result == SnapshotPublishSuccess(product_count=1, offer_count=1)
    assert writes == [
        "products.jsonl",
        "offers.jsonl",
        "evidence.jsonl",
        "exchange_rates.json",
        "manifest.json",
    ]
    assert validations == ["load", "aggregate"]
    assert not tuple(root.glob(".glodex-staging-*"))


def test_existing_target_is_not_overwritten(tmp_path: Path) -> None:
    root = _output_root(tmp_path)
    target = root / _CAPTURE_ID
    target.mkdir()
    marker = target / "operator-owned"
    marker.write_text("preserve", encoding="utf-8")

    result = snapshot_publisher.LocalSnapshotPublisher().publish(
        output_root=root,
        batch=_batch(),
        captured_at=_CAPTURED_AT,
    )

    assert result == SnapshotPublishFailure(issue_code=CaptureIssueCode.SNAPSHOT_PUBLISH_FAILED)
    assert marker.read_text(encoding="utf-8") == "preserve"
    assert not tuple(root.glob(".glodex-staging-*"))


def test_reverse_validation_failure_never_publishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _output_root(tmp_path)

    class EmptyCatalog:
        def __init__(self, _catalog_root: Path) -> None:
            pass

        async def load(
            self,
            snapshot_version: str,
            *,
            display_currency: str | None = None,
            budget_currency: str | None = None,
        ) -> CatalogBatch:
            return CatalogBatch(snapshot_version=snapshot_version)

    monkeypatch.setattr(snapshot_publisher, "LocalSnapshotCatalog", EmptyCatalog)

    result = snapshot_publisher.LocalSnapshotPublisher().publish(
        output_root=root,
        batch=_batch(),
        captured_at=_CAPTURED_AT,
    )

    assert result == SnapshotPublishFailure(issue_code=CaptureIssueCode.SNAPSHOT_VALIDATION_FAILED)
    assert not (root / _CAPTURE_ID).exists()
    assert not tuple(root.glob(".glodex-staging-*"))


def test_rename_failure_cleans_staging_and_leaves_no_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _output_root(tmp_path)

    def fail_rename(_source: Path, _target: Path) -> None:
        raise OSError("sentinel-rename-detail")

    monkeypatch.setattr(snapshot_publisher, "_rename_snapshot", fail_rename)

    result = snapshot_publisher.LocalSnapshotPublisher().publish(
        output_root=root,
        batch=_batch(),
        captured_at=_CAPTURED_AT,
    )

    assert result == SnapshotPublishFailure(issue_code=CaptureIssueCode.SNAPSHOT_PUBLISH_FAILED)
    assert "sentinel-rename-detail" not in repr(result)
    assert not (root / _CAPTURE_ID).exists()
    assert not tuple(root.glob(".glodex-staging-*"))


def test_cleanup_failure_after_rename_cannot_downgrade_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _output_root(tmp_path)

    def fail_cleanup(_staging_parent: Path) -> None:
        raise OSError("sentinel-cleanup-detail")

    monkeypatch.setattr(snapshot_publisher, "_cleanup_staging", fail_cleanup)

    result = snapshot_publisher.LocalSnapshotPublisher().publish(
        output_root=root,
        batch=_batch(),
        captured_at=_CAPTURED_AT,
    )

    assert result == SnapshotPublishSuccess(product_count=1, offer_count=1)
    assert (root / _CAPTURE_ID / "manifest.json").is_file()
    assert "sentinel-cleanup-detail" not in repr(result)


def test_mapping_quarantine_is_not_serialized(tmp_path: Path) -> None:
    root = _output_root(tmp_path)
    raw_sentinel = "sentinel-raw-quarantine-item"
    batch = replace(
        _batch(),
        quarantine_issues=(
            CatalogIssue(
                code=IssueCode.INVALID_RECORD,
                stage=IssueStage.PRODUCTS,
                disposition=IssueDisposition.QUARANTINE,
                message=raw_sentinel,
            ),
        ),
    )

    result = snapshot_publisher.LocalSnapshotPublisher().publish(
        output_root=root,
        batch=batch,
        captured_at=_CAPTURED_AT,
    )

    assert isinstance(result, SnapshotPublishSuccess)
    final = root / _CAPTURE_ID
    assert {path.name for path in final.iterdir()} == {
        "manifest.json",
        "products.jsonl",
        "offers.jsonl",
        "evidence.jsonl",
        "exchange_rates.json",
    }
    assert raw_sentinel.encode() not in b"".join(path.read_bytes() for path in final.iterdir())
