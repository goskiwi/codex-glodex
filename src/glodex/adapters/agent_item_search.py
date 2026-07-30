"""Trusted Demo and Capture-backed item sources for the M1d Agent."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

from glodex.adapters.agent_indexes import AgentIndexes
from glodex.adapters.local_snapshot import LocalSnapshotCatalog
from glodex.application.agent.catalog import ManifestRecord
from glodex.application.agent.contracts import (
    Candidate,
    CandidateAttribute,
    ItemSearchInput,
    ItemSearchRuntimeResult,
    Platform,
    ToolFailureCode,
)
from glodex.application.agent.ports import ToolPortError
from glodex.capture.bootstrap import build_capture_service
from glodex.capture.config import CaptureRequest, preflight_capture
from glodex.capture.contracts import (
    CaptureIssueCode,
    CaptureReceipt,
    FailedCaptureReceipt,
    PublishedCaptureReceipt,
    RejectedCaptureReceipt,
)
from glodex.capture.service import CaptureService
from glodex.domain.catalog import (
    CatalogBatch,
    Offer,
    OfferIdentity,
    Product,
)
from glodex.domain.pricing import KnownCost

_EBAY_PROVIDER_ID = "ebay-browse"
_EBAY_CREDENTIAL_NAMES = ("EBAY_APP_ID", "EBAY_CERT_ID")
_EBAY_PHONE_MARKETPLACE_QUERY = "smartphone"


class LiveEbayPreflightError(RuntimeError):
    """A stable pre-run Capture rejection without paths or credential values."""

    def __init__(self, code: CaptureIssueCode) -> None:
        if type(code) is not CaptureIssueCode:
            raise TypeError("live eBay preflight code must be CaptureIssueCode")
        self.code = code
        super().__init__(code.value)


class DemoItemSource:
    """Materialize exact semantic hits from one validated M1d demo index."""

    __slots__ = ("_indexes",)

    def __init__(self, indexes: AgentIndexes) -> None:
        if type(indexes) is not AgentIndexes:
            raise TypeError("Demo item source requires exact AgentIndexes")
        self._indexes = indexes

    @property
    def manifest_records(self) -> tuple[ManifestRecord, ...]:
        """Return the full trusted index ownership map in manifest order."""

        try:
            self._validate_index()
            records: list[ManifestRecord] = []
            for inventory in self._indexes.platform_inventories:
                for record_key in inventory.record_keys:
                    candidate = _project_candidate(
                        self._indexes.batch,
                        platform=inventory.platform,
                        record_key=record_key,
                        allowed_providers=inventory.provider_ids,
                    )
                    records.append(
                        _manifest_record(
                            self._indexes.batch,
                            platform=inventory.platform,
                            candidate=candidate,
                            allowed_providers=inventory.provider_ids,
                        )
                    )
            return tuple(records)
        except ToolPortError:
            raise
        except Exception:
            raise ToolPortError(ToolFailureCode.ITEM_SOURCE_INVALID) from None

    async def search(
        self,
        request: ItemSearchInput,
        *,
        query_vector: tuple[float, ...] | None,
    ) -> ItemSearchRuntimeResult:
        if type(request) is not ItemSearchInput or query_vector is None:
            raise ToolPortError(ToolFailureCode.ITEM_SOURCE_INVALID)
        try:
            self._validate_index()
            inventory = self._indexes.platform_inventory(request.platform)
            hits = self._indexes.search_items(
                platform=request.platform,
                query_vector=query_vector,
                top_k=request.top_k,
            )
            expected_count = min(request.top_k, len(inventory.record_keys))
            record_keys = tuple(hit.record_key for hit in hits)
            if (
                len(record_keys) != expected_count
                or len(record_keys) != len(set(record_keys))
                or not set(record_keys).issubset(inventory.record_keys)
            ):
                raise ValueError("item index hits do not match trusted ownership")
            candidates = tuple(
                _project_candidate(
                    self._indexes.batch,
                    platform=request.platform,
                    record_key=record_key,
                    allowed_providers=inventory.provider_ids,
                )
                for record_key in record_keys
            )
            sub_batch = _sub_batch(self._indexes.batch, record_keys)
            return ItemSearchRuntimeResult(
                platform=request.platform,
                candidates=candidates,
                platform_sub_batch=sub_batch,
                total_recall=len(inventory.record_keys),
                truncated=len(inventory.record_keys) > len(candidates),
            )
        except ToolPortError:
            raise
        except Exception:
            raise ToolPortError(ToolFailureCode.ITEM_SOURCE_INVALID) from None

    def materialize_record_keys(
        self,
        request: ItemSearchInput,
        *,
        record_keys: tuple[str, ...],
    ) -> ItemSearchRuntimeResult:
        """Rebuild candidates from a checked M2a key-only retrieval projection."""

        if (
            type(request) is not ItemSearchInput
            or type(record_keys) is not tuple
            or any(type(record_key) is not str for record_key in record_keys)
            or not record_keys
            or len(record_keys) != len(set(record_keys))
            or len(record_keys) > request.top_k
        ):
            raise ToolPortError(ToolFailureCode.ITEM_SOURCE_INVALID)
        try:
            self._validate_index()
            inventory = self._indexes.platform_inventory(request.platform)
            if not set(record_keys).issubset(inventory.record_keys):
                raise ValueError("M2a keys do not belong to the requested platform")
            candidates = tuple(
                _project_candidate(
                    self._indexes.batch,
                    platform=request.platform,
                    record_key=record_key,
                    allowed_providers=inventory.provider_ids,
                )
                for record_key in record_keys
            )
            return ItemSearchRuntimeResult(
                platform=request.platform,
                candidates=candidates,
                platform_sub_batch=_sub_batch(self._indexes.batch, record_keys),
                total_recall=len(inventory.record_keys),
                truncated=len(inventory.record_keys) > len(candidates),
            )
        except ToolPortError:
            raise
        except Exception:
            raise ToolPortError(ToolFailureCode.ITEM_SOURCE_INVALID) from None

    def manifest_records_for(
        self,
        result: ItemSearchRuntimeResult,
    ) -> tuple[ManifestRecord, ...]:
        """Re-derive bindings after proving the result is an exact index subset."""

        try:
            self._validate_index()
            if type(result) is not ItemSearchRuntimeResult:
                raise TypeError("result must be exact ItemSearchRuntimeResult")
            inventory = self._indexes.platform_inventory(result.platform)
            _require_exact_subset(
                result.platform_sub_batch,
                source=self._indexes.batch,
                allowed_record_keys=inventory.record_keys,
            )
            if result.total_recall != len(inventory.record_keys):
                raise ValueError("result total recall differs from trusted inventory")
            records = tuple(
                _manifest_record(
                    result.platform_sub_batch,
                    platform=result.platform,
                    candidate=candidate,
                    allowed_providers=inventory.provider_ids,
                )
                for candidate in result.candidates
            )
            if tuple(record.record_ref for record in records) != tuple(
                product.product_id for product in result.platform_sub_batch.products
            ):
                raise ValueError("result candidates do not match sub-batch products")
            return records
        except ToolPortError:
            raise
        except Exception:
            raise ToolPortError(ToolFailureCode.ITEM_SOURCE_INVALID) from None

    def _validate_index(self) -> None:
        indexes = self._indexes
        batch = indexes.batch
        if (
            batch.snapshot_version != indexes.snapshot_version
            or batch.fatal_issues
            or batch.quarantine_issues
            or batch.exchange_rates is None
        ):
            raise ValueError("Demo index batch is not a trusted complete snapshot")
        if tuple(inventory.platform for inventory in indexes.platform_inventories) != (
            Platform.AMAZON,
            Platform.SHOPEE,
            Platform.ALIEXPRESS,
            Platform.EBAY,
        ):
            raise ValueError("Demo platform inventory is incomplete")
        record_keys = tuple(
            record_key
            for inventory in indexes.platform_inventories
            for record_key in inventory.record_keys
        )
        if (
            len(record_keys) != len(set(record_keys))
            or set(record_keys) != {product.product_id for product in batch.products}
            or any(
                not inventory.provider_ids or not inventory.record_keys
                for inventory in indexes.platform_inventories
            )
        ):
            raise ValueError("Demo record ownership is incomplete or ambiguous")


class LiveEbayItemSource:
    """Run one owned eBay Capture worker and reverse-load its USD snapshot."""

    __slots__ = (
        "_capture_service",
        "_environ",
        "_merge_allowed",
        "_output_root",
        "_project_root",
        "_published_result",
        "_started",
        "_worker",
    )

    def __init__(
        self,
        *,
        capture_service: CaptureService,
        output_root: Path,
        environ: Mapping[str, str] | None = None,
        project_root: Path | None = None,
    ) -> None:
        if type(capture_service) is not CaptureService:
            raise TypeError("live item source requires an exact CaptureService")
        if not isinstance(output_root, Path) or not output_root.is_absolute():
            raise ValueError("live item source output_root must be an absolute Path")
        if project_root is not None and not isinstance(project_root, Path):
            raise TypeError("project_root must be a Path or None")
        if environ is not None and (
            not isinstance(environ, Mapping)
            or any(type(key) is not str or type(value) is not str for key, value in environ.items())
        ):
            raise TypeError("environ must contain string keys and values")
        self._capture_service = capture_service
        self._output_root = output_root
        self._environ = None if environ is None else MappingProxyType(dict(environ))
        self._project_root = project_root
        self._worker: asyncio.Task[CaptureReceipt] | None = None
        self._started = False
        self._merge_allowed = False
        self._published_result: ItemSearchRuntimeResult | None = None

    async def search(
        self,
        request: ItemSearchInput,
        *,
        query_vector: tuple[float, ...] | None,
    ) -> ItemSearchRuntimeResult:
        if type(request) is not ItemSearchInput:
            raise ToolPortError(ToolFailureCode.ITEM_SOURCE_INVALID)
        if request.platform is not Platform.EBAY:
            raise ToolPortError(ToolFailureCode.PROVIDER_NOT_CONFIGURED)
        if query_vector is not None:
            raise ToolPortError(ToolFailureCode.ITEM_SOURCE_INVALID)
        if self._started:
            raise ToolPortError(ToolFailureCode.PROVIDER_UNAVAILABLE)

        self._started = True
        self._merge_allowed = True
        capture_request = CaptureRequest(
            live=True,
            # The Agent's full shopping request is not an eBay Browse query.
            # M1d live data owns M1b's fixed US phone profile, whose approved
            # marketplace query is deliberately stable.  This keeps prose,
            # locale, and optional evidence instructions out of the provider
            # request while preserving them for the local Agent pipeline.
            query=_EBAY_PHONE_MARKETPLACE_QUERY,
            output_root=self._output_root,
        )
        worker = asyncio.create_task(
            asyncio.to_thread(
                self._capture_service.capture,
                capture_request,
                environ=self._environ,
                project_root=self._project_root,
            ),
            name="glodex-live-ebay-capture",
        )
        self._worker = worker
        try:
            receipt = await asyncio.shield(worker)
        except asyncio.CancelledError:
            self._merge_allowed = False
            await _drain_worker(worker)
            raise
        except Exception:
            self._merge_allowed = False
            raise ToolPortError(ToolFailureCode.PROVIDER_UNAVAILABLE) from None

        if not self._merge_allowed:
            raise ToolPortError(ToolFailureCode.PROVIDER_UNAVAILABLE)
        if type(receipt) is not PublishedCaptureReceipt:
            self._merge_allowed = False
            raise ToolPortError(_capture_failure_code(receipt))

        batch = await LocalSnapshotCatalog(self._output_root).load(
            receipt.snapshot_version,
            display_currency="USD",
            budget_currency="USD",
        )
        try:
            result = _materialize_live_result(
                batch,
                request=request,
                receipt=receipt,
            )
        except Exception:
            self._merge_allowed = False
            raise ToolPortError(ToolFailureCode.ITEM_SOURCE_INVALID) from None
        self._published_result = result
        return result

    async def drain(self) -> None:
        """Wait for the owned Capture worker without making its result mergeable."""

        worker = self._worker
        if worker is not None and not worker.done():
            self._merge_allowed = False
            await _drain_worker(worker)

    def manifest_records_for(
        self,
        result: ItemSearchRuntimeResult,
    ) -> tuple[ManifestRecord, ...]:
        """Re-derive records only for the exact result returned by this Capture."""

        if (
            type(result) is not ItemSearchRuntimeResult
            or result is not self._published_result
            or not self._merge_allowed
        ):
            raise ToolPortError(ToolFailureCode.ITEM_SOURCE_INVALID)
        try:
            if (
                result.platform is not Platform.EBAY
                or not result.platform_sub_batch.snapshot_version.startswith("capture-")
            ):
                raise ValueError("live result is not an eBay Capture")
            return tuple(
                _manifest_record(
                    result.platform_sub_batch,
                    platform=Platform.EBAY,
                    candidate=candidate,
                    allowed_providers=(_EBAY_PROVIDER_ID,),
                )
                for candidate in result.candidates
            )
        except Exception:
            raise ToolPortError(ToolFailureCode.ITEM_SOURCE_INVALID) from None


def build_live_ebay_item_source(
    *,
    query: str,
    output_root: Path,
    environ: Mapping[str, str] | None = None,
    project_root: Path | None = None,
) -> LiveEbayItemSource:
    """Preflight one Run's fixed Capture inputs, then build its owned source."""

    if type(query) is not str or not query.strip():
        raise LiveEbayPreflightError(CaptureIssueCode.CAPTURE_INPUT_INVALID)
    if project_root is not None and not isinstance(project_root, Path):
        raise LiveEbayPreflightError(CaptureIssueCode.CAPTURE_CONFIG_INVALID)
    environment = os.environ if environ is None else environ
    if not isinstance(environment, Mapping) or any(
        type(key) is not str or type(value) is not str for key, value in environment.items()
    ):
        raise LiveEbayPreflightError(CaptureIssueCode.CAPTURE_CONFIG_INVALID)
    captured_environment = {
        name: environment[name] for name in _EBAY_CREDENTIAL_NAMES if name in environment
    }
    try:
        prepared = preflight_capture(
            CaptureRequest(
                live=True,
                query=_EBAY_PHONE_MARKETPLACE_QUERY,
                output_root=output_root,
            ),
            environ=captured_environment,
            project_root=project_root,
        )
    except Exception:
        raise LiveEbayPreflightError(CaptureIssueCode.CAPTURE_CONFIG_INVALID) from None
    if isinstance(prepared, RejectedCaptureReceipt):
        raise LiveEbayPreflightError(prepared.issues[0].code)
    try:
        capture_service = build_capture_service()
    except Exception:
        raise LiveEbayPreflightError(CaptureIssueCode.CAPTURE_CONFIG_INVALID) from None
    return LiveEbayItemSource(
        capture_service=capture_service,
        output_root=prepared.output_root,
        environ=captured_environment,
        project_root=project_root,
    )


