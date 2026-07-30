"""Build the bounded, offline ESCI benchmark artifact from an operator checkout.

This script is intentionally build-only.  It reads an already-downloaded
Amazon Science ESCI checkout, makes no network requests, and writes the
versioned JSONL artifact used by the separate M1e evaluator.  Raw inputs are
never copied into the repository artifact.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pyarrow.parquet as parquet  # type: ignore[import-untyped]

BENCHMARK_ID: Final = "esci-small-us-v1"
SCHEMA_VERSION: Final = "esci-benchmark-artifact-v1"
BUILDER_VERSION: Final = "esci-builder-v1"
SCORER_VERSION: Final = "esci-bm25-v1"
LICENSE_SPDX: Final = "Apache-2.0"
SOURCE_REPOSITORY: Final = "https://github.com/amazon-science/esci-data"
SEED: Final = "glodex-esci-small-us-v1"
LOCALE: Final = "us"
SOURCE_LOCALES: Final = frozenset({"us", "es", "jp"})
SPLIT: Final = "test"
SMALL_VERSION: Final = 1
QUERY_LIMIT: Final = 500
CANDIDATES_PER_QUERY_LIMIT: Final = 20
MAX_ARTIFACT_BYTES: Final = 20 * 1024 * 1024

DATASET_DIRECTORY: Final = Path("shopping_queries_dataset")
EXAMPLES_RELATIVE_PATH: Final = DATASET_DIRECTORY / "shopping_queries_dataset_examples.parquet"
PRODUCTS_RELATIVE_PATH: Final = DATASET_DIRECTORY / "shopping_queries_dataset_products.parquet"
SOURCES_RELATIVE_PATH: Final = DATASET_DIRECTORY / "shopping_queries_dataset_sources.csv"

EXAMPLE_COLUMNS: Final = (
    "example_id",
    "query",
    "query_id",
    "product_id",
    "product_locale",
    "esci_label",
    "small_version",
    "large_version",
    "split",
)
OFFICIAL_EXAMPLE_TRAILING_INDEX_COLUMNS: Final = ("__index_level_0__",)
PRODUCT_COLUMNS: Final = (
    "product_id",
    "product_title",
    "product_description",
    "product_bullet_point",
    "product_brand",
    "product_color",
    "product_locale",
)
SOURCES_COLUMNS: Final = ("query_id", "source")

ARTIFACT_FILE_NAMES: Final = (
    "ATTRIBUTION.md",
    "LICENSE",
    "NOTICE",
    "judgements.jsonl",
    "products.jsonl",
    "queries.jsonl",
)
JSONL_FILE_NAMES: Final = (
    "queries.jsonl",
    "products.jsonl",
    "judgements.jsonl",
)
LABELS: Final = ("Exact", "Substitute", "Complement", "Irrelevant")
LABEL_NORMALIZATION: Final = {
    "E": "Exact",
    "Exact": "Exact",
    "S": "Substitute",
    "Substitute": "Substitute",
    "C": "Complement",
    "Complement": "Complement",
    "I": "Irrelevant",
    "Irrelevant": "Irrelevant",
}
REVISION_PATTERN: Final = re.compile(r"[0-9a-fA-F]{40}")


class BuildError(ValueError):
    """A safe, stable failure code for a rejected local build input."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class BuildResult:
    """The non-sensitive result of a completed artifact build."""

    benchmark_id: str
    queries: int
    products: int
    judgements: int
    artifact_bytes: int


@dataclass(frozen=True, slots=True)
class _Example:
    example_id: str
    query_id: str
    query: str
    product_id: str
    label: str


@dataclass(frozen=True, slots=True)
class _BuildData:
    queries: tuple[dict[str, str], ...]
    products: tuple[dict[str, str], ...]
    judgements: tuple[dict[str, str], ...]
    label_distribution: dict[str, int]


