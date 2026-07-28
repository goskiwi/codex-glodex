"""Contract tests for the fail-closed local snapshot manifest boundary."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from glodex.adapters.local_snapshot import (
    EXCHANGE_RATES_SCHEMA_VERSION,
    MANIFEST_SCHEMA_VERSION,
    MAX_FILE_BYTES,
    MAX_JSONL_RECORDS,
    LocalSnapshotCatalog,
)
from glodex.domain.catalog import CatalogBatch
from glodex.domain.issues import IssueCode

SNAPSHOT_VERSION = "m0-v1"
FILE_ROLES = ("products", "offers", "evidence", "exchange_rates")


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _rate_evidence(currency: str) -> dict[str, object]:
    return {
        "captured_at": "2026-01-01T00:00:00Z",
        "currency": currency,
        "entity_type": "EXCHANGE_RATE",
        "evidence_id": f"ev-rate-{currency.lower()}",
        "field_path": "exchange_rate.base_per_unit",
        "offer_id": None,
        "product_id": None,
        "provider_id": "fixture-fx",
        "snapshot_version": SNAPSHOT_VERSION,
        "source_uri": f"fixture://fx/{currency}",
    }


def _rate(
    currency: str,
    value: object,
    *,
    minor_units: object = 2,
) -> dict[str, object]:
    return {
        "base_per_unit": value,
        "currency": currency,
        "evidence_id": f"ev-rate-{currency.lower()}",
        "minor_units": minor_units,
    }


def _default_payloads() -> dict[str, bytes]:
    return {
        "products": b"",
        "offers": b"",
        "evidence": _json_bytes(_rate_evidence("USD")),
        "exchange_rates": _json_bytes(
            {
                "base_currency": "USD",
                "rates": [_rate("USD", "1")],
                "schema_version": EXCHANGE_RATES_SCHEMA_VERSION,
                "snapshot_version": SNAPSHOT_VERSION,
            }
        ),
    }


def _raw_count(role: str, payload: bytes) -> int:
    if role == "exchange_rates":
        value = json.loads(payload)
        assert isinstance(value, dict)
        rates = value.get("rates", [])
        assert isinstance(rates, list)
        return len(rates)
    return len(payload.splitlines())


def _manifest(
    payloads: dict[str, bytes],
    *,
    file_paths: dict[str, str] | None = None,
    currencies: list[str] | None = None,
) -> dict[str, object]:
    paths = file_paths or {
        "products": "products.jsonl",
        "offers": "offers.jsonl",
        "evidence": "evidence.jsonl",
        "exchange_rates": "exchange_rates.json",
    }
    files = {
        role: {
            "path": paths[role],
            "record_count": _raw_count(role, payloads[role]),
            "sha256": hashlib.sha256(payloads[role]).hexdigest(),
        }
        for role in FILE_ROLES
    }
    return {
        "base_currency": "USD",
        "categories": [],
        "created_at": "2026-01-01T00:00:00Z",
        "currencies": currencies or ["USD"],
        "files": files,
        "generator": {"name": "glodex-test-fixture", "version": "1"},
        "markets": [],
        "providers": [],
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "snapshot_version": SNAPSHOT_VERSION,
    }


def _write_snapshot(
    root: Path,
    *,
    payloads: dict[str, bytes] | None = None,
    manifest: dict[str, object] | None = None,
    file_paths: dict[str, str] | None = None,
) -> tuple[Path, dict[str, object], dict[str, bytes]]:
    snapshot = root / SNAPSHOT_VERSION
    snapshot.mkdir(parents=True)
    actual_payloads = _default_payloads() if payloads is None else payloads
    actual_manifest = (
        _manifest(actual_payloads, file_paths=file_paths) if manifest is None else manifest
    )
    files = actual_manifest.get("files")
    assert isinstance(files, dict)
    for role in FILE_ROLES:
        spec = files.get(role)
        if spec is None:
            continue
        assert isinstance(spec, dict)
        relative_path = spec["path"]
        assert isinstance(relative_path, str)
        if Path(relative_path).is_absolute() or ".." in Path(relative_path).parts:
            continue
        target = snapshot / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(actual_payloads[role])
    (snapshot / "manifest.json").write_bytes(_json_bytes(actual_manifest))
    return snapshot, actual_manifest, actual_payloads


def _load(
    root: Path,
    *,
    snapshot_version: str = SNAPSHOT_VERSION,
    display_currency: str = "USD",
    budget_currency: str | None = None,
) -> CatalogBatch:
    return asyncio.run(
        LocalSnapshotCatalog(root).load(
            snapshot_version,
            display_currency=display_currency,
            budget_currency=budget_currency,
        )
    )


def _only_code(batch: CatalogBatch) -> IssueCode:
    issues = batch.fatal_issues
    assert len(issues) == 1
    return issues[0].code


def _assert_safe_fatal(batch: CatalogBatch, root: Path, code: IssueCode) -> None:
    assert _only_code(batch) is code
    assert batch.products == ()
    assert batch.offers == ()
    assert batch.evidence == ()
    assert batch.exchange_rates is None
    rendered = repr(batch.fatal_issues)
    assert str(root.resolve()) not in rendered
    assert "Traceback" not in rendered


@pytest.mark.contract
@pytest.mark.spec("GLO-P0-003", "GLO-NFR-009")
def test_valid_manifest_loads_only_the_phase_b04_fx_slice(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"
    _write_snapshot(root)

    batch = _load(root)

    assert batch.snapshot_version == SNAPSHOT_VERSION
    assert batch.fatal_issues == ()
    assert batch.quarantine_issues == ()
    assert batch.products == ()
    assert batch.offers == ()
    assert batch.exchange_rates is not None
    assert batch.exchange_rates.base_currency == "USD"
    assert batch.exchange_rates.supported_currencies == frozenset({"USD"})
    assert tuple(item.evidence_id for item in batch.evidence) == ("ev-rate-usd",)


@pytest.mark.contract
@pytest.mark.spec("GLO-P0-003")
def test_missing_manifest_is_a_safe_fatal_outcome(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"
    (root / SNAPSHOT_VERSION).mkdir(parents=True)

    _assert_safe_fatal(_load(root), root, IssueCode.MANIFEST_MISSING)


@pytest.mark.contract
@pytest.mark.spec("GLO-P0-003")
@pytest.mark.parametrize(
    "mutate",
    [
        lambda manifest: manifest.update(schema_version="unsupported"),
        lambda manifest: manifest.update(unexpected=True),
        lambda manifest: manifest.pop("created_at"),
        lambda manifest: manifest.update(created_at="not-a-utc-instant"),
        lambda manifest: manifest.update(base_currency="usd"),
        lambda manifest: manifest.update(currencies=["USD", "USD"]),
        lambda manifest: manifest.update(generator={"name": "fixture"}),
        lambda manifest: manifest["files"].pop("offers"),
        lambda manifest: manifest["files"]["offers"].update(extra=True),
    ],
)
def test_manifest_schema_is_strict(
    tmp_path: Path,
    mutate: Callable[[dict[str, Any]], object],
) -> None:
    root = tmp_path / "snapshots"
    payloads = _default_payloads()
    manifest = _manifest(payloads)
    mutate(manifest)
    _write_snapshot(root, payloads=payloads, manifest=manifest)

    _assert_safe_fatal(_load(root), root, IssueCode.MANIFEST_INVALID)


@pytest.mark.contract
@pytest.mark.spec("GLO-P0-003")
def test_duplicate_manifest_key_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"
    snapshot, _, _ = _write_snapshot(root)
    manifest_path = snapshot / "manifest.json"
    raw = manifest_path.read_text()
    manifest_path.write_text(
        raw.replace(
            f'"schema_version":"{MANIFEST_SCHEMA_VERSION}"',
            f'"schema_version":"{MANIFEST_SCHEMA_VERSION}",'
            f'"schema_version":"{MANIFEST_SCHEMA_VERSION}"',
            1,
        )
    )

    _assert_safe_fatal(_load(root), root, IssueCode.MANIFEST_INVALID)


@pytest.mark.contract
@pytest.mark.spec("GLO-P0-003")
def test_requested_and_declared_snapshot_versions_must_match(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"
    payloads = _default_payloads()
    manifest = _manifest(payloads)
    manifest["snapshot_version"] = "m0-v2"
    _write_snapshot(root, payloads=payloads, manifest=manifest)

    _assert_safe_fatal(_load(root), root, IssueCode.SNAPSHOT_VERSION_MISMATCH)


@pytest.mark.contract
@pytest.mark.spec("GLO-P0-003")
def test_record_snapshot_version_mixing_is_fatal(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"
    payloads = _default_payloads()
    payloads["products"] = _json_bytes({"product_id": "p-1", "snapshot_version": "m0-v2"})
    _write_snapshot(root, payloads=payloads)

    _assert_safe_fatal(_load(root), root, IssueCode.SNAPSHOT_VERSION_MISMATCH)


@pytest.mark.contract
@pytest.mark.spec("GLO-P0-003")
def test_hash_and_raw_record_count_are_verified(tmp_path: Path) -> None:
    root = tmp_path / "hash"
    snapshot, _, _ = _write_snapshot(root)
    (snapshot / "products.jsonl").write_bytes(b"{}\n")
    _assert_safe_fatal(_load(root), root, IssueCode.HASH_MISMATCH)

    root = tmp_path / "count"
    payloads = _default_payloads()
    manifest = _manifest(payloads)
    files = manifest["files"]
    assert isinstance(files, dict)
    product_spec = files["products"]
    assert isinstance(product_spec, dict)
    product_spec["record_count"] = 1
    _write_snapshot(root, payloads=payloads, manifest=manifest)
    _assert_safe_fatal(_load(root), root, IssueCode.RECORD_COUNT_MISMATCH)


@pytest.mark.contract
@pytest.mark.spec("GLO-P0-003")
@pytest.mark.parametrize(
    ("role", "payload"),
    [
        ("products", b"{broken-json\n"),
        ("products", b'{"value":1,"value":2}\n'),
        (
            "exchange_rates",
            b'{"schema_version":"x","schema_version":"x"}\n',
        ),
    ],
)
def test_core_json_and_duplicate_keys_are_fatal(
    tmp_path: Path,
    role: str,
    payload: bytes,
) -> None:
    root = tmp_path / "snapshots"
    payloads = _default_payloads()
    payloads[role] = payload
    manifest = _manifest(payloads)
    if role == "exchange_rates":
        files = manifest["files"]
        assert isinstance(files, dict)
        spec = files["exchange_rates"]
        assert isinstance(spec, dict)
        spec["record_count"] = 0
    _write_snapshot(root, payloads=payloads, manifest=manifest)

    _assert_safe_fatal(_load(root), root, IssueCode.CORE_JSON_INVALID)


@pytest.mark.contract
@pytest.mark.spec("GLO-P0-003", "GLO-NFR-009")
@pytest.mark.parametrize("unsafe_path", ["/private/products.jsonl", "../products.jsonl", r"x\y"])
def test_absolute_parent_and_backslash_paths_are_rejected(
    tmp_path: Path,
    unsafe_path: str,
) -> None:
    root = tmp_path / "snapshots"
    payloads = _default_payloads()
    paths = {
        "products": unsafe_path,
        "offers": "offers.jsonl",
        "evidence": "evidence.jsonl",
        "exchange_rates": "exchange_rates.json",
    }
    manifest = _manifest(payloads, file_paths=paths)
    _write_snapshot(root, payloads=payloads, manifest=manifest)

    _assert_safe_fatal(_load(root), root, IssueCode.PATH_INVALID)


@pytest.mark.contract
@pytest.mark.spec("GLO-P0-003", "GLO-NFR-009")
def test_symlink_escape_is_rejected_without_leaking_target(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"
    snapshot, manifest, payloads = _write_snapshot(root)
    outside = tmp_path / "private-products.jsonl"
    outside.write_bytes(payloads["products"])
    product_path = snapshot / "products.jsonl"
    product_path.unlink()
    product_path.symlink_to(outside)
    (snapshot / "manifest.json").write_bytes(_json_bytes(manifest))

    _assert_safe_fatal(_load(root), root, IssueCode.PATH_INVALID)


@pytest.mark.contract
@pytest.mark.spec("GLO-P0-003", "GLO-NFR-009")
def test_unsafe_snapshot_version_cannot_escape_root(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"

    batch = _load(root, snapshot_version="../private")

    _assert_safe_fatal(batch, root, IssueCode.PATH_INVALID)
    assert batch.snapshot_version == "invalid"


@pytest.mark.contract
@pytest.mark.spec("GLO-P0-003", "GLO-NFR-009")
def test_file_size_limit_is_inclusive_and_fixed_at_128_mib(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import glodex.adapters.local_snapshot as adapter

    assert MAX_FILE_BYTES == 128 * 1024 * 1024
    test_limit = 4_096
    monkeypatch.setattr(adapter, "MAX_FILE_BYTES", test_limit)

    exact_root = tmp_path / "exact"
    exact_payloads = _default_payloads()
    exact_payloads["products"] = b"{}" + (b" " * (test_limit - 3)) + b"\n"
    _write_snapshot(exact_root, payloads=exact_payloads)
    assert _load(exact_root).fatal_issues == ()

    over_root = tmp_path / "over"
    over_payloads = _default_payloads()
    over_payloads["products"] = b"{}" + (b" " * (test_limit - 2)) + b"\n"
    _write_snapshot(over_root, payloads=over_payloads)
    _assert_safe_fatal(_load(over_root), over_root, IssueCode.FILE_TOO_LARGE)


@pytest.mark.contract
@pytest.mark.spec("GLO-P0-003")
def test_jsonl_record_limit_is_inclusive_and_fixed_at_500k(tmp_path: Path) -> None:
    assert MAX_JSONL_RECORDS == 500_000

    exact_root = tmp_path / "exact"
    payloads = _default_payloads()
    manifest = _manifest(payloads)
    files = manifest["files"]
    assert isinstance(files, dict)
    spec = files["products"]
    assert isinstance(spec, dict)
    spec["record_count"] = MAX_JSONL_RECORDS
    _write_snapshot(exact_root, payloads=payloads, manifest=manifest)
    _assert_safe_fatal(
        _load(exact_root),
        exact_root,
        IssueCode.RECORD_COUNT_MISMATCH,
    )

    over_root = tmp_path / "over"
    manifest = _manifest(payloads)
    files = manifest["files"]
    assert isinstance(files, dict)
    spec = files["products"]
    assert isinstance(spec, dict)
    spec["record_count"] = MAX_JSONL_RECORDS + 1
    _write_snapshot(over_root, payloads=payloads, manifest=manifest)
    _assert_safe_fatal(
        _load(over_root),
        over_root,
        IssueCode.RECORD_LIMIT_EXCEEDED,
    )


@pytest.mark.contract
@pytest.mark.spec("GLO-P0-003", "GLO-NFR-009")
@pytest.mark.parametrize(
    "record",
    [
        {"product_id": "x" * 129},
        {"source_uri": "x" * 4_097},
        {"title": "x" * 16_385},
    ],
)
def test_record_field_length_caps_are_enforced(
    tmp_path: Path,
    record: dict[str, str],
) -> None:
    root = tmp_path / "snapshots"
    payloads = _default_payloads()
    payloads["products"] = _json_bytes(record)
    _write_snapshot(root, payloads=payloads)

    _assert_safe_fatal(_load(root), root, IssueCode.CORE_JSON_INVALID)


@pytest.mark.contract
@pytest.mark.spec("GLO-P0-003")
@pytest.mark.parametrize(
    "mutate_fx",
    [
        lambda fx: fx.update(extra=True),
        lambda fx: fx.pop("schema_version"),
        lambda fx: fx.update(schema_version="unsupported"),
        lambda fx: fx.update(base_currency="EUR"),
        lambda fx: fx.update(rates=[]),
        lambda fx: fx.update(rates=[_rate("USD", "1.0")]),
        lambda fx: fx.update(rates=[_rate("USD", 1)]),
        lambda fx: fx.update(rates=[_rate("USD", "1", minor_units=True)]),
        lambda fx: fx.update(rates=[_rate("USD", "1", minor_units=5)]),
        lambda fx: fx.update(rates=[_rate("USD", "2")]),
        lambda fx: fx.update(rates=[_rate("USD", "1"), _rate("USD", "1")]),
    ],
)
def test_fx_schema_base_rate_and_minor_units_are_strict(
    tmp_path: Path,
    mutate_fx: Callable[[dict[str, Any]], object],
) -> None:
    root = tmp_path / "snapshots"
    payloads = _default_payloads()
    fx = json.loads(payloads["exchange_rates"])
    assert isinstance(fx, dict)
    mutate_fx(fx)
    payloads["exchange_rates"] = _json_bytes(fx)
    manifest = _manifest(payloads)
    files = manifest["files"]
    assert isinstance(files, dict)
    rate_spec = files["exchange_rates"]
    assert isinstance(rate_spec, dict)
    rates = fx.get("rates")
    rate_spec["record_count"] = len(rates) if isinstance(rates, list) else 0
    _write_snapshot(root, payloads=payloads, manifest=manifest)

    _assert_safe_fatal(_load(root), root, IssueCode.CORE_JSON_INVALID)


@pytest.mark.contract
@pytest.mark.spec("GLO-P0-003")
def test_manifest_currency_inventory_must_equal_fx_inventory(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"
    payloads = _default_payloads()
    manifest = _manifest(payloads, currencies=["USD", "EUR"])
    _write_snapshot(root, payloads=payloads, manifest=manifest)

    _assert_safe_fatal(_load(root), root, IssueCode.CORE_JSON_INVALID)


@pytest.mark.contract
@pytest.mark.spec("GLO-P0-003")
@pytest.mark.parametrize(
    ("display_currency", "budget_currency"),
    [("EUR", None), ("USD", "EUR")],
)
def test_display_and_explicit_budget_currency_must_be_supported(
    tmp_path: Path,
    display_currency: str,
    budget_currency: str | None,
) -> None:
    root = tmp_path / "snapshots"
    _write_snapshot(root)

    batch = _load(
        root,
        display_currency=display_currency,
        budget_currency=budget_currency,
    )

    _assert_safe_fatal(batch, root, IssueCode.CURRENCY_UNSUPPORTED)


@pytest.mark.contract
@pytest.mark.spec("GLO-P0-003")
def test_multi_currency_fx_load_is_deterministic(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"
    payloads = _default_payloads()
    payloads["evidence"] = b"".join(
        _json_bytes(_rate_evidence(currency)) for currency in ("USD", "EUR")
    )
    payloads["exchange_rates"] = _json_bytes(
        {
            "base_currency": "USD",
            "rates": [_rate("USD", "1"), _rate("EUR", "1.08")],
            "schema_version": EXCHANGE_RATES_SCHEMA_VERSION,
            "snapshot_version": SNAPSHOT_VERSION,
        }
    )
    manifest = _manifest(payloads, currencies=["USD", "EUR"])
    _write_snapshot(root, payloads=payloads, manifest=manifest)

    first = _load(root, display_currency="EUR", budget_currency="USD")
    second = _load(root, display_currency="EUR", budget_currency="USD")

    assert first == second
    assert first.exchange_rates is not None
    assert first.exchange_rates.supported_currencies == frozenset({"EUR", "USD"})
    assert tuple(rate.snapshot_ordinal for rate in first.exchange_rates.rates) == (0, 1)
