from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from glodex.adapters import agent_item_search
from glodex.adapters.agent_indexes import (
    EMBEDDING_DIMENSIONS,
    AgentIndexes,
    PlatformInventory,
    load_agent_indexes,
)
from glodex.adapters.agent_item_search import (
    DemoItemSource,
    LiveEbayItemSource,
    LiveEbayPreflightError,
    build_live_ebay_item_source,
)
from glodex.application.agent.catalog import CandidateManifest, CandidateStore
from glodex.application.agent.contracts import (
    DataMode,
    ItemSearchInput,
    Platform,
    ToolFailureCode,
)
from glodex.application.agent.ports import ToolPortError
from glodex.capture.config import CapturePreflightResult, CaptureRequest, EbayCredentials
from glodex.capture.contracts import CaptureIssueCode
from glodex.capture.ports import (
    EbayProviderFailure,
    EbayProviderResult,
    EbaySearchPage,
)
from glodex.capture.service import CaptureService
from glodex.capture.snapshot_publisher import LocalSnapshotPublisher
from glodex.domain.catalog import CatalogBatch

pytestmark = [
    pytest.mark.contract,
    pytest.mark.spec(
        "GLO-M1D-P0-004",
        "GLO-M1D-NFR-003",
        "GLO-M1D-NFR-006",
    ),
]

_ROOT = Path(__file__).parents[3]
_SNAPSHOT_ROOT = _ROOT / "data" / "snapshots"
_AGENT_ROOT = _ROOT / "data" / "agent"
_CAPTURE_ID = "capture-0123456789abcdef0123456789abcdef"
_CAPTURED_AT = datetime(2026, 7, 29, 8, 15, tzinfo=UTC)
_VALID_ITEM_A = {
    "itemId": "synthetic|agent|a",
    "title": "Synthetic Agent Phone A",
    "itemWebUrl": "https://www.ebay.com/itm/synthetic-agent-a",
    "price": {"value": "125.00", "currency": "USD"},
}
_VALID_ITEM_B = {
    "itemId": "synthetic|agent|b",
    "title": "Synthetic Agent Phone B",
    "itemWebUrl": "https://www.ebay.com/itm/synthetic-agent-b",
    "price": {"value": "135.00", "currency": "USD"},
}


def _load_indexes() -> AgentIndexes:
    return asyncio.run(
        load_agent_indexes(
            snapshot_root=_SNAPSHOT_ROOT,
            agent_root=_AGENT_ROOT,
        )
    )


def _item_vector(record_key: str) -> tuple[float, ...]:
    path = _AGENT_ROOT / "m1d-demo-v1" / "item_embeddings.jsonl"
    for line in path.read_text(encoding="utf-8").splitlines():
        item = cast("dict[str, object]", json.loads(line))
        if item["record_key"] == record_key:
            return tuple(cast("list[float]", item["vector"]))
    raise AssertionError(f"missing item vector: {record_key}")


def _basis(index: int) -> tuple[float, ...]:
    values = [0.0] * EMBEDDING_DIMENSIONS
    values[index] = 1.0
    return tuple(values)


@pytest.mark.parametrize(
    ("platform", "record_key"),
    [
        (Platform.AMAZON, "amazon-phone-air"),
        (Platform.SHOPEE, "shopee-phone-lite"),
        (Platform.ALIEXPRESS, "aliexpress-phone-max"),
        (Platform.EBAY, "ebay-phone-reference"),
    ],
)
def test_demo_source_materializes_four_platform_semantic_hits_and_closed_subbatches(
    platform: Platform,
    record_key: str,
) -> None:
    source = DemoItemSource(_load_indexes())
    request = ItemSearchInput(query="phone", platform=platform, top_k=1)

    first = asyncio.run(
        source.search(
            request,
            query_vector=_item_vector(record_key),
        )
    )
    second = asyncio.run(
        source.search(
            request,
            query_vector=_item_vector(record_key),
        )
    )

    assert first == second
    assert first.platform is platform
    assert tuple(item.record_ref for item in first.candidates) == (record_key,)
    assert tuple(item.item_id for item in first.candidates) == (record_key,)
    assert tuple(product.product_id for product in first.platform_sub_batch.products) == (
        record_key,
    )
    assert {offer.product_id for offer in first.platform_sub_batch.offers} == {record_key}
    assert first.platform_sub_batch.snapshot_version == "m1d-demo-v1"
    assert first.platform_sub_batch.exchange_rates is not None
    assert first.platform_sub_batch.exchange_rates.supported_currencies == frozenset({"CNY", "USD"})
    assert first.total_recall == 2
    assert first.truncated

    records = source.manifest_records_for(first)
    assert tuple(record.record_ref for record in records) == (record_key,)
    pool = CandidateStore(
        CandidateManifest(
            data_mode=DataMode.DEMO_SNAPSHOT,
            snapshot_version=first.platform_sub_batch.snapshot_version,
            records=source.manifest_records,
        )
    ).merge((first,))
    assert pool.candidates == first.candidates