def build_benchmark(
    source_root: Path,
    source_revision: str,
    output_root: Path,
) -> BuildResult:
    """Build one deterministic ``esci-small-us-v1`` artifact.

    ``source_root`` must be an absolute checkout outside this repository.  The
    output name is deliberately fixed so changing any selection rule requires
    an explicit new benchmark version rather than silently overwriting v1.
    """

    source_directory = _validated_source_root(source_root)
    normalized_revision = _validated_revision(source_revision)
    output_directory = _validated_output_root(output_root, source_directory)
    source_files = _validated_source_files(source_directory)
    license_bytes = _validated_apache_license(source_files["LICENSE"])
    notice_bytes = _validated_notice(source_files["NOTICE"])

    data = _collect_build_data(
        examples_path=source_files[EXAMPLES_RELATIVE_PATH.as_posix()],
        products_path=source_files[PRODUCTS_RELATIVE_PATH.as_posix()],
        sources_path=source_files[SOURCES_RELATIVE_PATH.as_posix()],
    )
    input_files = _input_file_records(source_files)

    output_directory.parent.mkdir(parents=True, exist_ok=True)
    staging_directory = Path(
        tempfile.mkdtemp(prefix=f".{BENCHMARK_ID}.staging-", dir=output_directory.parent)
    )
    try:
        manifest = _write_artifact(
            staging_directory=staging_directory,
            data=data,
            license_bytes=license_bytes,
            notice_bytes=notice_bytes,
            source_revision=normalized_revision,
            input_files=input_files,
        )
        _validate_staged_artifact(staging_directory, manifest)
        os.replace(staging_directory, output_directory)
    except BaseException:
        shutil.rmtree(staging_directory, ignore_errors=True)
        raise

    counts = manifest["counts"]
    if not isinstance(counts, dict):  # Defensive: _write_artifact always constructs a mapping.
        raise BuildError("ARTIFACT_MANIFEST_INVALID")
    return BuildResult(
        benchmark_id=BENCHMARK_ID,
        queries=_manifest_count(counts, "queries"),
        products=_manifest_count(counts, "products"),
        judgements=_manifest_count(counts, "judgements"),
        artifact_bytes=_artifact_size(output_directory),
    )


def _validated_source_root(source_root: Path) -> Path:
    if not source_root.is_absolute():
        raise BuildError("SOURCE_ROOT_NOT_ABSOLUTE")
    if source_root.is_symlink() or not source_root.is_dir():
        raise BuildError("SOURCE_ROOT_INVALID")

    resolved = source_root.resolve(strict=True)
    project_root = Path(__file__).resolve().parents[1]
    if _is_within(resolved, project_root):
        raise BuildError("SOURCE_ROOT_INSIDE_REPOSITORY")
    return resolved


def _validated_revision(source_revision: str) -> str:
    if REVISION_PATTERN.fullmatch(source_revision) is None:
        raise BuildError("SOURCE_REVISION_INVALID")
    return source_revision.lower()


def _validated_output_root(output_root: Path, source_root: Path) -> Path:
    resolved = output_root.resolve(strict=False)
    if resolved.name != BENCHMARK_ID:
        raise BuildError("OUTPUT_BENCHMARK_ID_INVALID")
    if resolved.exists() or resolved.is_symlink():
        raise BuildError("OUTPUT_ALREADY_EXISTS")
    if _is_within(resolved, source_root):
        raise BuildError("OUTPUT_INSIDE_SOURCE")
    return resolved


def _validated_source_files(source_root: Path) -> dict[str, Path]:
    relative_paths = (
        Path("LICENSE"),
        Path("NOTICE"),
        EXAMPLES_RELATIVE_PATH,
        PRODUCTS_RELATIVE_PATH,
        SOURCES_RELATIVE_PATH,
    )
    source_files: dict[str, Path] = {}
    for relative_path in relative_paths:
        path = source_root / relative_path
        if path.is_symlink() or not path.is_file() or not _is_within(path.resolve(), source_root):
            raise BuildError("SOURCE_FILE_INVALID")
        source_files[relative_path.as_posix()] = path
    return source_files


def _validated_apache_license(path: Path) -> bytes:
    content = path.read_bytes()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BuildError("LICENSE_INVALID") from error
    if "Apache License" not in text or "Version 2.0" not in text:
        raise BuildError("LICENSE_NOT_APACHE_2_0")
    return content