def _materialize_live_result(
    batch: CatalogBatch,
    *,
    request: ItemSearchInput,
    receipt: PublishedCaptureReceipt,
) -> ItemSearchRuntimeResult:
    if (
        type(batch) is not CatalogBatch
        or batch.snapshot_version != receipt.snapshot_version
        or batch.fatal_issues
        or batch.quarantine_issues
        or batch.exchange_rates is None
        or batch.exchange_rates.supported_currencies != frozenset({"USD"})
        or len(batch.products) != receipt.published_product_count
        or len(batch.offers) != receipt.published_offer_count
    ):
        raise ValueError("reverse-loaded Capture does not match its receipt")

    valid: list[Candidate] = []
    for product in batch.products:
        try:
            valid.append(
                _project_candidate(
                    batch,
                    platform=Platform.EBAY,
                    record_key=product.product_id,
                    allowed_providers=(_EBAY_PROVIDER_ID,),
                )
            )
        except (TypeError, ValueError):
            continue
    total_recall = len(valid)
    selected = tuple(valid[: request.top_k])
    record_keys = tuple(candidate.record_ref for candidate in selected)
    return ItemSearchRuntimeResult(
        platform=Platform.EBAY,
        candidates=selected,
        platform_sub_batch=_sub_batch(batch, record_keys),
        total_recall=total_recall,
        truncated=total_recall > len(selected),
    )


