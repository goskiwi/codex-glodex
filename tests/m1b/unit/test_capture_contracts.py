from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from glodex.capture.contracts import (
    CaptureIssue,
    CaptureIssueCode,
    FailedCaptureReceipt,
    PublishedCaptureReceipt,
    RejectedCaptureReceipt,
    build_capture_issues,
    capture_exit_code_for,
)
from glodex.capture.ports import (
    EbayProviderFailure,
    EbaySearchPage,
    SnapshotPublishFailure,
    SnapshotPublishSuccess,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec("GLO-M1B-P0-001", "GLO-M1B-P0-006", "GLO-M1B-NFR-006"),
]


def _failed_receipt(**overrides: object) -> FailedCaptureReceipt:
    values: dict[str, object] = {
        "request_count": 1,
        "issues": (CaptureIssue(code=CaptureIssueCode.PROVIDER_AUTH_REJECTED),),
        "provider_id": "ebay-browse",
        "capture_id": "capture-0123456789abcdef0123456789abcdef",
        "marketplace": "EBAY_US",
        "currency": "USD",
        "category": "phone",
        "received_record_count": 0,
    }
    values.update(overrides)
    return FailedCaptureReceipt.model_validate(values)


def _published_receipt(**overrides: object) -> PublishedCaptureReceipt:
    capture_id = "capture-0123456789abcdef0123456789abcdef"
    values: dict[str, object] = {
        "request_count": 2,
        "issues": (),
        "provider_id": "ebay-browse",
        "capture_id": capture_id,
        "marketplace": "EBAY_US",
        "currency": "USD",
        "category": "phone",
        "received_record_count": 1,
        "snapshot_version": capture_id,
        "captured_at": datetime(2026, 7, 29, 3, 0, tzinfo=UTC),
        "published_product_count": 1,
        "published_offer_count": 1,
        "quarantine_count": 0,
    }
    values.update(overrides)
    return PublishedCaptureReceipt.model_validate(values)


def test_issue_builder_merges_and_sorts_stable_codes() -> None:
    issues = build_capture_issues(
        (
            CaptureIssueCode.CAPTURE_INPUT_INVALID,
            CaptureIssueCode.CAPTURE_CONFIG_INVALID,
            CaptureIssueCode.CAPTURE_INPUT_INVALID,
        )
    )

    assert issues == (
        CaptureIssue(code=CaptureIssueCode.CAPTURE_CONFIG_INVALID, count=1),
        CaptureIssue(code=CaptureIssueCode.CAPTURE_INPUT_INVALID, count=2),
    )


def test_receipt_variants_have_exact_top_level_keys_and_exit_codes() -> None:
    rejected = RejectedCaptureReceipt(
        issues=(CaptureIssue(code=CaptureIssueCode.CAPTURE_INPUT_INVALID),)
    )
    failed = _failed_receipt()
    published = _published_receipt()

    assert set(rejected.model_dump(mode="json")) == {
        "schema_version",
        "status",
        "request_count",
        "issues",
    }
    assert set(failed.model_dump(mode="json")) == {
        "schema_version",
        "status",
        "request_count",
        "issues",
        "provider_id",
        "capture_id",
        "marketplace",
        "currency",
        "category",
        "received_record_count",
    }
    assert set(published.model_dump(mode="json")) == {
        *failed.model_dump(mode="json"),
        "snapshot_version",
        "captured_at",
        "published_product_count",
        "published_offer_count",
        "quarantine_count",
    }
    assert rejected.model_dump_json().count("\n") == 0
    assert capture_exit_code_for(published) == 0
    assert capture_exit_code_for(failed) == 1
    assert capture_exit_code_for(rejected) == 2


def test_receipts_reject_coercion_and_extension() -> None:
    with pytest.raises(ValidationError):
        RejectedCaptureReceipt.model_validate(
            {
                "request_count": False,
                "issues": (CaptureIssue(code=CaptureIssueCode.CAPTURE_INPUT_INVALID),),
            }
        )

    with pytest.raises(ValidationError):
        RejectedCaptureReceipt.model_validate(
            {
                "issues": (CaptureIssue(code=CaptureIssueCode.CAPTURE_INPUT_INVALID),),
                "unexpected": "value",
            }
        )


def test_receipts_reject_wrong_state_issues_and_counts() -> None:
    with pytest.raises(ValidationError):
        _failed_receipt(issues=(CaptureIssue(code=CaptureIssueCode.CAPTURE_INPUT_INVALID),))

    with pytest.raises(ValidationError):
        _published_receipt(request_count=1)

    failed_after_record_limit = _failed_receipt(
        request_count=2,
        issues=(CaptureIssue(code=CaptureIssueCode.PROVIDER_RESPONSE_LIMIT),),
        received_record_count=11,
    )
    assert failed_after_record_limit.received_record_count == 11

    with pytest.raises(ValidationError):
        _published_receipt(received_record_count=11)


def test_published_receipt_binds_snapshot_counts_and_quarantine_issue() -> None:
    quarantine_issue = CaptureIssue(
        code=CaptureIssueCode.RECORD_QUARANTINED,
        count=1,
    )
    receipt = _published_receipt(
        received_record_count=2,
        issues=(quarantine_issue,),
        quarantine_count=1,
    )

    assert receipt.capture_id == receipt.snapshot_version
    assert receipt.quarantine_count == receipt.issues[0].count

    with pytest.raises(ValidationError):
        _published_receipt(
            received_record_count=2,
            issues=(quarantine_issue,),
            quarantine_count=0,
        )


def test_published_receipt_requires_a_utc_capture_time() -> None:
    with pytest.raises(ValidationError):
        _published_receipt(captured_at=datetime(2026, 7, 29, 3, 0))

    with pytest.raises(ValidationError):
        _published_receipt(
            captured_at=datetime(
                2026,
                7,
                29,
                11,
                0,
                tzinfo=timezone(timedelta(hours=8)),
            )
        )


def test_provider_and_publisher_port_outcomes_are_small_and_closed() -> None:
    page = EbaySearchPage(item_summaries=({"itemId": "synthetic"},))
    provider_failure = EbayProviderFailure(
        issue_code=CaptureIssueCode.PROVIDER_TIMEOUT,
        request_count=1,
    )
    publish_success = SnapshotPublishSuccess(product_count=1, offer_count=1)
    publish_failure = SnapshotPublishFailure(issue_code=CaptureIssueCode.SNAPSHOT_PUBLISH_FAILED)

    assert page.request_count == 2
    assert page.received_record_count == 1
    assert provider_failure.request_count == 1
    assert publish_success.product_count == publish_success.offer_count == 1
    assert publish_failure.issue_code is CaptureIssueCode.SNAPSHOT_PUBLISH_FAILED

    with pytest.raises(ValueError):
        EbaySearchPage(item_summaries=tuple({} for _index in range(11)))

    with pytest.raises(ValueError):
        SnapshotPublishFailure(issue_code=CaptureIssueCode.PROVIDER_TIMEOUT)
