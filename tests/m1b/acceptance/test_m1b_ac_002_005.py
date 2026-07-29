from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

import glodex.capture.snapshot_publisher as snapshot_publisher
import glodex.cli as cli
from glodex.adapters.local_snapshot import LocalSnapshotCatalog
from glodex.capture.config import CaptureRequest, EbayCredentials
from glodex.capture.contracts import (
    CaptureIssueCode,
    FailedCaptureReceipt,
    PublishedCaptureReceipt,
    RejectedCaptureReceipt,
)
from glodex.capture.ports import (
    EbayProviderFailure,
    EbayProviderResult,
    EbaySearchPage,
)
from glodex.capture.service import CaptureService
from glodex.capture.snapshot_publisher import LocalSnapshotPublisher
from glodex.domain.catalog import CatalogBatch, aggregate_catalog_batch

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.spec(
        "GLO-M1B-P0-001",
        "GLO-M1B-P0-002",
        "GLO-M1B-P0-003",
        "GLO-M1B-P0-004",
        "GLO-M1B-P0-005",
        "M1B-AC-002",
        "M1B-AC-003",
        "M1B-AC-004",
        "M1B-AC-005",
        "GLO-M1B-NFR-002",
        "GLO-M1B-NFR-003",
        "GLO-M1B-NFR-004",
        "GLO-M1B-NFR-005",
        "GLO-M1B-NFR-006",
    ),
]

_FIXTURES = Path(__file__).parents[1] / "fixtures"
_CAPTURED_AT = datetime(2026, 7, 29, 9, 30, tzinfo=UTC)
_MONOTONIC_NS = 4_000_000_000
_QUERY = "private-phone-query-sentinel"
_APP_ID = "app-secret-sentinel"
_CERT_ID = "cert-secret-sentinel"
_RAW_RECORD_SENTINEL = "raw-record-must-not-persist"


@dataclass(slots=True)
class _FixedCaptureIds:
    capture_id: str
    calls: int = 0

    def next_capture_id(self) -> str:
        self.calls += 1
        return self.capture_id


@dataclass(slots=True)
class _FixedClock:
    now_calls: int = 0
    monotonic_calls: int = 0

    def now_utc(self) -> datetime:
        self.now_calls += 1
        return _CAPTURED_AT

    def monotonic_ns(self) -> int:
        self.monotonic_calls += 1
        return _MONOTONIC_NS


@dataclass(slots=True)
class _FakePageSource:
    result: EbayProviderResult
    calls: list[tuple[str, EbayCredentials, int]] = field(default_factory=list)

    def fetch_page(
        self,
        *,
        query: str,
        credentials: EbayCredentials,
        deadline_ns: int,
    ) -> EbayProviderResult:
        self.calls.append((query, credentials, deadline_ns))
        return self.result


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    project_root = tmp_path / "checkout"
    output_root = tmp_path / "snapshots"
    project_root.mkdir(mode=0o700)
    output_root.mkdir(mode=0o700)
    return project_root, output_root


def _fixture_items(filename: str) -> list[object]:
    payload = json.loads((_FIXTURES / filename).read_text(encoding="utf-8"))
    assert type(payload) is dict
    items = payload["itemSummaries"]
    assert type(items) is list
    return cast(list[object], items)


def _page(kind: str) -> EbaySearchPage:
    items = _fixture_items(
        "ebay_search_empty.json" if kind == "empty" else "ebay_search_success.json"
    )
    if kind == "mixed":
        items.append(
            {
                "itemId": _RAW_RECORD_SENTINEL,
                "title": _RAW_RECORD_SENTINEL,
                "itemWebUrl": "https://example.invalid/raw-record-must-not-persist",
                "price": {"value": "1.00", "currency": "USD"},
            }
        )
    return EbaySearchPage(item_summaries=tuple(items))


def _service(
    page_result: EbayProviderResult,
    capture_id: str,
) -> tuple[CaptureService, _FixedCaptureIds, _FixedClock, _FakePageSource]:
    ids = _FixedCaptureIds(capture_id)
    clock = _FixedClock()
    source = _FakePageSource(page_result)
    return (
        CaptureService(
            page_source=source,
            publisher=LocalSnapshotPublisher(),
            id_provider=ids,
            clock=clock,
        ),
        ids,
        clock,
        source,
    )