def _project_candidate(
    batch: CatalogBatch,
    *,
    platform: Platform,
    record_key: str,
    allowed_providers: tuple[str, ...],
) -> Candidate:
    product = _one_product(batch, record_key)
    offers = _offers_for(batch, product.product_id)
    if product.provider_id not in allowed_providers or any(
        offer.provider_id not in allowed_providers for offer in offers
    ):
        raise ValueError("record provider is outside trusted platform ownership")
    primary = offers[0]
    item_price = primary.cost_components.item_price
    if type(item_price) is not KnownCost:
        raise ValueError("candidate requires an exact item price")
    attributes = tuple(
        CandidateAttribute(name=attribute.name, value=attribute.value)
        for attribute in product.attributes
    )
    by_name = {attribute.name: attribute.value for attribute in product.attributes}
    pack_size = None if "pack_size" not in by_name else Decimal(by_name["pack_size"])
    return Candidate(
        candidate_id=product.product_id,
        item_id=product.product_id,
        platform=platform,
        title=product.title,
        price=item_price.amount,
        currency=primary.cost_components.currency,
        attributes=attributes,
        source_ref=primary.offer_id,
        record_ref=product.product_id,
        pack_size=pack_size,
        pack_note=by_name.get("pack_note"),
    )


def _manifest_record(
    batch: CatalogBatch,
    *,
    platform: Platform,
    candidate: Candidate,
    allowed_providers: tuple[str, ...],
) -> ManifestRecord:
    product = _one_product(batch, candidate.record_ref)
    offers = _offers_for(batch, product.product_id)
    expected = _project_candidate(
        batch,
        platform=platform,
        record_key=product.product_id,
        allowed_providers=allowed_providers,
    )
    if candidate != expected:
        raise ValueError("candidate differs from trusted Catalog facts")
    providers = tuple(
        sorted(
            {
                product.provider_id,
                *(offer.provider_id for offer in offers),
            }
        )
    )
    if not set(providers).issubset(allowed_providers):
        raise ValueError("manifest providers are outside trusted ownership")
    return ManifestRecord(
        record_ref=product.product_id,
        source_ref=offers[0].offer_id,
        item_id=product.product_id,
        platform=platform,
        product_id=product.product_id,
        offer_identities=tuple(
            OfferIdentity(provider_id=offer.provider_id, offer_id=offer.offer_id)
            for offer in offers
        ),
        provider_ids=providers,
    )


