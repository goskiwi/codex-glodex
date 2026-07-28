"""Fail-closed reader for versioned local catalog snapshots.

Phase B04 deliberately materializes only the exchange-rate slice.  Every
manifest-declared file is still path-, size-, hash-, count-, JSON-, and
snapshot-version checked before the successful ``CatalogBatch`` is returned.
Product/offer record isolation and full evidence materialization belong to B05.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Final, Never, cast

from glodex.domain.catalog import (
    CatalogBatch,
    CostComponents,
    EntityKind,
    ExchangeRate,
    ExchangeRateTable,
    Offer,
    Product,
    ProductAttribute,
    StockStatus,
)
from glodex.domain.evidence import EvidenceEntityType, EvidenceRef, FieldEvidence
from glodex.domain.issues import (
    CatalogIssue,
    IssueCode,
    IssueDetail,
    IssueDisposition,
    IssueStage,
)
from glodex.domain.pricing import CostValue, KnownCost, UnknownCost

MANIFEST_SCHEMA_VERSION: Final = "glodex.snapshot-manifest.v1"
EXCHANGE_RATES_SCHEMA_VERSION: Final = "glodex.exchange-rates.v1"
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_JSONL_RECORDS: Final = 500_000

_FILE_ROLES: Final = ("products", "offers", "evidence", "exchange_rates")
_JSONL_ROLES: Final = frozenset({"products", "offers", "evidence"})
_ROLE_STAGES: Final = {
    "products": IssueStage.PRODUCTS,
    "offers": IssueStage.OFFERS,
    "evidence": IssueStage.EVIDENCE,
    "exchange_rates": IssueStage.EXCHANGE_RATES,
}
_SNAPSHOT_VERSION = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,127})\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CURRENCY = re.compile(r"[A-Z]{3}\Z")
_FIXED_POSITIVE_DECIMAL = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?\Z")
_SHORT_TEXT_FIELDS: Final = frozenset(
    {
        "snapshot_version",
        "product_id",
        "offer_id",
        "evidence_id",
        "provider_id",
        "currency",
        "base_currency",
        "market",
        "category",
        "name",
        "version",
    }
)
_MANIFEST_KEYS: Final = frozenset(
    {
        "schema_version",
        "snapshot_version",
        "created_at",
        "base_currency",
        "files",
        "providers",
        "markets",
        "categories",
        "currencies",
        "generator",
    }
)
_FILE_SPEC_KEYS: Final = frozenset({"path", "sha256", "record_count"})
_GENERATOR_KEYS: Final = frozenset({"name", "version"})
_FX_KEYS: Final = frozenset({"schema_version", "snapshot_version", "base_currency", "rates"})
_RATE_KEYS: Final = frozenset({"currency", "base_per_unit", "minor_units", "evidence_id"})
_EVIDENCE_KEYS: Final = frozenset(
    {
        "evidence_id",
        "snapshot_version",
        "entity_type",
        "product_id",
        "offer_id",
        "currency",
        "field_path",
        "provider_id",
        "source_uri",
        "captured_at",
    }
)
_PRODUCT_KEYS: Final = frozenset(
    {
        "snapshot_version",
        "product_id",
        "provider_id",
        "source_uri",
        "title",
        "category",
        "entity_kind",
        "attributes",
        "field_evidence",
    }
)
_ATTRIBUTE_KEYS: Final = frozenset({"name", "value", "evidence_id"})
_FIELD_EVIDENCE_KEYS: Final = frozenset({"field_path", "evidence_id"})
_OFFER_KEYS: Final = frozenset(
    {
        "snapshot_version",
        "offer_id",
        "product_id",
        "provider_id",
        "source_uri",
        "market",
        "stock_status",
        "cost_components",
        "captured_at",
        "field_evidence",
    }
)
_COST_COMPONENTS_KEYS: Final = frozenset({"currency", "item_price", "shipping", "tax", "duty"})
_KNOWN_COST_KEYS: Final = frozenset({"kind", "amount", "evidence_id"})
_UNKNOWN_COST_KEYS: Final = frozenset({"kind", "reason"})
_COST_NAMES: Final = ("item_price", "shipping", "tax", "duty")
_CANONICAL_POSITIVE_AMOUNT = re.compile(r"[1-9][0-9]*(?:\.[0-9]*[1-9])?\Z")
_ISSUE_STAGE_ORDER: Final = {
    IssueStage.PRODUCTS: 0,
    IssueStage.OFFERS: 1,
    IssueStage.EVIDENCE: 2,
}


@dataclass(frozen=True, slots=True)
class SnapshotFile:
    """One immutable manifest file declaration."""

    role: str
    relative_path: str
    sha256: str
    record_count: int


@dataclass(frozen=True, slots=True)
class SnapshotManifest:
    """Validated manifest values needed by the local adapter."""

    snapshot_version: str
    created_at: datetime
    base_currency: str
    files: tuple[SnapshotFile, ...]
    providers: tuple[str, ...]
    markets: tuple[str, ...]
    categories: tuple[str, ...]
    currencies: tuple[str, ...]
    generator_name: str
    generator_version: str

    def file(self, role: str) -> SnapshotFile:
        return next(item for item in self.files if item.role == role)


@dataclass(frozen=True, slots=True)
class _LocatedProduct:
    ordinal: int
    value: Product


@dataclass(frozen=True, slots=True)
class _LocatedOffer:
    ordinal: int
    value: Offer


@dataclass(frozen=True, slots=True)
class _LocatedEvidence:
    ordinal: int
    value: EvidenceRef


@dataclass(frozen=True, slots=True)
class _QuarantinedRecord:
    ordinal: int
    issue: CatalogIssue


@dataclass(frozen=True, slots=True)
class _ProductRead:
    records: tuple[_LocatedProduct, ...]
    issues: tuple[_QuarantinedRecord, ...]
    providers: frozenset[str]
    categories: frozenset[str]


@dataclass(frozen=True, slots=True)
class _OfferRead:
    records: tuple[_LocatedOffer, ...]
    issues: tuple[_QuarantinedRecord, ...]
    providers: frozenset[str]
    markets: frozenset[str]
    currencies: frozenset[str]


@dataclass(frozen=True, slots=True)
class _EvidenceRead:
    records: tuple[_LocatedEvidence, ...]
    issues: tuple[_QuarantinedRecord, ...]


class _DuplicateKeyError(ValueError):
    pass


class _CoreJsonError(ValueError):
    pass


class _LoadFailure(Exception):
    def __init__(self, issue: CatalogIssue) -> None:
        self.issue = issue
        super().__init__(issue.code.value)


class LocalSnapshotCatalog:
    """Load one immutable snapshot from a trusted configured root directory."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root).expanduser().resolve()

    async def load(
        self,
        snapshot_version: str,
        *,
        display_currency: str | None = None,
        budget_currency: str | None = None,
    ) -> CatalogBatch:
        """Validate and load a snapshot without raising data/path errors.

        A fatal outcome is represented by a ``CatalogBatch`` containing exactly
        fatal issues and no products, offers, evidence, or exchange-rate table.
        """

        outcome_version = _safe_outcome_version(snapshot_version)
        try:
            return self._load_validated(
                snapshot_version,
                display_currency=display_currency,
                budget_currency=budget_currency,
            )
        except _LoadFailure as error:
            return CatalogBatch(
                snapshot_version=outcome_version,
                fatal_issues=(error.issue,),
            )
        except Exception:
            # The adapter is a trust boundary.  Unexpected parser/filesystem
            # failures become one static issue; exception text and paths never
            # cross into the public outcome.
            return CatalogBatch(
                snapshot_version=outcome_version,
                fatal_issues=(
                    _issue(
                        IssueCode.CORE_JSON_INVALID,
                        IssueStage.MANIFEST,
                        "Snapshot validation failed.",
                    ),
                ),
            )

    def _load_validated(
        self,
        snapshot_version: str,
        *,
        display_currency: str | None,
        budget_currency: str | None,
    ) -> CatalogBatch:
        snapshot_dir = self._resolve_snapshot_dir(snapshot_version)
        manifest = self._read_manifest(snapshot_dir, snapshot_version)
        paths = self._resolve_all_files(snapshot_dir, manifest)

        # Security metadata and integrity are checked for all four files before
        # any record is trusted or materialized.
        self._validate_file_metadata(manifest, paths)
        self._validate_hashes(manifest, paths)

        products = self._read_products(
            paths["products"],
            manifest.file("products"),
            snapshot_version,
        )
        offers = self._read_offers(
            paths["offers"],
            manifest.file("offers"),
            snapshot_version,
        )
        exchange_rates = self._read_exchange_rates(
            paths["exchange_rates"],
            manifest,
        )
        evidence = self._read_evidence(
            paths["evidence"],
            manifest.file("evidence"),
            snapshot_version,
        )
        self._validate_manifest_inventories(
            manifest,
            products,
            offers,
            exchange_rates,
        )
        evidence_by_id = {record.value.evidence_id: record.value for record in evidence.records}
        self._validate_rate_evidence(exchange_rates, evidence_by_id)
        valid_products, product_closure_issues = _filter_products(
            products.records,
            evidence_by_id,
        )
        valid_offers, offer_closure_issues = _filter_offers(
            offers.records,
            frozenset(product.product_id for product in valid_products),
            evidence_by_id,
        )
        self._validate_currency_compatibility(
            exchange_rates,
            display_currency=display_currency,
            budget_currency=budget_currency,
        )
        quarantine_issues = _ordered_quarantine_issues(
            (
                *products.issues,
                *product_closure_issues,
                *offers.issues,
                *offer_closure_issues,
                *evidence.issues,
            )
        )

        return CatalogBatch(
            snapshot_version=snapshot_version,
            products=valid_products,
            offers=valid_offers,
            evidence=tuple(record.value for record in evidence.records),
            exchange_rates=exchange_rates,
            quarantine_issues=quarantine_issues,
        )

    def _resolve_snapshot_dir(self, snapshot_version: str) -> Path:
        if (
            type(snapshot_version) is not str
            or _SNAPSHOT_VERSION.fullmatch(snapshot_version) is None
            or "\\" in snapshot_version
            or snapshot_version in {".", ".."}
        ):
            _fail(
                IssueCode.PATH_INVALID,
                IssueStage.MANIFEST,
                "Snapshot version is not a safe path component.",
            )
        resolved = (self._root / snapshot_version).resolve()
        if not _is_within(resolved, self._root):
            _fail(
                IssueCode.PATH_INVALID,
                IssueStage.MANIFEST,
                "Snapshot directory escapes the configured root.",
            )
        return resolved

    def _read_manifest(
        self,
        snapshot_dir: Path,
        requested_version: str,
    ) -> SnapshotManifest:
        candidate = snapshot_dir / "manifest.json"
        resolved = candidate.resolve()
        if not _is_within(resolved, snapshot_dir) or not _is_within(resolved, self._root):
            _fail(
                IssueCode.PATH_INVALID,
                IssueStage.MANIFEST,
                "Manifest path escapes the snapshot directory.",
            )
        if not resolved.is_file():
            _fail(
                IssueCode.MANIFEST_MISSING,
                IssueStage.MANIFEST,
                "Snapshot manifest is missing.",
            )
        try:
            if resolved.stat().st_size > MAX_FILE_BYTES:
                _fail(
                    IssueCode.FILE_TOO_LARGE,
                    IssueStage.MANIFEST,
                    "Manifest exceeds the file size limit.",
                )
            payload = _parse_json(resolved.read_bytes())
            return _parse_manifest(payload, requested_version)
        except _LoadFailure:
            raise
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            _fail(
                IssueCode.MANIFEST_INVALID,
                IssueStage.MANIFEST,
                "Snapshot manifest is invalid.",
            )

    def _resolve_all_files(
        self,
        snapshot_dir: Path,
        manifest: SnapshotManifest,
    ) -> dict[str, Path]:
        resolved: dict[str, Path] = {}
        seen: set[Path] = set()
        for spec in manifest.files:
            relative = spec.relative_path
            posix = PurePosixPath(relative)
            windows = PureWindowsPath(relative)
            if (
                not relative
                or "\\" in relative
                or "\x00" in relative
                or posix.is_absolute()
                or windows.is_absolute()
                or windows.drive
                or any(part in {"", ".", ".."} for part in posix.parts)
                or posix.as_posix() != relative
            ):
                _fail(
                    IssueCode.PATH_INVALID,
                    _ROLE_STAGES[spec.role],
                    "Manifest file path is not a safe relative path.",
                    entity_ref=spec.role,
                )
            target = (snapshot_dir / Path(*posix.parts)).resolve()
            if not _is_within(target, snapshot_dir) or not _is_within(target, self._root):
                _fail(
                    IssueCode.PATH_INVALID,
                    _ROLE_STAGES[spec.role],
                    "Manifest file path escapes the snapshot directory.",
                    entity_ref=spec.role,
                )
            if target in seen:
                _fail(
                    IssueCode.PATH_INVALID,
                    _ROLE_STAGES[spec.role],
                    "Manifest file paths must be unique.",
                    entity_ref=spec.role,
                )
            seen.add(target)
            resolved[spec.role] = target
        return resolved

    def _validate_file_metadata(
        self,
        manifest: SnapshotManifest,
        paths: dict[str, Path],
    ) -> None:
        for spec in manifest.files:
            path = paths[spec.role]
            try:
                if not path.is_file():
                    _fail(
                        IssueCode.PATH_INVALID,
                        _ROLE_STAGES[spec.role],
                        "Manifest file is missing or is not a regular file.",
                        entity_ref=spec.role,
                    )
                if path.stat().st_size > MAX_FILE_BYTES:
                    _fail(
                        IssueCode.FILE_TOO_LARGE,
                        _ROLE_STAGES[spec.role],
                        "Snapshot file exceeds the size limit.",
                        entity_ref=spec.role,
                        details=(IssueDetail("limit_bytes", MAX_FILE_BYTES),),
                    )
                if spec.role in _JSONL_ROLES and spec.record_count > MAX_JSONL_RECORDS:
                    _fail(
                        IssueCode.RECORD_LIMIT_EXCEEDED,
                        _ROLE_STAGES[spec.role],
                        "JSONL record count exceeds the limit.",
                        entity_ref=spec.role,
                        details=(
                            IssueDetail("limit_records", MAX_JSONL_RECORDS),
                            IssueDetail("declared_records", spec.record_count),
                        ),
                    )
            except _LoadFailure:
                raise
            except OSError:
                _fail(
                    IssueCode.PATH_INVALID,
                    _ROLE_STAGES[spec.role],
                    "Snapshot file metadata is unavailable.",
                    entity_ref=spec.role,
                )

    def _validate_hashes(
        self,
        manifest: SnapshotManifest,
        paths: dict[str, Path],
    ) -> None:
        for spec in manifest.files:
            digest = hashlib.sha256()
            try:
                with paths[spec.role].open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError:
                _fail(
                    IssueCode.PATH_INVALID,
                    _ROLE_STAGES[spec.role],
                    "Snapshot file could not be read.",
                    entity_ref=spec.role,
                )
            if digest.hexdigest() != spec.sha256:
                _fail(
                    IssueCode.HASH_MISMATCH,
                    _ROLE_STAGES[spec.role],
                    "Snapshot file hash does not match its manifest.",
                    entity_ref=spec.role,
                )

    def _read_products(
        self,
        path: Path,
        spec: SnapshotFile,
        snapshot_version: str,
    ) -> _ProductRead:
        count = 0
        records: list[_LocatedProduct] = []
        issues: list[_QuarantinedRecord] = []
        providers: set[str] = set()
        categories: set[str] = set()
        try:
            with path.open("rb") as stream:
                for ordinal, raw_line in enumerate(stream):
                    count += 1
                    if count > MAX_JSONL_RECORDS:
                        _fail(
                            IssueCode.RECORD_LIMIT_EXCEEDED,
                            IssueStage.PRODUCTS,
                            "JSONL record count exceeds the limit.",
                            entity_ref="products",
                        )
                    record = _parse_record(raw_line)
                    _validate_record_version(
                        record,
                        snapshot_version,
                        IssueStage.PRODUCTS,
                        "products",
                    )
                    _add_inventory_text(providers, record.get("provider_id"))
                    _add_inventory_text(categories, record.get("category"))
                    issue_code = _missing_record_requirement(
                        record,
                        identity_fields=("product_id",),
                        source_fields=("provider_id", "source_uri"),
                    )
                    if issue_code is not None:
                        issues.append(
                            _record_issue(
                                issue_code,
                                IssueStage.PRODUCTS,
                                ordinal,
                            )
                        )
                        continue
                    try:
                        product = _parse_product(record, ordinal)
                    except (ValueError, TypeError, InvalidOperation):
                        issues.append(
                            _record_issue(
                                IssueCode.INVALID_RECORD,
                                IssueStage.PRODUCTS,
                                ordinal,
                            )
                        )
                        continue
                    records.append(_LocatedProduct(ordinal=ordinal, value=product))
        except _LoadFailure:
            raise
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            _fail(
                IssueCode.CORE_JSON_INVALID,
                IssueStage.PRODUCTS,
                "Core JSONL content is invalid.",
                entity_ref="products",
            )
        if count != spec.record_count:
            _fail(
                IssueCode.RECORD_COUNT_MISMATCH,
                IssueStage.PRODUCTS,
                "Snapshot record count does not match its manifest.",
                entity_ref="products",
                details=(
                    IssueDetail("declared_records", spec.record_count),
                    IssueDetail("actual_records", count),
                ),
            )
        return _ProductRead(
            records=tuple(records),
            issues=tuple(issues),
            providers=frozenset(providers),
            categories=frozenset(categories),
        )

    def _read_offers(
        self,
        path: Path,
        spec: SnapshotFile,
        snapshot_version: str,
    ) -> _OfferRead:
        count = 0
        records: list[_LocatedOffer] = []
        issues: list[_QuarantinedRecord] = []
        providers: set[str] = set()
        markets: set[str] = set()
        currencies: set[str] = set()
        try:
            with path.open("rb") as stream:
                for ordinal, raw_line in enumerate(stream):
                    count += 1
                    if count > MAX_JSONL_RECORDS:
                        _fail(
                            IssueCode.RECORD_LIMIT_EXCEEDED,
                            IssueStage.OFFERS,
                            "JSONL record count exceeds the limit.",
                            entity_ref="offers",
                        )
                    record = _parse_record(raw_line)
                    _validate_record_version(
                        record,
                        snapshot_version,
                        IssueStage.OFFERS,
                        "offers",
                    )
                    _add_inventory_text(providers, record.get("provider_id"))
                    _add_inventory_text(markets, record.get("market"))
                    raw_costs = record.get("cost_components")
                    if type(raw_costs) is dict:
                        _add_inventory_text(
                            currencies,
                            cast(dict[str, Any], raw_costs).get("currency"),
                        )
                    issue_code = _missing_record_requirement(
                        record,
                        identity_fields=("offer_id", "product_id"),
                        source_fields=("provider_id", "source_uri"),
                    )
                    if issue_code is not None:
                        issues.append(
                            _record_issue(
                                issue_code,
                                IssueStage.OFFERS,
                                ordinal,
                            )
                        )
                        continue
                    try:
                        offer = _parse_offer(record, ordinal)
                    except (ValueError, TypeError, InvalidOperation):
                        issues.append(
                            _record_issue(
                                IssueCode.INVALID_RECORD,
                                IssueStage.OFFERS,
                                ordinal,
                            )
                        )
                        continue
                    records.append(_LocatedOffer(ordinal=ordinal, value=offer))
        except _LoadFailure:
            raise
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            _fail(
                IssueCode.CORE_JSON_INVALID,
                IssueStage.OFFERS,
                "Core JSONL content is invalid.",
                entity_ref="offers",
            )
        if count != spec.record_count:
            _fail(
                IssueCode.RECORD_COUNT_MISMATCH,
                IssueStage.OFFERS,
                "Snapshot record count does not match its manifest.",
                entity_ref="offers",
                details=(
                    IssueDetail("declared_records", spec.record_count),
                    IssueDetail("actual_records", count),
                ),
            )
        return _OfferRead(
            records=tuple(records),
            issues=tuple(issues),
            providers=frozenset(providers),
            markets=frozenset(markets),
            currencies=frozenset(currencies),
        )

    def _read_exchange_rates(
        self,
        path: Path,
        manifest: SnapshotManifest,
    ) -> ExchangeRateTable:
        try:
            payload = _parse_json(path.read_bytes())
            root = _strict_object(payload, _FX_KEYS)
            if root["schema_version"] != EXCHANGE_RATES_SCHEMA_VERSION:
                raise _CoreJsonError
            if root["snapshot_version"] != manifest.snapshot_version:
                _fail(
                    IssueCode.SNAPSHOT_VERSION_MISMATCH,
                    IssueStage.EXCHANGE_RATES,
                    "Exchange-rate snapshot version does not match the manifest.",
                    entity_ref="exchange_rates",
                )
            base_currency = _require_currency(root["base_currency"])
            rates_payload = root["rates"]
            if type(rates_payload) is not list or not rates_payload:
                raise _CoreJsonError
            if len(rates_payload) != manifest.file("exchange_rates").record_count:
                _fail(
                    IssueCode.RECORD_COUNT_MISMATCH,
                    IssueStage.EXCHANGE_RATES,
                    "Exchange-rate count does not match its manifest.",
                    entity_ref="exchange_rates",
                )

            rates: list[ExchangeRate] = []
            for ordinal, raw_rate in enumerate(rates_payload):
                rate = _strict_object(raw_rate, _RATE_KEYS)
                currency = _require_currency(rate["currency"])
                decimal_text = _require_text(rate["base_per_unit"], maximum=128)
                if _FIXED_POSITIVE_DECIMAL.fullmatch(decimal_text) is None:
                    raise _CoreJsonError
                value = Decimal(decimal_text)
                minor_units = rate["minor_units"]
                if (
                    type(minor_units) is not int
                    or isinstance(minor_units, bool)
                    or not 0 <= minor_units <= 4
                ):
                    raise _CoreJsonError
                evidence_id = _require_text(rate["evidence_id"], maximum=128)
                rates.append(
                    ExchangeRate(
                        snapshot_version=manifest.snapshot_version,
                        currency=currency,
                        base_per_unit=value,
                        minor_units=minor_units,
                        evidence_id=evidence_id,
                        snapshot_ordinal=ordinal,
                    )
                )

            table = ExchangeRateTable(
                snapshot_version=manifest.snapshot_version,
                base_currency=base_currency,
                rates=tuple(rates),
            )
            if base_currency != manifest.base_currency:
                raise _CoreJsonError
            if table.supported_currencies != frozenset(manifest.currencies):
                raise _CoreJsonError
            return table
        except _LoadFailure:
            raise
        except (
            OSError,
            UnicodeError,
            ValueError,
            TypeError,
            InvalidOperation,
            json.JSONDecodeError,
        ):
            _fail(
                IssueCode.CORE_JSON_INVALID,
                IssueStage.EXCHANGE_RATES,
                "Exchange-rate JSON is invalid.",
                entity_ref="exchange_rates",
            )

    def _read_evidence(
        self,
        path: Path,
        spec: SnapshotFile,
        snapshot_version: str,
    ) -> _EvidenceRead:
        records: list[_LocatedEvidence] = []
        issues: list[_QuarantinedRecord] = []
        raw_ordinals_by_id: dict[str, list[int]] = {}
        count = 0
        try:
            with path.open("rb") as stream:
                for ordinal, raw_line in enumerate(stream):
                    count += 1
                    if count > MAX_JSONL_RECORDS:
                        _fail(
                            IssueCode.RECORD_LIMIT_EXCEEDED,
                            IssueStage.EVIDENCE,
                            "JSONL record count exceeds the limit.",
                            entity_ref="evidence",
                        )
                    record = _parse_record(raw_line)
                    _validate_record_version(
                        record,
                        snapshot_version,
                        IssueStage.EVIDENCE,
                        "evidence",
                    )
                    raw_evidence_id = record.get("evidence_id")
                    if _is_present_text(raw_evidence_id):
                        raw_ordinals_by_id.setdefault(
                            cast(str, raw_evidence_id),
                            [],
                        ).append(ordinal)
                    issue_code = _missing_record_requirement(
                        record,
                        identity_fields=("evidence_id",),
                        source_fields=("provider_id", "source_uri"),
                    )
                    if issue_code is not None:
                        issues.append(
                            _record_issue(
                                issue_code,
                                IssueStage.EVIDENCE,
                                ordinal,
                            )
                        )
                        continue
                    try:
                        evidence = _parse_evidence(record)
                    except (ValueError, TypeError):
                        issues.append(
                            _record_issue(
                                IssueCode.INVALID_RECORD,
                                IssueStage.EVIDENCE,
                                ordinal,
                            )
                        )
                        continue
                    records.append(_LocatedEvidence(ordinal=ordinal, value=evidence))
        except _LoadFailure:
            raise
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            _fail(
                IssueCode.CORE_JSON_INVALID,
                IssueStage.EVIDENCE,
                "Exchange-rate evidence JSON is invalid.",
                entity_ref="evidence",
            )
        if count != spec.record_count:
            _fail(
                IssueCode.RECORD_COUNT_MISMATCH,
                IssueStage.EVIDENCE,
                "Snapshot record count does not match its manifest.",
                entity_ref="evidence",
                details=(
                    IssueDetail("declared_records", spec.record_count),
                    IssueDetail("actual_records", count),
                ),
            )

        duplicate_ordinals = {
            ordinal
            for ordinals in raw_ordinals_by_id.values()
            if len(ordinals) > 1
            for ordinal in ordinals
        }
        issues = [
            quarantined for quarantined in issues if quarantined.ordinal not in duplicate_ordinals
        ]
        issues.extend(
            _record_issue(
                IssueCode.DUPLICATE_EVIDENCE,
                IssueStage.EVIDENCE,
                ordinal,
            )
            for ordinal in sorted(duplicate_ordinals)
        )
        unique_records = [
            located for located in records if located.ordinal not in duplicate_ordinals
        ]
        return _EvidenceRead(
            records=tuple(unique_records),
            issues=tuple(issues),
        )

    def _validate_rate_evidence(
        self,
        exchange_rates: ExchangeRateTable,
        evidence_by_id: dict[str, EvidenceRef],
    ) -> None:
        for rate in exchange_rates.rates:
            evidence = evidence_by_id.get(rate.evidence_id)
            if (
                evidence is None
                or evidence.entity_type is not EvidenceEntityType.EXCHANGE_RATE
                or evidence.currency != rate.currency
                or evidence.field_path != "exchange_rate.base_per_unit"
            ):
                _fail(
                    IssueCode.CORE_JSON_INVALID,
                    IssueStage.EVIDENCE,
                    "Exchange-rate evidence is missing or invalid.",
                    entity_ref="evidence",
                )

    def _validate_manifest_inventories(
        self,
        manifest: SnapshotManifest,
        products: _ProductRead,
        offers: _OfferRead,
        exchange_rates: ExchangeRateTable,
    ) -> None:
        observed_providers = products.providers | offers.providers
        inventories_match = (
            observed_providers == frozenset(manifest.providers)
            and offers.markets == frozenset(manifest.markets)
            and products.categories == frozenset(manifest.categories)
            and offers.currencies.issubset(frozenset(manifest.currencies))
            and exchange_rates.supported_currencies == frozenset(manifest.currencies)
        )
        if not inventories_match:
            _fail(
                IssueCode.MANIFEST_INVALID,
                IssueStage.MANIFEST,
                "Manifest inventories do not match snapshot records.",
            )

    def _validate_currency_compatibility(
        self,
        exchange_rates: ExchangeRateTable,
        *,
        display_currency: str | None,
        budget_currency: str | None,
    ) -> None:
        requested = (
            ("display_currency", display_currency),
            ("budget_currency", budget_currency),
        )
        for role, currency in requested:
            if currency is None:
                continue
            if type(currency) is not str or currency not in exchange_rates.supported_currencies:
                safe_currency = (
                    currency
                    if type(currency) is str and _CURRENCY.fullmatch(currency) is not None
                    else "invalid"
                )
                _fail(
                    IssueCode.CURRENCY_UNSUPPORTED,
                    IssueStage.EXCHANGE_RATES,
                    "Requested currency is not supported by the snapshot.",
                    entity_ref=role,
                    details=(
                        IssueDetail("currency_role", role),
                        IssueDetail("currency", safe_currency),
                    ),
                )