def _validated_notice(path: Path) -> bytes:
    content = path.read_bytes()
    if not content:
        raise BuildError("NOTICE_INVALID")
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BuildError("NOTICE_INVALID") from error
    return content


def _collect_build_data(
    *,
    examples_path: Path,
    products_path: Path,
    sources_path: Path,
) -> _BuildData:
    candidates, query_texts = _collect_eligible_examples(examples_path)
    selected_query_ids = _select_query_ids(candidates)
    selected_examples = _select_examples(candidates, selected_query_ids)
    _validated_sources(sources_path, selected_query_ids)

    selected_product_ids = {
        example.product_id for examples in selected_examples.values() for example in examples
    }
    products = _collect_selected_products(products_path, selected_product_ids)
    missing_product_ids = selected_product_ids.difference(products)
    if missing_product_ids:
        raise BuildError("PRODUCT_RELATION_MISSING")

    queries = tuple(
        {"query_id": query_id, "query": query_texts[query_id]}
        for query_id in sorted(selected_query_ids)
    )
    selected_product_records = tuple(products[product_id] for product_id in sorted(products))
    judgements = tuple(
        {
            "query_id": query_id,
            "product_id": example.product_id,
            "label": example.label,
        }
        for query_id in sorted(selected_examples)
        for example in sorted(selected_examples[query_id], key=lambda item: item.product_id)
    )
    label_counts = Counter(judgement["label"] for judgement in judgements)
    label_distribution = {label: label_counts[label] for label in LABELS}

    if not queries or not judgements:
        raise BuildError("SELECTED_SAMPLE_EMPTY")
    if len(queries) > QUERY_LIMIT or len(judgements) > QUERY_LIMIT * CANDIDATES_PER_QUERY_LIMIT:
        raise BuildError("SELECTION_LIMIT_EXCEEDED")
    return _BuildData(
        queries=queries,
        products=selected_product_records,
        judgements=judgements,
        label_distribution=label_distribution,
    )


def _collect_eligible_examples(
    examples_path: Path,
) -> tuple[dict[str, list[_Example]], dict[str, str]]:
    candidates: dict[str, list[_Example]] = defaultdict(list)
    query_texts: dict[str, str] = {}
    seen_example_ids: set[str] = set()
    seen_query_product_ids: set[tuple[str, str, str]] = set()

    for row in _read_parquet_rows(
        examples_path,
        EXAMPLE_COLUMNS,
        allowed_trailing_columns=OFFICIAL_EXAMPLE_TRAILING_INDEX_COLUMNS,
    ):
        example_id = _required_identifier(row, "example_id")
        if example_id in seen_example_ids:
            raise BuildError("EXAMPLE_ID_DUPLICATE")
        seen_example_ids.add(example_id)

        query = _required_text(row, "query")
        query_id = _required_identifier(row, "query_id")
        product_id = _required_text(row, "product_id")
        locale = _required_text(row, "product_locale")
        if locale not in SOURCE_LOCALES:
            raise BuildError("PRODUCT_LOCALE_INVALID")
        split = _required_text(row, "split")
        _validated_version_flag(row.get("small_version"), "small_version")
        _validated_version_flag(row.get("large_version"), "large_version")
        label = _normalized_label(row.get("esci_label"))

        prior_query = query_texts.setdefault(query_id, query)
        if prior_query != query:
            raise BuildError("QUERY_IDENTITY_CONFLICT")

        query_product_id = (query_id, locale, product_id)
        if query_product_id in seen_query_product_ids:
            raise BuildError("QUERY_PRODUCT_DUPLICATE")
        seen_query_product_ids.add(query_product_id)

        if not (_is_version_one(row.get("small_version")) and split == SPLIT and locale == LOCALE):
            continue

        candidates[query_id].append(
            _Example(
                example_id=example_id,
                query_id=query_id,
                query=query,
                product_id=product_id,
                label=label,
            )
        )

    return candidates, query_texts


def _validated_version_flag(value: object, field: str) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, int) and not isinstance(value, bool):
        return
    raise BuildError(f"{field.upper()}_INVALID")