def test_demo_source_top_k_and_ties_are_stable() -> None:
    source = DemoItemSource(_load_indexes())
    request = ItemSearchInput(
        query="amazon products",
        platform=Platform.AMAZON,
        top_k=2,
    )

    result = asyncio.run(source.search(request, query_vector=_basis(100)))

    assert tuple(candidate.record_ref for candidate in result.candidates) == (
        "amazon-laptop-travel",
        "amazon-phone-air",
    )
    assert result.total_recall == 2
    assert not result.truncated
    assert len(result.platform_sub_batch.products) == 2
    assert len(source.manifest_records) == 8


def test_demo_source_rejects_cross_snapshot_cross_platform_and_bad_record_facts() -> None:
    indexes = _load_indexes()
    request = ItemSearchInput(
        query="phone",
        platform=Platform.AMAZON,
        top_k=1,
    )

    wrong_snapshot = DemoItemSource(replace(indexes, snapshot_version="m1d-other-v1"))
    with pytest.raises(ToolPortError) as snapshot_error:
        asyncio.run(
            wrong_snapshot.search(
                request,
                query_vector=_item_vector("amazon-phone-air"),
            )
        )
    assert snapshot_error.value.code is ToolFailureCode.ITEM_SOURCE_INVALID

    cross_platform_inventories = tuple(
        PlatformInventory(
            platform=inventory.platform,
            provider_ids=inventory.provider_ids,
            record_keys=(
                ("shopee-phone-lite",)
                if inventory.platform is Platform.AMAZON
                else inventory.record_keys
            ),
        )
        for inventory in indexes.platform_inventories
    )
    cross_platform = DemoItemSource(
        replace(indexes, platform_inventories=cross_platform_inventories)
    )
    with pytest.raises(ToolPortError) as platform_error:
        asyncio.run(
            cross_platform.search(
                request,
                query_vector=_item_vector("shopee-phone-lite"),
            )
        )
    assert platform_error.value.code is ToolFailureCode.ITEM_SOURCE_INVALID

    target = next(
        product for product in indexes.batch.products if product.product_id == "amazon-phone-air"
    )
    bad_product = replace(target, title="x" * 257)
    bad_batch = CatalogBatch(
        snapshot_version=indexes.batch.snapshot_version,
        products=tuple(
            bad_product if product is target else product for product in indexes.batch.products
        ),
        offers=indexes.batch.offers,
        evidence=indexes.batch.evidence,
        exchange_rates=indexes.batch.exchange_rates,
    )
    bad_record = DemoItemSource(replace(indexes, batch=bad_batch))
    with pytest.raises(ToolPortError) as record_error:
        asyncio.run(
            bad_record.search(
                request,
                query_vector=_item_vector("amazon-phone-air"),
            )
        )
    assert record_error.value.code is ToolFailureCode.ITEM_SOURCE_INVALID


class _FixedIds:
    def next_capture_id(self) -> str:
        return _CAPTURE_ID


class _FixedClock:
    def now_utc(self) -> datetime:
        return _CAPTURED_AT

    def monotonic_ns(self) -> int:
        return 7_000_000_000


class _PageSource:
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


class _BlockingPageSource:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def fetch_page(
        self,
        *,
        query: str,
        credentials: EbayCredentials,
        deadline_ns: int,
    ) -> EbayProviderResult:
        del query, credentials, deadline_ns
        self.started.set()
        self.release.wait(timeout=1)
        self.finished.set()
        return EbaySearchPage(item_summaries=(_VALID_ITEM_A,))