def _parse_manifest(payload: object, requested_version: str) -> SnapshotManifest:
    root = _strict_object(payload, _MANIFEST_KEYS)
    if root["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise _CoreJsonError
    snapshot_version = _require_text(root["snapshot_version"], maximum=128)
    if _SNAPSHOT_VERSION.fullmatch(snapshot_version) is None:
        raise _CoreJsonError
    if snapshot_version != requested_version:
        _fail(
            IssueCode.SNAPSHOT_VERSION_MISMATCH,
            IssueStage.MANIFEST,
            "Manifest snapshot version does not match the requested version.",
        )
    created_at = _parse_utc(root["created_at"])
    base_currency = _require_currency(root["base_currency"])
    providers = _string_inventory(root["providers"], "providers")
    markets = _string_inventory(root["markets"], "markets")
    categories = _string_inventory(root["categories"], "categories")
    currencies = _currency_inventory(root["currencies"])
    if base_currency not in currencies:
        raise _CoreJsonError

    generator = _strict_object(root["generator"], _GENERATOR_KEYS)
    generator_name = _require_text(generator["name"], maximum=128)
    generator_version = _require_text(generator["version"], maximum=128)

    raw_files = _strict_object(root["files"], frozenset(_FILE_ROLES))
    files: list[SnapshotFile] = []
    for role in _FILE_ROLES:
        raw_spec = _strict_object(raw_files[role], _FILE_SPEC_KEYS)
        relative_path = _require_text(raw_spec["path"], maximum=4_096)
        sha256 = _require_text(raw_spec["sha256"], maximum=64)
        if _SHA256.fullmatch(sha256) is None:
            raise _CoreJsonError
        record_count = raw_spec["record_count"]
        if type(record_count) is not int or isinstance(record_count, bool) or record_count < 0:
            raise _CoreJsonError
        files.append(
            SnapshotFile(
                role=role,
                relative_path=relative_path,
                sha256=sha256,
                record_count=record_count,
            )
        )

    return SnapshotManifest(
        snapshot_version=snapshot_version,
        created_at=created_at,
        base_currency=base_currency,
        files=tuple(files),
        providers=providers,
        markets=markets,
        categories=categories,
        currencies=currencies,
        generator_name=generator_name,
        generator_version=generator_version,
    )


def _parse_record(raw_line: bytes) -> dict[str, Any]:
    if not raw_line.strip():
        raise _CoreJsonError
    payload = _parse_json(raw_line)
    if type(payload) is not dict:
        raise _CoreJsonError
    return cast(dict[str, Any], payload)


def _parse_product(record: dict[str, Any], ordinal: int) -> Product:
    root = _strict_object(record, _PRODUCT_KEYS)
    raw_attributes = root["attributes"]
    if type(raw_attributes) is not list:
        raise _CoreJsonError
    attributes = tuple(
        ProductAttribute(
            name=_require_text(attribute["name"], maximum=128),
            value=_require_text(attribute["value"], maximum=16_384),
            evidence_id=_require_text(attribute["evidence_id"], maximum=128),
        )
        for raw_attribute in raw_attributes
        for attribute in (_strict_object(raw_attribute, _ATTRIBUTE_KEYS),)
    )
    return Product(
        snapshot_version=_require_text(root["snapshot_version"], maximum=128),
        product_id=_require_text(root["product_id"], maximum=128),
        provider_id=_require_text(root["provider_id"], maximum=128),
        source_uri=_require_text(root["source_uri"], maximum=4_096),
        title=_require_text(root["title"], maximum=16_384),
        category=_require_text(root["category"], maximum=128),
        entity_kind=EntityKind(_require_text(root["entity_kind"], maximum=128)),
        snapshot_ordinal=ordinal,
        attributes=attributes,
        field_evidence=_parse_field_evidence(root["field_evidence"]),
    )


def _parse_offer(record: dict[str, Any], ordinal: int) -> Offer:
    root = _strict_object(record, _OFFER_KEYS)
    costs, known_evidence, unknown_components = _parse_cost_components(root["cost_components"])
    bindings = _parse_field_evidence(root["field_evidence"])
    by_path = {binding.field_path: binding.evidence_id for binding in bindings}
    required_paths = {
        "offer.market",
        "offer.cost_components.currency",
        "offer.inventory",
    }
    if not required_paths.issubset(by_path):
        raise _CoreJsonError
    for component, evidence_id in known_evidence.items():
        if by_path.get(f"offer.cost_components.{component}") != evidence_id:
            raise _CoreJsonError
    if any(f"offer.cost_components.{component}" in by_path for component in unknown_components):
        raise _CoreJsonError

    return Offer(
        snapshot_version=_require_text(root["snapshot_version"], maximum=128),
        offer_id=_require_text(root["offer_id"], maximum=128),
        product_id=_require_text(root["product_id"], maximum=128),
        provider_id=_require_text(root["provider_id"], maximum=128),
        source_uri=_require_text(root["source_uri"], maximum=4_096),
        market=_require_text(root["market"], maximum=128),
        stock_status=StockStatus(_require_text(root["stock_status"], maximum=128)),
        cost_components=costs,
        captured_at=_parse_utc(root["captured_at"]),
        snapshot_ordinal=ordinal,
        field_evidence=bindings,
    )


def _parse_cost_components(
    value: object,
) -> tuple[CostComponents, dict[str, str], frozenset[str]]:
    root = _strict_object(value, _COST_COMPONENTS_KEYS)
    currency = _require_currency(root["currency"])
    costs: dict[str, CostValue] = {}
    known_evidence: dict[str, str] = {}
    unknown_components: set[str] = set()
    for name in _COST_NAMES:
        raw_component = root[name]
        if type(raw_component) is not dict:
            raise _CoreJsonError
        component = cast(dict[str, Any], raw_component)
        kind = component.get("kind")
        if kind == "KNOWN":
            known = _strict_object(component, _KNOWN_COST_KEYS)
            amount_text = _require_text(known["amount"], maximum=128)
            if amount_text != "0.00" and _CANONICAL_POSITIVE_AMOUNT.fullmatch(amount_text) is None:
                raise _CoreJsonError
            amount = Decimal(amount_text)
            if amount < 0 or (amount.is_zero() and amount_text != "0.00"):
                raise _CoreJsonError
            evidence_id = _require_text(known["evidence_id"], maximum=128)
            costs[name] = KnownCost(amount=amount, evidence_id=evidence_id)
            known_evidence[name] = evidence_id
        elif kind == "UNKNOWN":
            unknown = _strict_object(component, _UNKNOWN_COST_KEYS)
            reason = _require_text(unknown["reason"], maximum=128)
            costs[name] = UnknownCost(reason=reason)
            unknown_components.add(name)
        else:
            raise _CoreJsonError
    return (
        CostComponents(
            currency=currency,
            item_price=costs["item_price"],
            shipping=costs["shipping"],
            tax=costs["tax"],
            duty=costs["duty"],
        ),
        known_evidence,
        frozenset(unknown_components),
    )


def _parse_field_evidence(value: object) -> tuple[FieldEvidence, ...]:
    if type(value) is not list:
        raise _CoreJsonError
    return tuple(
        FieldEvidence(
            field_path=_require_text(binding["field_path"], maximum=512),
            evidence_id=_require_text(binding["evidence_id"], maximum=128),
        )
        for raw_binding in value
        for binding in (_strict_object(raw_binding, _FIELD_EVIDENCE_KEYS),)
    )


def _parse_evidence(record: dict[str, Any]) -> EvidenceRef:
    root = _strict_object(record, _EVIDENCE_KEYS)
    return EvidenceRef(
        evidence_id=_require_text(root["evidence_id"], maximum=128),
        snapshot_version=_require_text(root["snapshot_version"], maximum=128),
        entity_type=EvidenceEntityType(_require_text(root["entity_type"], maximum=128)),
        product_id=_optional_text(root["product_id"], maximum=128),
        offer_id=_optional_text(root["offer_id"], maximum=128),
        currency=_optional_currency(root["currency"]),
        field_path=_require_text(root["field_path"], maximum=512),
        provider_id=_require_text(root["provider_id"], maximum=128),
        source_uri=_require_text(root["source_uri"], maximum=4_096),
        captured_at=_parse_utc(root["captured_at"]),
    )


def _filter_products(
    records: tuple[_LocatedProduct, ...],
    evidence_by_id: dict[str, EvidenceRef],
) -> tuple[tuple[Product, ...], tuple[_QuarantinedRecord, ...]]:
    valid: list[Product] = []
    issues: list[_QuarantinedRecord] = []
    for record in records:
        product = record.value
        bindings = (
            *product.field_evidence,
            *(
                FieldEvidence(
                    field_path=f"product.attributes.{attribute.name}",
                    evidence_id=attribute.evidence_id,
                )
                for attribute in product.attributes
            ),
        )
        issue_code: IssueCode | None = None
        for binding in bindings:
            evidence = evidence_by_id.get(binding.evidence_id)
            if evidence is None:
                issue_code = IssueCode.EVIDENCE_NOT_FOUND
                break
            if (
                evidence.entity_type is not EvidenceEntityType.PRODUCT
                or evidence.product_id != product.product_id
                or evidence.provider_id != product.provider_id
                or evidence.source_uri != product.source_uri
                or evidence.field_path != binding.field_path
            ):
                issue_code = IssueCode.EVIDENCE_ENTITY_MISMATCH
                break
        if issue_code is None:
            valid.append(product)
        else:
            issues.append(_record_issue(issue_code, IssueStage.PRODUCTS, record.ordinal))
    return tuple(valid), tuple(issues)


def _filter_offers(
    records: tuple[_LocatedOffer, ...],
    product_ids: frozenset[str],
    evidence_by_id: dict[str, EvidenceRef],
) -> tuple[tuple[Offer, ...], tuple[_QuarantinedRecord, ...]]:
    valid: list[Offer] = []
    issues: list[_QuarantinedRecord] = []
    for record in records:
        offer = record.value
        if offer.product_id not in product_ids:
            issues.append(
                _record_issue(
                    IssueCode.ORPHAN_OFFER,
                    IssueStage.OFFERS,
                    record.ordinal,
                )
            )
            continue
        issue_code: IssueCode | None = None
        for binding in offer.field_evidence:
            evidence = evidence_by_id.get(binding.evidence_id)
            if evidence is None:
                issue_code = IssueCode.EVIDENCE_NOT_FOUND
                break
            if (
                evidence.entity_type is not EvidenceEntityType.OFFER
                or evidence.product_id != offer.product_id
                or evidence.offer_id != offer.offer_id
                or evidence.provider_id != offer.provider_id
                or evidence.source_uri != offer.source_uri
                or evidence.field_path != binding.field_path
            ):
                issue_code = IssueCode.EVIDENCE_ENTITY_MISMATCH
                break
        if issue_code is None:
            valid.append(offer)
        else:
            issues.append(_record_issue(issue_code, IssueStage.OFFERS, record.ordinal))
    return tuple(valid), tuple(issues)


def _missing_record_requirement(
    record: dict[str, Any],
    *,
    identity_fields: tuple[str, ...],
    source_fields: tuple[str, ...],
) -> IssueCode | None:
    if any(not _is_present_text(record.get(field)) for field in identity_fields):
        return IssueCode.MISSING_IDENTITY
    if any(not _is_present_text(record.get(field)) for field in source_fields):
        return IssueCode.MISSING_SOURCE
    return None


def _is_present_text(value: object) -> bool:
    return type(value) is str and bool(value.strip())


def _add_inventory_text(inventory: set[str], value: object) -> None:
    if _is_present_text(value):
        inventory.add(cast(str, value))


def _record_issue(
    code: IssueCode,
    stage: IssueStage,
    ordinal: int,
) -> _QuarantinedRecord:
    messages = {
        IssueCode.MISSING_IDENTITY: "Record is missing a stable identity.",
        IssueCode.MISSING_SOURCE: "Record is missing provider or source provenance.",
        IssueCode.INVALID_RECORD: "Record schema is invalid.",
        IssueCode.DUPLICATE_EVIDENCE: "Duplicate evidence record was quarantined.",
        IssueCode.EVIDENCE_NOT_FOUND: "Referenced evidence was not found.",
        IssueCode.EVIDENCE_ENTITY_MISMATCH: "Evidence ownership or field binding is invalid.",
        IssueCode.ORPHAN_OFFER: "Offer references no valid product.",
    }
    return _QuarantinedRecord(
        ordinal=ordinal,
        issue=CatalogIssue(
            code=code,
            stage=stage,
            disposition=IssueDisposition.QUARANTINE,
            message=messages[code],
            entity_ref=f"{stage.value}[{ordinal}]",
            details=(IssueDetail("record_ordinal", ordinal),),
        ),
    )


def _ordered_quarantine_issues(
    records: tuple[_QuarantinedRecord, ...],
) -> tuple[CatalogIssue, ...]:
    ordered = sorted(
        records,
        key=lambda record: (
            _ISSUE_STAGE_ORDER[record.issue.stage],
            record.ordinal,
        ),
    )
    return tuple(record.issue for record in ordered)


def _validate_record_version(
    record: dict[str, Any],
    expected: str,
    stage: IssueStage,
    entity_ref: str,
) -> None:
    version = record.get("snapshot_version")
    if version is not None and version != expected:
        _fail(
            IssueCode.SNAPSHOT_VERSION_MISMATCH,
            stage,
            "Record snapshot version does not match the manifest.",
            entity_ref=entity_ref,
        )


def _parse_json(raw: bytes) -> object:
    try:
        decoded = raw.decode("utf-8")
        value = json.loads(
            decoded,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
        _validate_json_values(value)
        return value
    except (
        UnicodeError,
        ValueError,
        TypeError,
        RecursionError,
        json.JSONDecodeError,
    ) as error:
        raise _CoreJsonError from error


def _unique_object(pairs: Iterable[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> object:
    raise _CoreJsonError


def _validate_json_values(value: object, *, key: str | None = None, depth: int = 0) -> None:
    if depth > 64:
        raise _CoreJsonError
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is float:
        return
    if type(value) is str:
        maximum = 16_384
        if key == "source_uri":
            maximum = 4_096
        elif key == "field_path":
            maximum = 512
        elif key in _SHORT_TEXT_FIELDS or (key is not None and key.endswith("_id")):
            maximum = 128
        if len(value) > maximum:
            raise _CoreJsonError
        return
    if type(value) is list:
        for item in cast(list[object], value):
            _validate_json_values(item, depth=depth + 1)
        return
    if type(value) is dict:
        for child_key, child in cast(dict[str, object], value).items():
            if type(child_key) is not str or len(child_key) > 128:
                raise _CoreJsonError
            _validate_json_values(child, key=child_key, depth=depth + 1)
        return
    raise _CoreJsonError


def _strict_object(value: object, keys: frozenset[str]) -> dict[str, Any]:
    if type(value) is not dict:
        raise _CoreJsonError
    result = cast(dict[str, Any], value)
    if frozenset(result) != keys:
        raise _CoreJsonError
    return result


def _string_inventory(value: object, _name: str) -> tuple[str, ...]:
    if type(value) is not list:
        raise _CoreJsonError
    items = tuple(_require_text(item, maximum=128) for item in value)
    if len(set(items)) != len(items):
        raise _CoreJsonError
    return items


def _currency_inventory(value: object) -> tuple[str, ...]:
    if type(value) is not list or not value:
        raise _CoreJsonError
    items = tuple(_require_currency(item) for item in value)
    if len(set(items)) != len(items):
        raise _CoreJsonError
    return items


def _require_text(value: object, *, maximum: int) -> str:
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise _CoreJsonError
    return value


def _require_currency(value: object) -> str:
    text = _require_text(value, maximum=3)
    if _CURRENCY.fullmatch(text) is None:
        raise _CoreJsonError
    return text


def _optional_text(value: object, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _require_text(value, maximum=maximum)


def _optional_currency(value: object) -> str | None:
    if value is None:
        return None
    return _require_currency(value)


def _parse_utc(value: object) -> datetime:
    text = _require_text(value, maximum=64)
    if not text.endswith("Z"):
        raise _CoreJsonError
    parsed = datetime.fromisoformat(f"{text[:-1]}+00:00")
    offset = parsed.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise _CoreJsonError
    return parsed


def _safe_outcome_version(value: object) -> str:
    if (
        type(value) is str
        and _SNAPSHOT_VERSION.fullmatch(value) is not None
        and value not in {".", ".."}
        and "\\" not in value
    ):
        return value
    return "invalid"


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _issue(
    code: IssueCode,
    stage: IssueStage,
    message: str,
    *,
    entity_ref: str | None = None,
    details: tuple[IssueDetail, ...] = (),
) -> CatalogIssue:
    return CatalogIssue(
        code=code,
        stage=stage,
        disposition=IssueDisposition.FATAL,
        message=message,
        entity_ref=entity_ref,
        details=details,
    )


def _fail(
    code: IssueCode,
    stage: IssueStage,
    message: str,
    *,
    entity_ref: str | None = None,
    details: tuple[IssueDetail, ...] = (),
) -> Never:
    raise _LoadFailure(
        _issue(
            code,
            stage,
            message,
            entity_ref=entity_ref,
            details=details,
        )
    )


__all__ = [
    "EXCHANGE_RATES_SCHEMA_VERSION",
    "MANIFEST_SCHEMA_VERSION",
    "MAX_FILE_BYTES",
    "MAX_JSONL_RECORDS",
    "LocalSnapshotCatalog",
    "SnapshotFile",
    "SnapshotManifest",
]