def _is_version_one(value: object) -> bool:
    return value is True or (isinstance(value, int) and not isinstance(value, bool) and value == 1)


def _normalized_label(value: object) -> str:
    if not isinstance(value, str):
        raise BuildError("LABEL_INVALID")
    label = LABEL_NORMALIZATION.get(value)
    if label is None:
        raise BuildError("LABEL_UNKNOWN")
    return label


def _select_query_ids(candidates: Mapping[str, Sequence[_Example]]) -> tuple[str, ...]:
    ordered = sorted(
        candidates,
        key=lambda query_id: (_query_digest(query_id), query_id),
    )
    return tuple(ordered[:QUERY_LIMIT])


def _query_digest(query_id: str) -> str:
    return hashlib.sha256(f"{SEED}\0{query_id}".encode()).hexdigest()


def _select_examples(
    candidates: Mapping[str, Sequence[_Example]],
    selected_query_ids: Iterable[str],
) -> dict[str, tuple[_Example, ...]]:
    selected: dict[str, tuple[_Example, ...]] = {}
    for query_id in selected_query_ids:
        ordered_examples = sorted(
            candidates[query_id],
            key=lambda example: (example.example_id, example.product_id),
        )
        examples = tuple(ordered_examples[:CANDIDATES_PER_QUERY_LIMIT])
        if not examples:
            raise BuildError("SELECTED_QUERY_WITHOUT_CANDIDATES")
        selected[query_id] = examples
    return selected


def _validated_sources(sources_path: Path, selected_query_ids: Iterable[str]) -> None:
    source_query_ids: set[str] = set()
    try:
        with sources_path.open("r", encoding="utf-8", newline="") as source_file:
            reader = csv.reader(source_file)
            try:
                header = next(reader)
            except StopIteration as error:
                raise BuildError("SOURCES_SCHEMA_INVALID") from error
            if tuple(header) != SOURCES_COLUMNS:
                raise BuildError("SOURCES_SCHEMA_INVALID")
            for row in reader:
                if len(row) != len(SOURCES_COLUMNS):
                    raise BuildError("SOURCES_ROW_INVALID")
                query_id, source = row
                normalized_query_id = query_id.strip()
                if (
                    not normalized_query_id
                    or not source.strip()
                    or normalized_query_id in source_query_ids
                ):
                    raise BuildError("SOURCES_ROW_INVALID")
                source_query_ids.add(normalized_query_id)
    except UnicodeDecodeError as error:
        raise BuildError("SOURCES_ENCODING_INVALID") from error
    except csv.Error as error:
        raise BuildError("SOURCES_ROW_INVALID") from error

    if not set(selected_query_ids).issubset(source_query_ids):
        raise BuildError("SOURCES_RELATION_MISSING")


def _collect_selected_products(
    products_path: Path,
    selected_product_ids: set[str],
) -> dict[str, dict[str, str]]:
    selected_products: dict[str, dict[str, str]] = {}
    seen_product_identities: set[tuple[str, str]] = set()
    for row in _read_parquet_rows(products_path, PRODUCT_COLUMNS):
        product_id = _required_text(row, "product_id")
        locale = _required_text(row, "product_locale")
        if locale not in SOURCE_LOCALES:
            raise BuildError("PRODUCT_LOCALE_INVALID")
        identity = (locale, product_id)
        if identity in seen_product_identities:
            raise BuildError("PRODUCT_ID_DUPLICATE")
        seen_product_identities.add(identity)
        if locale != LOCALE or product_id not in selected_product_ids:
            continue
        selected_products[product_id] = {
            "product_id": product_id,
            "product_locale": locale,
            "product_title": _required_text(row, "product_title"),
            "product_description": _optional_text(row, "product_description"),
            "product_bullet_point": _optional_text(row, "product_bullet_point"),
            "product_brand": _optional_text(row, "product_brand"),
            "product_color": _optional_text(row, "product_color"),
        }
    return selected_products


