#!/usr/bin/env python3
"""Serve the current OpenSearch Hybrid alias through a loopback-only API.

This is intentionally a separate, default-off application boundary.  It does
not start OpenSearch or RetrievalModel, does not load the Faiss service, and does not
discover an index by prefix.  Before it binds a port it proves that the one
configured current catalog, binding, alias, physical index,
mapping metadata, count, pipeline, and RetrievalModel identity agree.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from glodex.retrieval.product_attributes import (  # noqa: E402
    project_product_attributes,
    validate_product_attributes,
)

APP_SCHEMA_VERSION = "glodex.current-product-hybrid-gateway.v12"
BINDING_SCHEMA_VERSION = "glodex.current-product-binding.v2"
INDEX_SCHEMA_VERSION = "glodex.current-product-hybrid-index.v6"
RETRIEVAL_MODEL_MANIFEST_SCHEMA_VERSION = "glodex.retrieval-model-private-manifest.v1"
RETRIEVAL_MODEL_SERVICE_SCHEMA_VERSION = "glodex.retrieval-model-service.v1"

DIMENSION = 1024
MAX_QUERY_CHARACTERS = 512
MAX_TOP_K = 50
RERANK_CANDIDATE_DEPTH = 50
KNN_CANDIDATE_DEPTH = 200
HTTP_RESPONSE_LIMIT_BYTES = 8 * 1024 * 1024
HTTP_REQUEST_LIMIT_BYTES = 2 * 1024 * 1024
INBOUND_REQUEST_LIMIT_BYTES = 64 * 1024
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
IDENTIFIER_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,255}\Z")
PLATFORMS = frozenset({"amazon", "shopee", "aliexpress", "ebay"})
SYNTHETIC_DATA_MODE = "SYNTHETIC_INTERVIEW"
SYNTHETIC_COMMERCE_RULESET_VERSION = "synthetic-multiplatform-cny-v2"
USER_PREFERENCE_WEIGHT = 0.20
_CURRENCY_ORDER = ("CNY", "USD", "EUR", "SGD")
_FX_TO_CNY = {
    "CNY": Decimal("1.00"),
    "USD": Decimal("7.20"),
    "EUR": Decimal("7.85"),
    "SGD": Decimal("5.30"),
}
_BUDGET_PREFILTER_TOLERANCE_CNY = 0.11
FORBIDDEN_LEGACY_FIELD_NAMES = frozenset(
    {
        "source_item_search_binding_sha256",
        "legacy_v8_used",
        "local_input_cache",
    }
)


class HybridGatewayError(RuntimeError):
    """Raised when the one current-only Hybrid contract cannot be proven."""


def _landed_cost_filter(
    *,
    minimum: float | None,
    maximum: float | None,
) -> dict[str, object] | None:
    """Build a server-owned pre-recall filter over stored offer facts."""

    if minimum is None and maximum is None:
        return None
    clauses: list[str] = []
    params: dict[str, float] = {
        "cny": float(_FX_TO_CNY["CNY"]),
        "usd": float(_FX_TO_CNY["USD"]),
        "eur": float(_FX_TO_CNY["EUR"]),
        "sgd": float(_FX_TO_CNY["SGD"]),
    }
    if minimum is not None:
        params["minimum"] = minimum - _BUDGET_PREFILTER_TOLERANCE_CNY
        clauses.append("landed >= params.minimum")
    if maximum is not None:
        params["maximum"] = maximum + _BUDGET_PREFILTER_TOLERANCE_CNY
        clauses.append("landed <= params.maximum")
    return {
        "script": {
            "script": {
                "lang": "painless",
                "source": (
                    "String currency = doc['currency'].value; "
                    "double rate = currency == 'CNY' ? params.cny : "
                    "currency == 'USD' ? params.usd : currency == 'EUR' ? params.eur : "
                    "currency == 'SGD' ? params.sgd : 0.0; "
                    "double landed = (doc['item_price'].value + doc['shipping'].value + "
                    "doc['tax'].value + doc['duty'].value) * rate; return rate > 0.0 && "
                    + " && ".join(clauses)
                    + ";"
                ),
                "params": params,
            }
        }
    }


class DuplicateJsonKey(ValueError):
    """Raised when an HTTP or on-disk JSON object repeats a key."""


def _object(value: object, field: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise HybridGatewayError(field + " must be an object")
    return value


def _list(value: object, field: str) -> list[Any]:
    if type(value) is not list:
        raise HybridGatewayError(field + " must be an array")
    return value


def _string(value: object, field: str, *, maximum: int = 4096) -> str:
    if type(value) is not str or not value or len(value) > maximum or "\0" in value:
        raise HybridGatewayError(field + " is invalid")
    return value


def _optional_string(value: object, field: str, *, maximum: int = 48_000) -> str | None:
    if value is None:
        return None
    return _string(value, field, maximum=maximum)


def _positive_integer(value: object, field: str) -> int:
    if type(value) is not int or isinstance(value, bool) or value < 1:
        raise HybridGatewayError(field + " must be a positive integer")
    return value


def _sha256(value: object, field: str) -> str:
    if type(value) is not str or SHA256_RE.fullmatch(value) is None:
        raise HybridGatewayError(field + " must be a SHA-256 digest")
    return value


def _identifier(value: object, field: str) -> str:
    result = _string(value, field, maximum=256)
    if IDENTIFIER_RE.fullmatch(result) is None:
        raise HybridGatewayError(field + " is not a safe OpenSearch identifier")
    if any(token in result for token in ("v7", "v8", "legacy", "interview", "canary")):
        raise HybridGatewayError(field + " is not a current-product identifier")
    return result


def _finite_number(value: object, field: str) -> float:
    if (
        type(value) not in (int, float)
        or isinstance(value, bool)
        or not math.isfinite(float(value))
    ):
        raise HybridGatewayError(field + " must be a finite number")
    return float(value)


def _request_vector(value: object, *, field: str, dimension: int) -> tuple[float, ...]:
    if type(value) is not list or len(value) != dimension:
        raise HybridGatewayError(field + " has an invalid dimension")
    vector = tuple(_finite_number(component, field) for component in value)
    norm = math.sqrt(sum(component * component for component in vector))
    if not math.isclose(norm, 1.0, rel_tol=0.0, abs_tol=1e-6):
        raise HybridGatewayError(field + " must be L2-normalized")
    return vector


def _fuse_query_preference_vectors(
    query_vector: Sequence[float],
    preference_vector: Sequence[float] | None,
) -> tuple[float, ...]:
    """Fuse the trusted query and user towers without changing no-profile retrieval."""

    query = tuple(float(component) for component in query_vector)
    if preference_vector is None:
        return query
    preference = tuple(float(component) for component in preference_vector)
    if len(preference) != len(query):
        raise HybridGatewayError("query and preference vector dimensions differ")
    mixed = tuple(
        ((1.0 - USER_PREFERENCE_WEIGHT) * query_component)
        + (USER_PREFERENCE_WEIGHT * preference_component)
        for query_component, preference_component in zip(query, preference, strict=True)
    )
    norm = math.sqrt(sum(component * component for component in mixed))
    if norm <= 0.0 or not math.isfinite(norm):
        raise HybridGatewayError("fused user-query vector is invalid")
    return tuple(component / norm for component in mixed)


def _merge_recall_channels(
    query_candidates: Sequence[dict[str, Any]],
    personalized_candidates: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Union two independently recalled channels with deterministic RRF."""

    rows: dict[str, dict[str, Any]] = {}
    scores: dict[str, float] = {}
    for candidates in (query_candidates, personalized_candidates):
        for rank, candidate in enumerate(candidates, start=1):
            document_id = _string(
                candidate.get("document_id"), "recall candidate document_id", maximum=1024
            )
            rows.setdefault(document_id, dict(candidate))
            scores[document_id] = scores.get(document_id, 0.0) + (1.0 / (60.0 + rank))
    ordered = sorted(rows, key=lambda document_id: (-scores[document_id], document_id))
    total_recall = len(ordered)
    merged: list[dict[str, Any]] = []
    for rank, document_id in enumerate(ordered[:RERANK_CANDIDATE_DEPTH], start=1):
        row = rows[document_id]
        row["rank"] = rank
        row["recall_fusion_score"] = round(scores[document_id], 8)
        merged.append(row)
    return merged, total_recall


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKey(key)
        result[key] = value
    return result