def _one_product(batch: CatalogBatch, product_id: str) -> Product:
    products = tuple(product for product in batch.products if product.product_id == product_id)
    if len(products) != 1:
        raise ValueError("record key must bind one product")
    return products[0]


def _offers_for(batch: CatalogBatch, product_id: str) -> tuple[Offer, ...]:
    offers = tuple(
        sorted(
            (offer for offer in batch.offers if offer.product_id == product_id),
            key=lambda offer: (
                offer.snapshot_ordinal,
                offer.provider_id,
                offer.offer_id,
            ),
        )
    )
    if not offers:
        raise ValueError("record key must bind at least one offer")
    return offers


def _sub_batch(
    source: CatalogBatch,
    record_keys: tuple[str, ...],
) -> CatalogBatch:
    if len(record_keys) != len(set(record_keys)):
        raise ValueError("sub-batch record keys must be unique")
    products = tuple(_one_product(source, record_key) for record_key in record_keys)
    offers = tuple(
        offer for product in products for offer in _offers_for(source, product.product_id)
    )
    exchange_rates = source.exchange_rates
    if exchange_rates is None:
        raise ValueError("item source batch requires exchange rates")
    evidence_ids = {
        *(binding.evidence_id for product in products for binding in product.field_evidence),
        *(attribute.evidence_id for product in products for attribute in product.attributes),
        *(binding.evidence_id for offer in offers for binding in offer.field_evidence),
        *(rate.evidence_id for rate in exchange_rates.rates),
    }
    evidence = tuple(item for item in source.evidence if item.evidence_id in evidence_ids)
    if {item.evidence_id for item in evidence} != evidence_ids:
        raise ValueError("item source evidence closure is incomplete")
    return CatalogBatch(
        snapshot_version=source.snapshot_version,
        products=products,
        offers=offers,
        evidence=evidence,
        exchange_rates=exchange_rates,
    )