def _read_parquet_rows(
    path: Path,
    expected_columns: tuple[str, ...],
    *,
    allowed_trailing_columns: tuple[str, ...] = (),
) -> Iterator[Mapping[str, object]]:
    try:
        file = parquet.ParquetFile(path)
        source_columns = tuple(file.schema_arrow.names)
        if source_columns not in {
            expected_columns,
            (*expected_columns, *allowed_trailing_columns),
        }:
            raise BuildError("PARQUET_SCHEMA_INVALID")
        for batch in file.iter_batches(columns=list(expected_columns), batch_size=65_536):
            for row in batch.to_pylist():
                if not isinstance(row, dict):
                    raise BuildError("PARQUET_ROW_INVALID")
                if tuple(row) != expected_columns:
                    raise BuildError("PARQUET_ROW_INVALID")
                yield row
    except BuildError:
        raise
    except Exception as error:
        raise BuildError("PARQUET_READ_INVALID") from error


def _required_text(row: Mapping[str, object], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise BuildError(f"{field.upper()}_INVALID")
    return value


def _required_identifier(row: Mapping[str, object], field: str) -> str:
    value = row.get(field)
    if isinstance(value, str):
        return _required_text(row, field)
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    raise BuildError(f"{field.upper()}_INVALID")


def _optional_text(row: Mapping[str, object], field: str) -> str:
    value = row.get(field)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise BuildError(f"{field.upper()}_INVALID")
    return value


def _input_file_records(source_files: Mapping[str, Path]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for relative_path in (
        EXAMPLES_RELATIVE_PATH.as_posix(),
        PRODUCTS_RELATIVE_PATH.as_posix(),
        SOURCES_RELATIVE_PATH.as_posix(),
    ):
        path = source_files[relative_path]
        records.append(
            {
                "path": relative_path,
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    return records


def _write_artifact(
    *,
    staging_directory: Path,
    data: _BuildData,
    license_bytes: bytes,
    notice_bytes: bytes,
    source_revision: str,
    input_files: list[dict[str, object]],
) -> dict[str, object]:
    _write_bytes(staging_directory / "LICENSE", license_bytes)
    _write_bytes(staging_directory / "NOTICE", notice_bytes)
    _write_text(staging_directory / "ATTRIBUTION.md", _attribution_text(source_revision))
    _write_jsonl(staging_directory / "queries.jsonl", data.queries)
    _write_jsonl(staging_directory / "products.jsonl", data.products)
    _write_jsonl(staging_directory / "judgements.jsonl", data.judgements)

    artifact_files = {
        name: {
            "bytes": (staging_directory / name).stat().st_size,
            "sha256": _sha256(staging_directory / name),
        }
        for name in ARTIFACT_FILE_NAMES
    }
    manifest: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "benchmark_id": BENCHMARK_ID,
        "builder_version": BUILDER_VERSION,
        "scorer_version": SCORER_VERSION,
        "license": LICENSE_SPDX,
        "source": {
            "repository": SOURCE_REPOSITORY,
            "revision": source_revision,
            "files": input_files,
        },
        "selection": {
            "locale": LOCALE,
            "split": SPLIT,
            "small_version": SMALL_VERSION,
            "seed": SEED,
            "query_limit": QUERY_LIMIT,
            "candidates_per_query_limit": CANDIDATES_PER_QUERY_LIMIT,
            "query_selection": "sha256(seed + \\0 + query_id), then query_id; first 500",
            "candidate_selection": "example_id, then product_id; first 20 per selected query",
        },
        "counts": {
            "queries": len(data.queries),
            "products": len(data.products),
            "judgements": len(data.judgements),
        },
        "label_distribution": data.label_distribution,
        "artifact_files": artifact_files,
    }
    _write_canonical_json(staging_directory / "manifest.json", manifest)
    return manifest


def _attribution_text(source_revision: str) -> str:
    return (
        "# ESCI benchmark attribution\n\n"
        "This artifact is derived from the Amazon Science Shopping Queries Dataset (ESCI).\n\n"
        f"- Source repository: {SOURCE_REPOSITORY}\n"
        f"- Source revision: `{source_revision}`\n"
        f"- License: {LICENSE_SPDX}; see `LICENSE` and `NOTICE`.\n"
        "- Scope: historical offline retrieval benchmark; not Amazon live marketplace data, "
        "pricing, inventory, shipping, or a shopping recommendation.\n\n"
        "Please cite the upstream Shopping Queries Dataset publication when using this artifact.\n"
    )


def _write_jsonl(path: Path, records: Iterable[Mapping[str, str]]) -> None:
    text = "".join(_canonical_json(record) + "\n" for record in records)
    _write_text(path, text)


def _write_canonical_json(path: Path, record: Mapping[str, object]) -> None:
    _write_text(path, _canonical_json(record) + "\n")


def _canonical_json(record: object) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_bytes(path: Path, content: bytes) -> None:
    with path.open("xb") as file:
        file.write(content)


def _write_text(path: Path, content: str) -> None:
    _write_bytes(path, content.encode("utf-8"))


def _validate_staged_artifact(root: Path, expected_manifest: Mapping[str, object]) -> None:
    expected_file_names = set(ARTIFACT_FILE_NAMES) | {"manifest.json"}
    actual_file_names = {path.name for path in root.iterdir()}
    if actual_file_names != expected_file_names:
        raise BuildError("ARTIFACT_FILESET_INVALID")
    for path in root.iterdir():
        if path.is_symlink() or not path.is_file():
            raise BuildError("ARTIFACT_FILE_INVALID")
    if _artifact_size(root) > MAX_ARTIFACT_BYTES:
        raise BuildError("ARTIFACT_SIZE_EXCEEDED")

    manifest_path = root / "manifest.json"
    try:
        manifest_text = manifest_path.read_text(encoding="utf-8")
        manifest = json.loads(manifest_text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BuildError("ARTIFACT_MANIFEST_INVALID") from error
    if not isinstance(manifest, dict) or manifest != expected_manifest:
        raise BuildError("ARTIFACT_MANIFEST_INVALID")
    if manifest_text != _canonical_json(manifest) + "\n":
        raise BuildError("ARTIFACT_MANIFEST_INVALID")

    artifact_files = manifest.get("artifact_files")
    if not isinstance(artifact_files, dict) or set(artifact_files) != set(ARTIFACT_FILE_NAMES):
        raise BuildError("ARTIFACT_MANIFEST_INVALID")
    for name in ARTIFACT_FILE_NAMES:
        record = artifact_files.get(name)
        if not isinstance(record, dict):
            raise BuildError("ARTIFACT_MANIFEST_INVALID")
        expected_bytes = record.get("bytes")
        expected_sha256 = record.get("sha256")
        path = root / name
        if (
            not isinstance(expected_bytes, int)
            or not isinstance(expected_sha256, str)
            or path.stat().st_size != expected_bytes
            or _sha256(path) != expected_sha256
        ):
            raise BuildError("ARTIFACT_HASH_INVALID")

    _validate_artifact_records(root, manifest)


def _validate_artifact_records(root: Path, manifest: Mapping[str, object]) -> None:
    queries = _read_jsonl(root / "queries.jsonl", {"query_id", "query"})
    products = _read_jsonl(
        root / "products.jsonl",
        {
            "product_id",
            "product_locale",
            "product_title",
            "product_description",
            "product_bullet_point",
            "product_brand",
            "product_color",
        },
    )
    judgements = _read_jsonl(root / "judgements.jsonl", {"query_id", "product_id", "label"})

    query_ids = [_required_text(record, "query_id") for record in queries]
    if query_ids != sorted(query_ids) or len(query_ids) != len(set(query_ids)):
        raise BuildError("ARTIFACT_QUERIES_INVALID")
    if len(query_ids) > QUERY_LIMIT:
        raise BuildError("ARTIFACT_QUERIES_INVALID")

    product_ids: list[str] = []
    for record in products:
        if _required_text(record, "product_locale") != LOCALE:
            raise BuildError("ARTIFACT_PRODUCTS_INVALID")
        _required_text(record, "product_title")
        product_ids.append(_required_text(record, "product_id"))
        for optional_field in (
            "product_description",
            "product_bullet_point",
            "product_brand",
            "product_color",
        ):
            if not isinstance(record.get(optional_field), str):
                raise BuildError("ARTIFACT_PRODUCTS_INVALID")
    if product_ids != sorted(product_ids) or len(product_ids) != len(set(product_ids)):
        raise BuildError("ARTIFACT_PRODUCTS_INVALID")

    pairs: list[tuple[str, str]] = []
    label_counts: Counter[str] = Counter()
    candidates_per_query: Counter[str] = Counter()
    for record in judgements:
        query_id = _required_text(record, "query_id")
        product_id = _required_text(record, "product_id")
        label = _normalized_label(record.get("label"))
        if record.get("label") != label:
            raise BuildError("ARTIFACT_JUDGEMENTS_INVALID")
        pairs.append((query_id, product_id))
        candidates_per_query[query_id] += 1
        label_counts[label] += 1
    if pairs != sorted(pairs) or len(pairs) != len(set(pairs)):
        raise BuildError("ARTIFACT_JUDGEMENTS_INVALID")
    if set(candidates_per_query) != set(query_ids) or any(
        count < 1 or count > CANDIDATES_PER_QUERY_LIMIT for count in candidates_per_query.values()
    ):
        raise BuildError("ARTIFACT_JUDGEMENTS_INVALID")
    query_id_set = set(query_ids)
    product_id_set = set(product_ids)
    if any(
        query_id not in query_id_set or product_id not in product_id_set
        for query_id, product_id in pairs
    ):
        raise BuildError("ARTIFACT_RELATION_INVALID")

    counts = manifest.get("counts")
    if not isinstance(counts, dict):
        raise BuildError("ARTIFACT_MANIFEST_INVALID")
    if (
        _manifest_count(counts, "queries") != len(queries)
        or _manifest_count(counts, "products") != len(products)
        or _manifest_count(counts, "judgements") != len(judgements)
    ):
        raise BuildError("ARTIFACT_COUNTS_INVALID")
    label_distribution = manifest.get("label_distribution")
    expected_label_distribution = {label: label_counts[label] for label in LABELS}
    if label_distribution != expected_label_distribution:
        raise BuildError("ARTIFACT_LABEL_DISTRIBUTION_INVALID")


def _read_jsonl(path: Path, expected_keys: set[str]) -> list[dict[str, object]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise BuildError("ARTIFACT_JSONL_INVALID") from error
    if not text or not text.endswith("\n") or "\r" in text:
        raise BuildError("ARTIFACT_JSONL_INVALID")
    lines = text[:-1].split("\n")
    if not lines:
        raise BuildError("ARTIFACT_JSONL_INVALID")

    records: list[dict[str, object]] = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise BuildError("ARTIFACT_JSONL_INVALID") from error
        if not isinstance(record, dict) or set(record) != expected_keys:
            raise BuildError("ARTIFACT_JSONL_INVALID")
        if line != _canonical_json(record):
            raise BuildError("ARTIFACT_JSONL_INVALID")
        records.append(record)
    return records


def _manifest_count(counts: Mapping[object, object], name: str) -> int:
    value = counts.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise BuildError("ARTIFACT_COUNTS_INVALID")
    return value


def _artifact_size(root: Path) -> int:
    return sum(path.stat().st_size for path in root.iterdir() if path.is_file())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build the local ESCI benchmark artifact.")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the operator-only builder with safe, aggregate terminal output."""

    arguments = _parser().parse_args(argv)
    try:
        result = build_benchmark(
            source_root=arguments.source_root,
            source_revision=arguments.source_revision,
            output_root=arguments.output_root,
        )
    except BuildError as error:
        print(_canonical_json({"status": "error", "code": error.code}), file=sys.stderr)
        return 1
    except Exception:
        # Parsers can otherwise include a local path or raw input value in their error text.
        print(_canonical_json({"status": "error", "code": "BUILD_FAILED"}), file=sys.stderr)
        return 1

    print(
        _canonical_json(
            {
                "status": "built",
                "benchmark_id": result.benchmark_id,
                "counts": {
                    "queries": result.queries,
                    "products": result.products,
                    "judgements": result.judgements,
                },
                "artifact_bytes": result.artifact_bytes,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
