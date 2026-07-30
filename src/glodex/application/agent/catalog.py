"""Run-scoped candidate validation, deterministic rebinding, and final gateway."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256

from glodex.application.agent.contracts import (
    Candidate,
    DataMode,
    ItemSearchRuntimeResult,
    Platform,
)
from glodex.application.agent.state import (
    CANDIDATE_STORE_BYTE_LIMIT,
    TOOL_RESULT_BYTE_LIMIT,
    canonical_json_bytes,
    require_byte_limit,
)
from glodex.application.eligibility_evaluator import (
    EligibilityEvaluation,
    evaluate_eligibility,
)
from glodex.domain.catalog import (
    CatalogBatch,
    CostComponents,
    ExchangeRate,
    ExchangeRateTable,
    Offer,
    OfferIdentity,
    Product,
    ProductAttribute,
    aggregate_catalog_batch,
)
from glodex.domain.evidence import EvidenceEntityType, EvidenceRef, FieldEvidence
from glodex.domain.intent import InterpretedRequest
from glodex.domain.pricing import KnownCost, UnknownCost


@dataclass(frozen=True, slots=True)
class ManifestRecord:
    """Trusted versioned mapping from one opaque record ref to Catalog facts."""

    record_ref: str
    source_ref: str
    item_id: str
    platform: Platform
    product_id: str
    offer_identities: tuple[OfferIdentity, ...]
    provider_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for name, value in (
            ("record_ref", self.record_ref),
            ("source_ref", self.source_ref),
            ("item_id", self.item_id),
            ("product_id", self.product_id),
        ):
            _require_text(value, name)
        if type(self.platform) is not Platform:
            raise TypeError("manifest platform must be an exact Platform")
        if type(self.offer_identities) is not tuple or any(
            type(identity) is not OfferIdentity for identity in self.offer_identities
        ):
            raise TypeError("manifest offers must contain exact OfferIdentity values")
        if not self.offer_identities or len(self.offer_identities) != len(
            set(self.offer_identities)
        ):
            raise ValueError("manifest record requires unique offers")
        if type(self.provider_ids) is not tuple or any(
            type(provider_id) is not str or not provider_id for provider_id in self.provider_ids
        ):
            raise TypeError("manifest provider IDs must contain non-empty strings")
        if not self.provider_ids or len(self.provider_ids) != len(set(self.provider_ids)):
            raise ValueError("manifest record requires unique providers")
        if not {identity.provider_id for identity in self.offer_identities}.issubset(
            self.provider_ids
        ):
            raise ValueError("manifest offer providers must be declared")


@dataclass(frozen=True, slots=True)
class CandidateManifest:
    """The sole trusted source of platform and record ownership."""

    data_mode: DataMode
    snapshot_version: str
    records: tuple[ManifestRecord, ...]

    def __post_init__(self) -> None:
        if type(self.data_mode) is not DataMode:
            raise TypeError("manifest data_mode must be an exact DataMode")
        _require_text(self.snapshot_version, "snapshot_version")
        if type(self.records) is not tuple or any(
            type(record) is not ManifestRecord for record in self.records
        ):
            raise TypeError("manifest records must contain exact ManifestRecord values")
        if self.data_mode is DataMode.DEMO_SNAPSHOT and not self.records:
            raise ValueError("Demo candidate manifest requires records")
        record_refs = tuple(record.record_ref for record in self.records)
        if len(record_refs) != len(set(record_refs)):
            raise ValueError("manifest record refs must be unique")
        keys = tuple((record.platform, record.item_id) for record in self.records)
        if len(keys) != len(set(keys)):
            raise ValueError("manifest platform item IDs must be unique")
        if self.data_mode is DataMode.LIVE_MARKETPLACE:
            if not self.snapshot_version.startswith("capture-"):
                raise ValueError("live marketplace snapshots must use capture-* versions")
            if any(record.platform is not Platform.EBAY for record in self.records):
                raise ValueError("live marketplace manifests only support ebay")


@dataclass(frozen=True, slots=True)
class ValidatedCandidatePool:
    candidates: tuple[Candidate, ...]
    evaluation_batch: CatalogBatch
    records: tuple[ManifestRecord, ...]

    def __post_init__(self) -> None:
        if type(self.candidates) is not tuple or any(
            type(candidate) is not Candidate for candidate in self.candidates
        ):
            raise TypeError("candidate pool requires exact Candidate values")
        if len(self.candidates) > 100:
            raise ValueError("candidate pool cannot contain more than 100 candidates")
        if type(self.evaluation_batch) is not CatalogBatch:
            raise TypeError("candidate pool batch must be an exact CatalogBatch")
        if type(self.records) is not tuple or any(
            type(record) is not ManifestRecord for record in self.records
        ):
            raise TypeError("candidate pool records must contain exact ManifestRecord values")
        if len(self.candidates) != len(self.records):
            raise ValueError("candidate pool records must pair every candidate")
        if tuple(candidate.record_ref for candidate in self.candidates) != tuple(
            record.record_ref for record in self.records
        ):
            raise ValueError("candidate pool records must preserve candidate order")


@dataclass(frozen=True, slots=True)
class CandidateEligibility:
    """Canonical hard-gate result bound to one exact validated candidate pool."""

    pool: ValidatedCandidatePool
    evaluation: EligibilityEvaluation
    eligible_candidate_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.pool) is not ValidatedCandidatePool:
            raise TypeError("candidate eligibility requires an exact pool")
        if type(self.evaluation) is not EligibilityEvaluation:
            raise TypeError("candidate eligibility requires an exact evaluation")
        if type(self.eligible_candidate_ids) is not tuple or any(
            type(candidate_id) is not str or not candidate_id
            for candidate_id in self.eligible_candidate_ids
        ):
            raise TypeError("eligible candidate IDs must contain non-empty strings")
        expected = _eligible_candidate_ids(self.pool, self.evaluation)
        if self.eligible_candidate_ids != expected:
            raise ValueError("eligible candidate IDs differ from the canonical evaluation")


def evaluate_candidate_pool(
    pool: ValidatedCandidatePool,
    interpreted_request: InterpretedRequest,
    *,
    display_currency: str,
) -> CandidateEligibility:
    """Run the shared hard gates and bind their exact output to Candidate IDs."""

    if type(pool) is not ValidatedCandidatePool:
        raise TypeError("candidate evaluation requires an exact pool")
    if type(interpreted_request) is not InterpretedRequest:
        raise TypeError("candidate evaluation requires an exact interpreted request")
    aggregation = aggregate_catalog_batch(pool.evaluation_batch)
    evaluation = evaluate_eligibility(
        aggregation,
        pool.evaluation_batch,
        interpreted_request,
        display_currency=display_currency,
    )
    return CandidateEligibility(
        pool=pool,
        evaluation=evaluation,
        eligible_candidate_ids=_eligible_candidate_ids(pool, evaluation),
    )


class CandidateStore:
    """The one mutable root-owned store; every merge commits atomically."""

    def __init__(
        self,
        manifest: CandidateManifest,
        *,
        maximum_bytes: int = CANDIDATE_STORE_BYTE_LIMIT,
    ) -> None:
        if type(manifest) is not CandidateManifest:
            raise TypeError("candidate store requires an exact CandidateManifest")
        if type(maximum_bytes) is not int or isinstance(maximum_bytes, bool) or maximum_bytes < 1:
            raise ValueError("candidate store maximum_bytes must be positive")
        self._manifest = manifest
        self._maximum_bytes = maximum_bytes
        self._pool: ValidatedCandidatePool | None = None
        self._byte_count = 0
        self._cleared = False

    @property
    def byte_count(self) -> int:
        return self._byte_count

    @property
    def is_cleared(self) -> bool:
        return self._cleared

    @property
    def pool(self) -> ValidatedCandidatePool:
        if self._cleared:
            raise RuntimeError("candidate store is cleared")
        if self._pool is None:
            raise RuntimeError("candidate store has no validated pool")
        return self._pool

    def merge(
        self,
        results: tuple[ItemSearchRuntimeResult, ...],
    ) -> ValidatedCandidatePool:
        if self._cleared:
            raise RuntimeError("candidate store is cleared")
        if (
            type(results) is not tuple
            or not results
            or any(type(result) is not ItemSearchRuntimeResult for result in results)
        ):
            raise TypeError("candidate merge requires exact non-empty item results")

        existing_candidates = () if self._pool is None else self._pool.candidates
        existing_records = () if self._pool is None else self._pool.records
        existing_batches = () if self._pool is None else (self._pool.evaluation_batch,)
        staged_candidates = list(existing_candidates)
        staged_records = list(existing_records)
        staged_batches = list(existing_batches)
        seen_keys = {
            (candidate.platform, candidate.item_id): candidate for candidate in existing_candidates
        }
        seen_candidate_ids = {candidate.candidate_id for candidate in existing_candidates}

        manifest_by_ref = {record.record_ref: record for record in self._manifest.records}
        for result in results:
            require_byte_limit(
                result,
                maximum=TOOL_RESULT_BYTE_LIMIT,
                code="TOOL_RESULT_TOO_LARGE",
            )
            self._validate_result(result, manifest_by_ref)
            staged_batches.append(result.platform_sub_batch)
            for candidate in result.candidates:
                record = manifest_by_ref[candidate.record_ref]
                key = (candidate.platform, candidate.item_id)
                previous = seen_keys.get(key)
                if previous is not None:
                    if previous != candidate:
                        raise ValueError("duplicate platform item facts conflict")
                    continue
                if candidate.candidate_id in seen_candidate_ids:
                    raise ValueError("candidate ID maps to multiple platform items")
                seen_keys[key] = candidate
                seen_candidate_ids.add(candidate.candidate_id)
                staged_candidates.append(candidate)
                staged_records.append(record)

        if len(staged_candidates) > 100:
            raise ValueError("candidate merge exceeds 100 candidates")
        evaluation_batch = _merge_batches(
            tuple(staged_batches),
            snapshot_version=self._manifest.snapshot_version,
        )
        pool = ValidatedCandidatePool(
            candidates=tuple(staged_candidates),
            evaluation_batch=evaluation_batch,
            records=tuple(staged_records),
        )
        encoded = canonical_json_bytes(pool)
        if len(encoded) > self._maximum_bytes:
            raise ValueError("candidate store exceeds byte limit")
        self._pool = pool
        self._byte_count = len(encoded)
        return pool

    def clear(self) -> None:
        self._pool = None
        self._byte_count = 0
        self._cleared = True

    def _validate_result(
        self,
        result: ItemSearchRuntimeResult,
        manifest_by_ref: dict[str, ManifestRecord],
    ) -> None:
        batch = result.platform_sub_batch
        if batch.snapshot_version != self._manifest.snapshot_version:
            raise ValueError("item result snapshot does not match manifest")
        if batch.fatal_issues:
            raise ValueError("item result cannot contain a fatal batch")
        records: list[ManifestRecord] = []
        for candidate in result.candidates:
            record = manifest_by_ref.get(candidate.record_ref)
            if record is None:
                raise ValueError("candidate record ref is not in the manifest")
            if (
                record.platform is not result.platform
                or candidate.platform is not record.platform
                or candidate.item_id != record.item_id
                or candidate.source_ref != record.source_ref
            ):
                raise ValueError("candidate ownership does not match the manifest")
            records.append(record)

        expected_products = {record.product_id for record in records}
        products_by_id: dict[str, list[Product]] = {}
        for product in batch.products:
            products_by_id.setdefault(product.product_id, []).append(product)
        if set(products_by_id) != expected_products:
            raise ValueError("item result products do not match record refs")
        if any(len(products_by_id[product_id]) != 1 for product_id in expected_products):
            raise ValueError("each record ref must bind exactly one Catalog product")

        expected_offers = {identity for record in records for identity in record.offer_identities}
        offers_by_identity: dict[OfferIdentity, list[Offer]] = {}
        for offer in batch.offers:
            identity = OfferIdentity(
                provider_id=offer.provider_id,
                offer_id=offer.offer_id,
            )
            offers_by_identity.setdefault(identity, []).append(offer)
        if set(offers_by_identity) != expected_offers:
            raise ValueError("item result offers do not match record refs")
        if any(len(offers_by_identity[identity]) != 1 for identity in expected_offers):
            raise ValueError("each manifest identity must bind exactly one Catalog offer")

        actual_evidence_ids = {evidence.evidence_id for evidence in batch.evidence}
        if actual_evidence_ids != _required_evidence_ids(batch):
            raise ValueError("item result does not have exact evidence closure")

        for candidate, record in zip(result.candidates, records, strict=True):
            product = products_by_id[record.product_id][0]
            if product.provider_id not in record.provider_ids:
                raise ValueError(
                    "item result provider ownership does not match the manifest record"
                )

            record_offers: list[Offer] = []
            for identity in record.offer_identities:
                offer = offers_by_identity[identity][0]
                if offer.product_id != record.product_id:
                    raise ValueError("manifest offer parent does not match its record product")
                if offer.provider_id not in record.provider_ids:
                    raise ValueError(
                        "item result provider ownership does not match the manifest record"
                    )
                record_offers.append(offer)

            source_offers = tuple(
                offer for offer in record_offers if offer.offer_id == record.source_ref
            )
            if len(source_offers) != 1:
                raise ValueError("candidate source ref must bind exactly one manifest source offer")
            source_offer = source_offers[0]
            item_price = source_offer.cost_components.item_price
            if (
                type(item_price) is not KnownCost
                or item_price.amount != candidate.price
                or source_offer.cost_components.currency != candidate.currency
            ):
                raise ValueError("candidate price and currency do not match the source offer")
            _validate_candidate_projection(candidate, product)


def _required_evidence_ids(batch: CatalogBatch) -> set[str]:
    required = {
        binding.evidence_id for product in batch.products for binding in product.field_evidence
    }
    required.update(
        attribute.evidence_id for product in batch.products for attribute in product.attributes
    )
    required.update(
        binding.evidence_id for offer in batch.offers for binding in offer.field_evidence
    )
    if batch.exchange_rates is not None:
        required.update(rate.evidence_id for rate in batch.exchange_rates.rates)
    return required


def _validate_candidate_projection(candidate: Candidate, product: Product) -> None:
    expected_attributes = tuple(
        (attribute.name, attribute.value) for attribute in product.attributes
    )
    actual_attributes = tuple(
        (attribute.name, attribute.value) for attribute in candidate.attributes
    )
    attributes_by_name = {attribute.name: attribute.value for attribute in product.attributes}
    raw_pack_size = attributes_by_name.get("pack_size")
    try:
        expected_pack_size = None if raw_pack_size is None else Decimal(raw_pack_size)
    except InvalidOperation as error:
        raise ValueError("Catalog pack_size is not an exact Decimal") from error
    if expected_pack_size is not None and (
        not expected_pack_size.is_finite() or expected_pack_size <= 0
    ):
        raise ValueError("Catalog pack_size must be finite and positive")

    if (
        candidate.title != product.title
        or actual_attributes != expected_attributes
        or candidate.pack_size != expected_pack_size
        or candidate.pack_note != attributes_by_name.get("pack_note")
        or candidate.rating is not None
        or candidate.sales is not None
        or candidate.image_url is not None
    ):
        raise ValueError("candidate differs from the exact Catalog projection")


def _eligible_candidate_ids(
    pool: ValidatedCandidatePool,
    evaluation: EligibilityEvaluation,
) -> tuple[str, ...]:
    eligible_records = {
        (
            candidate.product.product_id,
            eligible_offer.offer.provider_id,
            eligible_offer.offer.offer_id,
        )
        for candidate in evaluation.eligibility_output.candidates
        for eligible_offer in candidate.eligible_offers
    }
    return tuple(
        candidate.candidate_id
        for candidate, record in zip(
            pool.candidates,
            pool.records,
            strict=True,
        )
        if any(
            (
                record.product_id,
                identity.provider_id,
                identity.offer_id,
            )
            in eligible_records
            for identity in record.offer_identities
        )
    )


def build_fx_evaluation_view(
    source_batch: CatalogBatch,
    fx_source_batch: CatalogBatch,
) -> CatalogBatch:
    """Rebind only a versioned FX table/evidence onto one source snapshot."""

    if type(source_batch) is not CatalogBatch or type(fx_source_batch) is not CatalogBatch:
        raise TypeError("FX evaluation view requires exact CatalogBatch values")
    table = fx_source_batch.exchange_rates
    if table is None:
        raise ValueError("FX source batch has no exchange-rate table")
    fx_evidence_by_id = {
        evidence.evidence_id: evidence
        for evidence in fx_source_batch.evidence
        if evidence.entity_type is EvidenceEntityType.EXCHANGE_RATE
    }
    evidence_id_map = {
        rate.evidence_id: _rebound_id("fx", "e", rate.evidence_id) for rate in table.rates
    }
    rates = tuple(
        ExchangeRate(
            snapshot_version=source_batch.snapshot_version,
            currency=rate.currency,
            base_per_unit=rate.base_per_unit,
            minor_units=rate.minor_units,
            evidence_id=evidence_id_map[rate.evidence_id],
            snapshot_ordinal=rate.snapshot_ordinal,
        )
        for rate in table.rates
    )
    rebound_evidence = tuple(
        _rebind_fx_evidence(
            fx_evidence_by_id[rate.evidence_id],
            snapshot_version=source_batch.snapshot_version,
            evidence_id=evidence_id_map[rate.evidence_id],
        )
        for rate in table.rates
    )
    source_non_fx = tuple(
        evidence
        for evidence in source_batch.evidence
        if evidence.entity_type is not EvidenceEntityType.EXCHANGE_RATE
    )
    return CatalogBatch(
        snapshot_version=source_batch.snapshot_version,
        products=source_batch.products,
        offers=source_batch.offers,
        evidence=(*source_non_fx, *rebound_evidence),
        exchange_rates=ExchangeRateTable(
            snapshot_version=source_batch.snapshot_version,
            base_currency=table.base_currency,
            rates=rates,
        ),
        quarantine_issues=source_batch.quarantine_issues,
        fatal_issues=source_batch.fatal_issues,
    )


@dataclass(frozen=True, slots=True)
class RebindingEntry:
    candidate_id: str
    product_id: str
    offer_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_text(self.candidate_id, "candidate_id")
        _require_text(self.product_id, "product_id")
        if (
            type(self.offer_ids) is not tuple
            or not self.offer_ids
            or any(type(offer_id) is not str or not offer_id for offer_id in self.offer_ids)
        ):
            raise TypeError("rebound offer IDs must be a non-empty tuple")


@dataclass(frozen=True, slots=True)
class RebindingMap:
    entries: tuple[RebindingEntry, ...]

    def __post_init__(self) -> None:
        if type(self.entries) is not tuple or any(
            type(entry) is not RebindingEntry for entry in self.entries
        ):
            raise TypeError("rebinding map entries must be exact RebindingEntry values")
        candidate_ids = tuple(entry.candidate_id for entry in self.entries)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("rebinding map candidate IDs must be unique")

    def for_candidate(self, candidate_id: str) -> RebindingEntry:
        matching = tuple(entry for entry in self.entries if entry.candidate_id == candidate_id)
        if len(matching) != 1:
            raise KeyError(candidate_id)
        return matching[0]


@dataclass(frozen=True, slots=True)
class ReboundCatalog:
    batch: CatalogBatch
    mapping: RebindingMap

    def __post_init__(self) -> None:
        if type(self.batch) is not CatalogBatch:
            raise TypeError("rebound catalog batch must be an exact CatalogBatch")
        if type(self.mapping) is not RebindingMap:
            raise TypeError("rebound catalog mapping must be an exact RebindingMap")


def rebind_selected_candidates(
    pool: ValidatedCandidatePool,
    candidate_ids: tuple[str, ...],
    *,
    run_id: str,
    fx_source_batch: CatalogBatch,
) -> ReboundCatalog:
    """Build the final evidence-closed run-scoped batch for selected candidates."""

    if type(pool) is not ValidatedCandidatePool:
        raise TypeError("rebinding requires an exact ValidatedCandidatePool")
    if type(candidate_ids) is not tuple or any(
        type(candidate_id) is not str or not candidate_id for candidate_id in candidate_ids
    ):
        raise TypeError("candidate_ids must be a tuple of non-empty strings")
    if len(candidate_ids) != len(set(candidate_ids)) or len(candidate_ids) > 3:
        raise ValueError("candidate_ids must contain at most three unique IDs")
    _require_text(run_id, "run_id")
    if type(fx_source_batch) is not CatalogBatch:
        raise TypeError("fx_source_batch must be an exact CatalogBatch")

    paired = dict(
        zip(
            (candidate.candidate_id for candidate in pool.candidates),
            zip(pool.candidates, pool.records, strict=True),
            strict=True,
        )
    )
    try:
        selected = tuple(paired[candidate_id] for candidate_id in candidate_ids)
    except KeyError as error:
        raise ValueError("selected candidate is outside the validated pool") from error

    snapshot_version = f"agent-{sha256(run_id.encode('utf-8')).hexdigest()[:16]}"
    selected_product_ids = {record.product_id for _, record in selected}
    selected_offer_identities = {
        identity for _, record in selected for identity in record.offer_identities
    }
    products = tuple(
        product
        for product in pool.evaluation_batch.products
        if product.product_id in selected_product_ids
    )
    offers = tuple(
        offer
        for offer in pool.evaluation_batch.offers
        if OfferIdentity(provider_id=offer.provider_id, offer_id=offer.offer_id)
        in selected_offer_identities
    )
    records_by_product = {record.product_id: record for _, record in selected}
    records_by_offer = {
        identity: record for _, record in selected for identity in record.offer_identities
    }
    if {product.product_id for product in products} != selected_product_ids:
        raise ValueError("selected products are missing from the evaluation batch")
    if {
        OfferIdentity(provider_id=offer.provider_id, offer_id=offer.offer_id) for offer in offers
    } != selected_offer_identities:
        raise ValueError("selected offers are missing from the evaluation batch")

    product_id_map = {
        product_id: _rebound_id(records_by_product[product_id].platform.value, "p", product_id)
        for product_id in selected_product_ids
    }
    offer_id_map = {
        identity: _rebound_id(
            records_by_offer[identity].platform.value,
            "o",
            identity.offer_id,
        )
        for identity in selected_offer_identities
    }
    _require_injective(tuple(product_id_map.items()), "product")
    _require_injective(tuple(offer_id_map.items()), "offer")

    used_evidence_ids = {
        *(binding.evidence_id for product in products for binding in product.field_evidence),
        *(attribute.evidence_id for product in products for attribute in product.attributes),
        *(binding.evidence_id for offer in offers for binding in offer.field_evidence),
    }
    evidence_by_id = {
        evidence.evidence_id: evidence
        for evidence in pool.evaluation_batch.evidence
        if evidence.evidence_id in used_evidence_ids
    }
    if set(evidence_by_id) != used_evidence_ids:
        raise ValueError("selected record evidence closure is incomplete")
    evidence_id_map: dict[str, str] = {}
    for evidence in evidence_by_id.values():
        if evidence.entity_type is EvidenceEntityType.PRODUCT:
            assert evidence.product_id is not None
            namespace = records_by_product[evidence.product_id].platform.value
        elif evidence.entity_type is EvidenceEntityType.OFFER:
            assert evidence.offer_id is not None
            identity = OfferIdentity(
                provider_id=evidence.provider_id,
                offer_id=evidence.offer_id,
            )
            namespace = records_by_offer[identity].platform.value
        else:
            raise ValueError("source record closure cannot include source FX evidence")
        evidence_id_map[evidence.evidence_id] = _rebound_id(
            namespace,
            "e",
            evidence.evidence_id,
        )

    rebound_products = tuple(
        _rebind_product(
            product,
            snapshot_version=snapshot_version,
            product_id=product_id_map[product.product_id],
            evidence_id_map=evidence_id_map,
        )
        for product in products
    )
    rebound_offers = tuple(
        _rebind_offer(
            offer,
            snapshot_version=snapshot_version,
            product_id=product_id_map[offer.product_id],
            offer_id=offer_id_map[
                OfferIdentity(provider_id=offer.provider_id, offer_id=offer.offer_id)
            ],
            evidence_id_map=evidence_id_map,
        )
        for offer in offers
    )
    rebound_source_evidence = tuple(
        _rebind_record_evidence(
            evidence,
            snapshot_version=snapshot_version,
            evidence_id=evidence_id_map[evidence.evidence_id],
            product_id=(
                None if evidence.product_id is None else product_id_map[evidence.product_id]
            ),
            offer_id=(
                None
                if evidence.offer_id is None
                else offer_id_map[
                    OfferIdentity(
                        provider_id=evidence.provider_id,
                        offer_id=evidence.offer_id,
                    )
                ]
            ),
        )
        for evidence in evidence_by_id.values()
    )

    fx_view = build_fx_evaluation_view(
        CatalogBatch(
            snapshot_version=snapshot_version,
            products=rebound_products,
            offers=rebound_offers,
            evidence=rebound_source_evidence,
        ),
        fx_source_batch,
    )
    mapping = RebindingMap(
        entries=tuple(
            RebindingEntry(
                candidate_id=candidate.candidate_id,
                product_id=product_id_map[record.product_id],
                offer_ids=tuple(offer_id_map[identity] for identity in record.offer_identities),
            )
            for candidate, record in selected
        )
    )
    return ReboundCatalog(batch=fx_view, mapping=mapping)


class InMemoryCatalogGateway:
    """A read-only final SearchService gateway scoped to one Agent run."""

    def __init__(
        self,
        batch: CatalogBatch,
        *,
        display_currency: str,
        budget_currency: str | None,
    ) -> None:
        if type(batch) is not CatalogBatch:
            raise TypeError("in-memory gateway requires an exact CatalogBatch")
        _require_currency(display_currency)
        if budget_currency is not None:
            _require_currency(budget_currency)
        self._batch: CatalogBatch | None = batch
        self._display_currency = display_currency
        self._budget_currency = budget_currency

    async def load(
        self,
        snapshot_version: str,
        *,
        display_currency: str | None = None,
        budget_currency: str | None = None,
    ) -> CatalogBatch:
        batch = self._batch
        if batch is None:
            raise RuntimeError("in-memory gateway is cleared")
        if snapshot_version != batch.snapshot_version:
            raise ValueError("in-memory gateway snapshot mismatch")
        if display_currency != self._display_currency or budget_currency != self._budget_currency:
            raise ValueError("in-memory gateway currency mismatch")
        return batch

    def clear(self) -> None:
        self._batch = None


def _merge_batches(
    batches: tuple[CatalogBatch, ...],
    *,
    snapshot_version: str,
) -> CatalogBatch:
    if not batches:
        raise ValueError("candidate merge requires at least one batch")
    if any(batch.snapshot_version != snapshot_version for batch in batches):
        raise ValueError("cannot merge multiple snapshot versions")
    products = _deduplicate_exact(
        tuple(product for batch in batches for product in batch.products),
        key=lambda product: (product.provider_id, product.product_id, product.source_uri),
        name="product",
    )
    offers = _deduplicate_exact(
        tuple(offer for batch in batches for offer in batch.offers),
        key=lambda offer: (offer.provider_id, offer.offer_id),
        name="offer",
    )
    evidence = _deduplicate_exact(
        tuple(evidence for batch in batches for evidence in batch.evidence),
        key=lambda item: item.evidence_id,
        name="evidence",
    )
    tables = tuple(batch.exchange_rates for batch in batches if batch.exchange_rates is not None)
    table = None
    if tables:
        table = tables[0]
        if any(candidate != table for candidate in tables[1:]):
            raise ValueError("exchange-rate facts conflict across item results")
    quarantine = _deduplicate_exact(
        tuple(issue for batch in batches for issue in batch.quarantine_issues),
        key=lambda issue: (
            issue.stage,
            issue.code,
            issue.entity_ref,
            issue.message,
        ),
        name="quarantine issue",
    )
    return CatalogBatch(
        snapshot_version=snapshot_version,
        products=products,
        offers=offers,
        evidence=evidence,
        exchange_rates=table,
        quarantine_issues=quarantine,
    )


def _deduplicate_exact[T, K](
    values: tuple[T, ...],
    *,
    key: Callable[[T], K],
    name: str,
) -> tuple[T, ...]:
    seen: dict[K, T] = {}
    ordered: list[T] = []
    for value in values:
        identity = key(value)
        previous = seen.get(identity)
        if previous is None:
            seen[identity] = value
            ordered.append(value)
        elif previous != value:
            raise ValueError(f"{name} identity facts conflict")
    return tuple(ordered)


def _rebind_product(
    product: Product,
    *,
    snapshot_version: str,
    product_id: str,
    evidence_id_map: dict[str, str],
) -> Product:
    return Product(
        snapshot_version=snapshot_version,
        product_id=product_id,
        provider_id=product.provider_id,
        source_uri=product.source_uri,
        title=product.title,
        category=product.category,
        entity_kind=product.entity_kind,
        snapshot_ordinal=product.snapshot_ordinal,
        attributes=tuple(
            ProductAttribute(
                name=attribute.name,
                value=attribute.value,
                evidence_id=evidence_id_map[attribute.evidence_id],
            )
            for attribute in product.attributes
        ),
        field_evidence=tuple(
            FieldEvidence(
                field_path=binding.field_path,
                evidence_id=evidence_id_map[binding.evidence_id],
            )
            for binding in product.field_evidence
        ),
    )


def _rebind_offer(
    offer: Offer,
    *,
    snapshot_version: str,
    product_id: str,
    offer_id: str,
    evidence_id_map: dict[str, str],
) -> Offer:
    def cost(value: object) -> KnownCost | UnknownCost:
        if type(value) is KnownCost:
            return KnownCost(
                amount=value.amount,
                evidence_id=evidence_id_map[value.evidence_id],
            )
        if type(value) is UnknownCost:
            return value
        raise TypeError("offer contains an invalid cost component")

    return Offer(
        snapshot_version=snapshot_version,
        offer_id=offer_id,
        product_id=product_id,
        provider_id=offer.provider_id,
        source_uri=offer.source_uri,
        market=offer.market,
        stock_status=offer.stock_status,
        cost_components=CostComponents(
            currency=offer.cost_components.currency,
            item_price=cost(offer.cost_components.item_price),
            shipping=cost(offer.cost_components.shipping),
            tax=cost(offer.cost_components.tax),
            duty=cost(offer.cost_components.duty),
        ),
        captured_at=offer.captured_at,
        snapshot_ordinal=offer.snapshot_ordinal,
        field_evidence=tuple(
            FieldEvidence(
                field_path=binding.field_path,
                evidence_id=evidence_id_map[binding.evidence_id],
            )
            for binding in offer.field_evidence
        ),
    )


def _rebind_record_evidence(
    evidence: EvidenceRef,
    *,
    snapshot_version: str,
    evidence_id: str,
    product_id: str | None,
    offer_id: str | None,
) -> EvidenceRef:
    return EvidenceRef(
        evidence_id=evidence_id,
        snapshot_version=snapshot_version,
        entity_type=evidence.entity_type,
        product_id=product_id,
        offer_id=offer_id,
        currency=evidence.currency,
        field_path=evidence.field_path,
        provider_id=evidence.provider_id,
        source_uri=evidence.source_uri,
        captured_at=evidence.captured_at,
    )


def _rebind_fx_evidence(
    evidence: EvidenceRef,
    *,
    snapshot_version: str,
    evidence_id: str,
) -> EvidenceRef:
    if evidence.entity_type is not EvidenceEntityType.EXCHANGE_RATE:
        raise ValueError("FX table references non-FX evidence")
    return EvidenceRef(
        evidence_id=evidence_id,
        snapshot_version=snapshot_version,
        entity_type=evidence.entity_type,
        product_id=None,
        offer_id=None,
        currency=evidence.currency,
        field_path=evidence.field_path,
        provider_id=evidence.provider_id,
        source_uri=evidence.source_uri,
        captured_at=evidence.captured_at,
    )


def _rebound_id(namespace: str, kind: str, original_id: str) -> str:
    return f"{namespace}.{kind}.{sha256(original_id.encode('utf-8')).hexdigest()[:24]}"


def _require_injective(values: tuple[tuple[object, str], ...], name: str) -> None:
    rebound = tuple(value for _, value in values)
    if len(rebound) != len(set(rebound)):
        raise ValueError(f"{name} rebinding collision")


def _require_text(value: object, name: str) -> str:
    if type(value) is not str or not value.strip() or len(value) > 128:
        raise ValueError(f"{name} must be a non-empty string of at most 128 code points")
    return value


def _require_currency(value: object) -> str:
    if (
        type(value) is not str
        or len(value) != 3
        or not value.isascii()
        or not value.isalpha()
        or not value.isupper()
    ):
        raise ValueError("currency must be an uppercase three-letter code")
    return value


__all__ = [
    "CandidateEligibility",
    "CandidateManifest",
    "CandidateStore",
    "InMemoryCatalogGateway",
    "ManifestRecord",
    "RebindingEntry",
    "RebindingMap",
    "ReboundCatalog",
    "ValidatedCandidatePool",
    "build_fx_evaluation_view",
    "evaluate_candidate_pool",
    "rebind_selected_candidates",
]
