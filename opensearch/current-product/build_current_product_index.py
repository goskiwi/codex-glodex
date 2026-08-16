#!/usr/bin/env python3
"""Build and verify the current-product OpenSearch Hybrid index.

This operator utility deliberately has no dependency on the project runtime.
It reads only the read-only current catalog and its formal Item-vector
manifest, performs every product join by canonical ``document_id``, and
publishes an alias only after the complete index is validated.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from glodex.interview_catalog.category_classifier import CategoryClassifier  # noqa: E402
from glodex.interview_catalog.category_policy import (  # noqa: E402
    COMMERCE_GENERATION_SEED,
    CategoryPolicySet,
    load_category_cards,
)
from glodex.interview_catalog.commerce_generator import generate_commerce  # noqa: E402
from glodex.retrieval.product_attributes import project_product_attributes  # noqa: E402

try:  # Keep ``--action plan`` and syntax validation useful off the server.
    import numpy as np
except ImportError:  # pragma: no cover - deployment dependency.
    np = None  # type: ignore[assignment]


class BuildError(RuntimeError):
    """An operator-visible error that never changes an existing alias."""


def _require_numpy() -> Any:
    if np is None:
        raise BuildError("numpy is required for current Item-vector validation or indexing")
    return np


@dataclass(frozen=True)
class Binding:
    config_path: Path
    current_catalog_path: Path
    expected_count: int
    source_binding_sha256: str
    vector_manifest_path: Path
    vector_manifest_sha256: str
    vector_manifest_schema_version: str
    endpoint: str
    alias: str
    physical_index: str
    pipeline: str
    dimension: int
    vector_field: str
    semantic_weight: float
    lexical_weight: float


@dataclass(frozen=True)
class VectorAssets:
    manifest_path: Path
    manifest_sha256: str
    vectors_path: Path
    vectors_sha256: str
    ids_path: Path
    ids_sha256: str


def _object(value: object, name: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise BuildError(name + " must be an object")
    return value


def _list(value: object, name: str) -> list[Any]:
    if type(value) is not list:
        raise BuildError(name + " must be a list")
    return value


def _string(value: object, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise BuildError(name + " must be a non-empty string")
    return value.strip()


def _integer(value: object, name: str) -> int:
    if type(value) is not int or isinstance(value, bool) or value < 1:
        raise BuildError(name + " must be a positive integer")
    return value


def _sha256_string(value: object, name: str) -> str:
    result = _string(value, name)
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise BuildError(name + " must be a SHA-256 digest")
    return result


def _exact_keys(value: dict[str, Any], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise BuildError(name + " has an invalid shape")


def _regular_file(path: Path, name: str) -> Path:
    if not path.is_file() or path.is_symlink():
        raise BuildError(name + " is unavailable: " + str(path))
    return path.resolve()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _json_file(path: Path) -> dict[str, Any]:
    try:
        return _object(
            json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys),
            str(path),
        )
    except (OSError, json.JSONDecodeError, DuplicateJsonKey) as error:
        raise BuildError("cannot read " + str(path)) from error


def load_binding(path: Path) -> Binding:
    root = _json_file(path)
    _exact_keys(
        root,
        {
            "schema_version",
            "purpose",
            "current_product_contract",
            "current_catalog",
            "current_item_vectors",
            "opensearch",
            "schema_policy",
        },
        "current-product binding",
    )
    if root.get("schema_version") != "glodex.current-product-binding.v2":
        raise BuildError("binding schema version is unsupported")
    _string(root.get("purpose"), "purpose")
    contract = _object(root.get("current_product_contract"), "current_product_contract")
    _exact_keys(
        contract,
        {"canonical_join_key", "expected_document_count", "identity_policy"},
        "current_product_contract",
    )
    if (
        contract.get("canonical_join_key") != "document_id"
        or contract.get("identity_policy") != "canonical_document_id_only"
    ):
        raise BuildError("only canonical document_id identity is allowed")
    expected_count = _integer(contract.get("expected_document_count"), "expected_document_count")
    current_catalog = _object(root.get("current_catalog"), "current_catalog")
    _exact_keys(
        current_catalog,
        {"sqlite_path", "expected_document_count", "source_binding_sha256"},
        "current_catalog",
    )
    current_catalog_path = Path(
        _string(current_catalog.get("sqlite_path"), "current_catalog.sqlite_path")
    )
    if not current_catalog_path.is_absolute():
        raise BuildError("current_catalog.sqlite_path must be absolute")
    catalog_expected_count = _integer(
        current_catalog.get("expected_document_count"), "current_catalog.expected_document_count"
    )
    if catalog_expected_count != expected_count:
        raise BuildError("current catalog count does not match the product contract")
    source_sha = _sha256_string(
        current_catalog.get("source_binding_sha256"), "current_catalog.source_binding_sha256"
    )

    current_item_vectors = _object(root.get("current_item_vectors"), "current_item_vectors")
    _exact_keys(
        current_item_vectors,
        {"manifest_path", "manifest_sha256", "expected_schema_version"},
        "current_item_vectors",
    )
    vector_manifest_path = Path(
        _string(current_item_vectors.get("manifest_path"), "current_item_vectors.manifest_path")
    )
    if not vector_manifest_path.is_absolute():
        raise BuildError("current_item_vectors.manifest_path must be absolute")
    vector_manifest_sha256 = _sha256_string(
        current_item_vectors.get("manifest_sha256"), "current_item_vectors.manifest_sha256"
    )
    vector_manifest_schema_version = _string(
        current_item_vectors.get("expected_schema_version"),
        "current_item_vectors.expected_schema_version",
    )
    if vector_manifest_schema_version != "current_catalog_item_vectors_v1":
        raise BuildError("only the formal current Item-vector manifest schema is allowed")

    opensearch = _object(root.get("opensearch"), "opensearch")
    _exact_keys(
        opensearch,
        {
            "endpoint",
            "index_alias",
            "physical_index",
            "pipeline_id",
            "vector_field",
            "embedding_dimension",
            "primary_shards",
            "vector_engine",
            "hybrid_weights",
        },
        "opensearch",
    )
    endpoint = _string(opensearch.get("endpoint"), "opensearch.endpoint").rstrip("/")
    parsed = urllib.parse.urlsplit(endpoint)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port != 9200
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise BuildError(
            "OpenSearch endpoint must be the dedicated 127.0.0.1 server instance on port 9200"
        )
    dimension = _integer(opensearch.get("embedding_dimension"), "embedding_dimension")
    if dimension != 1024:
        raise BuildError("only the current 1024-dimensional vectors are accepted")
    if (
        _integer(opensearch.get("primary_shards"), "opensearch.primary_shards") != 1
        or opensearch.get("vector_engine") != "lucene_hnsw_cosinesimil"
    ):
        raise BuildError("only the current single-shard Lucene HNSW mapping is allowed")
    weights = _object(opensearch.get("hybrid_weights"), "hybrid_weights")
    semantic_weight = weights.get("semantic")
    lexical_weight = weights.get("lexical")
    if (
        type(semantic_weight) not in (int, float)
        or type(lexical_weight) not in (int, float)
        or isinstance(semantic_weight, bool)
        or isinstance(lexical_weight, bool)
        or float(semantic_weight) <= 0
        or float(lexical_weight) <= 0
        or abs(float(semantic_weight) + float(lexical_weight) - 1.0) > 1e-9
    ):
        raise BuildError("hybrid weights must be positive and sum to one")
    schema_policy = _object(root.get("schema_policy"), "schema_policy")
    _exact_keys(
        schema_policy,
        {"full_text_fields", "filter_fields", "category_policy", "raw_locale_policy", "serving"},
        "schema_policy",
    )
    if _list(schema_policy.get("full_text_fields"), "schema_policy.full_text_fields") != [
        "title",
        "search_text",
    ] or _list(schema_policy.get("filter_fields"), "schema_policy.filter_fields") != [
        "source",
        "source_locale_raw",
        "item_language",
        "target_market_locale",
        "category",
        "brand",
        "color",
        "has_category",
        "platform",
        "category_card_id",
    ]:
        raise BuildError("schema_policy does not match the current Hybrid mapping")
    for key in ("category_policy", "raw_locale_policy", "serving"):
        _string(schema_policy.get(key), "schema_policy." + key)
    return Binding(
        config_path=path,
        current_catalog_path=current_catalog_path,
        expected_count=expected_count,
        source_binding_sha256=source_sha,
        vector_manifest_path=vector_manifest_path,
        vector_manifest_sha256=vector_manifest_sha256,
        vector_manifest_schema_version=vector_manifest_schema_version,
        endpoint=endpoint,
        alias=_string(opensearch.get("index_alias"), "opensearch.index_alias"),
        physical_index=_string(opensearch.get("physical_index"), "opensearch.physical_index"),
        pipeline=_string(opensearch.get("pipeline_id"), "opensearch.pipeline_id"),
        dimension=dimension,
        vector_field=_string(opensearch.get("vector_field"), "opensearch.vector_field"),
        semantic_weight=float(semantic_weight),
        lexical_weight=float(lexical_weight),
    )


class DuplicateJsonKey(ValueError):
    """Raised when a formal JSON artifact repeats a key."""


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKey(key)
        result[key] = value
    return result


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _open_current_catalog(binding: Binding) -> sqlite3.Connection:
    path = _regular_file(binding.current_catalog_path, "current catalog SQLite")
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        connection.execute("PRAGMA query_only = ON")
        return connection
    except sqlite3.Error as error:
        raise BuildError("current catalog SQLite cannot be opened read-only") from error


def _verify_current_catalog(binding: Binding) -> None:
    """Prove the formal catalog is complete without ever creating or changing it."""

    connection = _open_current_catalog(binding)
    try:
        if not all(
            _table_exists(connection, name)
            for name in ("metadata", "documents", "semantics", "crosswalk")
        ):
            raise BuildError("current catalog has an incomplete schema")
        metadata = dict(
            connection.execute(
                "SELECT key, value FROM metadata WHERE key IN (?, ?)",
                ("source_binding_sha256", "expected_document_count"),
            )
        )
        counts = {
            name: connection.execute("SELECT COUNT(*) FROM " + name).fetchone()[0]
            for name in ("documents", "semantics", "crosswalk")
        }
        invalid_identity = connection.execute(
            "SELECT COUNT(*) FROM crosswalk WHERE ann_document_id != current_base_document_id"
        ).fetchone()[0]
        missing_join = connection.execute(
            "SELECT COUNT(*) FROM crosswalk AS c "
            "LEFT JOIN documents AS d ON d.document_id = c.current_base_document_id "
            "LEFT JOIN semantics AS s ON s.document_id = c.ann_document_id "
            "WHERE d.document_id IS NULL OR s.document_id IS NULL"
        ).fetchone()[0]
        language_disagreement = connection.execute(
            "SELECT COUNT(*) FROM crosswalk AS c "
            "JOIN semantics AS s ON s.document_id = c.ann_document_id "
            "WHERE c.item_language != s.item_language"
        ).fetchone()[0]
    except sqlite3.Error as error:
        raise BuildError("current catalog SQLite cannot be verified read-only") from error
    finally:
        connection.close()
    if (
        metadata.get("source_binding_sha256") != binding.source_binding_sha256
        or metadata.get("expected_document_count") != str(binding.expected_count)
        or any(value != binding.expected_count for value in counts.values())
        or invalid_identity
        or missing_join
        or language_disagreement
    ):
        raise BuildError("current catalog SQLite does not satisfy canonical document_id identity")


def _manifest_child(manifest_path: Path, value: object, field: str) -> Path:
    filename = _string(value, field)
    relative = Path(filename)
    if relative.is_absolute() or len(relative.parts) != 1 or relative.name != filename:
        raise BuildError(field + " must name a manifest-local artifact")
    return _regular_file(manifest_path.parent / relative, field)


def _load_vector_assets(binding: Binding) -> VectorAssets:
    manifest_path = _regular_file(binding.vector_manifest_path, "current Item-vector manifest")
    if _sha256(manifest_path) != binding.vector_manifest_sha256:
        raise BuildError("current Item-vector manifest checksum does not match binding")
    try:
        raw = json.loads(
            manifest_path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, DuplicateJsonKey) as error:
        raise BuildError("current Item-vector manifest is unreadable") from error
    manifest = _object(raw, "current Item-vector manifest")
    if (
        manifest.get("schema_version") != binding.vector_manifest_schema_version
        or manifest.get("legacy_qrels_used") is not False
        or any(value is not False for key, value in manifest.items() if key.startswith("legacy_"))
    ):
        raise BuildError("current Item-vector manifest is not current-only")
    catalog = _object(manifest.get("catalog"), "current Item-vector manifest.catalog")
    _exact_keys(
        catalog,
        {"path", "document_count", "source_binding_sha256", "document_id_key"},
        "current Item-vector manifest.catalog",
    )
    if (
        catalog.get("path") != str(binding.current_catalog_path)
        or catalog.get("document_count") != binding.expected_count
        or catalog.get("source_binding_sha256") != binding.source_binding_sha256
        or catalog.get("document_id_key") != "current canonical document_id"
    ):
        raise BuildError("current Item-vector manifest is not bound to the current catalog")
    model = _object(manifest.get("model"), "current Item-vector manifest.model")
    if (
        model.get("source") != "current_catalog_finetuned"
        or model.get("vector_dimension") != binding.dimension
    ):
        raise BuildError("current Item-vector manifest has an incompatible model identity")
    outputs = _object(manifest.get("outputs"), "current Item-vector manifest.outputs")
    _exact_keys(outputs, {"item_vectors", "item_ids"}, "current Item-vector manifest.outputs")
    vector_descriptor = _object(
        outputs.get("item_vectors"), "current Item-vector manifest.outputs.item_vectors"
    )
    _exact_keys(
        vector_descriptor,
        {"file", "sha256", "bytes", "dtype", "shape", "normalized"},
        "current Item-vector manifest.outputs.item_vectors",
    )
    ids_descriptor = _object(
        outputs.get("item_ids"), "current Item-vector manifest.outputs.item_ids"
    )
    _exact_keys(
        ids_descriptor,
        {"file", "sha256", "bytes", "row_count"},
        "current Item-vector manifest.outputs.item_ids",
    )
    if (
        vector_descriptor.get("dtype") != "float16"
        or vector_descriptor.get("shape") != [binding.expected_count, binding.dimension]
        or vector_descriptor.get("normalized") is not True
        or ids_descriptor.get("row_count") != binding.expected_count
    ):
        raise BuildError("current Item-vector manifest output shape is invalid")
    vectors_path = _manifest_child(
        manifest_path,
        vector_descriptor.get("file"),
        "current Item-vector manifest item_vectors.file",
    )
    ids_path = _manifest_child(
        manifest_path, ids_descriptor.get("file"), "current Item-vector manifest item_ids.file"
    )
    if vectors_path.stat().st_size != _integer(
        vector_descriptor.get("bytes"), "item_vectors.bytes"
    ) or ids_path.stat().st_size != _integer(ids_descriptor.get("bytes"), "item_ids.bytes"):
        raise BuildError("current Item-vector artifact size does not match manifest")
    return VectorAssets(
        manifest_path=manifest_path,
        manifest_sha256=binding.vector_manifest_sha256,
        vectors_path=vectors_path,
        vectors_sha256=_sha256_string(vector_descriptor.get("sha256"), "item_vectors.sha256"),
        ids_path=ids_path,
        ids_sha256=_sha256_string(ids_descriptor.get("sha256"), "item_ids.sha256"),
    )


def _verify_vector_output_hashes(assets: VectorAssets) -> None:
    if _sha256(assets.vectors_path) != assets.vectors_sha256:
        raise BuildError("current Item-vector matrix checksum does not match manifest")
    if _sha256(assets.ids_path) != assets.ids_sha256:
        raise BuildError("current Item-vector ID sidecar checksum does not match manifest")


def _validate_vector_matrix(assets: VectorAssets, binding: Binding) -> None:
    numpy = _require_numpy()
    try:
        matrix = numpy.load(assets.vectors_path, mmap_mode="r", allow_pickle=False)
    except (OSError, ValueError) as error:
        raise BuildError("formal current Item-vector matrix cannot be opened") from error
    if matrix.dtype != numpy.float16 or tuple(matrix.shape) != (
        binding.expected_count,
        binding.dimension,
    ):
        raise BuildError("formal current Item-vector matrix shape or dtype is invalid")


def _load_vector_ids(path: Path, count: int) -> Iterator[str]:
    observed = 0
    previous: str | None = None
    try:
        with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.endswith("\n") or not line.strip():
                    raise BuildError("current Item-vector ID sidecar has an invalid line")
                raw = json.loads(line, object_pairs_hook=_reject_duplicate_keys)
                row = _object(raw, "current Item-vector ID sidecar line " + str(line_number))
                if set(row) != {"id"}:
                    raise BuildError("current Item-vector ID sidecar row has an invalid shape")
                identity = _string(row.get("id"), "current Item-vector document_id")
                if previous is not None and identity <= previous:
                    raise BuildError("current Item-vector IDs are not strictly ordered")
                previous = identity
                observed += 1
                yield identity
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, DuplicateJsonKey) as error:
        raise BuildError("current Item-vector ID sidecar is unreadable") from error
    if observed != count:
        raise BuildError("current Item-vector ID sidecar count does not match catalog")


def _verify_vector_ids_match_catalog(binding: Binding, assets: VectorAssets) -> None:
    connection = _open_current_catalog(binding)
    try:
        rows = connection.execute("SELECT document_id FROM documents ORDER BY document_id")
        for vector_id, row in zip(
            _load_vector_ids(assets.ids_path, binding.expected_count), rows, strict=True
        ):
            if row[0] != vector_id:
                raise BuildError("current Item-vector IDs do not match catalog document_id order")
    except sqlite3.Error as error:
        raise BuildError("current catalog IDs cannot be verified") from error
    finally:
        connection.close()


def _http_json(
    binding: Binding,
    method: str,
    path: str,
    body: object | None = None,
    accepted: Sequence[int] = (200,),
) -> dict[str, Any] | None:
    data = None
    headers: dict[str, str] = {"accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["content-type"] = "application/json"
    request = urllib.request.Request(
        binding.endpoint + path, data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            if response.status not in accepted:
                raise BuildError("unexpected OpenSearch HTTP status " + str(response.status))
            raw = response.read()
            return _object(json.loads(raw), "OpenSearch response") if raw else None
    except urllib.error.HTTPError as error:
        if error.code in accepted:
            return None
        raise BuildError(
            "OpenSearch request failed with HTTP " + str(error.code) + " for " + path
        ) from error
    except urllib.error.URLError as error:
        raise BuildError(
            "cannot reach dedicated loopback OpenSearch at " + binding.endpoint
        ) from error


def _http_bulk(binding: Binding, payload: bytes) -> dict[str, Any]:
    request = urllib.request.Request(
        binding.endpoint + "/_bulk?refresh=false",
        data=payload,
        headers={"content-type": "application/x-ndjson", "accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return _object(json.loads(response.read()), "OpenSearch bulk response")
    except urllib.error.HTTPError as error:
        raise BuildError("OpenSearch bulk write failed with HTTP " + str(error.code)) from error
    except urllib.error.URLError as error:
        raise BuildError("OpenSearch bulk connection failed") from error


def pipeline_body(binding: Binding) -> dict[str, Any]:
    return {
        "description": "Current product Hybrid normalization: semantic 0.7 and lexical 0.3",
        "phase_results_processors": [
            {
                "normalization-processor": {
                    "normalization": {"technique": "min_max"},
                    "combination": {
                        "technique": "arithmetic_mean",
                        "parameters": {
                            "weights": [binding.semantic_weight, binding.lexical_weight]
                        },
                    },
                }
            }
        ],
    }


def _expected_mapping_meta(binding: Binding) -> dict[str, Any]:
    """The complete ItemSearch-attributes metadata contract."""

    return {
        "schema_version": "glodex.current-product-hybrid-index.v6",
        "current_catalog_binding_sha256": binding.source_binding_sha256,
        "current_item_vectors_manifest_sha256": binding.vector_manifest_sha256,
        "canonical_join_key": "document_id",
        "expected_document_count": binding.expected_count,
        "identity_policy": "canonical_document_id_only",
    }


def index_body(binding: Binding) -> dict[str, Any]:
    return {
        "settings": {
            "index": {
                "knn": True,
                "number_of_shards": 1,
                "number_of_replicas": 0,
                "refresh_interval": "-1",
                "translog.durability": "async",
                "merge.scheduler.max_thread_count": 1,
            }
        },
        "mappings": {
            "_source": {"excludes": [binding.vector_field, "search_text"]},
            "_meta": _expected_mapping_meta(binding),
            "properties": {
                "document_id": {"type": "keyword"},
                "family_id": {"type": "keyword"},
                "source": {"type": "keyword"},
                "source_locale_raw": {"type": "keyword"},
                "source_locale_semantics": {"type": "keyword"},
                "item_language": {"type": "keyword"},
                "item_language_confidence": {"type": "float"},
                "target_market_locale": {"type": "keyword"},
                "product_base_shard": {"type": "keyword"},
                "title": {"type": "text"},
                "search_text": {"type": "text"},
                "category": {"type": "keyword"},
                "has_category": {"type": "boolean"},
                "brand": {"type": "keyword"},
                "color": {"type": "keyword"},
                "attribute_names": {"type": "keyword"},
                # Candidate attributes are returned as a bounded source object.
                # They are not dynamically mapped, which prevents arbitrary
                # public attribute names from exploding the index field count.
                "attributes": {"type": "object", "enabled": False},
                "category_card_id": {"type": "keyword"},
                "category_assignment_method": {"type": "keyword"},
                "category_assignment_score": {"type": "float"},
                "entity_kind": {"type": "keyword"},
                "platform": {"type": "keyword"},
                "provider_id": {"type": "keyword"},
                "market": {"type": "keyword"},
                "offer_id": {"type": "keyword"},
                "source_uri": {"type": "keyword", "index": False},
                "currency": {"type": "keyword"},
                "item_price": {"type": "scaled_float", "scaling_factor": 100},
                "shipping": {"type": "scaled_float", "scaling_factor": 100},
                "tax": {"type": "scaled_float", "scaling_factor": 100},
                "duty": {"type": "scaled_float", "scaling_factor": 100},
                "stock_status": {"type": "keyword"},
                "delivery_days_min": {"type": "integer"},
                "delivery_days_max": {"type": "integer"},
                "commerce_ruleset_version": {"type": "keyword"},
                "captured_at": {"type": "date"},
                binding.vector_field: {
                    "type": "knn_vector",
                    "dimension": binding.dimension,
                    "method": {
                        "name": "hnsw",
                        "engine": "lucene",
                        "space_type": "cosinesimil",
                        "parameters": {"ef_construction": 128, "m": 16},
                    },
                },
            },
        },
    }


def _enrich_source(
    source: Mapping[str, Any],
    *,
    item_vector: Any,
    classifier: CategoryClassifier,
    policies: CategoryPolicySet,
    seed: str,
    vector_field: str = "item_vector",
    vector_match: tuple[str, float, float] | None = None,
) -> dict[str, Any]:
    """Attach one Card assignment and complete interview commerce facts."""

    document_id = _string(source.get("document_id"), "source document_id")
    title = _string(source.get("title"), "source title")
    category = source.get("category")
    if category is not None and type(category) is not str:
        raise BuildError("source category must be text or null")
    assignment = classifier.classify(
        category=category,
        vector_match=vector_match,
    )
    card = policies.require(assignment.card_id)
    commerce = generate_commerce(
        document_id=document_id,
        title=title,
        card=card,
        seed=seed,
    )
    platform = _platform_for(str(source.get("source") or ""), document_id)
    offer_digest = hashlib.sha256((document_id + "\x1f" + platform).encode("utf-8")).hexdigest()[
        :24
    ]
    enriched = dict(source)
    enriched.update(
        {
            "attributes": project_product_attributes(source),
            "category_card_id": card.card_id,
            "category_assignment_method": assignment.method,
            "category_assignment_score": assignment.score,
            "entity_kind": card.entity_kind,
            "platform": platform,
            "provider_id": "current-product-" + platform,
            "market": "CN",
            "offer_id": "offer-" + offer_digest,
            "source_uri": "urn:glodex:current-product:" + document_id,
            "currency": "CNY",
            "item_price": float(commerce.item_price),
            "shipping": float(commerce.shipping),
            "tax": 0.0,
            "duty": 0.0,
            "stock_status": commerce.stock_status,
            "delivery_days_min": commerce.delivery_days_min,
            "delivery_days_max": commerce.delivery_days_max,
            "commerce_ruleset_version": policies.ruleset_version,
            "captured_at": "2026-08-01T00:00:00Z",
            vector_field: item_vector,
        }
    )
    return enriched


def _platform_for(source: str, document_id: str) -> str:
    normalized = source.casefold()
    for token, platform in (
        ("amazon", "amazon"),
        ("ebay", "ebay"),
        ("shopee", "shopee"),
        ("aliexpress", "aliexpress"),
        ("alibaba", "aliexpress"),
    ):
        if token in normalized:
            return platform
    platforms = ("amazon", "shopee", "aliexpress", "ebay")
    digest = hashlib.sha256(document_id.encode("utf-8")).digest()[0]
    return platforms[digest % len(platforms)]


def _index_exists(binding: Binding, index: str) -> bool:
    request = urllib.request.Request(binding.endpoint + "/" + index, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status == 200
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return False
        raise BuildError("cannot inspect physical index") from error
    except urllib.error.URLError as error:
        raise BuildError("cannot reach dedicated loopback OpenSearch") from error


def _catalog_batch(
    connection: sqlite3.Connection, document_ids: Sequence[str]
) -> dict[str, dict[str, Any]]:
    placeholders = ",".join("?" for _ in document_ids)
    query = (
        "SELECT c.ann_document_id, c.current_base_document_id, d.payload_json, s.item_language, "
        "c.item_language, "
        "c.item_language_confidence, c.target_market_locale, "
        "c.source_locale_raw, c.source_locale_semantics, c.product_base_shard "
        "FROM crosswalk AS c JOIN documents AS d "
        "ON d.document_id = c.current_base_document_id "
        "JOIN semantics AS s ON s.document_id = c.ann_document_id "
        "WHERE c.ann_document_id IN (" + placeholders + ")"
    )
    rows = connection.execute(query, tuple(document_ids)).fetchall()
    payloads: dict[str, dict[str, Any]] = {}
    for (
        ann_document_id,
        current_base_document_id,
        raw_payload,
        semantic_language,
        crosswalk_language,
        language_confidence,
        target_locale,
        source_locale_raw,
        source_locale_semantics,
        product_base_shard,
    ) in rows:
        if ann_document_id != current_base_document_id or ann_document_id not in document_ids:
            raise BuildError("catalog crosswalk violates canonical document_id identity")
        if semantic_language != crosswalk_language:
            raise BuildError("catalog crosswalk language disagrees with catalog semantics")
        payload = _object(json.loads(raw_payload), "catalog payload")
        if payload.get("document_id") != ann_document_id:
            raise BuildError("catalog payload does not retain canonical document_id")
        payload["item_language"] = semantic_language
        payload["item_language_confidence"] = language_confidence
        payload["target_market_locale"] = target_locale
        payload["source_locale_raw"] = source_locale_raw
        payload["source_locale_semantics"] = source_locale_semantics
        payload["product_base_shard"] = product_base_shard
        payloads[ann_document_id] = payload
    if len(payloads) != len(document_ids):
        raise BuildError(
            "document_id key join failed between vector sidecar and current product data"
        )
    return payloads


def _bulk_payload(index: str, sources: Sequence[tuple[str, Mapping[str, Any]]]) -> bytes:
    encoded = bytearray()
    for identity, source in sources:
        encoded.extend(
            json.dumps({"index": {"_index": index, "_id": identity}}, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        encoded.extend(b"\n")
        encoded.extend(
            json.dumps(source, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        encoded.extend(b"\n")
    return bytes(encoded)


def _validate_bulk_response(response: Mapping[str, Any]) -> None:
    if response.get("errors") is not False:
        raise BuildError("OpenSearch rejected one or more indexed documents")


def _iter_sources(
    binding: Binding,
    assets: VectorAssets,
    *,
    classifier: CategoryClassifier,
    policies: CategoryPolicySet,
    batch_size: int = 768,
    start_offset: int = 0,
) -> Iterator[list[tuple[str, Mapping[str, Any]]]]:
    if start_offset < 0 or start_offset > binding.expected_count or start_offset % 192:
        raise BuildError("resume offset is not an exact committed batch boundary")
    numpy = _require_numpy()
    vectors = numpy.load(assets.vectors_path, mmap_mode="r", allow_pickle=False)
    if (
        tuple(vectors.shape) != (binding.expected_count, binding.dimension)
        or vectors.dtype != numpy.float16
    ):
        raise BuildError("formal Item-vector matrix shape or dtype does not match binding")
    connection = _open_current_catalog(binding)
    card_ids, card_rows = classifier.vector_rows()
    if len(card_ids) != len(policies.cards):
        raise BuildError("Category Card vector matrix is incomplete")
    card_matrix = numpy.asarray(card_rows, dtype=numpy.float32)
    if tuple(card_matrix.shape) != (len(policies.cards), binding.dimension):
        raise BuildError("Category Card vector matrix shape is incompatible")
    try:
        sidecar = _load_vector_ids(assets.ids_path, binding.expected_count)
        for _index in range(start_offset):
            next(sidecar)
        for start in range(start_offset, binding.expected_count, batch_size):
            end = min(start + batch_size, binding.expected_count)
            identities = [next(sidecar) for _ in range(start, end)]
            payloads = _catalog_batch(connection, identities)
            numeric = numpy.asarray(vectors[start:end], dtype=numpy.float32)
            norms = numpy.linalg.norm(numeric, axis=1)
            if (
                not numpy.isfinite(numeric).all()
                or numpy.any(norms < 0.95)
                or numpy.any(norms > 1.05)
            ):
                raise BuildError(
                    "formal current Item-vector matrix contains invalid cosine vectors"
                )
            similarities = numeric @ card_matrix.T
            ranked_indexes = numpy.argsort(similarities, axis=1)
            sources: list[tuple[str, Mapping[str, Any]]] = []
            for row_index, (identity, vector) in enumerate(zip(identities, numeric, strict=True)):
                payload = dict(payloads[identity])
                if payload.get("document_id") != identity:
                    raise BuildError("catalog changed a canonical document_id")
                item_vector = vector.tolist()
                best_index = int(ranked_indexes[row_index, -1])
                runner_up_index = int(ranked_indexes[row_index, -2])
                sources.append(
                    (
                        identity,
                        _enrich_source(
                            payload,
                            item_vector=item_vector,
                            classifier=classifier,
                            policies=policies,
                            seed=COMMERCE_GENERATION_SEED,
                            vector_field=binding.vector_field,
                            vector_match=(
                                card_ids[best_index],
                                float(similarities[row_index, best_index]),
                                float(similarities[row_index, runner_up_index]),
                            ),
                        ),
                    )
                )
            yield sources
    finally:
        connection.close()


def _prefetched_sources(
    binding: Binding,
    assets: VectorAssets,
    *,
    classifier: CategoryClassifier,
    policies: CategoryPolicySet,
    start_offset: int = 0,
) -> Iterator[list[tuple[str, Mapping[str, Any]]]]:
    """Build the next CPU-heavy vector batch while OpenSearch commits this one."""

    source_batches = _iter_sources(
        binding,
        assets,
        classifier=classifier,
        policies=policies,
        start_offset=start_offset,
    )
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="current-product-prefetch") as pool:
        pending = pool.submit(next, source_batches, None)
        while (sources := pending.result()) is not None:
            pending = pool.submit(next, source_batches, None)
            yield sources


def _publish_alias(binding: Binding, index: str) -> None:
    existing = _http_json(binding, "GET", "/_alias/" + binding.alias, accepted=(200, 404))
    actions: list[dict[str, Any]] = []
    if existing:
        for existing_index in existing:
            if not existing_index.startswith("glodex-current-product-hybrid-"):
                raise BuildError("refusing to replace an alias owned by another index family")
            actions.append({"remove": {"index": existing_index, "alias": binding.alias}})
    actions.append({"add": {"index": index, "alias": binding.alias}})
    _http_json(binding, "POST", "/_aliases", {"actions": actions}, accepted=(200,))


def _count(binding: Binding, index: str) -> int:
    response = _http_json(binding, "GET", "/" + index + "/_count", accepted=(200,))
    assert response is not None
    return _integer(response.get("count"), "OpenSearch count")


def _verify_alias_target(binding: Binding) -> None:
    response = _http_json(binding, "GET", "/_alias/" + binding.alias, accepted=(200, 404))
    if response is None or set(response) != {binding.physical_index}:
        raise BuildError("published alias does not point exactly to the current physical index")
    index_node = _object(response.get(binding.physical_index), "published alias index")
    aliases = _object(index_node.get("aliases"), "published aliases")
    if binding.alias not in aliases:
        raise BuildError("published alias is absent from the current physical index")


def _mapping_meta(binding: Binding, index: str) -> dict[str, Any]:
    response = _http_json(binding, "GET", "/" + index + "/_mapping", accepted=(200,))
    assert response is not None
    index_node = _object(response.get(index), "index mapping")
    mappings = _object(index_node.get("mappings"), "mappings")
    return _object(mappings.get("_meta"), "mapping metadata")


def _verify_hybrid_query(binding: Binding, assets: VectorAssets, index: str) -> int:
    # KNN vectors are not necessarily returned in OpenSearch _source. Use one
    # row from the formal manifest-bound current Item-vector artifact instead.
    numpy = _require_numpy()
    try:
        vectors = numpy.load(assets.vectors_path, mmap_mode="r", allow_pickle=False)
    except (OSError, ValueError) as error:
        raise BuildError("cannot load the formal current vector probe") from error
    if (
        vectors.shape != (binding.expected_count, binding.dimension)
        or vectors.dtype != numpy.float16
    ):
        raise BuildError("formal current vector probe has an invalid shape or dtype")
    vector = numpy.asarray(vectors[0], dtype=numpy.float32)
    if not numpy.isfinite(vector).all():
        raise BuildError("formal current vector probe contains non-finite values")
    body = {
        "size": 3,
        "_source": ["document_id", "title", "source", "item_language"],
        "query": {
            "hybrid": {
                "queries": [
                    {"knn": {binding.vector_field: {"vector": vector.tolist(), "k": 10}}},
                    {"match": {"search_text": {"query": "product"}}},
                ]
            }
        },
    }
    response = _http_json(
        binding,
        "POST",
        "/" + index + "/_search?search_pipeline=" + urllib.parse.quote(binding.pipeline, safe=""),
        body,
        accepted=(200,),
    )
    assert response is not None
    hits = _object(response.get("hits"), "hybrid hits")
    rows = _list(hits.get("hits"), "hybrid hit rows")
    if not rows:
        raise BuildError("hybrid query returned no rows")
    return len(rows)


def _verify_semantic_clean_audit(binding: Binding, index: str) -> dict[str, Any]:
    """Reject publication unless semantic-only assignment and fixed canaries pass."""

    aggregation = _http_json(
        binding,
        "POST",
        "/" + index + "/_search",
        {
            "size": 0,
            "aggs": {"methods": {"terms": {"field": "category_assignment_method", "size": 10}}},
        },
        accepted=(200,),
    )
    assert aggregation is not None
    aggregations = _object(aggregation.get("aggregations"), "semantic-clean aggregations")
    methods = _object(aggregations.get("methods"), "semantic-clean methods")
    buckets = _list(methods.get("buckets"), "semantic-clean method buckets")
    counts = {
        _string(_object(bucket, "semantic-clean method bucket").get("key"), "method"): _integer(
            _object(bucket, "semantic-clean method bucket").get("doc_count"),
            "method count",
        )
        for bucket in buckets
    }
    if set(counts).difference({"SOURCE_CATEGORY", "SEMANTIC_VECTOR", "FALLBACK"}):
        raise BuildError("semantic-clean index contains a forbidden assignment method")
    if sum(counts.values()) != binding.expected_count or counts.get("SEMANTIC_VECTOR", 0) == 0:
        raise BuildError("semantic-clean assignment coverage is incomplete")

    expected_canaries = {
        "esci:es:B091TQ934W": ("electronics.laptop", "SEMANTIC_VECTOR"),
        "esci:jp:B07ZVMGGQQ": ("electronics.laptop", "SEMANTIC_VECTOR"),
        "esci:jp:B0989NTGB8": ("electronics.laptop-accessory", "SEMANTIC_VECTOR"),
        "esci:jp:B07KJC7Z8T": ("electronics.keyboard", "SEMANTIC_VECTOR"),
        "esci:jp:B092D673DL": ("general.general-merchandise", "FALLBACK"),
        "esci:us:B07F2YJRN2": ("luggage.bag", "SEMANTIC_VECTOR"),
        "esci:us:B07MK6J5NT": ("luggage.bag", "SEMANTIC_VECTOR"),
        "esci:us:B077RV492Y": ("luggage.bag", "SEMANTIC_VECTOR"),
        "esci:us:B07JHXX5YR": ("electronics.phone", "SEMANTIC_VECTOR"),
    }
    canaries = _http_json(
        binding,
        "POST",
        "/" + index + "/_mget",
        {"ids": list(expected_canaries)},
        accepted=(200,),
    )
    assert canaries is not None
    documents = _list(canaries.get("docs"), "semantic-clean canary documents")
    observed: dict[str, str] = {}
    for value in documents:
        document = _object(value, "semantic-clean canary document")
        document_id = _string(document.get("_id"), "semantic-clean canary ID")
        source = _object(document.get("_source"), "semantic-clean canary source")
        observed_card = _string(source.get("category_card_id"), "semantic-clean canary Card")
        expected_card, expected_method = expected_canaries.get(document_id, ("", ""))
        if (
            observed_card != expected_card
            or source.get("category_assignment_method") != expected_method
        ):
            raise BuildError("semantic-clean canary classification failed")
        observed[document_id] = observed_card
    if set(observed) != set(expected_canaries):
        raise BuildError("semantic-clean canary classification failed")
    return {"assignment_methods": counts, "canary_cards": observed}


def _build_category_classifier(
    *,
    cards_path: Path,
    dimension: int,
    encode: Callable[[tuple[str, ...]], Sequence[Sequence[float]]],
) -> tuple[CategoryPolicySet, CategoryClassifier]:
    policies = load_category_cards(_regular_file(cards_path, "Category Card artifact"))
    rows: list[tuple[float, ...]] = []
    for start in range(0, len(policies.cards), 8):
        batch = policies.cards[start : start + 8]
        encoded = encode(tuple(card.prototype_text for card in batch))
        if len(encoded) != len(batch):
            raise BuildError("model service returned the wrong Category Card vector count")
        for row in encoded:
            vector = tuple(float(value) for value in row)
            if len(vector) != dimension:
                raise BuildError("model service returned the wrong Category Card vector dimension")
            rows.append(vector)
    card_ids = tuple(card.card_id for card in policies.cards)
    return policies, CategoryClassifier(
        policies,
        card_vectors={card_id: row for card_id, row in zip(card_ids, rows, strict=True)},
    )


def _server_card_encoder(
    endpoint: str,
    *,
    dimension: int,
) -> Callable[[tuple[str, ...]], tuple[tuple[float, ...], ...]]:
    parsed = urllib.parse.urlsplit(endpoint)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port != 18000
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise BuildError("model service endpoint must be http://127.0.0.1:18000")

    def encode(texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        request = urllib.request.Request(
            endpoint.rstrip("/") + "/v1/embed",
            data=json.dumps({"texts": list(texts)}, separators=(",", ":")).encode("utf-8"),
            headers={"content-type": "application/json", "accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = _object(json.loads(response.read()), "model service response")
        except (urllib.error.URLError, json.JSONDecodeError) as error:
            raise BuildError(
                "cannot encode Category Cards with the server model service"
            ) from error
        raw = _list(payload.get("embeddings"), "Category Card embeddings")
        vectors: list[tuple[float, ...]] = []
        for row in raw:
            values = _list(row, "Category Card embedding")
            vector = tuple(float(value) for value in values)
            if len(vector) != dimension:
                raise BuildError("server model service returned an incompatible dimension")
            vectors.append(vector)
        return tuple(vectors)

    return encode


def action_validate_assets(
    binding: Binding,
    *,
    cards_path: Path,
    model_service_endpoint: str,
) -> dict[str, Any]:
    """Verify the one current catalog/vector lineage without starting OpenSearch."""

    _verify_current_catalog(binding)
    assets = _load_vector_assets(binding)
    _verify_vector_output_hashes(assets)
    _validate_vector_matrix(assets, binding)
    _verify_vector_ids_match_catalog(binding, assets)
    policies, _ = _build_category_classifier(
        cards_path=cards_path,
        dimension=binding.dimension,
        encode=_server_card_encoder(model_service_endpoint, dimension=binding.dimension),
    )
    return {
        "status": "ASSETS_VALID",
        "canonical_join_key": "document_id",
        "identity_policy": "canonical_document_id_only",
        "current_catalog": str(binding.current_catalog_path),
        "current_item_vectors_manifest": str(assets.manifest_path),
        "expected_document_count": binding.expected_count,
        "vector_shape": [binding.expected_count, binding.dimension],
        "category_card_count": len(policies.cards),
        "category_card_vector_shape": [len(policies.cards), binding.dimension],
    }


def action_build(
    binding: Binding,
    *,
    cards_path: Path,
    model_service_endpoint: str,
) -> dict[str, Any]:
    _verify_current_catalog(binding)
    assets = _load_vector_assets(binding)
    _verify_vector_output_hashes(assets)
    _validate_vector_matrix(assets, binding)
    _verify_vector_ids_match_catalog(binding, assets)
    policies, classifier = _build_category_classifier(
        cards_path=cards_path,
        dimension=binding.dimension,
        encode=_server_card_encoder(model_service_endpoint, dimension=binding.dimension),
    )
    index = binding.physical_index
    _http_json(
        binding, "GET", "/_cluster/health?wait_for_status=yellow&timeout=30s", accepted=(200,)
    )
    if _index_exists(binding, index):
        raise BuildError("physical index already exists; refusing to mix or overwrite " + index)
    _http_json(
        binding,
        "PUT",
        "/_search/pipeline/" + binding.pipeline,
        pipeline_body(binding),
        accepted=(200,),
    )
    _http_json(binding, "PUT", "/" + index, index_body(binding), accepted=(200,))
    started = time.monotonic()
    indexed = 0
    for sources in _prefetched_sources(
        binding,
        assets,
        classifier=classifier,
        policies=policies,
    ):
        response = _http_bulk(binding, _bulk_payload(index, sources))
        _validate_bulk_response(response)
        indexed += len(sources)
        if indexed % 4_800 == 0 or indexed == binding.expected_count:
            elapsed = max(time.monotonic() - started, 0.001)
            print(
                "indexed "
                + str(indexed)
                + "/"
                + str(binding.expected_count)
                + " at "
                + format(indexed / elapsed, ".1f")
                + " docs/s",
                file=sys.stderr,
                flush=True,
            )
    _http_json(binding, "POST", "/" + index + "/_refresh", accepted=(200,))
    if _count(binding, index) != binding.expected_count:
        raise BuildError("OpenSearch count is incomplete; alias remains unchanged")
    semantic_clean_audit = _verify_semantic_clean_audit(binding, index)
    _http_json(
        binding,
        "PUT",
        "/" + index + "/_settings",
        {
            "index": {
                "refresh_interval": "1s",
                "translog.durability": "request",
                "blocks.write": True,
            }
        },
        accepted=(200,),
    )
    hybrid_hits = _verify_hybrid_query(binding, assets, index)
    _publish_alias(binding, index)
    return {
        "status": "READY",
        "physical_index": index,
        "alias": binding.alias,
        "document_count": binding.expected_count,
        "hybrid_probe_hits": hybrid_hits,
        "semantic_clean_audit": semantic_clean_audit,
        "published": True,
    }


def action_resume_build(
    binding: Binding,
    *,
    cards_path: Path,
    model_service_endpoint: str,
) -> dict[str, Any]:
    """Resume one unpublished candidate from its exact refreshed batch boundary."""

    _verify_current_catalog(binding)
    assets = _load_vector_assets(binding)
    _verify_vector_output_hashes(assets)
    _validate_vector_matrix(assets, binding)
    _verify_vector_ids_match_catalog(binding, assets)
    policies, classifier = _build_category_classifier(
        cards_path=cards_path,
        dimension=binding.dimension,
        encode=_server_card_encoder(model_service_endpoint, dimension=binding.dimension),
    )
    index = binding.physical_index
    if not _index_exists(binding, index):
        raise BuildError("semantic-clean resume candidate does not exist")
    if _mapping_meta(binding, index) != _expected_mapping_meta(binding):
        raise BuildError("semantic-clean resume candidate identity is invalid")
    _http_json(binding, "POST", "/" + index + "/_refresh", accepted=(200,))
    indexed = _count(binding, index)
    if indexed > binding.expected_count or (indexed < binding.expected_count and indexed % 192):
        raise BuildError("semantic-clean resume count is not a committed batch boundary")
    started = time.monotonic()
    resumed_from = indexed
    if indexed < binding.expected_count:
        for sources in _prefetched_sources(
            binding,
            assets,
            classifier=classifier,
            policies=policies,
            start_offset=resumed_from,
        ):
            response = _http_bulk(binding, _bulk_payload(index, sources))
            _validate_bulk_response(response)
            indexed += len(sources)
            if indexed % 4_800 == 0 or indexed == binding.expected_count:
                elapsed = max(time.monotonic() - started, 0.001)
                print(
                    "resumed "
                    + str(indexed)
                    + "/"
                    + str(binding.expected_count)
                    + " at "
                    + format((indexed - resumed_from) / elapsed, ".1f")
                    + " docs/s",
                    file=sys.stderr,
                    flush=True,
                )
    _http_json(binding, "POST", "/" + index + "/_refresh", accepted=(200,))
    if _count(binding, index) != binding.expected_count:
        raise BuildError("OpenSearch resumed count is incomplete; alias remains unchanged")
    semantic_clean_audit = _verify_semantic_clean_audit(binding, index)
    _http_json(
        binding,
        "PUT",
        "/" + index + "/_settings",
        {
            "index": {
                "refresh_interval": "1s",
                "translog.durability": "request",
                "blocks.write": True,
            }
        },
        accepted=(200,),
    )
    hybrid_hits = _verify_hybrid_query(binding, assets, index)
    _publish_alias(binding, index)
    return {
        "status": "READY",
        "physical_index": index,
        "alias": binding.alias,
        "document_count": binding.expected_count,
        "resumed_from": resumed_from,
        "hybrid_probe_hits": hybrid_hits,
        "semantic_clean_audit": semantic_clean_audit,
        "published": True,
    }


def action_verify(binding: Binding) -> dict[str, Any]:
    _verify_current_catalog(binding)
    assets = _load_vector_assets(binding)
    _verify_vector_output_hashes(assets)
    _validate_vector_matrix(assets, binding)
    _verify_alias_target(binding)
    meta = _mapping_meta(binding, binding.physical_index)
    if meta != _expected_mapping_meta(binding):
        raise BuildError("physical index does not have the exact semantic-clean v5 metadata")
    if _count(binding, binding.alias) != binding.expected_count:
        raise BuildError("published alias count does not match the current product count")
    hits = _verify_hybrid_query(binding, assets, binding.alias)
    semantic_clean_audit = _verify_semantic_clean_audit(binding, binding.alias)
    return {
        "status": "VERIFIED",
        "alias": binding.alias,
        "physical_index": binding.physical_index,
        "document_count": binding.expected_count,
        "hybrid_probe_hits": hits,
        "semantic_clean_audit": semantic_clean_audit,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--binding",
        type=Path,
        default=Path(__file__).with_name("current_product_binding.json"),
    )
    parser.add_argument(
        "--action",
        choices=("plan", "validate-assets", "build", "resume-build", "verify"),
        required=True,
    )
    repository_root = Path(__file__).resolve().parents[2]
    parser.add_argument(
        "--category-cards",
        type=Path,
        default=repository_root
        / "data"
        / "current-product"
        / "category-cards-v2"
        / "category_cards.json",
    )
    parser.add_argument("--model-service-endpoint", default="http://127.0.0.1:18000")
    args = parser.parse_args()
    try:
        binding = load_binding(args.binding)
        if args.action == "plan":
            result: dict[str, Any] = {
                "status": "PLAN_READY",
                "canonical_join_key": "document_id",
                "expected_document_count": binding.expected_count,
                "endpoint": binding.endpoint,
                "physical_index": binding.physical_index,
                "alias": binding.alias,
            }
        elif args.action == "validate-assets":
            result = action_validate_assets(
                binding,
                cards_path=args.category_cards,
                model_service_endpoint=args.model_service_endpoint,
            )
        elif args.action == "build":
            result = action_build(
                binding,
                cards_path=args.category_cards,
                model_service_endpoint=args.model_service_endpoint,
            )
        elif args.action == "resume-build":
            result = action_resume_build(
                binding,
                cards_path=args.category_cards,
                model_service_endpoint=args.model_service_endpoint,
            )
        else:
            result = action_verify(binding)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except BuildError as error:
        print(
            json.dumps({"status": "FAILED", "error": str(error)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
