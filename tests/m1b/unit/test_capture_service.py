from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

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
    SnapshotPublishFailure,
    SnapshotPublishResult,
    SnapshotPublishSuccess,
)
from glodex.capture.service import CaptureService
from glodex.domain.catalog import CatalogBatch

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec(
        "GLO-M1B-P0-001",
        "GLO-M1B-P0-002",
        "GLO-M1B-P0-003",
        "GLO-M1B-P0-005",
        "M1B-AC-002",
        "M1B-AC-003",
        "M1B-AC-004",
        "GLO-M1B-NFR-004",
        "GLO-M1B-NFR-005",
        "GLO-M1B-NFR-006",
    ),
]

_CAPTURE_ID = "capture-0123456789abcdef0123456789abcdef"
_CAPTURED_AT = datetime(2026, 7, 29, 8, 15, tzinfo=UTC)
_MONOTONIC_NS = 7_000_000_000
_VALID_ITEM = {
    "itemId": "synthetic|service|valid",
    "title": "Synthetic Service Phone",
    "itemWebUrl": "https://www.ebay.com/itm/synthetic-service",
    "price": {"value": "125.00", "currency": "USD"},
}


class FakeCaptureIds:
    def __init__(self) -> None:
        self.calls = 0

    def next_capture_id(self) -> str:
        self.calls += 1
        return _CAPTURE_ID


class FakeClock:
    def __init__(self) -> None:
        self.now_calls = 0
        self.monotonic_calls = 0

    def now_utc(self) -> datetime:
        self.now_calls += 1
        return _CAPTURED_AT

    def monotonic_ns(self) -> int:
        self.monotonic_calls += 1
        return _MONOTONIC_NS


class FakePageSource:
    def __init__(self, result: EbayProviderResult) -> None:
        self.result = result
        self.calls: list[tuple[str, EbayCredentials, int]] = []

    def fetch_page(
        self,
        *,
        query: str,
        credentials: EbayCredentials,
        deadline_ns: int,
    ) -> EbayProviderResult:
        self.calls.append((query, credentials, deadline_ns))
        return self.result


class FakePublisher:
    def __init__(self, result: SnapshotPublishResult) -> None:
        self.result = result
        self.calls: list[tuple[Path, CatalogBatch, datetime]] = []

    def publish(
        self,
        *,
        output_root: Path,
        batch: CatalogBatch,
        captured_at: datetime,
    ) -> SnapshotPublishResult:
        self.calls.append((output_root, batch, captured_at))
        return self.result


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    project_root = tmp_path / "checkout"
    output_root = tmp_path / "snapshots"
    project_root.mkdir(mode=0o700)
    output_root.mkdir(mode=0o700)
    return project_root, output_root


def _request(output_root: Path, *, live: bool = True) -> CaptureRequest:
    return CaptureRequest(
        live=live,
        query="  smartphone  ",
        output_root=output_root,
    )


def _service(
    page_result: EbayProviderResult,
    publish_result: SnapshotPublishResult,
) -> tuple[CaptureService, FakeCaptureIds, FakeClock, FakePageSource, FakePublisher]:
    ids = FakeCaptureIds()
    clock = FakeClock()
    source = FakePageSource(page_result)
    publisher = FakePublisher(publish_result)
    return (
        CaptureService(
            page_source=source,
            publisher=publisher,
            id_provider=ids,
            clock=clock,
        ),
        ids,
        clock,
        source,
        publisher,
    )


def test_preflight_rejection_allocates_no_id_and_performs_no_io(tmp_path: Path) -> None:
    project_root, output_root = _roots(tmp_path)
    service, ids, clock, source, publisher = _service(
        EbaySearchPage(item_summaries=()),
        SnapshotPublishSuccess(product_count=0, offer_count=0),
    )

    result = service.capture(
        _request(output_root, live=False),
        environ={"EBAY_APP_ID": "app", "EBAY_CERT_ID": "cert"},
        project_root=project_root,
    )

    assert isinstance(result, RejectedCaptureReceipt)
    assert result.request_count == 0
    assert result.issues[0].code is CaptureIssueCode.CAPTURE_INPUT_INVALID
    assert ids.calls == 0
    assert clock.monotonic_calls == 0
    assert source.calls == []
    assert publisher.calls == []


def test_success_uses_one_id_one_deadline_and_response_completion_time(
    tmp_path: Path,
) -> None:
    project_root, output_root = _roots(tmp_path)
    service, ids, clock, source, publisher = _service(
        EbaySearchPage(item_summaries=(_VALID_ITEM,)),
        SnapshotPublishSuccess(product_count=1, offer_count=1),
    )

    result = service.capture(
        _request(output_root),
        environ={"EBAY_APP_ID": "app", "EBAY_CERT_ID": "cert"},
        project_root=project_root,
    )

    assert isinstance(result, PublishedCaptureReceipt)
    assert result.capture_id == result.snapshot_version == _CAPTURE_ID
    assert result.captured_at == _CAPTURED_AT
    assert result.request_count == 2
    assert result.received_record_count == 1
    assert result.published_product_count == result.published_offer_count == 1
    assert result.quarantine_count == 0
    assert result.issues == ()
    assert ids.calls == 1
    assert clock.monotonic_calls == 1
    assert clock.now_calls == 1
    assert source.calls[0][0] == "smartphone"
    assert source.calls[0][2] == _MONOTONIC_NS + 30_000_000_000
    assert publisher.calls[0][0] == output_root.resolve()
    assert publisher.calls[0][1].snapshot_version == _CAPTURE_ID
    assert publisher.calls[0][2] == _CAPTURED_AT


