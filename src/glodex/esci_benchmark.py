"""Read and evaluate the fixed, offline ESCI benchmark artifact.

This module is deliberately independent from Glodex's shopping runtime.  It
only accepts the versioned JSONL artifact produced by
``scripts/build_esci_benchmark.py`` and never downloads data, reads
configuration, or contacts a network service.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final, Never

ARTIFACT_SCHEMA_VERSION: Final = "esci-benchmark-artifact-v1"
BENCHMARK_ID: Final = "esci-small-us-v1"
SCORER_VERSION: Final = "esci-bm25-v1"
_BUILDER_VERSION: Final = "esci-builder-v1"
_LICENSE: Final = "Apache-2.0"
_SOURCE_REPOSITORY: Final = "https://github.com/amazon-science/esci-data"
_SOURCE_FILE_PATHS: Final[tuple[str, ...]] = (
    "shopping_queries_dataset/shopping_queries_dataset_examples.parquet",
    "shopping_queries_dataset/shopping_queries_dataset_products.parquet",
    "shopping_queries_dataset/shopping_queries_dataset_sources.csv",
)
_EXPECTED_FILES: Final[frozenset[str]] = frozenset(
    {
        "ATTRIBUTION.md",
        "LICENSE",
        "NOTICE",
        "judgements.jsonl",
        "manifest.json",
        "products.jsonl",
        "queries.jsonl",
    }
)
_HASHED_FILES: Final[frozenset[str]] = _EXPECTED_FILES - {"manifest.json"}
_LABELS: Final[tuple[str, ...]] = (
    "Exact",
    "Substitute",
    "Complement",
    "Irrelevant",
)
_LABEL_GAINS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "Exact": 3,
        "Substitute": 2,
        "Complement": 1,
        "Irrelevant": 0,
    }
)
_MAX_ARTIFACT_BYTES: Final = 20 * 1024 * 1024
_MAX_QUERIES: Final = 500
_MAX_CANDIDATES_PER_QUERY: Final = 20
_TOP_K: Final = 10
_K1: Final = 1.2
_B: Final = 0.75
_SELECTION_SEED: Final = "glodex-esci-small-us-v1"
_TOKEN_PATTERN: Final = re.compile(r"[a-z0-9]+")
_HEX_40: Final = re.compile(r"[0-9a-f]{40}\Z")
_HEX_64: Final = re.compile(r"[0-9a-f]{64}\Z")
_QUERY_SELECTION: Final = "sha256(seed + \\0 + query_id), then query_id; first 500"
_CANDIDATE_SELECTION: Final = "example_id, then product_id; first 20 per selected query"


class BenchmarkArtifactError(ValueError):
    """A safe, non-diagnostic failure while reading an ESCI artifact."""

    code: Final[str] = "BENCHMARK_ARTIFACT_INVALID"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class Query:
    """A query included in the immutable benchmark slice."""

    query_id: str
    query: str


@dataclass(frozen=True, slots=True)
class Product:
    """The permitted historical product-text fields for one candidate."""

    product_id: str
    product_locale: str
    product_title: str
    product_description: str
    product_bullet_point: str
    product_brand: str
    product_color: str


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    """One candidate-only ranking result; it intentionally has no label."""

    product_id: str
    score: float


@dataclass(frozen=True, slots=True)
class ArtifactManifest:
    """Safe manifest facts needed by the evaluator and public aggregate output."""

    benchmark_id: str
    source_repository: str
    source_revision: str
    scorer_version: str
    query_count: int
    product_count: int
    judgement_count: int
    label_distribution: Mapping[str, int]
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class CandidatePool:
    """Queries, text and fixed candidate IDs, deliberately without labels."""

    _artifact_root: Path
    manifest: ArtifactManifest
    queries: tuple[Query, ...]
    products: Mapping[str, Product]
    candidates_by_query: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class Judgements:
    """Offline labels loaded only after a candidate ranking is complete."""

    labels_by_query: Mapping[str, Mapping[str, str]]


@dataclass(frozen=True, slots=True)
class MetricSummary:
    """A metric with the explicit evaluation denominator and exclusions."""

    value: float
    denominator: int
    excluded: int


@dataclass(frozen=True, slots=True)
class EvaluationMetrics:
    """The frozen M1e top-ten metric set."""

    exact_at_10: MetricSummary
    mrr_at_10: MetricSummary
    ndcg_at_10: MetricSummary


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    """The safe aggregate evaluation result returned by this module."""

    manifest: ArtifactManifest
    metrics: EvaluationMetrics


@dataclass(frozen=True, slots=True)
class _ArtifactContext:
    root: Path
    manifest: ArtifactManifest


def _fail() -> Never:
    raise BenchmarkArtifactError


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _as_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        _fail()
    result: dict[str, object] = {}
    for key, item in value.items():
        if type(key) is not str:
            _fail()
        result[key] = item
    return result


def _as_list(value: object) -> list[object]:
    if not isinstance(value, list):
        _fail()
    return list(value)


def _as_string(value: object) -> str:
    if type(value) is not str:
        _fail()
    return value


def _as_nonnegative_integer(value: object) -> int:
    if type(value) is not int or value < 0:
        _fail()
    return value


def _require_exact_keys(value: Mapping[str, object], expected: frozenset[str]) -> None:
    if set(value) != expected:
        _fail()


def _read_utf8_lf(path: Path) -> tuple[bytes, str]:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        _fail()
    if not raw or not raw.endswith(b"\n") or b"\r" in raw or text.startswith("\ufeff"):
        _fail()
    return raw, text


def _validate_artifact_directory(root: Path) -> dict[str, Path]:
    try:
        if root.is_symlink() or not root.is_dir():
            _fail()
        entries = tuple(root.iterdir())
    except OSError:
        _fail()
    names = {entry.name for entry in entries}
    if names != _EXPECTED_FILES:
        _fail()
    paths: dict[str, Path] = {}
    total_bytes = 0
    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_file():
                _fail()
            size = entry.stat().st_size
        except OSError:
            _fail()
        if size < 1:
            _fail()
        total_bytes += size
        paths[entry.name] = entry
    if total_bytes > _MAX_ARTIFACT_BYTES:
        _fail()
    return paths


def _validate_sha256(value: object) -> str:
    digest = _as_string(value)
    if _HEX_64.fullmatch(digest) is None:
        _fail()
    return digest


def _validate_source(source_value: object) -> tuple[str, str]:
    source = _as_object(source_value)
    _require_exact_keys(source, frozenset({"files", "repository", "revision"}))
    repository = _as_string(source["repository"])
    revision = _as_string(source["revision"])
    if repository != _SOURCE_REPOSITORY or _HEX_40.fullmatch(revision) is None:
        _fail()

    files = _as_list(source["files"])
    if len(files) != len(_SOURCE_FILE_PATHS):
        _fail()
    for expected_path, record_value in zip(_SOURCE_FILE_PATHS, files, strict=True):
        record = _as_object(record_value)
        _require_exact_keys(record, frozenset({"bytes", "path", "sha256"}))
        if _as_string(record["path"]) != expected_path:
            _fail()
        if _as_nonnegative_integer(record["bytes"]) < 1:
            _fail()
        _validate_sha256(record["sha256"])
    return repository, revision


def _validate_selection(value: object) -> None:
    selection = _as_object(value)
    _require_exact_keys(
        selection,
        frozenset(
            {
                "candidate_selection",
                "candidates_per_query_limit",
                "locale",
                "query_limit",
                "query_selection",
                "seed",
                "small_version",
                "split",
            }
        ),
    )
    if (
        _as_string(selection["locale"]) != "us"
        or _as_string(selection["split"]) != "test"
        or _as_nonnegative_integer(selection["small_version"]) != 1
        or _as_string(selection["seed"]) != _SELECTION_SEED
        or _as_nonnegative_integer(selection["query_limit"]) != _MAX_QUERIES
        or _as_nonnegative_integer(selection["candidates_per_query_limit"])
        != _MAX_CANDIDATES_PER_QUERY
        or _as_string(selection["query_selection"]) != _QUERY_SELECTION
        or _as_string(selection["candidate_selection"]) != _CANDIDATE_SELECTION
    ):
        _fail()


def _validate_counts(value: object) -> tuple[int, int, int]:
    counts = _as_object(value)
    _require_exact_keys(counts, frozenset({"judgements", "products", "queries"}))
    query_count = _as_nonnegative_integer(counts["queries"])
    product_count = _as_nonnegative_integer(counts["products"])
    judgement_count = _as_nonnegative_integer(counts["judgements"])
    if not 1 <= query_count <= _MAX_QUERIES or product_count < 1 or judgement_count < query_count:
        _fail()
    return query_count, product_count, judgement_count


def _validate_label_distribution(value: object, *, judgement_count: int) -> Mapping[str, int]:
    distribution = _as_object(value)
    _require_exact_keys(distribution, frozenset(_LABELS))
    result = {label: _as_nonnegative_integer(distribution[label]) for label in _LABELS}
    if sum(result.values()) != judgement_count:
        _fail()
    return MappingProxyType(result)


def _validate_artifact_hashes(paths: Mapping[str, Path], value: object) -> None:
    records = _as_object(value)
    _require_exact_keys(records, _HASHED_FILES)
    for filename in sorted(_HASHED_FILES):
        record = _as_object(records[filename])
        _require_exact_keys(record, frozenset({"bytes", "sha256"}))
        expected_bytes = _as_nonnegative_integer(record["bytes"])
        expected_hash = _validate_sha256(record["sha256"])
        try:
            actual = paths[filename].read_bytes()
        except OSError:
            _fail()
        if len(actual) != expected_bytes or hashlib.sha256(actual).hexdigest() != expected_hash:
            _fail()


def _load_context(artifact_root: Path) -> _ArtifactContext:
    paths = _validate_artifact_directory(artifact_root)
    raw_manifest, manifest_text = _read_utf8_lf(paths["manifest.json"])
    try:
        parsed_manifest: object = json.loads(manifest_text)
    except json.JSONDecodeError:
        _fail()
    manifest = _as_object(parsed_manifest)
    _require_exact_keys(
        manifest,
        frozenset(
            {
                "artifact_files",
                "benchmark_id",
                "builder_version",
                "counts",
                "label_distribution",
                "license",
                "schema_version",
                "scorer_version",
                "selection",
                "source",
            }
        ),
    )
    if raw_manifest != (_canonical_json(manifest) + "\n").encode("utf-8"):
        _fail()
    if (
        _as_string(manifest["schema_version"]) != ARTIFACT_SCHEMA_VERSION
        or _as_string(manifest["benchmark_id"]) != BENCHMARK_ID
        or _as_string(manifest["builder_version"]) != _BUILDER_VERSION
        or _as_string(manifest["scorer_version"]) != SCORER_VERSION
        or _as_string(manifest["license"]) != _LICENSE
    ):
        _fail()
    source_repository, source_revision = _validate_source(manifest["source"])
    _validate_selection(manifest["selection"])
    query_count, product_count, judgement_count = _validate_counts(manifest["counts"])
    label_distribution = _validate_label_distribution(
        manifest["label_distribution"],
        judgement_count=judgement_count,
    )
    _validate_artifact_hashes(paths, manifest["artifact_files"])
    return _ArtifactContext(
        root=artifact_root,
        manifest=ArtifactManifest(
            benchmark_id=BENCHMARK_ID,
            source_repository=source_repository,
            source_revision=source_revision,
            scorer_version=SCORER_VERSION,
            query_count=query_count,
            product_count=product_count,
            judgement_count=judgement_count,
            label_distribution=label_distribution,
            manifest_sha256=hashlib.sha256(raw_manifest).hexdigest(),
        ),
    )


def _load_jsonl(path: Path) -> list[dict[str, object]]:
    _raw, text = _read_utf8_lf(path)
    lines = text[:-1].split("\n")
    if not lines:
        _fail()
    records: list[dict[str, object]] = []
    for line in lines:
        if not line:
            _fail()
        try:
            parsed: object = json.loads(line)
        except json.JSONDecodeError:
            _fail()
        record = _as_object(parsed)
        if line != _canonical_json(record):
            _fail()
        records.append(record)
    return records


def _nonblank_text(value: object) -> str:
    text = _as_string(value)
    if not text.strip():
        _fail()
    return text


def _load_queries(context: _ArtifactContext) -> tuple[Query, ...]:
    records = _load_jsonl(context.root / "queries.jsonl")
    queries: list[Query] = []
    previous_id: str | None = None
    for record in records:
        _require_exact_keys(record, frozenset({"query", "query_id"}))
        query_id = _nonblank_text(record["query_id"])
        if previous_id is not None and query_id <= previous_id:
            _fail()
        previous_id = query_id
        queries.append(Query(query_id=query_id, query=_nonblank_text(record["query"])))
    if len(queries) != context.manifest.query_count:
        _fail()
    return tuple(queries)


def _load_products(context: _ArtifactContext) -> Mapping[str, Product]:
    records = _load_jsonl(context.root / "products.jsonl")
    products: dict[str, Product] = {}
    previous_key: tuple[str, str] | None = None
    expected_keys = frozenset(
        {
            "product_brand",
            "product_bullet_point",
            "product_color",
            "product_description",
            "product_id",
            "product_locale",
            "product_title",
        }
    )
    for record in records:
        _require_exact_keys(record, expected_keys)
        product_id = _nonblank_text(record["product_id"])
        product_locale = _as_string(record["product_locale"])
        key = (product_id, product_locale)
        if product_locale != "us" or (previous_key is not None and key <= previous_key):
            _fail()
        previous_key = key
        if product_id in products:
            _fail()
        products[product_id] = Product(
            product_id=product_id,
            product_locale=product_locale,
            product_title=_nonblank_text(record["product_title"]),
            product_description=_as_string(record["product_description"]),
            product_bullet_point=_as_string(record["product_bullet_point"]),
            product_brand=_as_string(record["product_brand"]),
            product_color=_as_string(record["product_color"]),
        )
    if len(products) != context.manifest.product_count:
        _fail()
    return MappingProxyType(products)


def _load_candidate_records(context: _ArtifactContext) -> list[tuple[str, str]]:
    """Read only candidate links; deliberately do not inspect ESCI labels yet."""

    records = _load_jsonl(context.root / "judgements.jsonl")
    rows: list[tuple[str, str]] = []
    previous_key: tuple[str, str] | None = None
    for record in records:
        _require_exact_keys(record, frozenset({"label", "product_id", "query_id"}))
        query_id = _nonblank_text(record["query_id"])
        product_id = _nonblank_text(record["product_id"])
        key = (query_id, product_id)
        if previous_key is not None and key <= previous_key:
            _fail()
        previous_key = key
        rows.append((query_id, product_id))
    if len(rows) != context.manifest.judgement_count:
        _fail()
    return rows


def _load_judgement_records(context: _ArtifactContext) -> list[tuple[str, str, str]]:
    """Read labels only for post-ranking evaluation."""

    candidate_records = _load_candidate_records(context)
    records = _load_jsonl(context.root / "judgements.jsonl")
    rows: list[tuple[str, str, str]] = []
    for record, (query_id, product_id) in zip(records, candidate_records, strict=True):
        label = _as_string(record["label"])
        if label not in _LABELS:
            _fail()
        rows.append((query_id, product_id, label))
    return rows


def load_candidate_pool(artifact_root: Path) -> CandidatePool:
    """Load verified query/product/candidate data without exposing labels to the scorer."""

    context = _load_context(artifact_root)
    queries = _load_queries(context)
    products = _load_products(context)
    query_ids = {query.query_id for query in queries}
    candidates: dict[str, list[str]] = {query_id: [] for query_id in query_ids}
    for query_id, product_id in _load_candidate_records(context):
        if query_id not in candidates or product_id not in products:
            _fail()
        candidates[query_id].append(product_id)
    if set(candidates) != query_ids:
        _fail()
    immutable_candidates: dict[str, tuple[str, ...]] = {}
    for query_id in sorted(candidates):
        candidate_ids = tuple(candidates[query_id])
        if not 1 <= len(candidate_ids) <= _MAX_CANDIDATES_PER_QUERY:
            _fail()
        immutable_candidates[query_id] = candidate_ids
    candidate_count = sum(len(value) for value in immutable_candidates.values())
    if candidate_count != context.manifest.judgement_count:
        _fail()
    return CandidatePool(
        _artifact_root=context.root,
        manifest=context.manifest,
        queries=queries,
        products=products,
        candidates_by_query=MappingProxyType(immutable_candidates),
    )


def load_judgements(candidate_pool: CandidatePool) -> Judgements:
    """Load labels only after ranking and re-check the immutable artifact identity."""

    context = _load_context(candidate_pool._artifact_root)
    if context.manifest != candidate_pool.manifest:
        _fail()
    labels: dict[str, dict[str, str]] = {query.query_id: {} for query in candidate_pool.queries}
    for query_id, product_id, label in _load_judgement_records(context):
        expected_candidates = candidate_pool.candidates_by_query.get(query_id)
        if expected_candidates is None or product_id not in expected_candidates:
            _fail()
        if product_id in labels[query_id]:
            _fail()
        labels[query_id][product_id] = label
    if set(labels) != set(candidate_pool.candidates_by_query):
        _fail()
    immutable_labels: dict[str, Mapping[str, str]] = {}
    for query_id, candidates in candidate_pool.candidates_by_query.items():
        query_labels = labels[query_id]
        if set(query_labels) != set(candidates):
            _fail()
        immutable_labels[query_id] = MappingProxyType(query_labels)
    actual_distribution = Counter(
        label for query_labels in immutable_labels.values() for label in query_labels.values()
    )
    normalized_distribution = {label: actual_distribution[label] for label in _LABELS}
    if normalized_distribution != dict(candidate_pool.manifest.label_distribution):
        _fail()
    return Judgements(labels_by_query=MappingProxyType(immutable_labels))


def _tokens(text: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return tuple(_TOKEN_PATTERN.findall(normalized))


def _weighted_document_tokens(product: Product) -> tuple[str, ...]:
    return (
        _tokens(product.product_title) * 3
        + _tokens(product.product_description)
        + _tokens(product.product_bullet_point)
        + _tokens(product.product_brand)
        + _tokens(product.product_color)
    )


def _rank_query(query: Query, products: Sequence[Product]) -> tuple[RankedCandidate, ...]:
    """Rank exactly one supplied upstream candidate pool using fixed lexical BM25."""

    query_tokens = Counter(_tokens(query.query))
    document_terms = [
        (product, Counter(_weighted_document_tokens(product))) for product in products
    ]
    candidate_count = len(document_terms)
    if candidate_count == 0:
        raise ValueError("candidate pool must not be empty")
    average_length = (
        sum(sum(terms.values()) for _product, terms in document_terms) / candidate_count
    )
    document_frequency: Counter[str] = Counter()
    for _product, terms in document_terms:
        document_frequency.update(terms.keys())

    ranked: list[RankedCandidate] = []
    for product, terms in document_terms:
        document_length = sum(terms.values())
        score = 0.0
        for token, query_frequency in query_tokens.items():
            term_frequency = terms.get(token, 0)
            if term_frequency == 0:
                continue
            inverse_document_frequency = math.log(
                1.0
                + (candidate_count - document_frequency[token] + 0.5)
                / (document_frequency[token] + 0.5)
            )
            denominator = term_frequency + _K1 * (1.0 - _B + _B * document_length / average_length)
            score += (
                query_frequency
                * inverse_document_frequency
                * term_frequency
                * (_K1 + 1.0)
                / denominator
            )
        ranked.append(RankedCandidate(product_id=product.product_id, score=score))
    return tuple(sorted(ranked, key=lambda result: (-result.score, result.product_id)))


def rank_candidate_pool(candidate_pool: CandidatePool) -> Mapping[str, tuple[RankedCandidate, ...]]:
    """Rank every query only against its own fixed candidate IDs, without labels."""

    rankings: dict[str, tuple[RankedCandidate, ...]] = {}
    for query in candidate_pool.queries:
        candidate_ids = candidate_pool.candidates_by_query[query.query_id]
        products = tuple(candidate_pool.products[product_id] for product_id in candidate_ids)
        rankings[query.query_id] = _rank_query(query, products)
    return MappingProxyType(rankings)


def _validate_rankings(
    candidate_pool: CandidatePool,
    rankings: Mapping[str, Sequence[RankedCandidate]],
) -> None:
    if set(rankings) != set(candidate_pool.candidates_by_query):
        raise ValueError("rankings must cover exactly the benchmark queries")
    for query_id, expected_candidates in candidate_pool.candidates_by_query.items():
        ranking = rankings[query_id]
        actual_ids = tuple(candidate.product_id for candidate in ranking)
        if len(actual_ids) != len(expected_candidates) or set(actual_ids) != set(
            expected_candidates
        ):
            raise ValueError("ranking must contain exactly its upstream candidate pool")


def _mean(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return float(round(sum(values) / len(values), 12))


def _discounted_gain(gains: Sequence[int]) -> float:
    return float(
        sum((2**gain - 1) / math.log2(rank + 1) for rank, gain in enumerate(gains, start=1))
    )


def evaluate_rankings(
    candidate_pool: CandidatePool,
    rankings: Mapping[str, Sequence[RankedCandidate]],
) -> EvaluationResult:
    """Evaluate completed candidate-only rankings against labels loaded afterwards."""

    _validate_rankings(candidate_pool, rankings)
    judgements = load_judgements(candidate_pool)
    exact_hits: list[float] = []
    reciprocal_ranks: list[float] = []
    normalized_discounted_gains: list[float] = []

    for query in candidate_pool.queries:
        query_id = query.query_id
        labels = judgements.labels_by_query[query_id]
        ranked_ids = tuple(candidate.product_id for candidate in rankings[query_id])
        top_labels = tuple(labels[product_id] for product_id in ranked_ids[:_TOP_K])
        exact_hits.append(1.0 if "Exact" in top_labels else 0.0)

        if "Exact" in labels.values():
            first_exact_rank = next(
                (rank for rank, label in enumerate(top_labels, start=1) if label == "Exact"),
                None,
            )
            reciprocal_ranks.append(0.0 if first_exact_rank is None else 1.0 / first_exact_rank)

        ranked_gains = tuple(_LABEL_GAINS[label] for label in top_labels)
        ideal_gains = tuple(
            sorted((_LABEL_GAINS[label] for label in labels.values()), reverse=True)
        )[:_TOP_K]
        ideal_dcg = _discounted_gain(ideal_gains)
        if ideal_dcg > 0.0:
            normalized_discounted_gains.append(_discounted_gain(ranked_gains) / ideal_dcg)

    query_count = len(candidate_pool.queries)
    metrics = EvaluationMetrics(
        exact_at_10=MetricSummary(
            value=_mean(exact_hits),
            denominator=query_count,
            excluded=0,
        ),
        mrr_at_10=MetricSummary(
            value=_mean(reciprocal_ranks),
            denominator=len(reciprocal_ranks),
            excluded=query_count - len(reciprocal_ranks),
        ),
        ndcg_at_10=MetricSummary(
            value=_mean(normalized_discounted_gains),
            denominator=len(normalized_discounted_gains),
            excluded=query_count - len(normalized_discounted_gains),
        ),
    )
    return EvaluationResult(manifest=candidate_pool.manifest, metrics=metrics)


def evaluate_artifact(artifact_root: Path) -> EvaluationResult:
    """Run the fixed local scorer and evaluator for one verified artifact."""

    candidate_pool = load_candidate_pool(artifact_root)
    return evaluate_rankings(candidate_pool, rank_candidate_pool(candidate_pool))


def _metric_payload(metric: MetricSummary) -> dict[str, int | float]:
    return {
        "denominator": metric.denominator,
        "excluded": metric.excluded,
        "value": metric.value,
    }


def evaluation_summary(result: EvaluationResult) -> dict[str, object]:
    """Return the public aggregate-only payload; no source text or path is exposed."""

    manifest = result.manifest
    return {
        "artifact_manifest_sha256": manifest.manifest_sha256,
        "benchmark_id": manifest.benchmark_id,
        "counts": {
            "judgements": manifest.judgement_count,
            "products": manifest.product_count,
            "queries": manifest.query_count,
        },
        "label_distribution": dict(manifest.label_distribution),
        "metrics": {
            "exact_at_10": _metric_payload(result.metrics.exact_at_10),
            "mrr_at_10": _metric_payload(result.metrics.mrr_at_10),
            "ndcg_at_10": _metric_payload(result.metrics.ndcg_at_10),
        },
        "scorer_version": manifest.scorer_version,
        "source": {
            "repository": manifest.source_repository,
            "revision": manifest.source_revision,
        },
        "status": "COMPLETED",
    }


def benchmark_summary(artifact_root: Path) -> dict[str, object]:
    """Evaluate one artifact and return the safe, deterministic CLI payload."""

    return evaluation_summary(evaluate_artifact(artifact_root))


__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "BENCHMARK_ID",
    "SCORER_VERSION",
    "ArtifactManifest",
    "BenchmarkArtifactError",
    "CandidatePool",
    "EvaluationMetrics",
    "EvaluationResult",
    "Judgements",
    "MetricSummary",
    "Product",
    "Query",
    "RankedCandidate",
    "benchmark_summary",
    "evaluate_artifact",
    "evaluate_rankings",
    "evaluation_summary",
    "load_candidate_pool",
    "load_judgements",
    "rank_candidate_pool",
]