def _capture(
    service: CaptureService,
    output_root: Path,
    project_root: Path,
    *,
    live: bool = True,
) -> PublishedCaptureReceipt | FailedCaptureReceipt | RejectedCaptureReceipt:
    return service.capture(
        CaptureRequest(
            live=live,
            query=_QUERY,
            output_root=output_root,
        ),
        environ={"EBAY_APP_ID": _APP_ID, "EBAY_CERT_ID": _CERT_ID},
        project_root=project_root,
    )


def _load(output_root: Path, capture_id: str) -> CatalogBatch:
    return asyncio.run(
        LocalSnapshotCatalog(output_root).load(
            capture_id,
            display_currency="USD",
        )
    )


def _assert_safe_text(text: str, output_root: Path) -> None:
    for sentinel in (
        _QUERY,
        _APP_ID,
        _CERT_ID,
        _RAW_RECORD_SENTINEL,
        str(output_root),
    ):
        assert sentinel not in text
    assert "traceback" not in text.lower()


def _single_json_line(output: str) -> dict[str, object]:
    lines = output.splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert type(parsed) is dict
    return cast(dict[str, object], parsed)


@pytest.mark.parametrize(
    ("kind", "capture_number", "published_count", "quarantine_count"),
    [
        ("success", 1, 2, 0),
        ("empty", 2, 0, 0),
        ("mixed", 3, 2, 1),
    ],
)
def test_capture_publishes_reloadable_success_empty_and_mixed_snapshots(
    tmp_path: Path,
    pytestconfig: pytest.Config,
    capsys: pytest.CaptureFixture[str],
    kind: str,
    capture_number: int,
    published_count: int,
    quarantine_count: int,
) -> None:
    project_root, output_root = _roots(tmp_path)
    capture_id = f"capture-{capture_number:032x}"
    page = _page(kind)
    service, ids, clock, source = _service(page, capture_id)

    receipt = _capture(service, output_root, project_root)

    assert pytestconfig.getoption("--disable-socket") is True
    assert isinstance(receipt, PublishedCaptureReceipt)
    assert receipt.capture_id == receipt.snapshot_version == capture_id
    assert receipt.request_count == 2
    assert receipt.received_record_count == len(page.item_summaries)
    assert receipt.published_product_count == published_count
    assert receipt.published_offer_count == published_count
    assert receipt.quarantine_count == quarantine_count
    assert ids.calls == clock.now_calls == clock.monotonic_calls == 1
    assert len(source.calls) == 1

    loaded = _load(output_root, capture_id)
    assert loaded.fatal_issues == ()
    assert loaded.quarantine_issues == ()
    assert loaded.exchange_rates is not None
    assert loaded.exchange_rates.supported_currencies == frozenset({"USD"})
    aggregation = aggregate_catalog_batch(loaded)
    assert aggregation.quarantine_issues == ()
    assert aggregation.offers_conserved
    assert len(aggregation.products) == len(aggregation.offers) == published_count

    if quarantine_count:
        assert len(receipt.issues) == 1
        assert receipt.issues[0].code is CaptureIssueCode.RECORD_QUARANTINED
        assert receipt.issues[0].count == quarantine_count
    else:
        assert receipt.issues == ()

    snapshot_text = "".join(
        path.read_text(encoding="utf-8") for path in sorted((output_root / capture_id).iterdir())
    )
    _assert_safe_text(receipt.model_dump_json(), output_root)
    _assert_safe_text(snapshot_text, output_root)

    config_path = tmp_path / f"{kind}.toml"
    config_path.write_text(
        "\n".join(
            (
                "[app]",
                f"data_dir = {json.dumps(str(output_root))}",
                f"default_snapshot = {json.dumps(receipt.snapshot_version)}",
                "",
                "[search]",
                'default_locale = "zh-CN"',
                f"default_currency = {json.dumps(receipt.currency)}",
                "default_top_k = 3",
                "",
            )
        ),
        encoding="utf-8",
    )

    validate_exit = cli.main(
        (
            "validate-snapshot",
            receipt.snapshot_version,
            "--config",
            str(config_path),
            "--currency",
            receipt.currency,
        )
    )
    validate_output = capsys.readouterr()
    validate_payload = _single_json_line(validate_output.out)

    assert validate_exit == 0
    assert validate_output.err == ""
    assert validate_payload["valid"] is True
    assert validate_payload["snapshot_version"] == receipt.snapshot_version
    assert validate_payload["product_count"] == receipt.published_product_count
    assert validate_payload["offer_count"] == receipt.published_offer_count
    assert validate_payload["quarantine_count"] == 0

    search_exit = cli.main(
        (
            "search",
            "--config",
            str(config_path),
            "--query",
            "推荐一部手机",
            "--snapshot",
            receipt.snapshot_version,
            "--currency",
            receipt.currency,
        )
    )
    search_output = capsys.readouterr()
    search_payload = _single_json_line(search_output.out)

    assert search_exit == 0
    assert search_output.err == ""
    assert search_payload["status"] in {"COMPLETED", "NO_MATCH"}
    assert search_payload["snapshot_version"] == receipt.snapshot_version