@pytest.mark.parametrize(
    ("items", "published_count", "quarantine_count"),
    [
        ((), 0, 0),
        ((_VALID_ITEM, {"itemId": "invalid"}), 1, 1),
    ],
)
def test_empty_and_mixed_pages_publish_with_exact_counts(
    tmp_path: Path,
    items: tuple[object, ...],
    published_count: int,
    quarantine_count: int,
) -> None:
    project_root, output_root = _roots(tmp_path)
    service, _ids, _clock, _source, _publisher = _service(
        EbaySearchPage(item_summaries=items),
        SnapshotPublishSuccess(
            product_count=published_count,
            offer_count=published_count,
        ),
    )

    result = service.capture(
        _request(output_root),
        environ={"EBAY_APP_ID": "app", "EBAY_CERT_ID": "cert"},
        project_root=project_root,
    )

    assert isinstance(result, PublishedCaptureReceipt)
    assert result.received_record_count == len(items)
    assert result.published_product_count == published_count
    assert result.quarantine_count == quarantine_count
    if quarantine_count:
        assert len(result.issues) == 1
        assert result.issues[0].code is CaptureIssueCode.RECORD_QUARANTINED
        assert result.issues[0].count == quarantine_count
    else:
        assert result.issues == ()


def test_provider_failure_preserves_attempt_and_received_counts(tmp_path: Path) -> None:
    project_root, output_root = _roots(tmp_path)
    failure = EbayProviderFailure(
        issue_code=CaptureIssueCode.PROVIDER_RESPONSE_LIMIT,
        request_count=2,
        received_record_count=11,
    )
    service, ids, clock, _source, publisher = _service(
        failure,
        SnapshotPublishSuccess(product_count=0, offer_count=0),
    )

    result = service.capture(
        _request(output_root),
        environ={"EBAY_APP_ID": "app", "EBAY_CERT_ID": "cert"},
        project_root=project_root,
    )

    assert result == FailedCaptureReceipt(
        request_count=2,
        issues=(
            {
                "code": CaptureIssueCode.PROVIDER_RESPONSE_LIMIT,
                "count": 1,
            },
        ),
        capture_id=_CAPTURE_ID,
        received_record_count=11,
    )
    assert ids.calls == 1
    assert clock.now_calls == 0
    assert publisher.calls == []


def test_all_bad_page_is_failed_and_never_published(tmp_path: Path) -> None:
    project_root, output_root = _roots(tmp_path)
    service, _ids, clock, _source, publisher = _service(
        EbaySearchPage(item_summaries=({"itemId": "invalid"},)),
        SnapshotPublishSuccess(product_count=0, offer_count=0),
    )

    result = service.capture(
        _request(output_root),
        environ={"EBAY_APP_ID": "app", "EBAY_CERT_ID": "cert"},
        project_root=project_root,
    )

    assert isinstance(result, FailedCaptureReceipt)
    assert result.request_count == 2
    assert result.received_record_count == 1
    assert result.issues[0].code is CaptureIssueCode.PROVIDER_RESPONSE_INVALID
    assert clock.now_calls == 1
    assert publisher.calls == []


def test_publish_failure_is_failed_without_fallback(tmp_path: Path) -> None:
    project_root, output_root = _roots(tmp_path)
    service, _ids, _clock, _source, publisher = _service(
        EbaySearchPage(item_summaries=(_VALID_ITEM,)),
        SnapshotPublishFailure(
            issue_code=CaptureIssueCode.SNAPSHOT_VALIDATION_FAILED,
        ),
    )

    result = service.capture(
        _request(output_root),
        environ={"EBAY_APP_ID": "app", "EBAY_CERT_ID": "cert"},
        project_root=project_root,
    )

    assert isinstance(result, FailedCaptureReceipt)
    assert result.request_count == 2
    assert result.received_record_count == 1
    assert result.issues[0].code is CaptureIssueCode.SNAPSHOT_VALIDATION_FAILED
    assert len(publisher.calls) == 1


def test_mapping_count_inconsistency_fails_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, output_root = _roots(tmp_path)
    source = FakePageSource(EbaySearchPage(item_summaries=(_VALID_ITEM,)))
    publisher = FakePublisher(SnapshotPublishSuccess(product_count=0, offer_count=0))
    monkeypatch.setattr(
        "glodex.capture.service.map_ebay_search_page",
        lambda *_args, **_kwargs: CatalogBatch(snapshot_version=_CAPTURE_ID),
    )

    result = CaptureService(
        page_source=source,
        publisher=publisher,
        id_provider=FakeCaptureIds(),
        clock=FakeClock(),
    ).capture(
        _request(output_root),
        environ={"EBAY_APP_ID": "app", "EBAY_CERT_ID": "cert"},
        project_root=project_root,
    )

    assert isinstance(result, FailedCaptureReceipt)
    assert result.request_count == 2
    assert result.received_record_count == 1
    assert result.issues[0].code is CaptureIssueCode.SNAPSHOT_VALIDATION_FAILED
    assert publisher.calls == []
