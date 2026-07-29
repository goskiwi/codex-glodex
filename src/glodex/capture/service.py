"""Synchronous orchestration for one operator-approved Provider Capture."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from glodex.capture.config import CaptureRequest, preflight_capture
from glodex.capture.contracts import (
    CaptureIssue,
    CaptureIssueCode,
    CaptureReceipt,
    FailedCaptureReceipt,
    PublishedCaptureReceipt,
    RejectedCaptureReceipt,
    build_capture_issues,
)
from glodex.capture.ebay_mapping import map_ebay_search_page
from glodex.capture.ports import (
    CaptureClock,
    CaptureIdProvider,
    EbayPageSource,
    EbayProviderFailure,
    SnapshotPublisher,
    SnapshotPublishFailure,
)


@dataclass(frozen=True, slots=True)
class CaptureService:
    """Run the one fixed eBay Capture path without fallback or retries."""

    page_source: EbayPageSource
    publisher: SnapshotPublisher
    id_provider: CaptureIdProvider
    clock: CaptureClock

    def capture(
        self,
        request: CaptureRequest,
        *,
        environ: Mapping[str, str] | None = None,
        project_root: Path | None = None,
    ) -> CaptureReceipt:
        """Preflight, fetch, map, publish, and return one exact receipt."""

        prepared = preflight_capture(
            request,
            environ=environ,
            project_root=project_root,
        )
        if isinstance(prepared, RejectedCaptureReceipt):
            return prepared

        capture_id = self.id_provider.next_capture_id()
        deadline_ns = self.clock.monotonic_ns() + (
            prepared.profile.capture_deadline_seconds * 1_000_000_000
        )
        provider_result = self.page_source.fetch_page(
            query=prepared.query,
            credentials=prepared.credentials,
            deadline_ns=deadline_ns,
        )
        if isinstance(provider_result, EbayProviderFailure):
            return _failed(
                capture_id=capture_id,
                issue_code=provider_result.issue_code,
                request_count=provider_result.request_count,
                received_record_count=provider_result.received_record_count,
            )

        try:
            captured_at = self.clock.now_utc()
            mapping_result = map_ebay_search_page(
                provider_result,
                snapshot_version=capture_id,
                captured_at=captured_at,
            )
        except Exception:
            return _failed(
                capture_id=capture_id,
                issue_code=CaptureIssueCode.PROVIDER_RESPONSE_INVALID,
                request_count=provider_result.request_count,
                received_record_count=provider_result.received_record_count,
            )
        if isinstance(mapping_result, EbayProviderFailure):
            return _failed(
                capture_id=capture_id,
                issue_code=mapping_result.issue_code,
                request_count=mapping_result.request_count,
                received_record_count=mapping_result.received_record_count,
            )

        quarantine_count = len(mapping_result.quarantine_issues)
        if (
            len(mapping_result.products) != len(mapping_result.offers)
            or len(mapping_result.products) + quarantine_count
            != provider_result.received_record_count
        ):
            return _failed(
                capture_id=capture_id,
                issue_code=CaptureIssueCode.SNAPSHOT_VALIDATION_FAILED,
                request_count=provider_result.request_count,
                received_record_count=provider_result.received_record_count,
            )

        publish_result = self.publisher.publish(
            output_root=prepared.output_root,
            batch=mapping_result,
            captured_at=captured_at,
        )
        if isinstance(publish_result, SnapshotPublishFailure):
            return _failed(
                capture_id=capture_id,
                issue_code=publish_result.issue_code,
                request_count=provider_result.request_count,
                received_record_count=provider_result.received_record_count,
            )

        issues = (
            build_capture_issues(
                (
                    CaptureIssue(
                        code=CaptureIssueCode.RECORD_QUARANTINED,
                        count=quarantine_count,
                    ),
                )
            )
            if quarantine_count
            else ()
        )
        return PublishedCaptureReceipt(
            request_count=provider_result.request_count,
            issues=issues,
            capture_id=capture_id,
            received_record_count=provider_result.received_record_count,
            snapshot_version=capture_id,
            captured_at=captured_at,
            published_product_count=publish_result.product_count,
            published_offer_count=publish_result.offer_count,
            quarantine_count=quarantine_count,
        )


def _failed(
    *,
    capture_id: str,
    issue_code: CaptureIssueCode,
    request_count: int,
    received_record_count: int,
) -> FailedCaptureReceipt:
    return FailedCaptureReceipt(
        request_count=request_count,
        issues=build_capture_issues((issue_code,)),
        capture_id=capture_id,
        received_record_count=received_record_count,
    )


__all__ = ["CaptureService"]