def _roots(tmp_path: Path, name: str) -> tuple[Path, Path]:
    project_root = tmp_path / f"{name}-checkout"
    output_root = tmp_path / f"{name}-snapshots"
    project_root.mkdir(mode=0o700)
    output_root.mkdir(mode=0o700)
    project_root.chmod(0o700)
    output_root.chmod(0o700)
    return project_root, output_root


def _live_source(
    tmp_path: Path,
    page_source: _PageSource | _BlockingPageSource,
    *,
    name: str,
) -> LiveEbayItemSource:
    project_root, output_root = _roots(tmp_path, name)
    return LiveEbayItemSource(
        capture_service=CaptureService(
            page_source=page_source,
            publisher=LocalSnapshotPublisher(),
            id_provider=_FixedIds(),
            clock=_FixedClock(),
        ),
        output_root=output_root,
        environ={"EBAY_APP_ID": "app", "EBAY_CERT_ID": "cert"},
        project_root=project_root,
    )


def test_live_source_builder_preflights_then_builds_one_capture_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, output_root = _roots(tmp_path, "builder-success")
    real_builder = agent_item_search.build_capture_service
    real_preflight = agent_item_search.preflight_capture
    build_calls = 0
    preflight_queries: list[str] = []

    def recording_builder() -> CaptureService:
        nonlocal build_calls
        build_calls += 1
        return real_builder()

    def recording_preflight(
        request: CaptureRequest,
        *,
        environ: Mapping[str, str] | None = None,
        project_root: Path | None = None,
    ) -> CapturePreflightResult:
        preflight_queries.append(request.query)
        return real_preflight(
            request,
            environ=environ,
            project_root=project_root,
        )

    monkeypatch.setattr(agent_item_search, "build_capture_service", recording_builder)
    monkeypatch.setattr(agent_item_search, "preflight_capture", recording_preflight)

    source = build_live_ebay_item_source(
        query="在 eBay 找手机\uff0c先分析品类并参考近期评测" + "很详细" * 80,
        output_root=output_root,
        environ={"EBAY_APP_ID": "app", "EBAY_CERT_ID": "cert"},
        project_root=project_root,
    )

    assert type(source) is LiveEbayItemSource
    assert build_calls == 1
    assert preflight_queries == ["smartphone"]


@pytest.mark.parametrize(
    ("query", "root_kind", "environ", "expected_code"),
    [
        (
            "phone",
            "relative",
            {"EBAY_APP_ID": "app", "EBAY_CERT_ID": "cert"},
            CaptureIssueCode.CAPTURE_CONFIG_INVALID,
        ),
        (
            "phone",
            "safe",
            {},
            CaptureIssueCode.CAPTURE_CREDENTIALS_MISSING,
        ),
        (
            " ",
            "safe",
            {"EBAY_APP_ID": "app", "EBAY_CERT_ID": "cert"},
            CaptureIssueCode.CAPTURE_INPUT_INVALID,
        ),
    ],
)
def test_live_source_builder_rejects_before_capture_service_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    query: str,
    root_kind: str,
    environ: dict[str, str],
    expected_code: CaptureIssueCode,
) -> None:
    project_root, safe_root = _roots(tmp_path, f"builder-{root_kind}")
    output_root = Path("relative-output") if root_kind == "relative" else safe_root
    build_calls = 0

    def forbidden_builder() -> CaptureService:
        nonlocal build_calls
        build_calls += 1
        raise AssertionError("invalid live source reached Capture construction")

    monkeypatch.setattr(agent_item_search, "build_capture_service", forbidden_builder)

    with pytest.raises(LiveEbayPreflightError) as raised:
        build_live_ebay_item_source(
            query=query,
            output_root=output_root,
            environ=environ,
            project_root=project_root,
        )

    assert raised.value.code is expected_code
    assert str(raised.value) == expected_code.value
    assert build_calls == 0