def test_preflight_rejection_allocates_no_id_performs_no_io_and_publishes_nothing(
    tmp_path: Path,
) -> None:
    project_root, output_root = _roots(tmp_path)
    capture_id = "capture-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    service, ids, clock, source = _service(_page("success"), capture_id)

    receipt = _capture(service, output_root, project_root, live=False)

    assert isinstance(receipt, RejectedCaptureReceipt)
    assert receipt.request_count == 0
    assert receipt.issues[0].code is CaptureIssueCode.CAPTURE_INPUT_INVALID
    assert ids.calls == clock.now_calls == clock.monotonic_calls == 0
    assert source.calls == []
    assert not (output_root / capture_id).exists()
    assert tuple(output_root.iterdir()) == ()
    _assert_safe_text(receipt.model_dump_json(), output_root)


def test_provider_failure_has_no_fallback_and_publishes_nothing(tmp_path: Path) -> None:
    project_root, output_root = _roots(tmp_path)
    capture_id = "capture-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    failure = EbayProviderFailure(
        issue_code=CaptureIssueCode.PROVIDER_TIMEOUT,
        request_count=1,
    )
    service, ids, clock, source = _service(failure, capture_id)

    receipt = _capture(service, output_root, project_root)

    assert isinstance(receipt, FailedCaptureReceipt)
    assert receipt.capture_id == capture_id
    assert receipt.request_count == 1
    assert receipt.issues[0].code is CaptureIssueCode.PROVIDER_TIMEOUT
    assert ids.calls == clock.monotonic_calls == 1
    assert clock.now_calls == 0
    assert len(source.calls) == 1
    assert not (output_root / capture_id).exists()
    assert tuple(output_root.iterdir()) == ()
    _assert_safe_text(receipt.model_dump_json(), output_root)


def test_publish_failure_before_commit_leaves_no_final_and_exposes_no_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    project_root, output_root = _roots(tmp_path)
    capture_id = "capture-cccccccccccccccccccccccccccccccc"
    service, _ids, _clock, _source = _service(_page("success"), capture_id)

    def fail_rename(source: Path, target: Path) -> None:
        raise OSError(f"{_APP_ID} {_RAW_RECORD_SENTINEL} {source} {target}")

    monkeypatch.setattr(snapshot_publisher, "_rename_snapshot", fail_rename)

    receipt = _capture(service, output_root, project_root)

    assert isinstance(receipt, FailedCaptureReceipt)
    assert receipt.capture_id == capture_id
    assert receipt.request_count == 2
    assert receipt.issues[0].code is CaptureIssueCode.SNAPSHOT_PUBLISH_FAILED
    assert not (output_root / capture_id).exists()
    assert tuple(output_root.iterdir()) == ()
    _assert_safe_text(receipt.model_dump_json(), output_root)
    _assert_safe_text(caplog.text, output_root)
