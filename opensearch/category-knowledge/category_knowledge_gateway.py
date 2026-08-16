#!/usr/bin/env python3
"""Build and serve the independent CategoryInsight knowledge index."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

APP_SCHEMA = "glodex.category-insight-gateway.v1"
BINDING_SCHEMA = "glodex.category-card-binding.v1"
INDEX_SCHEMA = "glodex.category-card-index.v1"
CARD_SCHEMA = "glodex.category-card.v1"
MANIFEST_SCHEMA = "glodex.category-card-manifest.v1"
DATA_MODE = "SYNTHETIC_INTERVIEW"
GENERATOR_VERSION = "category-card-electronics-v1"
DIMENSION = 1024
QUICK_RECALL_DEPTH = 8
DEEP_RECALL_DEPTH = 15
RERANK_DEPTH = 8
MAX_QUERY_CHARACTERS = 128
MAX_MODEL_TEXT_CHARACTERS = 2000
MAX_SUMMARY_CHARACTERS = 200
MAX_RERANK_TEXT_CHARACTERS = 256
MAX_RERANK_REQUEST_BYTES = 32 * 1024
MAX_RESPONSE_BYTES = 512 * 1024
RERANK_TIMEOUT_SECONDS = 3.0
MIN_CATEGORY_CONFIDENCE = Decimal("0.5")
MIN_RERANK_SCORE = 0.0001
MIN_RERANK_MARGIN_RATIO = 0.20
QUERY_TEMPLATE_VERSION = "electronics-category-card-query-v1"


class CategoryKnowledgeError(RuntimeError):
    """Reject an unproven category-knowledge runtime."""


class CategoryKnowledgeModelMismatch(CategoryKnowledgeError):
    """Reject a serving model that differs from the indexed model."""


@dataclass(frozen=True, slots=True)
class ModelIdentity:
    manifest_digest: str
    embedding_model: str
    reranker_model: str
    dimension: int


@dataclass(frozen=True, slots=True)
class RankedCategory:
    card_id: str
    score: float


@dataclass(frozen=True, slots=True)
class CategoryRanking:
    coarse_candidates: tuple[str, ...]
    candidates: tuple[RankedCategory, ...]
    selected_card_id: str | None
    route: str


def _json_request(
    base_url: str,
    method: str,
    path: str,
    payload: object | None = None,
    *,
    timeout: float = 120.0,
) -> dict[str, Any]:
    body = None
    headers: dict[str, str] = {}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base_url + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise CategoryKnowledgeError(
            f"loopback dependency request failed: {method} {path}: {error}"
        ) from error
    if len(raw) > MAX_RESPONSE_BYTES:
        raise CategoryKnowledgeError("loopback dependency response is too large")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CategoryKnowledgeError("loopback dependency returned invalid JSON") from error
    if type(value) is not dict:
        raise CategoryKnowledgeError("loopback dependency response must be an object")
    return value


def _delete(base_url: str, path: str) -> None:
    request = urllib.request.Request(base_url + path, method="DELETE")
    try:
        with urllib.request.urlopen(request, timeout=30.0):
            return
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise CategoryKnowledgeError("OpenSearch delete failed") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise CategoryKnowledgeError("OpenSearch delete failed") from error


def _alias_indexes(base_url: str, alias: str) -> tuple[str, ...]:
    request = urllib.request.Request(
        base_url + "/_alias/" + urllib.parse.quote(alias, safe=""),
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30.0) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return ()
        raise CategoryKnowledgeError("OpenSearch alias lookup failed") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise CategoryKnowledgeError("OpenSearch alias lookup failed") from error
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CategoryKnowledgeError("OpenSearch alias lookup returned invalid JSON") from error
    if type(value) is not dict or any(type(index) is not str for index in value):
        raise CategoryKnowledgeError("OpenSearch alias lookup returned invalid indexes")
    return tuple(value)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def _model_document(summary: object, raw_evidence: object) -> str:
    return (str(summary) + " " + str(raw_evidence))[:MAX_MODEL_TEXT_CHARACTERS]


def _flatten_text(value: object) -> list[str]:
    if type(value) is str:
        compact = " ".join(value.split())
        return [compact] if compact else []
    if type(value) in (int, float):
        return [str(value)]
    if type(value) is list:
        return [part for item in value for part in _flatten_text(item)]
    if type(value) is dict:
        return [
            part
            for key, item in value.items()
            if key not in {"why_popular", "notes"}
            for part in [str(key), *_flatten_text(item)]
        ]
    return []


def _bounded_text(parts: Sequence[str], *, limit: int) -> str:
    selected: list[str] = []
    used = 0
    for part in dict.fromkeys(part for part in parts if part):
        required = len(part) + int(bool(selected))
        if used + required <= limit:
            selected.append(part)
            used += required
        elif not selected:
            return part[:limit].rstrip()
    return " ".join(selected)


def _rerank_document(rows: Sequence[dict[str, object]]) -> str:
    parts: list[str] = []
    for row in rows:
        for field in ("category", "semantic_profile", "recall_terms"):
            parts.extend(_flatten_text(row.get(field)))
    compact = _bounded_text(parts, limit=MAX_RERANK_TEXT_CHARACTERS)
    if not compact:
        raise CategoryKnowledgeError("category rerank document is empty")
    return compact


def build_documents(cards_path: Path) -> list[dict[str, object]]:
    try:
        lines = cards_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise CategoryKnowledgeError("Category Card JSONL is unreadable") from error
    if not lines:
        raise CategoryKnowledgeError("Category Card JSONL is empty")
    documents: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for line_number, line in enumerate(lines, start=1):
        try:
            card = json.loads(line)
        except json.JSONDecodeError as error:
            raise CategoryKnowledgeError(
                f"Category Card JSONL line {line_number} is invalid"
            ) from error
        _validate_card(card)
        card_id = str(card["card_id"])
        if card_id in seen_ids:
            raise CategoryKnowledgeError("Category Card IDs must be unique")
        seen_ids.add(card_id)
        metadata, _payload = _card_evidence(card)
        documents.append(
            {
                "document_id": card_id,
                "card_id": card_id,
                "category_key": metadata["category_id"],
                "category": card["category"],
                "semantic_profile": metadata["semantic_profile"],
                "recall_terms": metadata["recall_terms"],
                "card_type": card["card_type"],
                "summary": card["summary"],
                "raw_evidence": card["raw_evidence"],
                "last_updated": card["last_updated"],
                "confidence": card["confidence"],
            }
        )
    return documents


def _validate_card(value: object) -> None:
    required = {
        "card_id",
        "category",
        "card_type",
        "summary",
        "raw_evidence",
        "last_updated",
        "confidence",
    }
    if type(value) is not dict or set(value) != required:
        raise CategoryKnowledgeError("Category Card schema is invalid")
    for field in ("card_id", "category", "summary", "last_updated"):
        if type(value[field]) is not str or not value[field] or "\0" in value[field]:
            raise CategoryKnowledgeError("Category Card text is invalid")
    if value["card_type"] not in {"bestseller", "attribute", "price_range"}:
        raise CategoryKnowledgeError("Category Card type is invalid")
    if len(value["summary"]) > MAX_SUMMARY_CHARACTERS:
        raise CategoryKnowledgeError("Category Card summary is too long")
    evidence = value["raw_evidence"]
    if (
        type(evidence) is not list
        or len(evidence) != 2
        or any(type(item) is not str or not item for item in evidence)
    ):
        raise CategoryKnowledgeError("Category Card evidence is invalid")
    confidence = Decimal(str(value["confidence"]))
    if not confidence.is_finite() or not MIN_CATEGORY_CONFIDENCE <= confidence <= Decimal(1):
        raise CategoryKnowledgeError("Category Card confidence is invalid")
    if _contains_nul(value):
        raise CategoryKnowledgeError("Category Card contains NUL")
    _card_evidence(value)


def _card_evidence(card: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    evidence = card.get("raw_evidence")
    if type(evidence) is not list or len(evidence) != 2:
        raise CategoryKnowledgeError("Category Card evidence is invalid")
    try:
        metadata = json.loads(str(evidence[0]))
        payload = json.loads(str(evidence[1]))
    except json.JSONDecodeError as error:
        raise CategoryKnowledgeError("Category Card evidence JSON is invalid") from error
    if (
        type(metadata) is not dict
        or metadata.get("kind") != "category_metadata"
        or metadata.get("schema") != CARD_SCHEMA
        or metadata.get("data_mode") != DATA_MODE
        or metadata.get("generator_version") != GENERATOR_VERSION
        or type(metadata.get("category_id")) is not str
        or type(metadata.get("semantic_profile")) is not str
        or type(metadata.get("recall_terms")) is not list
        or not metadata["recall_terms"]
        or any(type(item) is not str or not item for item in metadata["recall_terms"])
        or type(payload) is not dict
    ):
        raise CategoryKnowledgeError("Category Card evidence contract is invalid")
    expected_kind = {
        "bestseller": "bestseller_payload",
        "attribute": "attribute_payload",
        "price_range": "price_range_payload",
    }.get(str(card.get("card_type")))
    if payload.get("kind") != expected_kind:
        raise CategoryKnowledgeError("Category Card payload type is invalid")
    return metadata, payload


def _contains_nul(value: object) -> bool:
    if type(value) is str:
        return "\0" in value
    if type(value) is list:
        return any(_contains_nul(item) for item in value)
    if type(value) is dict:
        return any(_contains_nul(key) or _contains_nul(item) for key, item in value.items())
    return False


def _model_identity(endpoint: str) -> ModelIdentity:
    response = _json_request(endpoint, "GET", "/v1/health")
    manifest_digest = response.get("manifestDigest")
    embedding_model = response.get("embeddingModel")
    reranker_model = response.get("rerankerModel")
    dimension = response.get("dimension")
    if (
        type(manifest_digest) is not str
        or re.fullmatch(r"[0-9a-f]{64}", manifest_digest) is None
        or type(embedding_model) is not str
        or not embedding_model
        or type(reranker_model) is not str
        or not reranker_model
        or dimension != DIMENSION
    ):
        raise CategoryKnowledgeModelMismatch("retrieval model identity is invalid")
    return ModelIdentity(
        manifest_digest=manifest_digest,
        embedding_model=embedding_model,
        reranker_model=reranker_model,
        dimension=dimension,
    )


def _embed(
    endpoint: str,
    texts: Sequence[str],
    *,
    manifest_digest: str,
) -> list[list[float]]:
    response = _json_request(endpoint, "POST", "/v1/embed", {"texts": list(texts)})
    if response.get("manifestDigest") != manifest_digest:
        raise CategoryKnowledgeModelMismatch("embedding model identity changed")
    rows = response.get("embeddings")
    if type(rows) is not list or len(rows) != len(texts):
        raise CategoryKnowledgeError("embedding response size is invalid")
    result: list[list[float]] = []
    for row in rows:
        if (
            type(row) is not list
            or len(row) != DIMENSION
            or any(type(value) not in (int, float) or not math.isfinite(value) for value in row)
        ):
            raise CategoryKnowledgeError("embedding vector is invalid")
        result.append([float(value) for value in row])
    return result


def _bulk(opensearch: str, index: str, documents: Sequence[dict[str, object]]) -> None:
    chunks: list[bytes] = []
    for document in documents:
        chunks.append(_canonical({"index": {"_index": index, "_id": document["document_id"]}}))
        chunks.append(_canonical(document))
    raw = b"\n".join(chunks) + b"\n"
    request = urllib.request.Request(
        opensearch + "/_bulk?refresh=true",
        data=raw,
        headers={"Content-Type": "application/x-ndjson"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180.0) as response:
            result = json.loads(response.read(MAX_RESPONSE_BYTES + 1))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise CategoryKnowledgeError("category knowledge bulk indexing failed") from error
    if type(result) is not dict or result.get("errors") is not False:
        raise CategoryKnowledgeError("category knowledge bulk indexing contained errors")


def build_index(args: argparse.Namespace) -> None:
    manifest = _load_manifest(args.manifest)
    documents = build_documents(args.cards)
    if manifest["card_count"] != len(documents):
        raise CategoryKnowledgeError("Category Card manifest count differs")
    supported_scope_terms = manifest["supported_scope_terms"]
    unsupported_terms = manifest["unsupported_terms"]
    model = _model_identity(args.retrieval_model_url)
    for offset in range(0, len(documents), 8):
        batch = documents[offset : offset + 8]
        texts = [_model_document(item["summary"], item["raw_evidence"]) for item in batch]
        vectors = _embed(
            args.retrieval_model_url,
            texts,
            manifest_digest=model.manifest_digest,
        )
        for document, vector in zip(batch, vectors, strict=True):
            document["content_vector"] = vector
    vector_payload_sha256, index_version = _index_identity(documents, model)
    physical_index = args.alias + "-cards-v1-" + index_version[:12]
    pipeline = args.alias + "-cards-hybrid-v1"
    previous_indexes = _alias_indexes(args.opensearch, args.alias)
    _delete(args.opensearch, "/" + urllib.parse.quote(physical_index, safe=""))
    mapping = {
        "settings": {"index": {"knn": True, "number_of_shards": 1, "number_of_replicas": 0}},
        "mappings": {
            "_meta": {
                "schema_version": INDEX_SCHEMA,
                "index_version": index_version,
                "model_manifest_digest": model.manifest_digest,
                "query_template_version": QUERY_TEMPLATE_VERSION,
                "vector_payload_sha256": vector_payload_sha256,
            },
            "dynamic": "strict",
            "properties": {
                "document_id": {"type": "keyword"},
                "card_id": {"type": "keyword"},
                "category_key": {"type": "keyword"},
                "category": {"type": "text"},
                "semantic_profile": {"type": "text"},
                "recall_terms": {"type": "text"},
                "card_type": {"type": "keyword"},
                "summary": {"type": "text"},
                "raw_evidence": {"type": "text"},
                "last_updated": {"type": "date"},
                "confidence": {"type": "scaled_float", "scaling_factor": 10000},
                "content_vector": {
                    "type": "knn_vector",
                    "dimension": DIMENSION,
                    "method": {"name": "hnsw", "space_type": "cosinesimil", "engine": "lucene"},
                },
            },
        },
    }
    _json_request(args.opensearch, "PUT", "/" + physical_index, mapping)
    _json_request(
        args.opensearch,
        "PUT",
        "/_search/pipeline/" + pipeline,
        {
            "description": "CategoryInsight Hybrid KNN plus BM25",
            "phase_results_processors": [
                {
                    "normalization-processor": {
                        "normalization": {"technique": "min_max"},
                        "combination": {
                            "technique": "arithmetic_mean",
                            "parameters": {"weights": [0.65, 0.35]},
                        },
                    }
                }
            ],
        },
    )
    _bulk(args.opensearch, physical_index, documents)
    count = _json_request(args.opensearch, "GET", "/" + physical_index + "/_count")
    if count.get("count") != len(documents):
        raise CategoryKnowledgeError("category knowledge count verification failed")
    _json_request(
        args.opensearch,
        "POST",
        "/_aliases",
        {
            "actions": [
                {"remove": {"index": args.alias + "-*", "alias": args.alias, "must_exist": False}},
                {"add": {"index": physical_index, "alias": args.alias, "is_write_index": False}},
            ]
        },
    )
    _json_request(args.opensearch, "PUT", "/" + physical_index + "/_block/write")
    binding = {
        "schema_version": BINDING_SCHEMA,
        "index_alias": args.alias,
        "physical_index": physical_index,
        "pipeline_id": pipeline,
        "index_version": index_version,
        "document_count": len(documents),
        "opensearch_endpoint": args.opensearch,
        "retrieval_model_endpoint": args.retrieval_model_url,
        "model_manifest_digest": model.manifest_digest,
        "embedding_model": model.embedding_model,
        "reranker_model": model.reranker_model,
        "embedding_dimension": model.dimension,
        "query_template_version": QUERY_TEMPLATE_VERSION,
        "vector_payload_sha256": vector_payload_sha256,
        "supported_scope_terms": supported_scope_terms,
        "unsupported_terms": unsupported_terms,
    }
    args.binding.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.binding.with_suffix(args.binding.suffix + ".tmp")
    temporary.write_bytes(_canonical(binding) + b"\n")
    temporary.replace(args.binding)
    for previous_index in previous_indexes:
        if previous_index != physical_index:
            _delete(args.opensearch, "/" + urllib.parse.quote(previous_index, safe=""))
    print(
        "CATEGORY_KNOWLEDGE_BUILT documents=" + str(len(documents)) + " index=" + physical_index,
        flush=True,
    )


def _load_manifest(path: Path) -> dict[str, object]:
    try:
        source = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CategoryKnowledgeError("Category Card manifest is unreadable") from error
    required = {
        "schema_version",
        "card_schema",
        "data_mode",
        "generator_version",
        "source_artifacts",
        "snapshot_version",
        "last_updated",
        "card_count",
        "supported_scope_terms",
        "unsupported_terms",
    }
    if (
        type(source) is not dict
        or set(source) != required
        or source.get("schema_version") != MANIFEST_SCHEMA
        or source.get("card_schema") != CARD_SCHEMA
        or source.get("data_mode") != DATA_MODE
        or source.get("generator_version") != GENERATOR_VERSION
        or type(source.get("card_count")) is not int
        or source["card_count"] < 1
    ):
        raise CategoryKnowledgeError("Category Card manifest schema is invalid")
    for field in ("supported_scope_terms", "unsupported_terms"):
        terms = source[field]
        if (
            type(terms) is not list
            or not terms
            or len(terms) != len(set(terms))
            or any(type(term) is not str or len(term.strip()) < 2 for term in terms)
        ):
            raise CategoryKnowledgeError("Category Card scope terms are invalid")
    return source


def _index_identity(
    documents: list[dict[str, object]],
    model: ModelIdentity,
) -> tuple[str, str]:
    if not documents or any("content_vector" not in document for document in documents):
        raise CategoryKnowledgeError("category vectors are incomplete")
    vector_payload_sha256 = hashlib.sha256(
        _canonical([document["content_vector"] for document in documents])
    ).hexdigest()
    index_version = hashlib.sha256(
        _canonical(
            {
                "documents": documents,
                "model_manifest_digest": model.manifest_digest,
                "query_template_version": QUERY_TEMPLATE_VERSION,
            }
        )
    ).hexdigest()
    return vector_payload_sha256, index_version


@dataclass(frozen=True, slots=True)
class Runtime:
    alias: str
    physical_index: str
    pipeline: str
    index_version: str
    document_count: int
    opensearch: str
    retrieval_model: str
    model_manifest_digest: str
    embedding_model: str
    reranker_model: str
    embedding_dimension: int
    query_template_version: str
    vector_payload_sha256: str
    supported_scope_terms: tuple[str, ...]
    unsupported_terms: tuple[str, ...]

    @classmethod
    def load(cls, path: Path) -> Runtime:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CategoryKnowledgeError("category knowledge binding is unreadable") from error
        required = {
            "schema_version",
            "index_alias",
            "physical_index",
            "pipeline_id",
            "index_version",
            "document_count",
            "opensearch_endpoint",
            "retrieval_model_endpoint",
            "model_manifest_digest",
            "embedding_model",
            "reranker_model",
            "embedding_dimension",
            "query_template_version",
            "vector_payload_sha256",
            "supported_scope_terms",
            "unsupported_terms",
        }
        if (
            type(value) is not dict
            or set(value) != required
            or value.get("schema_version") != BINDING_SCHEMA
        ):
            raise CategoryKnowledgeError("category knowledge binding is invalid")
        for field in ("supported_scope_terms", "unsupported_terms"):
            terms = value[field]
            if (
                type(terms) is not list
                or not terms
                or len(terms) != len(set(terms))
                or any(type(term) is not str or len(term.strip()) < 2 for term in terms)
            ):
                raise CategoryKnowledgeError("category knowledge scope terms are invalid")
        runtime = cls(
            alias=value["index_alias"],
            physical_index=value["physical_index"],
            pipeline=value["pipeline_id"],
            index_version=value["index_version"],
            document_count=value["document_count"],
            opensearch=value["opensearch_endpoint"],
            retrieval_model=value["retrieval_model_endpoint"],
            model_manifest_digest=value["model_manifest_digest"],
            embedding_model=value["embedding_model"],
            reranker_model=value["reranker_model"],
            embedding_dimension=value["embedding_dimension"],
            query_template_version=value["query_template_version"],
            vector_payload_sha256=value["vector_payload_sha256"],
            supported_scope_terms=tuple(value["supported_scope_terms"]),
            unsupported_terms=tuple(value["unsupported_terms"]),
        )
        model = _model_identity(runtime.retrieval_model)
        if (
            model.manifest_digest != runtime.model_manifest_digest
            or model.embedding_model != runtime.embedding_model
            or model.reranker_model != runtime.reranker_model
            or model.dimension != runtime.embedding_dimension
            or runtime.query_template_version != QUERY_TEMPLATE_VERSION
        ):
            raise CategoryKnowledgeModelMismatch("category knowledge model binding differs")
        count = _json_request(runtime.opensearch, "GET", "/" + runtime.alias + "/_count")
        aliases = _json_request(runtime.opensearch, "GET", "/_alias/" + runtime.alias)
        if count.get("count") != runtime.document_count or set(aliases) != {runtime.physical_index}:
            raise CategoryKnowledgeError("category knowledge alias identity differs")
        mapping = _json_request(
            runtime.opensearch,
            "GET",
            "/" + runtime.physical_index + "/_mapping",
        )
        try:
            metadata = mapping[runtime.physical_index]["mappings"]["_meta"]
        except (KeyError, TypeError) as error:
            raise CategoryKnowledgeError("category knowledge mapping identity is absent") from error
        if metadata != {
            "schema_version": INDEX_SCHEMA,
            "index_version": runtime.index_version,
            "model_manifest_digest": runtime.model_manifest_digest,
            "query_template_version": runtime.query_template_version,
            "vector_payload_sha256": runtime.vector_payload_sha256,
        }:
            raise CategoryKnowledgeError("category knowledge mapping identity differs")
        return runtime

    def _search(self, category: str, depth: str) -> list[dict[str, object]]:
        recall_depth = _recall_depth(depth)
        vector = _embed(
            self.retrieval_model,
            ["[query_language=zh] " + category],
            manifest_digest=self.model_manifest_digest,
        )[0]
        body = {
            "size": recall_depth,
            "_source": [
                "card_id",
                "category_key",
                "category",
                "semantic_profile",
                "recall_terms",
                "card_type",
                "summary",
                "raw_evidence",
                "confidence",
            ],
            "query": {
                "hybrid": {
                    "queries": [
                        {"knn": {"content_vector": {"vector": vector, "k": recall_depth}}},
                        {
                            "multi_match": {
                                "query": category,
                                "fields": [
                                    "category^4",
                                    "recall_terms^4",
                                    "semantic_profile^3",
                                    "summary^2",
                                    "raw_evidence",
                                ],
                            }
                        },
                    ]
                }
            },
        }
        response = _json_request(
            self.opensearch,
            "POST",
            "/" + self.alias + "/_search?search_pipeline=" + self.pipeline,
            body,
        )
        hits = response.get("hits")
        if type(hits) is not dict or type(hits.get("hits")) is not list:
            raise CategoryKnowledgeError("category knowledge search response is invalid")
        rows: list[dict[str, object]] = []
        for hit in hits["hits"]:
            if type(hit) is not dict or type(hit.get("_source")) is not dict:
                raise CategoryKnowledgeError("category knowledge hit is invalid")
            row = dict(hit["_source"])
            row["_score"] = hit.get("_score")
            rows.append(row)
        return rows

    def _rerank(
        self,
        category: str,
        rows: list[dict[str, object]],
    ) -> tuple[RankedCategory, ...]:
        groups = _group_candidate_rows(rows)
        documents = [_rerank_document(group) for group in groups]
        payload = {"query": category, "documents": documents}
        if len(_canonical(payload)) > MAX_RERANK_REQUEST_BYTES:
            raise CategoryKnowledgeError("category rerank request is too large")
        response = _json_request(
            self.retrieval_model,
            "POST",
            "/v1/rerank",
            payload,
            timeout=RERANK_TIMEOUT_SECONDS,
        )
        if response.get("manifestDigest") != self.model_manifest_digest:
            raise CategoryKnowledgeModelMismatch("reranker model identity changed")
        scores = response.get("scores")
        if type(scores) is not list or len(scores) != len(groups):
            raise CategoryKnowledgeError("category knowledge rerank response is invalid")
        parsed: list[float] = []
        for score in scores:
            try:
                parsed_score = float(score)
            except (TypeError, ValueError) as error:
                raise CategoryKnowledgeError("category rerank score is invalid") from error
            if not math.isfinite(parsed_score):
                raise CategoryKnowledgeError("category rerank score is invalid")
            parsed.append(parsed_score)
        ranked = [
            RankedCategory(card_id=str(groups[index][0]["category_key"]), score=parsed[index])
            for index in sorted(range(len(groups)), key=lambda index: (-parsed[index], index))
        ]
        return tuple(ranked[:RERANK_DEPTH])

    def rank_categories(self, category: str, depth: str) -> CategoryRanking:
        rows = self._search(category, depth)
        return self._rank_rows(category, rows)

    def _rank_rows(
        self,
        category: str,
        rows: list[dict[str, object]],
    ) -> CategoryRanking:
        if not rows:
            return CategoryRanking(
                coarse_candidates=(),
                candidates=(),
                selected_card_id=None,
                route="EMPTY",
            )
        coarse_candidates = tuple(
            str(group[0]["category_key"]) for group in _group_candidate_rows(rows)
        )
        exact_card_id = _exact_alias_card_id(category, rows)
        if exact_card_id is not None:
            return CategoryRanking(
                coarse_candidates=coarse_candidates,
                candidates=(RankedCategory(card_id=exact_card_id, score=1.0),),
                selected_card_id=exact_card_id,
                route="EXACT_ALIAS",
            )
        if _contains_unsupported_term(category, self.unsupported_terms):
            return CategoryRanking(
                coarse_candidates=coarse_candidates,
                candidates=(),
                selected_card_id=None,
                route="OUT_OF_SCOPE",
            )
        if not _contains_supported_scope_term(category, self.supported_scope_terms):
            return CategoryRanking(
                coarse_candidates=coarse_candidates,
                candidates=(),
                selected_card_id=None,
                route="OUT_OF_SCOPE",
            )
        try:
            candidates = self._rerank(category, rows)
        except CategoryKnowledgeModelMismatch:
            raise
        except CategoryKnowledgeError:
            return CategoryRanking(
                coarse_candidates=coarse_candidates,
                candidates=(),
                selected_card_id=None,
                route="RERANK_UNAVAILABLE",
            )
        return CategoryRanking(
            coarse_candidates=coarse_candidates,
            candidates=candidates,
            selected_card_id=_confident_rerank_card_id(candidates),
            route="RERANK",
        )

    def retrieve(self, category: str, depth: str) -> dict[str, object]:
        rows = self._search(category, depth)
        selected_category = self._rank_rows(category, rows).selected_card_id
        if selected_category is None:
            return _empty_insight(category)
        selected_rows = [row for row in rows if row.get("category_key") == selected_category]
        if not selected_rows:
            raise CategoryKnowledgeError("selected Category Cards are absent")
        component_limit = _component_limit(depth)
        components: list[object] = []
        bestsellers: list[object] = []
        attributes: list[object] = []
        price_tiers: list[object] = []
        confidences: list[Decimal] = []
        for row in selected_rows:
            _metadata, payload = _card_evidence(row)
            confidences.append(Decimal(str(row.get("confidence", "0"))))
            card_type = row.get("card_type")
            fields: tuple[tuple[list[object], str, int], ...]
            if card_type == "bestseller":
                fields = (
                    (components, "components", component_limit),
                    (bestsellers, "bestsellers", 5),
                )
            elif card_type == "attribute" and depth == "deep":
                fields = ((attributes, "attributes", 12),)
            elif card_type == "price_range":
                fields = ((price_tiers, "price_tiers", 3),)
            else:
                fields = ()
            for target, field, limit in fields:
                values = payload.get(field, [])
                if type(values) is not list:
                    raise CategoryKnowledgeError("Category Card payload collection is invalid")
                for value in values:
                    if value not in target and len(target) < limit:
                        target.append(value)
        confidence = sum(confidences, Decimal(0)) / Decimal(len(confidences))
        if (
            not any((components, bestsellers, attributes, price_tiers))
            or confidence < MIN_CATEGORY_CONFIDENCE
        ):
            raise CategoryKnowledgeError("Category Cards contain no usable knowledge")
        return {
            "status": "FOUND",
            "category": category,
            "components": components,
            "bestsellers": bestsellers,
            "attributes": attributes,
            "price_tiers": price_tiers,
            "confidence": str(confidence),
        }


def _group_candidate_rows(
    rows: Sequence[dict[str, object]],
) -> list[list[dict[str, object]]]:
    groups: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        category_key = row.get("category_key")
        if type(category_key) is not str or not category_key:
            raise CategoryKnowledgeError("category candidate key is invalid")
        groups.setdefault(category_key, []).append(row)
    return list(groups.values())


def _exact_alias_card_id(category: str, rows: Sequence[dict[str, object]]) -> str | None:
    query = _normalize_term(category)
    if not query:
        return None
    matches: list[tuple[int, str]] = []
    for row in rows:
        card_id = row.get("category_key")
        terms = row.get("recall_terms")
        if type(card_id) is not str or type(terms) is not list:
            continue
        for term in terms:
            if type(term) is not str:
                continue
            normalized = _normalize_term(term)
            if len(normalized) >= 2 and normalized == query:
                matches.append((len(normalized), card_id))
    if not matches:
        return None
    longest = max(length for length, _card_id in matches)
    card_ids = {card_id for length, card_id in matches if length == longest}
    if len(card_ids) != 1:
        return None
    return next(iter(card_ids))


def _confident_rerank_card_id(candidates: Sequence[RankedCategory]) -> str | None:
    if not candidates or candidates[0].score < MIN_RERANK_SCORE:
        return None
    if len(candidates) > 1:
        margin = candidates[0].score - candidates[1].score
        if margin < candidates[0].score * MIN_RERANK_MARGIN_RATIO:
            return None
    return candidates[0].card_id


def _empty_insight(category: str) -> dict[str, object]:
    return {
        "status": "NO_INSIGHT",
        "category": category,
        "components": [],
        "bestsellers": [],
        "attributes": [],
        "price_tiers": [],
        "confidence": "0",
    }


def _normalize_term(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _contains_unsupported_term(category: str, terms: Sequence[str]) -> bool:
    query = _normalize_term(category)
    return any(
        (normalized := _normalize_term(term))
        and len(normalized) >= 2
        and query.endswith(normalized)
        for term in terms
    )


def _contains_supported_scope_term(category: str, terms: Sequence[str]) -> bool:
    query = _normalize_term(category)
    return any(
        (normalized := _normalize_term(term)) and len(normalized) >= 2 and normalized in query
        for term in terms
    )


def _component_limit(depth: str) -> int:
    if depth == "quick":
        return 3
    if depth == "deep":
        return 8
    raise CategoryKnowledgeError("category insight depth is invalid")


def _recall_depth(depth: str) -> int:
    if depth == "quick":
        return QUICK_RECALL_DEPTH
    if depth == "deep":
        return DEEP_RECALL_DEPTH
    raise CategoryKnowledgeError("category insight depth is invalid")


def create_app(runtime: Runtime) -> Any:
    try:
        from fastapi import Body, FastAPI, HTTPException
    except ImportError as error:
        raise CategoryKnowledgeError("FastAPI is required") from error
    app = FastAPI(
        title="Glodex CategoryInsight Gateway",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/v1/health")
    async def health() -> dict[str, object]:
        return {
            "schema_version": APP_SCHEMA,
            "status": "ready",
            "index_alias": runtime.alias,
            "index_version": runtime.index_version,
            "document_count": runtime.document_count,
        }

    @app.post("/v1/category-insight")
    async def category_insight(
        request: dict[str, object] = Body(...),  # noqa: B008
    ) -> dict[str, object]:
        if type(request) is not dict or set(request) != {"category", "depth"}:
            raise HTTPException(status_code=422, detail="INVALID_CATEGORY_INSIGHT_REQUEST")
        category = request.get("category")
        depth = request.get("depth")
        if (
            type(category) is not str
            or not category.strip()
            or "\0" in category
            or len(category) > MAX_QUERY_CHARACTERS
            or depth not in {"quick", "deep"}
        ):
            raise HTTPException(status_code=422, detail="INVALID_CATEGORY_INSIGHT_REQUEST")
        try:
            insight = await asyncio.to_thread(runtime.retrieve, " ".join(category.split()), depth)
        except CategoryKnowledgeError as error:
            raise HTTPException(status_code=503, detail="CATEGORY_INSIGHT_UNAVAILABLE") from error
        return {"schema_version": APP_SCHEMA, "insight": insight}

    return app


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--cards", type=Path, required=True)
    build.add_argument("--manifest", type=Path, required=True)
    build.add_argument("--binding", type=Path, required=True)
    build.add_argument("--alias", default="glodex-category-knowledge")
    build.add_argument("--opensearch", default="http://127.0.0.1:9200")
    build.add_argument("--retrieval-model-url", default="http://127.0.0.1:18000")
    serve = commands.add_parser("serve")
    serve.add_argument("--binding", type=Path, required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=18087)
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    if args.command == "build":
        build_index(args)
        return 0
    if args.host != "127.0.0.1" or args.port != 18087:
        raise CategoryKnowledgeError("CategoryInsight Gateway must bind 127.0.0.1:18087")
    runtime = Runtime.load(args.binding)
    try:
        import uvicorn
    except ImportError as error:
        raise CategoryKnowledgeError("uvicorn is required") from error
    uvicorn.run(create_app(runtime), host=args.host, port=args.port, access_log=False)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run(parse_args(argv))
    except CategoryKnowledgeError as error:
        print("CATEGORY_KNOWLEDGE_FAILED: " + str(error), file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