def test_live_ebay_capture_reverse_loads_usd_and_isolates_bad_records(
    tmp_path: Path,
) -> None:
    shopping_request = "在 eBay 找手机\uff0c先分析品类并参考近期评测"
    page = _PageSource(
        EbaySearchPage(
            item_summaries=(
                _VALID_ITEM_A,
                {
                    "itemId": "invalid-record",
                    "title": "must-not-merge",
                    "itemWebUrl": "http://invalid.example/item",
                    "price": {"value": "1.00", "currency": "USD"},
                },
                _VALID_ITEM_B,
            )
        )
    )
    source = _live_source(tmp_path, page, name="success")
    result = asyncio.run(
        source.search(
            ItemSearchInput(
                query=shopping_request,
                platform=Platform.EBAY,
                top_k=1,
            ),
            query_vector=None,
        )
    )

    assert len(page.calls) == 1
    assert page.calls[0][0] == "smartphone"
    assert shopping_request not in page.calls[0][0]
    assert result.platform is Platform.EBAY
    assert len(result.candidates) == 1
    assert result.total_recall == 2
    assert result.truncated
    assert result.platform_sub_batch.snapshot_version == _CAPTURE_ID
    assert result.platform_sub_batch.exchange_rates is not None
    assert result.platform_sub_batch.exchange_rates.supported_currencies == frozenset({"USD"})
    assert all(candidate.platform is Platform.EBAY for candidate in result.candidates)
    assert all("must-not-merge" not in candidate.title for candidate in result.candidates)

    records = source.manifest_records_for(result)
    assert len(records) == 1
    assert records[0].platform is Platform.EBAY
    assert records[0].provider_ids == ("ebay-browse",)
    CandidateStore(
        CandidateManifest(
            data_mode=DataMode.LIVE_MARKETPLACE,
            snapshot_version=result.platform_sub_batch.snapshot_version,
            records=records,
        )
    ).merge((result,))


def test_live_published_empty_page_remains_a_typed_empty_result(
    tmp_path: Path,
) -> None:
    page = _PageSource(EbaySearchPage(item_summaries=()))
    source = _live_source(tmp_path, page, name="empty")

    result = asyncio.run(
        source.search(
            ItemSearchInput(
                query="smartphone",
                platform=Platform.EBAY,
            ),
            query_vector=None,
        )
    )

    assert result.candidates == ()
    assert result.total_recall == 0
    assert not result.truncated
    assert result.platform_sub_batch.products == ()
    assert result.platform_sub_batch.offers == ()
    assert result.platform_sub_batch.exchange_rates is not None
    assert result.platform_sub_batch.exchange_rates.supported_currencies == frozenset({"USD"})
    assert source.manifest_records_for(result) == ()


@pytest.mark.parametrize(
    "platform",
    [Platform.AMAZON, Platform.SHOPEE, Platform.ALIEXPRESS],
)
def test_live_non_ebay_platforms_are_stably_not_configured(
    tmp_path: Path,
    platform: Platform,
) -> None:
    page = _PageSource(EbaySearchPage(item_summaries=()))
    source = _live_source(tmp_path, page, name=platform.value)

    with pytest.raises(ToolPortError) as raised:
        asyncio.run(
            source.search(
                ItemSearchInput(query="phone", platform=platform),
                query_vector=None,
            )
        )

    assert raised.value.code is ToolFailureCode.PROVIDER_NOT_CONFIGURED
    assert page.calls == []


def test_live_provider_failure_maps_to_one_safe_code(tmp_path: Path) -> None:
    page = _PageSource(
        EbayProviderFailure(
            issue_code=CaptureIssueCode.PROVIDER_UNAVAILABLE,
            request_count=2,
        )
    )
    source = _live_source(tmp_path, page, name="failure")

    with pytest.raises(ToolPortError) as raised:
        asyncio.run(
            source.search(
                ItemSearchInput(
                    query="phone",
                    platform=Platform.EBAY,
                ),
                query_vector=None,
            )
        )

    assert raised.value.code is ToolFailureCode.PROVIDER_UNAVAILABLE
    assert str(raised.value) == ToolFailureCode.PROVIDER_UNAVAILABLE.value


def test_live_cancel_marks_worker_non_mergeable_and_drains_before_return(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        page = _BlockingPageSource()
        source = _live_source(tmp_path, page, name="cancel")
        task = asyncio.create_task(
            source.search(
                ItemSearchInput(
                    query="phone",
                    platform=Platform.EBAY,
                ),
                query_vector=None,
            )
        )
        assert await asyncio.to_thread(page.started.wait, 0.5)

        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        page.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert page.finished.is_set()

    asyncio.run(scenario())