def _strict_json_file(path: Path, *, field: str) -> dict[str, Any]:
    resolved = path.resolve()
    if not resolved.is_file() or resolved.is_symlink():
        raise HybridGatewayError(field + " is unavailable")
    try:
        result = json.loads(
            resolved.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, DuplicateJsonKey) as error:
        raise HybridGatewayError(field + " is unreadable") from error
    return _object(result, field)


def _reject_forbidden_legacy_fields(value: object, *, field: str) -> None:
    """Reject retired v1/v8 wiring everywhere it could silently reappear."""

    if type(value) is dict:
        for name, child in value.items():
            if name in FORBIDDEN_LEGACY_FIELD_NAMES:
                raise HybridGatewayError(field + " contains retired field " + name)
            _reject_forbidden_legacy_fields(child, field=field)
    elif type(value) is list:
        for child in value:
            _reject_forbidden_legacy_fields(child, field=field)


def sha256_file(path: Path) -> str:
    resolved = path.resolve()
    if not resolved.is_file() or resolved.is_symlink():
        raise HybridGatewayError("required current artifact is unavailable: " + str(path))
    digest = hashlib.sha256()
    try:
        with resolved.open("rb") as handle:
            for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise HybridGatewayError("cannot hash required current artifact") from error
    return digest.hexdigest()


def _loopback_url(value: object, *, field: str, required_port: int | None = None) -> str:
    raw = _string(value, field)
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except ValueError as error:
        raise HybridGatewayError(field + " has an invalid port") from error
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or port is None
        or (required_port is not None and port != required_port)
    ):
        raise HybridGatewayError(field + " must be a loopback HTTP URL")
    # Do not leave a hostname alias in the final URL: it makes the outbound
    # boundary obvious in logs and avoids accidental proxy configuration.
    return "http://127.0.0.1:" + str(port)


@dataclass(frozen=True, slots=True)
class CurrentProductBinding:
    path: Path
    catalog_path: Path
    catalog_binding_sha256: str
    item_vectors_manifest_path: Path
    item_vectors_manifest_sha256: str
    item_vectors_expected_schema_version: str
    expected_document_count: int
    endpoint: str
    alias: str
    physical_index: str
    pipeline: str
    vector_field: str
    dimension: int
    semantic_weight: float
    lexical_weight: float


def load_binding(path: Path) -> CurrentProductBinding:
    """Load the one explicit current-product OpenSearch binding.

    The builder's configuration carries more ingestion-only data.  The
    gateway deliberately accepts only the serving fields it needs and rejects
    a binding that weakens the current-only policy.
    """

    source = _strict_json_file(path, field="current-product binding")
    _reject_forbidden_legacy_fields(source, field="current-product binding")
    if set(source) != {
        "schema_version",
        "purpose",
        "current_product_contract",
        "current_catalog",
        "current_item_vectors",
        "opensearch",
        "schema_policy",
    }:
        raise HybridGatewayError("current-product binding has an invalid v2 shape")
    if source.get("schema_version") != BINDING_SCHEMA_VERSION:
        raise HybridGatewayError("current-product binding has an unsupported schema")
    _string(source.get("purpose"), "purpose", maximum=4_000)
    _object(source.get("schema_policy"), "schema_policy")
    contract = _object(source.get("current_product_contract"), "current_product_contract")
    if set(contract) != {
        "canonical_join_key",
        "expected_document_count",
        "identity_policy",
    } or (
        contract.get("canonical_join_key") != "document_id"
        or contract.get("identity_policy") != "canonical_document_id_only"
    ):
        raise HybridGatewayError("current-product binding has an invalid identity policy")
    expected_count = _positive_integer(
        contract.get("expected_document_count"), "current_product_contract.expected_document_count"
    )

    current_catalog = _object(source.get("current_catalog"), "current_catalog")
    if set(current_catalog) != {"sqlite_path", "expected_document_count", "source_binding_sha256"}:
        raise HybridGatewayError("current_catalog has an invalid shape")
    catalog_path = Path(_string(current_catalog.get("sqlite_path"), "current_catalog.sqlite_path"))
    if not catalog_path.is_absolute():
        raise HybridGatewayError("current_catalog.sqlite_path must be absolute")
    catalog_expected_count = _positive_integer(
        current_catalog.get("expected_document_count"), "current_catalog.expected_document_count"
    )
    if catalog_expected_count != expected_count:
        raise HybridGatewayError(
            "current_catalog count does not match the current-product contract"
        )
    catalog_binding_sha256 = _sha256(
        current_catalog.get("source_binding_sha256"), "current_catalog.source_binding_sha256"
    )

    current_item_vectors = _object(source.get("current_item_vectors"), "current_item_vectors")
    if set(current_item_vectors) != {
        "manifest_path",
        "manifest_sha256",
        "expected_schema_version",
    }:
        raise HybridGatewayError("current_item_vectors has an invalid shape")
    item_vectors_manifest_path = Path(
        _string(current_item_vectors.get("manifest_path"), "current_item_vectors.manifest_path")
    )
    if not item_vectors_manifest_path.is_absolute():
        raise HybridGatewayError("current_item_vectors.manifest_path must be absolute")
    item_vectors_manifest_sha256 = _sha256(
        current_item_vectors.get("manifest_sha256"), "current_item_vectors.manifest_sha256"
    )
    item_vectors_expected_schema_version = _identifier(
        current_item_vectors.get("expected_schema_version"),
        "current_item_vectors.expected_schema_version",
    )

    opensearch = _object(source.get("opensearch"), "opensearch")
    if set(opensearch) != {
        "endpoint",
        "index_alias",
        "physical_index",
        "pipeline_id",
        "vector_field",
        "embedding_dimension",
        "primary_shards",
        "vector_engine",
        "hybrid_weights",
    }:
        raise HybridGatewayError("opensearch has an invalid v2 shape")
    endpoint = _loopback_url(
        opensearch.get("endpoint"), field="opensearch.endpoint", required_port=9200
    )
    alias = _identifier(opensearch.get("index_alias"), "opensearch.index_alias")
    physical_index = _identifier(opensearch.get("physical_index"), "opensearch.physical_index")
    pipeline = _identifier(opensearch.get("pipeline_id"), "opensearch.pipeline_id")
    vector_field = _identifier(opensearch.get("vector_field"), "opensearch.vector_field")
    if not physical_index.startswith(alias + "-"):
        raise HybridGatewayError("physical index is outside the current alias family")
    dimension = _positive_integer(
        opensearch.get("embedding_dimension"), "opensearch.embedding_dimension"
    )
    if dimension != DIMENSION:
        raise HybridGatewayError("only the current 1024-dimensional item vectors are accepted")
    if _positive_integer(opensearch.get("primary_shards"), "opensearch.primary_shards") != 1:
        raise HybridGatewayError("current Hybrid index must use one primary shard")
    if opensearch.get("vector_engine") != "lucene_hnsw_cosinesimil":
        raise HybridGatewayError("current Hybrid index must use Lucene HNSW cosine similarity")
    weights = _object(opensearch.get("hybrid_weights"), "opensearch.hybrid_weights")
    if set(weights) != {"semantic", "lexical"}:
        raise HybridGatewayError("current Hybrid weights have an invalid shape")
    semantic_weight = _finite_number(weights.get("semantic"), "opensearch.hybrid_weights.semantic")
    lexical_weight = _finite_number(weights.get("lexical"), "opensearch.hybrid_weights.lexical")
    if (
        semantic_weight <= 0
        or lexical_weight <= 0
        or not math.isclose(semantic_weight + lexical_weight, 1.0, abs_tol=1e-9)
    ):
        raise HybridGatewayError("current Hybrid weights must be positive and sum to one")
    return CurrentProductBinding(
        path=path.resolve(),
        catalog_path=catalog_path,
        catalog_binding_sha256=catalog_binding_sha256,
        item_vectors_manifest_path=item_vectors_manifest_path.resolve(),
        item_vectors_manifest_sha256=item_vectors_manifest_sha256,
        item_vectors_expected_schema_version=item_vectors_expected_schema_version,
        expected_document_count=expected_count,
        endpoint=endpoint,
        alias=alias,
        physical_index=physical_index,
        pipeline=pipeline,
        vector_field=vector_field,
        dimension=dimension,
        semantic_weight=semantic_weight,
        lexical_weight=lexical_weight,
    )


