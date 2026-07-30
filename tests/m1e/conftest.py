"""Synthetic, real-format ESCI source fixtures for M1e only."""

from __future__ import annotations

import csv
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as parquet  # type: ignore[import-untyped]
import pytest

from scripts.build_esci_benchmark import EXAMPLE_COLUMNS, PRODUCT_COLUMNS, SOURCES_COLUMNS

SYNTHETIC_SOURCE_REVISION = "0123456789abcdef0123456789abcdef01234567"
APACHE_LICENSE = b"Apache License\nVersion 2.0, January 2004\n"
NOTICE = b"Synthetic ESCI fixture notice.\n"

EXAMPLE_SCHEMA = pa.schema(
    [
        pa.field("example_id", pa.string()),
        pa.field("query", pa.string()),
        pa.field("query_id", pa.string()),
        pa.field("product_id", pa.string()),
        pa.field("product_locale", pa.string()),
        pa.field("esci_label", pa.string()),
        pa.field("small_version", pa.int64()),
        pa.field("large_version", pa.int64()),
        pa.field("split", pa.string()),
    ]
)
PRODUCT_SCHEMA = pa.schema(
    [
        pa.field("product_id", pa.string()),
        pa.field("product_title", pa.string()),
        pa.field("product_description", pa.string()),
        pa.field("product_bullet_point", pa.string()),
        pa.field("product_brand", pa.string()),
        pa.field("product_color", pa.string()),
        pa.field("product_locale", pa.string()),
    ]
)


@dataclass(frozen=True, slots=True)
class SyntheticEsciSource:
    """One standalone, operator-style source tree that tests may mutate."""

    root: Path
    revision: str = SYNTHETIC_SOURCE_REVISION


@pytest.fixture
def valid_esci_source(tmp_path: Path) -> Callable[..., SyntheticEsciSource]:
    """Create a tiny on-disk ESCI checkout with Parquet and CSV inputs.

    Callers can override rows or column order to exercise builder rejection
    paths without any external checkout or network access.
    """

    counter = 0

    def create(
        *,
        examples: Sequence[dict[str, object]] | None = None,
        products: Sequence[dict[str, object]] | None = None,
        source_rows: Sequence[tuple[str, str]] | None = None,
        example_columns: Sequence[str] = EXAMPLE_COLUMNS,
        product_columns: Sequence[str] = PRODUCT_COLUMNS,
    ) -> SyntheticEsciSource:
        nonlocal counter
        counter += 1
        root = tmp_path / f"esci-source-{counter}"
        dataset_directory = root / "shopping_queries_dataset"
        dataset_directory.mkdir(parents=True)
        (root / "LICENSE").write_bytes(APACHE_LICENSE)
        (root / "NOTICE").write_bytes(NOTICE)

        example_records = list(examples if examples is not None else _default_examples())
        product_records = list(products if products is not None else _default_products())
        example_table = pa.Table.from_pylist(example_records, schema=EXAMPLE_SCHEMA).select(
            list(example_columns)
        )
        product_table = pa.Table.from_pylist(product_records, schema=PRODUCT_SCHEMA).select(
            list(product_columns)
        )
        parquet.write_table(
            example_table,
            dataset_directory / "shopping_queries_dataset_examples.parquet",
        )
        parquet.write_table(
            product_table,
            dataset_directory / "shopping_queries_dataset_products.parquet",
        )

        rows = list(source_rows if source_rows is not None else _source_rows_for(example_records))
        with (dataset_directory / "shopping_queries_dataset_sources.csv").open(
            "w",
            encoding="utf-8",
            newline="",
        ) as source_file:
            writer = csv.writer(source_file)
            writer.writerow(SOURCES_COLUMNS)
            writer.writerows(rows)
        return SyntheticEsciSource(root=root)

    return create


def _default_examples() -> list[dict[str, object]]:
    return [
        _example("e-02", "q-1", "wireless keyboard", "p-2", "Substitute"),
        _example("e-01", "q-1", "wireless keyboard", "p-1", "Exact"),
        _example("e-03", "q-2", "coffee grinder", "p-3", "Complement"),
        _example("e-04", "q-ignored", "other locale", "p-4", "Irrelevant", locale="es"),
        _example("e-05", "q-ignored-split", "training row", "p-5", "Irrelevant", split="train"),
        _example("e-06", "q-ignored-version", "large only", "p-6", "Irrelevant", small_version=0),
    ]


def _default_products() -> list[dict[str, object]]:
    return [
        _product("p-1", "Wireless Keyboard One"),
        _product("p-2", "Keyboard Two", description=None, brand=None),
        _product("p-3", "Coffee Grinder"),
        _product("p-4", "Spanish Product", locale="es"),
        _product("p-5", "Training Product"),
        _product("p-6", "Large Product"),
    ]


def _source_rows_for(examples: Sequence[dict[str, object]]) -> list[tuple[str, str]]:
    query_ids = sorted({str(example["query_id"]) for example in examples})
    return [(query_id, "synthetic") for query_id in query_ids]


def _example(
    example_id: str,
    query_id: str,
    query: str,
    product_id: str,
    label: str,
    *,
    locale: str = "us",
    split: str = "test",
    small_version: int = 1,
    large_version: int = 1,
) -> dict[str, object]:
    return {
        "example_id": example_id,
        "query": query,
        "query_id": query_id,
        "product_id": product_id,
        "product_locale": locale,
        "esci_label": label,
        "small_version": small_version,
        "large_version": large_version,
        "split": split,
    }


def _product(
    product_id: str,
    title: str,
    *,
    description: str | None = "Description",
    bullet_point: str | None = "Bullet",
    brand: str | None = "Brand",
    color: str | None = "Black",
    locale: str = "us",
) -> dict[str, object]:
    return {
        "product_id": product_id,
        "product_title": title,
        "product_description": description,
        "product_bullet_point": bullet_point,
        "product_brand": brand,
        "product_color": color,
        "product_locale": locale,
    }