def _require_exact_subset(
    candidate: CatalogBatch,
    *,
    source: CatalogBatch,
    allowed_record_keys: tuple[str, ...],
) -> None:
    if (
        candidate.snapshot_version != source.snapshot_version
        or candidate.quarantine_issues
        or candidate.fatal_issues
    ):
        raise ValueError("result sub-batch metadata differs from its source")
    allowed = set(allowed_record_keys)
    record_keys = tuple(product.product_id for product in candidate.products)
    if not set(record_keys).issubset(allowed):
        raise ValueError("result product differs from trusted index facts")
    if candidate != _sub_batch(source, record_keys):
        raise ValueError("result sub-batch differs from trusted index facts")


async def _drain_worker(worker: asyncio.Task[CaptureReceipt]) -> None:
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            continue
        except Exception:
            return
    if worker.cancelled():
        return
    try:
        worker.result()
    except Exception:
        return


def _capture_failure_code(receipt: CaptureReceipt) -> ToolFailureCode:
    if type(receipt) is RejectedCaptureReceipt:
        codes = {issue.code for issue in receipt.issues}
        if CaptureIssueCode.CAPTURE_INPUT_INVALID in codes:
            return ToolFailureCode.ITEM_SOURCE_INVALID
        return ToolFailureCode.PROVIDER_NOT_CONFIGURED
    if type(receipt) is FailedCaptureReceipt:
        codes = {issue.code for issue in receipt.issues}
        if codes.intersection(
            {
                CaptureIssueCode.PROVIDER_RESPONSE_INVALID,
                CaptureIssueCode.PROVIDER_RESPONSE_INCOMPLETE,
                CaptureIssueCode.PROVIDER_RESPONSE_LIMIT,
            }
        ):
            return ToolFailureCode.PROVIDER_RESPONSE_INVALID
        if codes.intersection(
            {
                CaptureIssueCode.SNAPSHOT_VALIDATION_FAILED,
                CaptureIssueCode.SNAPSHOT_PUBLISH_FAILED,
            }
        ):
            return ToolFailureCode.ITEM_SOURCE_INVALID
        return ToolFailureCode.PROVIDER_UNAVAILABLE
    raise TypeError("Capture returned an unsupported receipt")


__all__ = [
    "DemoItemSource",
    "LiveEbayItemSource",
    "LiveEbayPreflightError",
    "build_live_ebay_item_source",
]