def validate_current_catalog(binding: CurrentProductBinding) -> None:
    """Prove the catalog SQLite metadata and row count before serving it."""

    path = binding.catalog_path
    if not path.is_file() or path.is_symlink():
        raise HybridGatewayError("current catalog SQLite is unavailable")
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        connection.execute("PRAGMA query_only = ON")
        metadata_rows = connection.execute(
            "SELECT key, value FROM metadata WHERE key IN (?, ?)",
            ("source_binding_sha256", "expected_document_count"),
        ).fetchall()
        metadata = {key: value for key, value in metadata_rows}
        document_count = connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    except (sqlite3.Error, OSError) as error:
        raise HybridGatewayError("current catalog SQLite cannot be verified read-only") from error
    finally:
        with suppress(UnboundLocalError):
            connection.close()
    if (
        metadata.get("source_binding_sha256") != binding.catalog_binding_sha256
        or metadata.get("expected_document_count") != str(binding.expected_document_count)
        or document_count != binding.expected_document_count
    ):
        raise HybridGatewayError(
            "current catalog SQLite metadata or document count does not match binding"
        )


@dataclass(frozen=True, slots=True)
class CurrentCatalogAttributeStore:
    """Restore source-backed attributes omitted from the OpenSearch ``_source``."""

    path: Path

    def project(self, candidates: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, str]]:
        if not candidates or len(candidates) > RERANK_CANDIDATE_DEPTH:
            raise HybridGatewayError("current catalog attribute request is invalid")
        document_ids = tuple(
            _string(candidate.get("document_id"), "attribute document_id", maximum=1024)
            for candidate in candidates
        )
        if len(set(document_ids)) != len(document_ids):
            raise HybridGatewayError("current catalog attribute request has duplicate IDs")
        placeholders = ",".join("?" for _document_id in document_ids)
        try:
            connection = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True)
            connection.execute("PRAGMA query_only = ON")
            rows = connection.execute(
                "SELECT document_id, payload_json FROM documents WHERE document_id IN ("
                + placeholders
                + ")",
                document_ids,
            ).fetchall()
        except (sqlite3.Error, OSError) as error:
            raise HybridGatewayError("current catalog attributes are unavailable") from error
        finally:
            with suppress(UnboundLocalError):
                connection.close()
        output: dict[str, dict[str, str]] = {}
        try:
            for document_id, raw_payload in rows:
                if type(document_id) is not str or type(raw_payload) is not str:
                    raise HybridGatewayError("current catalog attribute row is invalid")
                payload = json.loads(raw_payload, object_pairs_hook=_reject_duplicate_keys)
                source = _object(payload, "current catalog attribute payload")
                if source.get("document_id") != document_id:
                    raise HybridGatewayError("current catalog attribute identity is invalid")
                output[document_id] = project_product_attributes(source)
        except (json.JSONDecodeError, DuplicateJsonKey, TypeError, ValueError) as error:
            raise HybridGatewayError("current catalog attribute payload is invalid") from error
        if set(output) != set(document_ids):
            raise HybridGatewayError("current catalog attribute join is incomplete")
        return output


@dataclass(frozen=True, slots=True)
class CurrentItemVectorsIdentity:
    """The small signed descriptor of the current item-vector generation."""

    manifest_path: Path
    manifest_sha256: str
    schema_version: str


def load_current_item_vectors_identity(
    binding: CurrentProductBinding,
) -> CurrentItemVectorsIdentity:
    """Validate vector lineage from its compact manifest, never by mmap-ing vectors.

    OpenSearch has already ingested the vectors.  The gateway only needs the
    manifest's signed identity to prove that this immutable index, the current
    catalog, and the encoding contract belong to the same generation.
    """

    path = binding.item_vectors_manifest_path
    if sha256_file(path) != binding.item_vectors_manifest_sha256:
        raise HybridGatewayError("current item-vector manifest digest does not match binding")
    source = _strict_json_file(path, field="current item-vector manifest")
    _reject_forbidden_legacy_fields(source, field="current item-vector manifest")
    if source.get("schema_version") != binding.item_vectors_expected_schema_version:
        raise HybridGatewayError("current item-vector manifest has an unsupported schema")
    if source.get("legacy_qrels_used") is not False:
        raise HybridGatewayError("current item-vector manifest does not reject legacy qrels")
    for name, value in source.items():
        if name.startswith("legacy_") and value is not False:
            raise HybridGatewayError(
                "current item-vector manifest has an invalid legacy declaration"
            )

    catalog = _object(source.get("catalog"), "current item-vector manifest catalog")
    if (
        catalog.get("path") != str(binding.catalog_path)
        or catalog.get("source_binding_sha256") != binding.catalog_binding_sha256
        or catalog.get("document_count") != binding.expected_document_count
        or catalog.get("document_id_key") != "current canonical document_id"
    ):
        raise HybridGatewayError("current item-vector manifest is not bound to the current catalog")
    model = _object(source.get("model"), "current item-vector manifest model")
    if (
        model.get("source") != "current_catalog_finetuned"
        or model.get("vector_dimension") != DIMENSION
    ):
        raise HybridGatewayError("current item-vector manifest has an invalid current model")
    _sha256(model.get("weights_sha256"), "current item-vector model.weights_sha256")
    item_text = _object(source.get("item_text"), "current item-vector manifest item_text")
    _string(item_text.get("template_version"), "current item-vector item_text.template_version")

    outputs = _object(source.get("outputs"), "current item-vector manifest outputs")
    vectors = _object(outputs.get("item_vectors"), "current item-vector output descriptor")
    ids = _object(outputs.get("item_ids"), "current item-vector ID descriptor")
    if set(vectors) != {"file", "sha256", "bytes", "dtype", "shape", "normalized"}:
        raise HybridGatewayError("current item-vector output descriptor has an invalid shape")
    if set(ids) != {"file", "sha256", "bytes", "row_count"}:
        raise HybridGatewayError("current item-vector ID descriptor has an invalid shape")
    shape = _list(vectors.get("shape"), "current item-vector output shape")
    if (
        len(shape) != 2
        or type(shape[0]) is not int
        or type(shape[1]) is not int
        or shape != [binding.expected_document_count, DIMENSION]
        or vectors.get("dtype") != "float16"
        or vectors.get("normalized") is not True
        or ids.get("row_count") != binding.expected_document_count
    ):
        raise HybridGatewayError("current item-vector manifest output identity is invalid")
    for field, descriptor in (
        ("current item-vector output", vectors),
        ("current item-vector ID output", ids),
    ):
        _string(descriptor.get("file"), field + ".file", maximum=1_024)
        _sha256(descriptor.get("sha256"), field + ".sha256")
        _positive_integer(descriptor.get("bytes"), field + ".bytes")
    return CurrentItemVectorsIdentity(
        manifest_path=path.resolve(),
        manifest_sha256=binding.item_vectors_manifest_sha256,
        schema_version=binding.item_vectors_expected_schema_version,
    )


