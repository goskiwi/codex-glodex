"""JSON-only command-line entrypoint for the M0 application."""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Never

from glodex.adapters.local_snapshot import LocalSnapshotCatalog
from glodex.application.issue_mapping import catalog_issue_to_public
from glodex.bootstrap import build_service, submit_search
from glodex.config import ConfigurationError, GlodexConfig, load_config
from glodex.contracts import (
    Issue,
    IssueSeverity,
    RequestFieldError,
    RequestRejected,
    RunStatus,
    SearchResponse,
    SnapshotValidationResponse,
)
from glodex.domain.catalog import aggregate_catalog_batch

DEMO_QUERY = "推荐 800 美元以内、有库存、适合出差的轻薄本"
_SNAPSHOT_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


class CliUsageError(ValueError):
    """An argparse failure that can be rendered through the public JSON contract."""


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise CliUsageError(message)


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        help="path to glodex.toml (overrides GLODEX_CONFIG)",
    )
    parser.add_argument("--locale", help="request locale (M0 supports zh-CN)")
    parser.add_argument("--snapshot", help="snapshot version")
    parser.add_argument("--currency", help="three-letter display currency")
    parser.add_argument("--top-k", type=int, help="number of results, from 1 to 3")


def _build_parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(prog="glodex")
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo = subparsers.add_parser("demo", help="run the offline deterministic M0 demo")
    _add_common_options(demo)

    search = subparsers.add_parser("search", help="submit one search request")
    _add_common_options(search)
    search.add_argument("--query", required=True, help="shopping request text")

    validate = subparsers.add_parser(
        "validate-snapshot",
        help="validate and summarize one local snapshot",
    )
    validate.add_argument("snapshot_version", help="snapshot version")
    validate.add_argument(
        "--config",
        type=Path,
        help="path to glodex.toml (overrides GLODEX_CONFIG)",
    )
    validate.add_argument("--currency", help="currency compatibility check")
    return parser


def _rejected(*, field: str, code: str, message: str) -> RequestRejected:
    return RequestRejected(
        errors=(
            RequestFieldError(
                field=field,
                code=code,
                message=message,
            ),
        )
    )


def _configuration_rejected(error: ConfigurationError) -> RequestRejected:
    message = str(error).partition(": ")[2] or "configuration is invalid"
    return _rejected(field="config", code=error.code, message=message)


def _payload(namespace: argparse.Namespace, config: GlodexConfig) -> dict[str, object]:
    query = DEMO_QUERY if namespace.command == "demo" else namespace.query
    return {
        "query": query,
        "locale": namespace.locale or config.default_locale,
        "display_currency": namespace.currency or config.default_currency,
        "top_k": (namespace.top_k if namespace.top_k is not None else config.default_top_k),
        "snapshot_version": namespace.snapshot or config.default_snapshot,
    }


CliOutcome = SearchResponse | RequestRejected | SnapshotValidationResponse


def exit_code_for(outcome: CliOutcome) -> int:
    """Map every public outcome to its approved process exit code."""

    if isinstance(outcome, RequestRejected):
        return 2
    if isinstance(outcome, SnapshotValidationResponse):
        return 0 if outcome.valid else 1
    if outcome.status is RunStatus.FAILED:
        return 1
    return 0


def _emit(outcome: CliOutcome) -> None:
    sys.stdout.write(outcome.model_dump_json() + "\n")
    if isinstance(outcome, RequestRejected):
        codes = ",".join(error.code for error in outcome.errors)
        sys.stderr.write(f"glodex: request rejected ({codes})\n")


async def _validate_snapshot(
    namespace: argparse.Namespace,
    config: GlodexConfig,
) -> SnapshotValidationResponse:
    currency = namespace.currency or config.default_currency
    batch = await LocalSnapshotCatalog(config.data_dir).load(
        namespace.snapshot_version,
        display_currency=currency,
    )
    if batch.fatal_issues:
        return SnapshotValidationResponse(
            snapshot_version=batch.snapshot_version,
            valid=False,
            issues=tuple(catalog_issue_to_public(issue) for issue in batch.fatal_issues),
        )
    try:
        aggregation = aggregate_catalog_batch(batch)
    except Exception:
        return SnapshotValidationResponse(
            snapshot_version=batch.snapshot_version,
            valid=False,
            issues=(
                Issue(
                    code="catalog.aggregation-failed",
                    stage="aggregation",
                    message="Catalog aggregation failed.",
                    severity=IssueSeverity.ERROR,
                ),
            ),
        )
    return SnapshotValidationResponse(
        snapshot_version=batch.snapshot_version,
        valid=True,
        product_count=len(aggregation.products),
        offer_count=len(aggregation.offers),
        quarantine_count=len(aggregation.quarantine_issues),
        issues=tuple(catalog_issue_to_public(issue) for issue in aggregation.quarantine_issues),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Parse once, run the async application once, and emit one JSON document."""

    try:
        namespace = _build_parser().parse_args(argv)
    except CliUsageError as error:
        rejection = _rejected(
            field="cli",
            code="CLI_USAGE",
            message=str(error),
        )
        _emit(rejection)
        return exit_code_for(rejection)

    try:
        config = load_config(explicit_path=namespace.config)
    except ConfigurationError as error:
        rejection = _configuration_rejected(error)
        _emit(rejection)
        return exit_code_for(rejection)

    if namespace.command == "validate-snapshot":
        if _SNAPSHOT_VERSION.fullmatch(namespace.snapshot_version) is None:
            rejection = _rejected(
                field="snapshot_version",
                code="string_pattern_mismatch",
                message="snapshot version must be a safe identifier of at most 64 characters",
            )
            _emit(rejection)
            return exit_code_for(rejection)
        currency = namespace.currency or config.default_currency
        if (
            len(currency) != 3
            or not currency.isascii()
            or not currency.isalpha()
            or not currency.isupper()
        ):
            rejection = _rejected(
                field="currency",
                code="string_pattern_mismatch",
                message="currency must be an uppercase three-letter code",
            )
            _emit(rejection)
            return exit_code_for(rejection)
        snapshot_outcome = asyncio.run(_validate_snapshot(namespace, config))
        _emit(snapshot_outcome)
        return exit_code_for(snapshot_outcome)

    service = build_service(config)
    search_outcome = asyncio.run(submit_search(_payload(namespace, config), service))
    _emit(search_outcome)
    return exit_code_for(search_outcome)


__all__ = ["DEMO_QUERY", "exit_code_for", "main"]
