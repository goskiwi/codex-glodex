"""JSON-only command-line entrypoint for Glodex."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Awaitable, Callable, Sequence
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
    validate_search_request,
)
from glodex.domain.catalog import aggregate_catalog_batch

DEMO_QUERY = "推荐 800 美元以内、有库存、适合出差的轻薄本"
_SNAPSHOT_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_AGENT_DEMO_SNAPSHOT = "m1d-demo-v1"
_DEFAULT_ESCI_ARTIFACT_ROOT = (
    Path(__file__).resolve().parents[2] / "data" / "benchmarks" / "esci-small-us-v1"
)

type AgentServiceFactory = Callable[..., Awaitable[object]]


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
    search.add_argument(
        "--live-intent",
        action="store_true",
        help="send the complete query to DeepSeek for live Intent interpretation",
    )

    agent_demo = subparsers.add_parser(
        "agent-demo",
        help="run the explicit fixed DeepSeek shopping Agent",
    )
    _add_common_options(agent_demo)
    agent_demo.add_argument(
        "--live",
        action="store_true",
        required=True,
        help="explicitly enable DeepSeek and DashScope for this Agent run",
    )
    agent_demo.add_argument(
        "--live-data",
        action="store_true",
        help="also enable fixed Tavily and eBay live-data capabilities",
    )
    agent_demo.add_argument(
        "--query",
        required=True,
        help="shopping request text sent to the fixed Agent composition",
    )
    agent_demo.add_argument(
        "--output-root",
        type=Path,
        help="absolute external 0700 output root required by --live-data",
    )

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

    capture = subparsers.add_parser(
        "capture-provider",
        help="capture one approved live eBay result page into a local snapshot",
    )
    capture.add_argument(
        "--live",
        action="store_true",
        required=True,
        help="explicitly opt in to the approved live Provider request",
    )
    capture.add_argument(
        "--query",
        required=True,
        help="shopping query text sent to eBay",
    )
    capture.add_argument(
        "--env-file",
        type=Path,
        help="explicit local file containing eBay credentials",
    )
    capture.add_argument(
        "--output-root",
        type=Path,
        required=True,
        help="absolute external snapshot output root",
    )

    benchmark = subparsers.add_parser(
        "benchmark-esci",
        help="evaluate the fixed offline ESCI retrieval benchmark",
    )
    benchmark.add_argument(
        "--artifact-root",
        type=Path,
        default=_DEFAULT_ESCI_ARTIFACT_ROOT,
        help="versioned local ESCI benchmark artifact root",
    )
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


def _agent_payload(namespace: argparse.Namespace) -> dict[str, object]:
    return {
        "query": namespace.query,
        "locale": namespace.locale or "zh-CN",
        "display_currency": namespace.currency or "CNY",
        "top_k": namespace.top_k if namespace.top_k is not None else 3,
        "snapshot_version": (
            None if namespace.live_data else namespace.snapshot or _AGENT_DEMO_SNAPSHOT
        ),
    }


def _agent_mode_rejection(namespace: argparse.Namespace) -> RequestRejected | None:
    if namespace.live_data:
        if namespace.snapshot is not None:
            return _rejected(
                field="snapshot",
                code="AGENT_MODE_INVALID",
                message="live-data mode does not accept a snapshot",
            )
        if namespace.output_root is None:
            return _rejected(
                field="output_root",
                code="AGENT_OUTPUT_ROOT_REQUIRED",
                message="live-data mode requires an external output root",
            )
        return None
    if namespace.output_root is not None:
        return _rejected(
            field="output_root",
            code="AGENT_MODE_INVALID",
            message="output root is only accepted in live-data mode",
        )
    if namespace.snapshot not in (None, _AGENT_DEMO_SNAPSHOT):
        return _rejected(
            field="snapshot",
            code="AGENT_SNAPSHOT_UNSUPPORTED",
            message="Agent Demo supports only m1d-demo-v1",
        )
    return None


async def _execute_agent(
    *,
    config: GlodexConfig,
    request: object,
    live_data: bool,
    output_root: Path | None,
    service_factory: AgentServiceFactory | None,
) -> object:
    from glodex.application.agent.contracts import AgentExecution
    from glodex.contracts import SearchRequest

    if type(request) is not SearchRequest:
        raise TypeError("Agent CLI requires an exact SearchRequest")
    if service_factory is None:
        from glodex.agent_bootstrap import build_agent_service

        service_factory = build_agent_service
    service = await service_factory(
        config,
        live_data=live_data,
        output_root=output_root,
        preflight_query=request.query,
    )
    execute = getattr(service, "execute", None)
    if not callable(execute):
        raise TypeError("Agent composition must provide execute")
    execution = await execute(request)
    if type(execution) is not AgentExecution:
        raise TypeError("Agent service must return exact AgentExecution")
    return execution


def _emit_agent_execution(execution: object) -> int:
    from glodex.application.agent.contracts import AgentExecution

    if type(execution) is not AgentExecution:
        raise TypeError("Agent CLI requires exact AgentExecution")
    sys.stdout.write(execution.response.model_dump_json() + "\n")
    return 1 if execution.response.status is RunStatus.FAILED else 0


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


def _emit_capture(receipt: object) -> None:
    from glodex.capture.contracts import (
        FailedCaptureReceipt,
        PublishedCaptureReceipt,
        RejectedCaptureReceipt,
    )

    if not isinstance(
        receipt,
        (RejectedCaptureReceipt, FailedCaptureReceipt, PublishedCaptureReceipt),
    ):
        raise TypeError("unsupported Capture receipt type")
    sys.stdout.write(receipt.model_dump_json() + "\n")
    if isinstance(receipt, RejectedCaptureReceipt):
        codes = ",".join(issue.code.value for issue in receipt.issues)
        sys.stderr.write(f"glodex: capture rejected ({codes})\n")
    elif isinstance(receipt, FailedCaptureReceipt):
        codes = ",".join(issue.code.value for issue in receipt.issues)
        sys.stderr.write(f"glodex: capture failed ({codes})\n")


def _run_capture(namespace: argparse.Namespace) -> int:
    from glodex.capture.bootstrap import run_capture
    from glodex.capture.config import CaptureRequest
    from glodex.capture.contracts import capture_exit_code_for

    receipt = run_capture(
        CaptureRequest(
            live=namespace.live,
            query=namespace.query,
            env_file=namespace.env_file,
            output_root=namespace.output_root,
        )
    )
    _emit_capture(receipt)
    return capture_exit_code_for(receipt)


def _capture_usage_rejected() -> int:
    from glodex.capture.contracts import (
        CaptureIssue,
        CaptureIssueCode,
        RejectedCaptureReceipt,
        capture_exit_code_for,
    )

    receipt = RejectedCaptureReceipt(
        issues=(
            CaptureIssue(
                code=CaptureIssueCode.CAPTURE_INPUT_INVALID,
            ),
        )
    )
    _emit_capture(receipt)
    return capture_exit_code_for(receipt)


def _emit_esci_failure(*, code: str) -> None:
    """Emit an aggregate-safe ESCI failure without echoing untrusted paths."""

    sys.stdout.write(json.dumps({"code": code, "status": "FAILED"}, separators=(",", ":")) + "\n")


def _run_esci_benchmark(namespace: argparse.Namespace) -> int:
    """Evaluate the standalone local benchmark before any normal configuration loads."""

    from glodex.esci_benchmark import BenchmarkArtifactError, benchmark_summary

    try:
        payload = benchmark_summary(namespace.artifact_root)
    except BenchmarkArtifactError:
        _emit_esci_failure(code="BENCHMARK_ARTIFACT_INVALID")
        return 1
    rendered = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    sys.stdout.write(rendered + "\n")
    return 0


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


def main(
    argv: Sequence[str] | None = None,
    *,
    agent_service_factory: AgentServiceFactory | None = None,
) -> int:
    """Parse once, run the async application once, and emit one JSON document."""

    arguments = tuple(sys.argv[1:] if argv is None else argv)
    try:
        namespace = _build_parser().parse_args(arguments)
    except CliUsageError as error:
        if arguments and arguments[0] == "capture-provider":
            return _capture_usage_rejected()
        if arguments and arguments[0] == "benchmark-esci":
            _emit_esci_failure(code="BENCHMARK_USAGE")
            return 2
        rejection = _rejected(
            field="cli",
            code="CLI_USAGE",
            message=str(error),
        )
        _emit(rejection)
        return exit_code_for(rejection)

    if namespace.command == "capture-provider":
        return _run_capture(namespace)

    if namespace.command == "benchmark-esci":
        return _run_esci_benchmark(namespace)

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

    if namespace.command == "agent-demo":
        mode_rejection = _agent_mode_rejection(namespace)
        if mode_rejection is not None:
            _emit(mode_rejection)
            return exit_code_for(mode_rejection)
        request = validate_search_request(_agent_payload(namespace))
        if isinstance(request, RequestRejected):
            _emit(request)
            return exit_code_for(request)
        from glodex.agent_bootstrap import AgentPreflightError

        try:
            execution = asyncio.run(
                _execute_agent(
                    config=config,
                    request=request,
                    live_data=namespace.live_data,
                    output_root=namespace.output_root,
                    service_factory=agent_service_factory,
                )
            )
        except AgentPreflightError as error:
            message = str(error).partition(": ")[2] or "Agent live activation failed."
            rejection = _rejected(
                field="agent",
                code=error.code,
                message=message,
            )
            _emit(rejection)
            return exit_code_for(rejection)
        return _emit_agent_execution(execution)

    if namespace.command == "search" and namespace.live_intent:
        from glodex.adapters.deepseek_http import DeepSeekPreflightError
        from glodex.bootstrap import build_live_intent_service

        try:
            service = build_live_intent_service(config)
        except DeepSeekPreflightError as error:
            message = str(error).partition(": ")[2] or "DeepSeek live activation failed."
            rejection = _rejected(
                field="intent",
                code=error.code,
                message=message,
            )
            _emit(rejection)
            return exit_code_for(rejection)
    else:
        service = build_service(config)
    search_outcome = asyncio.run(submit_search(_payload(namespace, config), service))
    _emit(search_outcome)
    return exit_code_for(search_outcome)


__all__ = ["DEMO_QUERY", "exit_code_for", "main"]