@dataclass(frozen=True, slots=True)
class RetrievalModelIdentity:
    manifest_path: Path
    manifest_digest: str


def load_retrieval_model_identity(path: Path) -> RetrievalModelIdentity:
    source = _strict_json_file(path, field="final RetrievalModel manifest")
    required = {
        "embeddingModel",
        "embeddingModelPath",
        "embeddingWeightsSha256",
        "maxSequenceLength",
        "rerankerModel",
        "rerankerModelPath",
        "rerankerWeightsSha256",
        "schemaVersion",
        "serviceBuildDigest",
    }
    if (
        set(source) != required
        or source.get("schemaVersion") != RETRIEVAL_MODEL_MANIFEST_SCHEMA_VERSION
    ):
        raise HybridGatewayError("final RetrievalModel manifest has an invalid shape")
    if (
        source.get("embeddingModel") != "glodex-bge-m3-v1"
        or source.get("rerankerModel") != "glodex-bge-reranker-v1"
        or source.get("maxSequenceLength") != 256
    ):
        raise HybridGatewayError(
            "final RetrievalModel manifest has an invalid current model identity"
        )
    for name in ("embeddingModelPath", "rerankerModelPath"):
        _string(source.get(name), "final RetrievalModel " + name)
    embedding_weights = _sha256(
        source.get("embeddingWeightsSha256"), "final RetrievalModel embeddingWeightsSha256"
    )
    reranker_weights = _sha256(
        source.get("rerankerWeightsSha256"), "final RetrievalModel rerankerWeightsSha256"
    )
    service_digest = _sha256(
        source.get("serviceBuildDigest"), "final RetrievalModel serviceBuildDigest"
    )
    public_material = {
        "embeddingModel": source["embeddingModel"],
        "embeddingWeightsSha256": embedding_weights,
        "maxSequenceLength": source["maxSequenceLength"],
        "rerankerModel": source["rerankerModel"],
        "rerankerWeightsSha256": reranker_weights,
        "schemaVersion": source["schemaVersion"],
        "serviceBuildDigest": service_digest,
    }
    return RetrievalModelIdentity(
        manifest_path=path.resolve(),
        manifest_digest=hashlib.sha256(
            _canonical_json(public_material).encode("utf-8")
        ).hexdigest(),
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:  # type: ignore[override]
        return None


_HTTP_OPENER = urllib.request.build_opener(_NoRedirect)


def http_json(
    *,
    base_url: str,
    method: str,
    path: str,
    payload: dict[str, object] | None = None,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Make one bounded JSON request to an already-normalized loopback URL."""

    if not path.startswith("/") or "\r" in path or "\n" in path:
        raise HybridGatewayError("internal HTTP path is invalid")
    if not 0.1 <= timeout_seconds <= 120:
        raise HybridGatewayError("internal HTTP timeout is invalid")
    data: bytes | None = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = _canonical_json(payload).encode("utf-8")
        if len(data) > HTTP_REQUEST_LIMIT_BYTES:
            raise HybridGatewayError("internal HTTP request exceeds its bound")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base_url + path, data=data, headers=headers, method=method)
    try:
        with _HTTP_OPENER.open(request, timeout=timeout_seconds) as response:
            raw_status = getattr(response, "status", None)
            status = int(response.getcode() if raw_status is None else raw_status)
            if status != 200:
                raise HybridGatewayError("loopback dependency returned a non-success response")
            raw = response.read(HTTP_RESPONSE_LIMIT_BYTES + 1)
    except HybridGatewayError:
        raise
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as error:
        raise HybridGatewayError("required loopback dependency is unavailable") from error
    if len(raw) > HTTP_RESPONSE_LIMIT_BYTES:
        raise HybridGatewayError("loopback dependency response exceeds its bound")
    try:
        decoded = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, DuplicateJsonKey) as error:
        raise HybridGatewayError("loopback dependency returned invalid JSON") from error
    return _object(decoded, "loopback dependency response")


HttpRequest = Callable[..., dict[str, Any]]


class RetrievalModelClient:
    def __init__(
        self, endpoint: str, identity: RetrievalModelIdentity, *, request: HttpRequest = http_json
    ) -> None:
        self._endpoint = _loopback_url(endpoint, field="RetrievalModel endpoint")
        self._identity = identity
        self._request = request

    @property
    def manifest_digest(self) -> str:
        return self._identity.manifest_digest

    def verify_health(self) -> None:
        response = self._request(base_url=self._endpoint, method="GET", path="/v1/health")
        required = {
            "deviceClass",
            "dimension",
            "embeddingModel",
            "gpuModelClass",
            "manifestDigest",
            "maxEmbeddingTexts",
            "maxQueryCharacters",
            "maxRerankDocuments",
            "maxTextCharacters",
            "rerankerModel",
            "schemaVersion",
        }
        if (
            set(response) != required
            or response.get("manifestDigest") != self._identity.manifest_digest
            or response.get("dimension") != DIMENSION
            or response.get("embeddingModel") != "glodex-bge-m3-v1"
            or response.get("rerankerModel") != "glodex-bge-reranker-v1"
            or response.get("maxEmbeddingTexts") != 8
            or response.get("maxQueryCharacters") != MAX_QUERY_CHARACTERS
            or response.get("maxRerankDocuments") != RERANK_CANDIDATE_DEPTH
            or response.get("maxTextCharacters") != 2000
            or response.get("deviceClass") != "cuda"
            or response.get("gpuModelClass") != "a100"
            or response.get("schemaVersion") != RETRIEVAL_MODEL_SERVICE_SCHEMA_VERSION
        ):
            raise HybridGatewayError(
                "RetrievalModel health identity does not match the final current manifest"
            )

    def embed_query(self, query: str, *, language: str) -> list[float]:
        response = self._request(
            base_url=self._endpoint,
            method="POST",
            path="/v1/embed",
            payload={"texts": ["[query_language=" + language + "] " + query]},
            timeout_seconds=120.0,
        )
        values = response.get("embeddings")
        if (
            set(response) != {"embeddings", "manifestDigest"}
            or response.get("manifestDigest") != self._identity.manifest_digest
        ):
            raise HybridGatewayError("RetrievalModel embedding response has an invalid identity")
        rows = _list(values, "RetrievalModel embeddings")
        if len(rows) != 1:
            raise HybridGatewayError("RetrievalModel embedding response has an invalid batch size")
        row = _list(rows[0], "RetrievalModel embedding vector")
        if len(row) != DIMENSION:
            raise HybridGatewayError("RetrievalModel embedding response has an invalid dimension")
        vector = [_finite_number(value, "RetrievalModel embedding value") for value in row]
        norm = math.sqrt(math.fsum(value * value for value in vector))
        if not math.isclose(norm, 1.0, abs_tol=1e-4):
            raise HybridGatewayError("RetrievalModel embedding response is not normalized")
        return [value / norm for value in vector]

    def rerank(self, query: str, rows: Sequence[Mapping[str, Any]]) -> list[float]:
        """Score a bounded Hybrid candidate set with the verified cross-encoder."""

        if not rows or len(rows) > RERANK_CANDIDATE_DEPTH:
            raise HybridGatewayError("rerank candidate set is invalid")
        documents: list[str] = []
        for row in rows:
            title = _string(row.get("title"), "rerank title", maximum=48_000)
            attributes = validate_product_attributes(row.get("attributes"))
            specification = "\n".join(f"{name}: {value}" for name, value in attributes.items())
            documents.append((title + ("\n" + specification if specification else ""))[:2000])
        response = self._request(
            base_url=self._endpoint,
            method="POST",
            path="/v1/rerank",
            payload={"query": query, "documents": documents},
            timeout_seconds=120.0,
        )
        raw_scores = response.get("scores")
        if (
            set(response) != {"manifestDigest", "scores"}
            or response.get("manifestDigest") != self._identity.manifest_digest
        ):
            raise HybridGatewayError("RetrievalModel rerank response has an invalid identity")
        scores = _list(raw_scores, "RetrievalModel rerank scores")
        if len(scores) != len(rows):
            raise HybridGatewayError("RetrievalModel rerank response has an invalid size")
        return [_finite_number(score, "RetrievalModel rerank score") for score in scores]


class OpenSearchClient:
    def __init__(self, binding: CurrentProductBinding, *, request: HttpRequest = http_json) -> None:
        self._binding = binding
        self._request = request

    def _get(self, path: str) -> dict[str, Any]:
        return self._request(base_url=self._binding.endpoint, method="GET", path=path)

    def _post(self, path: str, payload: dict[str, object]) -> dict[str, Any]:
        return self._request(
            base_url=self._binding.endpoint, method="POST", path=path, payload=payload
        )

    def verify_startup(self) -> None:
        health = self._get("/_cluster/health?wait_for_status=yellow&timeout=30s")
        if health.get("status") not in {"yellow", "green"}:
            raise HybridGatewayError("OpenSearch is not ready for current-product search")
        self._verify_alias()
        self._verify_mapping()
        self._verify_count()
        self._verify_pipeline()

    def _verify_alias(self) -> None:
        alias = urllib.parse.quote(self._binding.alias, safe="")
        response = self._get("/_alias/" + alias)
        if set(response) != {self._binding.physical_index}:
            raise HybridGatewayError(
                "current alias does not resolve to exactly its configured physical index"
            )
        index_node = _object(response.get(self._binding.physical_index), "current alias response")
        aliases = _object(index_node.get("aliases"), "current alias metadata")
        if self._binding.alias not in aliases:
            raise HybridGatewayError("configured current alias is missing from the physical index")

    def _verify_mapping(self) -> None:
        index = urllib.parse.quote(self._binding.physical_index, safe="")
        response = self._get("/" + index + "/_mapping")
        if set(response) != {self._binding.physical_index}:
            raise HybridGatewayError("physical-index mapping response is ambiguous")
        index_node = _object(response.get(self._binding.physical_index), "physical-index mapping")
        mappings = _object(index_node.get("mappings"), "physical-index mappings")
        meta = _object(mappings.get("_meta"), "physical-index mapping metadata")
        expected_meta = {
            "schema_version": INDEX_SCHEMA_VERSION,
            "current_catalog_binding_sha256": self._binding.catalog_binding_sha256,
            "current_item_vectors_manifest_sha256": self._binding.item_vectors_manifest_sha256,
            "canonical_join_key": "document_id",
            "expected_document_count": self._binding.expected_document_count,
            "identity_policy": "canonical_document_id_only",
        }
        if meta != expected_meta:
            raise HybridGatewayError("physical index is not bound to the current product identity")
        properties = _object(mappings.get("properties"), "physical-index mapping properties")
        document_id = _object(properties.get("document_id"), "document_id mapping")
        search_text = _object(properties.get("search_text"), "search_text mapping")
        vector = _object(properties.get(self._binding.vector_field), "item-vector mapping")
        category_card = _object(properties.get("category_card_id"), "category-card mapping")
        item_price = _object(properties.get("item_price"), "item-price mapping")
        shipping = _object(properties.get("shipping"), "shipping mapping")
        stock_status = _object(properties.get("stock_status"), "stock-status mapping")
        attributes = _object(properties.get("attributes"), "attributes mapping")
        if (
            document_id.get("type") != "keyword"
            or search_text.get("type") != "text"
            or vector.get("type") != "knn_vector"
            or vector.get("dimension") != self._binding.dimension
            or category_card.get("type") != "keyword"
            or item_price.get("type") != "scaled_float"
            or shipping.get("type") != "scaled_float"
            or stock_status.get("type") != "keyword"
            or attributes != {"type": "object", "enabled": False}
        ):
            raise HybridGatewayError("physical index has an incompatible current Hybrid mapping")

    def _verify_count(self) -> None:
        alias = urllib.parse.quote(self._binding.alias, safe="")
        response = self._get("/" + alias + "/_count")
        if response.get("count") != self._binding.expected_document_count:
            raise HybridGatewayError(
                "current alias count does not match the current product binding"
            )
        index = urllib.parse.quote(self._binding.physical_index, safe="")
        physical_response = self._get("/" + index + "/_count")
        if physical_response.get("count") != self._binding.expected_document_count:
            raise HybridGatewayError(
                "current physical-index count does not match the current product binding"
            )
        settings = self._get("/" + index + "/_settings")
        setting_node = _object(
            settings.get(self._binding.physical_index), "physical-index settings"
        )
        index_settings = _object(setting_node.get("settings"), "physical-index settings node")
        index_values = _object(index_settings.get("index"), "physical-index index settings")
        blocks = _object(index_values.get("blocks"), "physical-index write block")
        if str(blocks.get("write")).lower() != "true":
            raise HybridGatewayError("current physical index is not write-blocked")

    def _verify_pipeline(self) -> None:
        pipeline = urllib.parse.quote(self._binding.pipeline, safe="")
        response = self._get("/_search/pipeline/" + pipeline)
        if set(response) != {self._binding.pipeline}:
            raise HybridGatewayError("current Hybrid pipeline response is ambiguous")
        pipeline_node = _object(response.get(self._binding.pipeline), "current Hybrid pipeline")
        processors = _list(
            pipeline_node.get("phase_results_processors"), "current Hybrid processors"
        )
        if len(processors) != 1:
            raise HybridGatewayError("current Hybrid pipeline has an unexpected processor list")
        processor = _object(processors[0], "current Hybrid processor")
        normalization = _object(
            processor.get("normalization-processor"), "current Hybrid normalization"
        )
        if (
            _object(normalization.get("normalization"), "current Hybrid normalization mode").get(
                "technique"
            )
            != "min_max"
        ):
            raise HybridGatewayError("current Hybrid pipeline does not use min-max normalization")
        combination = _object(normalization.get("combination"), "current Hybrid combination")
        parameters = _object(combination.get("parameters"), "current Hybrid combination parameters")
        weights = _list(parameters.get("weights"), "current Hybrid weights")
        if (
            combination.get("technique") != "arithmetic_mean"
            or len(weights) != 2
            or not math.isclose(
                _finite_number(weights[0], "semantic Hybrid weight"),
                self._binding.semantic_weight,
                abs_tol=1e-9,
            )
            or not math.isclose(
                _finite_number(weights[1], "lexical Hybrid weight"),
                self._binding.lexical_weight,
                abs_tol=1e-9,
            )
        ):
            raise HybridGatewayError("current Hybrid pipeline weights do not match the binding")

    def hybrid_search(
        self,
        *,
        query: str,
        vector: Sequence[float],
        top_k: int,
        platform: str,
        min_landed_cost_cny: float | None = None,
        max_landed_cost_cny: float | None = None,
    ) -> list[dict[str, Any]]:
        if (
            len(vector) != self._binding.dimension
            or top_k < 1
            or top_k > MAX_TOP_K
            or platform not in PLATFORMS
        ):
            raise HybridGatewayError("current Hybrid query is invalid")
        candidate_count = max(top_k, KNN_CANDIDATE_DEPTH)
        source_fields = [
            "document_id",
            "family_id",
            "source",
            "title",
            "category",
            "brand",
            "color",
            "attributes",
            "attribute_names",
            "search_text",
            "has_category",
            "source_locale_raw",
            "source_locale_semantics",
            "item_language",
            "target_market_locale",
            "category_card_id",
            "entity_kind",
            "platform",
            "provider_id",
            "market",
            "offer_id",
            "source_uri",
            "currency",
            "item_price",
            "shipping",
            "tax",
            "duty",
            "stock_status",
            "delivery_days_min",
            "delivery_days_max",
            "commerce_ruleset_version",
            "captured_at",
        ]
        lexical_query: dict[str, object] = {"match": {"search_text": {"query": query}}}
        knn_spec: dict[str, object] = {"vector": list(vector), "k": candidate_count}
        budget_filter = _landed_cost_filter(
            minimum=min_landed_cost_cny,
            maximum=max_landed_cost_cny,
        )
        filters: list[dict[str, object]] = [{"term": {"platform": platform}}]
        if budget_filter is not None:
            filters.append(budget_filter)
        knn_query: dict[str, object] = {"knn": {self._binding.vector_field: knn_spec}}
        knn_query = {"bool": {"must": [knn_query], "filter": filters}}
        lexical_query = {"bool": {"must": [lexical_query], "filter": filters}}
        body: dict[str, object] = {
            "size": top_k,
            "track_total_hits": False,
            "_source": source_fields,
            "query": {
                "hybrid": {
                    "queries": [
                        knn_query,
                        lexical_query,
                    ]
                }
            },
        }
        alias = urllib.parse.quote(self._binding.alias, safe="")
        pipeline = urllib.parse.quote(self._binding.pipeline, safe="")
        response = self._post("/" + alias + "/_search?search_pipeline=" + pipeline, body)
        hits = _object(response.get("hits"), "current Hybrid hits")
        rows = _list(hits.get("hits"), "current Hybrid hit rows")
        if len(rows) > top_k:
            raise HybridGatewayError("current Hybrid returned too many results")
        formatted = [_format_hit(row, expected_index=self._binding.physical_index) for row in rows]
        if len({row["document_id"] for row in formatted}) != len(formatted):
            raise HybridGatewayError("current Hybrid returned duplicate document IDs")
        for rank, row in enumerate(formatted, start=1):
            row["rank"] = rank
        return formatted


def _format_hit(value: object, *, expected_index: str) -> dict[str, Any]:
    hit = _object(value, "current Hybrid hit")
    source = _object(hit.get("_source"), "current Hybrid hit source")
    document_id = _string(source.get("document_id"), "current Hybrid document_id", maximum=1024)
    hit_id = _string(hit.get("_id"), "current Hybrid hit _id", maximum=1024)
    index = _string(hit.get("_index"), "current Hybrid hit _index", maximum=256)
    if hit_id != document_id or index != expected_index:
        raise HybridGatewayError("current Hybrid hit violates the document_id identity")
    score = _finite_number(hit.get("_score"), "current Hybrid hit score")
    try:
        stored_attributes = validate_product_attributes(source.get("attributes"))
        if (
            isinstance(source.get("search_text"), str)
            and type(source.get("attribute_names")) is list
        ):
            projected_attributes = project_product_attributes(source)
            attributes = dict(projected_attributes)
            for name, raw in stored_attributes.items():
                attributes.setdefault(name, raw)
            attributes = validate_product_attributes(attributes)
        else:
            attributes = stored_attributes
    except ValueError as error:
        raise HybridGatewayError("current Hybrid attributes are invalid") from error
    has_category = source.get("has_category")
    if type(has_category) is not bool:
        raise HybridGatewayError("current Hybrid has_category is invalid")
    result: dict[str, Any] = {
        "document_id": document_id,
        "family_id": _string(source.get("family_id"), "current Hybrid family_id", maximum=1024),
        "source": _string(source.get("source"), "current Hybrid source", maximum=256),
        "title": _string(source.get("title"), "current Hybrid title", maximum=48_000),
        "product_category": _optional_string(
            source.get("category"), "current Hybrid category", maximum=4_000
        )
        or "Uncategorized",
        "brand": _optional_string(source.get("brand"), "current Hybrid brand", maximum=1_000),
        "color": _optional_string(source.get("color"), "current Hybrid color", maximum=1_000),
        "attributes": attributes,
        "has_category": has_category,
        "source_locale_raw": _optional_string(
            source.get("source_locale_raw"), "current Hybrid source_locale_raw", maximum=256
        ),
        "source_locale_semantics": _optional_string(
            source.get("source_locale_semantics"),
            "current Hybrid source_locale_semantics",
            maximum=256,
        ),
        "item_language": _string(
            source.get("item_language"), "current Hybrid item_language", maximum=64
        ),
        "target_market_locale": _optional_string(
            source.get("target_market_locale"), "current Hybrid target_market_locale", maximum=64
        ),
        "category_card_id": _string(
            source.get("category_card_id"), "current Hybrid category_card_id", maximum=128
        ),
        "entity_kind": _string(source.get("entity_kind"), "current Hybrid entity_kind", maximum=32),
        "platform": _string(source.get("platform"), "current Hybrid platform", maximum=32),
        "provider_id": _string(
            source.get("provider_id"), "current Hybrid provider_id", maximum=128
        ),
        "market": _string(source.get("market"), "current Hybrid market", maximum=32),
        "offer_id": _string(source.get("offer_id"), "current Hybrid offer_id", maximum=128),
        "source_uri": _string(source.get("source_uri"), "current Hybrid source_uri", maximum=4096),
        "currency": _string(source.get("currency"), "current Hybrid currency", maximum=3),
        "item_price": _non_negative_number(source.get("item_price"), "current Hybrid item_price"),
        "shipping": _non_negative_number(source.get("shipping"), "current Hybrid shipping"),
        "tax": _non_negative_number(source.get("tax"), "current Hybrid tax"),
        "duty": _non_negative_number(source.get("duty"), "current Hybrid duty"),
        "stock_status": _stock_status(source.get("stock_status")),
        "delivery_days_min": _non_negative_integer(
            source.get("delivery_days_min"), "current Hybrid delivery_days_min"
        ),
        "delivery_days_max": _non_negative_integer(
            source.get("delivery_days_max"), "current Hybrid delivery_days_max"
        ),
        # The stored field identifies the category-cleaning pass used while
        # materialising the index.  This gateway's public contract identifies
        # the commerce projection it serves; do not leak the ingest ruleset as
        # though it were the offer-pricing ruleset.
        "commerce_ruleset_version": SYNTHETIC_COMMERCE_RULESET_VERSION,
        "captured_at": _string(source.get("captured_at"), "current Hybrid captured_at", maximum=64),
        "hybrid_score": round(score, 8),
    }
    _string(
        source.get("commerce_ruleset_version"),
        "current Hybrid source commerce_ruleset_version",
        maximum=128,
    )
    group_digest = hashlib.sha256(result["family_id"].encode()).hexdigest()[:24]
    result["same_product_group_id"] = "sg-v1-" + group_digest
    result["product_provider_id"] = "current-product-catalog"
    if result["delivery_days_min"] > result["delivery_days_max"]:
        raise HybridGatewayError("current Hybrid delivery bounds are reversed")
    return result


def _offer_matches_landed_cost(
    offer: dict[str, Any],
    *,
    minimum: float | None,
    maximum: float | None,
) -> bool:
    currency = _string(offer.get("currency"), "stored offer currency", maximum=3)
    try:
        rate = _FX_TO_CNY[currency]
    except KeyError as error:
        raise HybridGatewayError("stored offer currency is invalid") from error
    landed = sum(
        (
            Decimal(str(_non_negative_number(offer.get(field), "stored offer " + field))) * rate
            for field in ("item_price", "shipping", "tax", "duty")
        ),
        Decimal(0),
    )
    if minimum is not None and landed < Decimal(str(minimum)):
        return False
    return maximum is None or landed <= Decimal(str(maximum))


def _synthetic_exchange_rates() -> list[dict[str, object]]:
    return [
        {
            "currency": currency,
            "base_per_unit": float(_FX_TO_CNY[currency]),
            "minor_units": 2,
            "evidence_id": "ev-synthetic-fx-" + currency.lower(),
            "source_uri": "urn:glodex:synthetic-interview:fx:" + currency,
            "captured_at": "2026-08-01T00:00:00Z",
        }
        for currency in _CURRENCY_ORDER
    ]


def _non_negative_number(value: object, field: str) -> float:
    result = _finite_number(value, field)
    if result < 0:
        raise HybridGatewayError(field + " must be non-negative")
    return result


def _non_negative_integer(value: object, field: str) -> int:
    if type(value) is not int or isinstance(value, bool) or value < 0:
        raise HybridGatewayError(field + " must be a non-negative integer")
    return value


def _stock_status(value: object) -> str:
    result = _string(value, "current Hybrid stock_status", maximum=32)
    if result not in {"IN_STOCK", "OUT_OF_STOCK"}:
        raise HybridGatewayError("current Hybrid stock_status is invalid")
    return result


def _rank_fusion_score(
    *,
    hybrid_rank: int,
    reranker_rank: int,
    candidate_count: int,
) -> float:
    """Fuse independent ranks while keeping full-corpus Hybrid recall authoritative.

    The cross-encoder adds a small ordering signal, but must not erase strong
    multilingual Hybrid matches when the reranker sees an untranslated title.
    """

    if (
        type(hybrid_rank) is not int
        or type(reranker_rank) is not int
        or type(candidate_count) is not int
        or not 1 <= hybrid_rank <= candidate_count
        or not 1 <= reranker_rank <= candidate_count
    ):
        raise HybridGatewayError("candidate ranks are invalid")
    denominator = max(candidate_count - 1, 1)
    hybrid_signal = 1.0 - ((hybrid_rank - 1) / denominator)
    reranker_signal = 1.0 - ((reranker_rank - 1) / denominator)
    return (0.90 * hybrid_signal) + (0.10 * reranker_signal)


def _candidate_fusion_score(
    *,
    candidate: Mapping[str, Any],
    hybrid_rank: int,
    reranker_rank: int,
    candidate_count: int,
) -> float:
    """Prefer complete products softly without excluding the full corpus."""

    entity_kind = candidate.get("entity_kind")
    if type(entity_kind) is not str:
        raise HybridGatewayError("candidate entity kind is invalid")
    primary_product_bonus = 0.15 if entity_kind == "PRIMARY_PRODUCT" else 0.0
    return (
        _rank_fusion_score(
            hybrid_rank=hybrid_rank,
            reranker_rank=reranker_rank,
            candidate_count=candidate_count,
        )
        + primary_product_bonus
    )


def detect_query_language(query: str) -> str:
    """Use the current embedding query-language contract without a Faiss import."""

    if any("\u3040" <= char <= "\u30ff" or "\uff66" <= char <= "\uff9f" for char in query):
        return "ja"
    lowered = query.casefold()
    if any(marker in lowered for marker in ("á", "é", "í", "ó", "ú", "ü", "ñ", "¿", "¡")):
        return "es"
    words = set(re.findall(r"[a-záéíóúüñ]+", lowered))
    if words & {
        "de",
        "del",
        "para",
        "con",
        "sin",
        "mujer",
        "hombre",
        "niño",
        "niña",
        "coche",
        "zapatos",
        "vestido",
        "camiseta",
    }:
        return "es"
    if re.search(r"[a-z]", lowered):
        return "en"
    return "und"


@dataclass(slots=True)
class GatewayRuntime:
    binding: CurrentProductBinding
    item_vectors: CurrentItemVectorsIdentity
    retrieval_model: RetrievalModelClient
    opensearch: OpenSearchClient
    catalog_attributes: CurrentCatalogAttributeStore

    @classmethod
    def load(
        cls,
        *,
        binding_path: Path,
        retrieval_model_manifest_path: Path,
        retrieval_model_url: str,
    ) -> GatewayRuntime:
        binding = load_binding(binding_path)
        validate_current_catalog(binding)
        item_vectors = load_current_item_vectors_identity(binding)
        retrieval_model = RetrievalModelClient(
            retrieval_model_url, load_retrieval_model_identity(retrieval_model_manifest_path)
        )
        opensearch = OpenSearchClient(binding)
        # Validate both independently before uvicorn is allowed to bind.  The
        # endpoint itself never performs an identity downgrade or fallback.
        retrieval_model.verify_health()
        opensearch.verify_startup()
        probe_vector = retrieval_model.embed_query("current product binding probe", language="en")
        if not opensearch.hybrid_search(
            query="current product binding probe",
            vector=probe_vector,
            top_k=1,
            platform="amazon",
        ):
            raise HybridGatewayError("current Hybrid startup probe returned no current product")
        return cls(
            binding=binding,
            item_vectors=item_vectors,
            retrieval_model=retrieval_model,
            opensearch=opensearch,
            catalog_attributes=CurrentCatalogAttributeStore(binding.catalog_path),
        )

    async def search(
        self,
        *,
        query: str,
        top_k: int,
        platform: str,
        min_landed_cost_cny: float | None,
        max_landed_cost_cny: float | None,
        query_vector: Sequence[float],
        preference_vector: Sequence[float] | None,
    ) -> dict[str, object]:
        language = detect_query_language(query)
        recall_arguments = {
            "query": query,
            "top_k": RERANK_CANDIDATE_DEPTH,
            "platform": platform,
            "min_landed_cost_cny": min_landed_cost_cny,
            "max_landed_cost_cny": max_landed_cost_cny,
        }
        query_recall = asyncio.to_thread(
            self.opensearch.hybrid_search,
            vector=query_vector,
            **recall_arguments,
        )
        if preference_vector is None:
            query_candidates = await query_recall
            personalized_candidates: list[dict[str, Any]] = []
        else:
            personalized_vector = _fuse_query_preference_vectors(
                query_vector, preference_vector
            )
            query_candidates, personalized_candidates = await asyncio.gather(
                query_recall,
                asyncio.to_thread(
                    self.opensearch.hybrid_search,
                    vector=personalized_vector,
                    **recall_arguments,
                ),
            )
        candidates, total_recall = _merge_recall_channels(
            query_candidates, personalized_candidates
        )
        if any(candidate.get("platform") != platform for candidate in candidates):
            raise HybridGatewayError("current Hybrid platform filter was violated")
        if candidates:
            projected_attributes = await asyncio.to_thread(
                self.catalog_attributes.project, candidates
            )
            enriched_candidates: list[dict[str, Any]] = []
            for candidate in candidates:
                document_id = _string(
                    candidate.get("document_id"), "candidate document_id", maximum=1024
                )
                attributes = dict(projected_attributes[document_id])
                stored_attributes = validate_product_attributes(candidate.get("attributes"))
                for name, raw in stored_attributes.items():
                    attributes.setdefault(name, raw)
                enriched = dict(candidate)
                enriched["attributes"] = validate_product_attributes(attributes)
                enriched_candidates.append(enriched)
            candidates = enriched_candidates
        candidates = [
            candidate
            for candidate in candidates
            if _offer_matches_landed_cost(
                candidate,
                minimum=min_landed_cost_cny,
                maximum=max_landed_cost_cny,
            )
        ]
        results: list[dict[str, Any]] = []
        if candidates:
            scores = await asyncio.to_thread(self.retrieval_model.rerank, query, candidates)
            reranker_order = sorted(
                range(len(candidates)),
                key=lambda index: (-scores[index], index),
            )
            reranker_ranks = {
                candidate_index: rank
                for rank, candidate_index in enumerate(reranker_order, start=1)
            }
            fused = sorted(
                range(len(candidates)),
                key=lambda index: (
                    -_candidate_fusion_score(
                        candidate=candidates[index],
                        hybrid_rank=index + 1,
                        reranker_rank=reranker_ranks[index],
                        candidate_count=len(candidates),
                    ),
                    index,
                ),
            )[:top_k]
            for rank, candidate_index in enumerate(fused, start=1):
                candidate = candidates[candidate_index]
                score = scores[candidate_index]
                row = dict(candidate)
                row["hybrid_rank"] = row["rank"]
                row["reranker_rank"] = reranker_ranks[candidate_index]
                row["reranker_score"] = round(score, 8)
                row["fusion_score"] = round(
                    _candidate_fusion_score(
                        candidate=candidate,
                        hybrid_rank=candidate_index + 1,
                        reranker_rank=reranker_ranks[candidate_index],
                        candidate_count=len(candidates),
                    ),
                    8,
                )
                row["rank"] = rank
                results.append(row)
        return {
            "schema_version": APP_SCHEMA_VERSION,
            "data_mode": SYNTHETIC_DATA_MODE,
            "commerce_ruleset_version": SYNTHETIC_COMMERCE_RULESET_VERSION,
            "query": query,
            "detected_language": language,
            "catalog_binding_sha256": self.binding.catalog_binding_sha256,
            "item_vectors_manifest_sha256": self.item_vectors.manifest_sha256,
            "retrieval_model_manifest_digest": self.retrieval_model.manifest_digest,
            "candidate_engine": "opensearch_hybrid_rerank_fusion",
            "index_alias": self.binding.alias,
            "search_pipeline": self.binding.pipeline,
            "candidate_pool_count": len(candidates),
            "total_recall": total_recall,
            "truncated": total_recall > len(results),
            "exchange_rates": _synthetic_exchange_rates(),
            "results": results,
        }


def create_app(runtime: GatewayRuntime) -> Any:
    try:
        from fastapi import Body, FastAPI, HTTPException
        from fastapi.responses import JSONResponse
    except ImportError as error:  # pragma: no cover - operator dependency.
        raise HybridGatewayError("FastAPI is required to serve the Hybrid gateway") from error

    app = FastAPI(
        title="Glodex Current Product Hybrid Gateway",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def request_size_limit(request: Any, call_next: Any) -> Any:
        if request.method == "POST" and len(await request.body()) > INBOUND_REQUEST_LIMIT_BYTES:
            return JSONResponse(status_code=413, content={"detail": "REQUEST_TOO_LARGE"})
        return await call_next(request)

    @app.get("/v1/health")
    async def health() -> dict[str, object]:
        return {
            "schema_version": APP_SCHEMA_VERSION,
            "status": "ready",
            "data_mode": SYNTHETIC_DATA_MODE,
            "commerce_ruleset_version": SYNTHETIC_COMMERCE_RULESET_VERSION,
            "catalog_binding_sha256": runtime.binding.catalog_binding_sha256,
            "item_vectors_manifest_sha256": runtime.item_vectors.manifest_sha256,
            "retrieval_model_manifest_digest": runtime.retrieval_model.manifest_digest,
            "candidate_engine": "opensearch_hybrid_rerank_fusion",
            "index_alias": runtime.binding.alias,
            "search_pipeline": runtime.binding.pipeline,
        }

    @app.post("/v1/hybrid-search")
    async def hybrid_search(request: dict = Body(...)) -> dict[str, object]:  # noqa: B008
        # Parsing explicitly keeps the endpoint independent of local Pydantic
        # behaviour. Raw profile/history/owner IDs and arbitrary DSL remain unavailable.
        if type(request) is not dict or set(request) != {
            "query",
            "top_k",
            "platform",
            "min_landed_cost_cny",
            "max_landed_cost_cny",
            "query_vector",
            "preference_vector",
        }:
            raise HTTPException(status_code=422, detail="INVALID_HYBRID_SEARCH_REQUEST")
        query = request["query"]
        top_k = request.get("top_k", 10)
        platform = request.get("platform")
        raw_minimum = request.get("min_landed_cost_cny")
        raw_maximum = request.get("max_landed_cost_cny")
        if (
            type(query) is not str
            or "\0" in query
            or not query.strip()
            or len(query) > MAX_QUERY_CHARACTERS
            or type(top_k) is not int
            or not 1 <= top_k <= MAX_TOP_K
            or type(platform) is not str
            or platform not in PLATFORMS
        ):
            raise HTTPException(status_code=422, detail="INVALID_HYBRID_SEARCH_REQUEST")
        try:
            minimum = (
                None if raw_minimum is None else _finite_number(raw_minimum, "min_landed_cost_cny")
            )
            maximum = (
                None if raw_maximum is None else _finite_number(raw_maximum, "max_landed_cost_cny")
            )
            if (
                (minimum is not None and minimum <= 0)
                or (maximum is not None and maximum <= 0)
                or (minimum is not None and maximum is not None and minimum > maximum)
            ):
                raise HybridGatewayError("landed-cost bounds are invalid")
        except HybridGatewayError as error:
            raise HTTPException(status_code=422, detail="INVALID_HYBRID_SEARCH_REQUEST") from error
        query = " ".join(query.split())
        try:
            query_vector = _request_vector(
                request["query_vector"],
                field="query_vector",
                dimension=runtime.binding.dimension,
            )
            raw_preference = request["preference_vector"]
            preference_vector = (
                None
                if raw_preference is None
                else _request_vector(
                    raw_preference,
                    field="preference_vector",
                    dimension=runtime.binding.dimension,
                )
            )
        except HybridGatewayError as error:
            raise HTTPException(status_code=422, detail="INVALID_HYBRID_SEARCH_REQUEST") from error
        try:
            return await runtime.search(
                query=query,
                top_k=top_k,
                platform=platform,
                min_landed_cost_cny=minimum,
                max_landed_cost_cny=maximum,
                query_vector=query_vector,
                preference_vector=preference_vector,
            )
        except HybridGatewayError as error:
            raise HTTPException(
                status_code=503, detail="CURRENT_PRODUCT_HYBRID_UNAVAILABLE"
            ) from error

    return app


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--binding",
        type=Path,
        default=Path(__file__).with_name("current_product_binding.json"),
        help="the one current-product OpenSearch binding",
    )
    parser.add_argument("--retrieval-model-manifest", type=Path, required=True)
    parser.add_argument("--retrieval-model-url", default="http://127.0.0.1:18000")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser(
        "validate", help="validate current loopback dependencies without binding a port"
    )
    _add_common_arguments(validate)
    serve = commands.add_parser("serve", help="run the foreground loopback Hybrid gateway")
    _add_common_arguments(serve)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, required=True, help="explicit temporary loopback port")
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    if args.command == "serve" and (args.host != "127.0.0.1" or not 1 <= args.port <= 65535):
        raise HybridGatewayError("Hybrid gateway must use 127.0.0.1 and a valid explicit port")
    runtime = GatewayRuntime.load(
        binding_path=args.binding,
        retrieval_model_manifest_path=args.retrieval_model_manifest,
        retrieval_model_url=args.retrieval_model_url,
    )
    if args.command == "validate":
        print(
            "CURRENT_PRODUCT_HYBRID_VALID "
            + "items="
            + str(runtime.binding.expected_document_count)
            + " binding="
            + runtime.binding.catalog_binding_sha256[:16],
            flush=True,
        )
        return 0
    if args.command == "serve":
        try:
            import uvicorn
        except ImportError as error:  # pragma: no cover - operator dependency.
            raise HybridGatewayError("uvicorn is required to serve the Hybrid gateway") from error
        uvicorn.run(create_app(runtime), host=args.host, port=args.port, access_log=False)
        return 0
    raise HybridGatewayError("unknown command")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run(parse_args(argv))
    except HybridGatewayError as error:
        print("CURRENT_PRODUCT_HYBRID_FAILED: " + str(error), file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
