"""JSON-only command-line entrypoint for Glodex."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import uuid
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

    m2a_index = subparsers.add_parser(
        "m2a-index",
        help="build or verify the fixed local M2a OpenSearch indexes",
    )
    m2a_index.add_argument("--config", type=Path, help="path to glodex.toml")
    m2a_index.add_argument("--action", choices=("build", "verify"), required=True)
    m2a_index.add_argument("--snapshot", default=_AGENT_DEMO_SNAPSHOT)

    m2a_profile = subparsers.add_parser(
        "m2a-profile",
        help="manage one explicit local M2a soft-preference profile",
    )
    m2a_profile.add_argument("--config", type=Path, help="path to glodex.toml")
    m2a_profile.add_argument("--action", choices=("set", "list", "delete"), required=True)
    m2a_profile.add_argument("--live", action="store_true")
    m2a_profile.add_argument("--profile", required=True)
    m2a_profile.add_argument("--entry-id")
    m2a_profile.add_argument("--scope", choices=("soft",), default="soft")
    m2a_profile.add_argument("--kind", choices=("preference",), default="preference")
    m2a_profile.add_argument("--value")

    m2c_index = subparsers.add_parser(
        "m2c-index",
        help="build or verify the isolated BGE-backed M2c OpenSearch indexes",
    )
    m2c_index.add_argument("--config", type=Path, help="path to glodex.toml")
    m2c_index.add_argument("--action", choices=("build", "verify"), required=True)
    m2c_index.add_argument("--snapshot", default=_AGENT_DEMO_SNAPSHOT)
    m2c_index.add_argument("--live", action="store_true")

    m2c_profile = subparsers.add_parser(
        "m2c-profile",
        help="manage one explicit local M2c BGE soft-preference profile",
    )
    m2c_profile.add_argument("--config", type=Path, help="path to glodex.toml")
    m2c_profile.add_argument("--action", choices=("set", "list", "delete"), required=True)
    m2c_profile.add_argument("--live", action="store_true")
    m2c_profile.add_argument("--profile", required=True)
    m2c_profile.add_argument("--entry-id")
    m2c_profile.add_argument("--scope", choices=("soft",), default="soft")
    m2c_profile.add_argument("--kind", choices=("preference",), default="preference")
    m2c_profile.add_argument("--value")

    m2a_agent = subparsers.add_parser(
        "m2a-agent-demo",
        help="run the explicit OpenSearch/Profile/Rerank M2a Agent composition",
    )
    _add_common_options(m2a_agent)
    m2a_agent.add_argument("--live", action="store_true", required=True)
    m2a_agent.add_argument("--profile")
    m2a_agent.add_argument("--query", required=True)

    m2c_agent = subparsers.add_parser(
        "m2c-agent-demo",
        help="run the explicit BGE/OpenSearch/cross-encoder M2c Agent composition",
    )
    _add_common_options(m2c_agent)
    m2c_agent.add_argument("--live", action="store_true")
    m2c_agent.add_argument("--profile")
    m2c_agent.add_argument("--query", required=True)

    m2a_eval = subparsers.add_parser(
        "m2a-eval-esci",
        help="compare fixed coarse and explicit qwen3-rerank ESCI aggregate metrics",
    )
    m2a_eval.add_argument("--live", action="store_true", required=True)
    m2a_eval.add_argument(
        "--artifact-root",
        type=Path,
        default=_DEFAULT_ESCI_ARTIFACT_ROOT,
    )

    m2c_eval = subparsers.add_parser(
        "m2c-eval-esci",
        help="compare fixed coarse and explicit BGE cross-encoder ESCI aggregate metrics",
    )
    m2c_eval.add_argument("--live", action="store_true")
    m2c_eval.add_argument(
        "--artifact-root",
        type=Path,
        default=_DEFAULT_ESCI_ARTIFACT_ROOT,
    )

    m2b_migrate = subparsers.add_parser(
        "m2b-migrate",
        help="apply the explicit local M2b PostgreSQL schema migration",
    )
    m2b_migrate.add_argument("--live", action="store_true", required=True)

    m2b_profile = subparsers.add_parser(
        "m2b-profile",
        help="manage one durable M2b soft-preference profile",
    )
    m2b_profile.add_argument("--action", choices=("set", "list", "delete"), required=True)
    m2b_profile.add_argument("--live", action="store_true")
    m2b_profile.add_argument("--profile", required=True)
    m2b_profile.add_argument("--entry-id")
    m2b_profile.add_argument("--value")

    m2b_verify = subparsers.add_parser(
        "m2b-verify",
        help="verify explicit local M2b PostgreSQL and Redis services",
    )
    m2b_verify.add_argument("--live", action="store_true", required=True)

    m2b_serve = subparsers.add_parser(
        "m2b-serve",
        help="serve the explicit loopback M2b durable Agent API",
    )
    m2b_serve.add_argument("--config", type=Path, help="path to glodex.toml")
    m2b_serve.add_argument("--live", action="store_true", required=True)

    m2c_model_verify = subparsers.add_parser(
        "m2c-model-verify",
        help="verify the fixed loopback M2c GPU model service",
    )
    m2c_model_verify.add_argument("--live", action="store_true")

    m2c_gpu_service = subparsers.add_parser(
        "m2c-gpu-service",
        help="run the private M2c GPU model service",
    )
    m2c_gpu_service.add_argument("--manifest", type=Path, required=True)

    m2b_agent = subparsers.add_parser(
        "m2b-durable-agent-demo",
        help="run the explicit PostgreSQL-backed M2a durable Agent composition",
    )
    _add_common_options(m2b_agent)
    m2b_agent.add_argument("--live", action="store_true", required=True)
    m2b_agent.add_argument("--profile")
    m2b_agent.add_argument("--query", required=True)
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


def _m2a_agent_payload(namespace: argparse.Namespace) -> dict[str, object]:
    return {
        "query": namespace.query,
        "locale": namespace.locale or "zh-CN",
        "display_currency": namespace.currency or "CNY",
        "top_k": namespace.top_k if namespace.top_k is not None else 3,
        "snapshot_version": namespace.snapshot or _AGENT_DEMO_SNAPSHOT,
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


def _emit_m2a_agent_execution(execution: object) -> int:
    """Emit the unchanged safe Agent response plus M2a's opaque retrieval trace."""

    from glodex.agent_bootstrap import M2aAgentExecution

    if type(execution) is not M2aAgentExecution:
        raise TypeError("M2a Agent CLI requires exact M2aAgentExecution")
    payload = {
        "agent": execution.execution.response.model_dump(mode="json"),
        "retrieval_trace": {
            "category": [
                {"candidate_count": count, "safe_codes": codes, "selected_ids": identities}
                for count, identities, codes in execution.category_traces
            ],
            "items": [
                {
                    "query_candidate_count": query_count,
                    "safe_codes": codes,
                    "selected_ids": identities,
                    "user_candidate_count": user_count,
                }
                for query_count, user_count, identities, codes in execution.item_traces
            ],
        },
        "status": execution.execution.response.status.value,
    }
    rendered = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    sys.stdout.write(rendered + "\n")
    return 1 if execution.execution.response.status is RunStatus.FAILED else 0


def _emit_m2c_agent_execution(execution: object) -> int:
    """Emit the Agent response plus M2c's manifest prefix and opaque trace only."""

    from glodex.agent_bootstrap import M2cAgentExecution

    if type(execution) is not M2cAgentExecution:
        raise TypeError("M2c Agent CLI requires exact M2cAgentExecution")
    payload = {
        "agent": execution.execution.response.model_dump(mode="json"),
        "model_manifest": execution.manifest_prefix,
        "retrieval_trace": {
            "category": [
                {"candidate_count": count, "safe_codes": codes, "selected_ids": identities}
                for count, identities, codes in execution.category_traces
            ],
            "items": [
                {
                    "query_candidate_count": query_count,
                    "safe_codes": codes,
                    "selected_ids": identities,
                    "user_candidate_count": user_count,
                }
                for query_count, user_count, identities, codes in execution.item_traces
            ],
        },
        "status": execution.execution.response.status.value,
    }
    sys.stdout.write(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    )
    return 1 if execution.execution.response.status is RunStatus.FAILED else 0


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


def _emit_m2a(*, status: str, code: str | None = None, **values: object) -> None:
    """Emit a one-line M2a envelope containing only safe counts and opaque IDs."""

    payload: dict[str, object] = {"status": status, **values}
    if code is not None:
        payload["code"] = code
    rendered = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    sys.stdout.write(rendered + "\n")


def _emit_m2b(*, status: str, code: str | None = None, **values: object) -> None:
    """Emit only safe M2b operator facts; no profile value/vector or DSN is public."""

    payload: dict[str, object] = {"status": status, **values}
    if code is not None:
        payload["code"] = code
    rendered = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    sys.stdout.write(rendered + "\n")


def _emit_m2c(*, status: str, code: str | None = None, **values: object) -> None:
    """Emit only M2c model identity summaries, never private manifest details."""

    payload: dict[str, object] = {"status": status, **values}
    if code is not None:
        payload["code"] = code
    rendered = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    sys.stdout.write(rendered + "\n")


async def _run_m2b_migrate(namespace: argparse.Namespace) -> int:
    from glodex.adapters.m2b_postgres import M2bPostgresStore
    from glodex.application.durable.contracts import DurableStoreError

    if not namespace.live:
        _emit_m2b(status="FAILED", code="M2B_LIVE_REQUIRED")
        return 2
    store = M2bPostgresStore()
    try:
        applied = await store.migrate()
        await store.health()
        _emit_m2b(status="OK", action="migrate", migrations_applied=applied)
        return 0
    except DurableStoreError:
        _emit_m2b(status="FAILED", code="M2B_STORE_UNAVAILABLE")
        return 1
    finally:
        await store.close()


async def _run_m2b_profile(namespace: argparse.Namespace) -> int:
    from glodex.adapters.agent_indexes import EMBEDDING_MODEL
    from glodex.adapters.agent_live_http import build_dashscope_embedding
    from glodex.adapters.m2b_postgres import M2bPostgresStore
    from glodex.application.agent.contracts import EmbeddingBatch
    from glodex.application.agent.ports import ToolPortError
    from glodex.application.durable.contracts import DurableStoreError
    from glodex.application.m2a_profile import profile_entry_from_operator

    if namespace.action == "set" and (not namespace.live or namespace.value is None):
        _emit_m2b(status="FAILED", code="M2B_PROFILE_LIVE_REQUIRED")
        return 2
    if namespace.action == "delete" and namespace.entry_id is None:
        _emit_m2b(status="FAILED", code="M2B_PROFILE_ENTRY_REQUIRED")
        return 2
    store = M2bPostgresStore()
    try:
        if namespace.action == "set":
            embedding_port = build_dashscope_embedding()
            embedding = await embedding_port.embed(EmbeddingBatch(texts=(namespace.value,)))
            entry = profile_entry_from_operator(
                profile_id=namespace.profile,
                entry_id=namespace.entry_id,
                value=namespace.value,
                vector=embedding.vectors[0],
            )
            snapshot = await store.set_profile_entry(
                entry=entry,
                embedding_model=EMBEDDING_MODEL,
            )
            _emit_m2b(
                status="OK",
                action="set",
                entry_id=entry.entry_id,
                profile=entry.profile_id,
                revision=snapshot.revision,
            )
            return 0
        if namespace.action == "delete":
            snapshot = await store.delete_profile_entry(
                profile_id=namespace.profile,
                entry_id=namespace.entry_id,
            )
            _emit_m2b(
                status="OK",
                action="delete",
                entry_count=len(snapshot.entries),
                profile=namespace.profile,
                revision=snapshot.revision,
            )
            return 0
        snapshot = await store.profile_snapshot(profile_id=namespace.profile)
        _emit_m2b(
            status="OK",
            action="list",
            entry_ids=[entry.entry_id for entry in snapshot.entries],
            profile=namespace.profile,
            revision=snapshot.revision,
        )
        return 0
    except (DurableStoreError, ToolPortError, ValueError):
        _emit_m2b(status="FAILED", code="M2B_PROFILE_UNAVAILABLE")
        return 1
    finally:
        await store.close()


async def _run_m2b_verify(namespace: argparse.Namespace) -> int:
    from glodex.adapters.m2b_postgres import M2bPostgresStore
    from glodex.adapters.m2b_redis import M2bRedisCache
    from glodex.application.durable.contracts import DurableCacheValue, DurableStoreError, cache_key

    if not namespace.live:
        _emit_m2b(status="FAILED", code="M2B_LIVE_REQUIRED")
        return 2
    store = M2bPostgresStore()
    cache = M2bRedisCache()
    try:
        await store.health()
        key = cache_key(namespace="context", material={"command": "m2b-verify"})
        value = DurableCacheValue(
            namespace="context",
            version="glodex.m2b.v1",
            identities=("m2b-verify",),
            metadata=(("verified", 1),),
        )
        await cache.set(key=key, value=value, ttl_seconds=300)
        cache_ok = (await cache.get(key=key, namespace="context")) == value
        _emit_m2b(status="OK", cache_round_trip=cache_ok, postgres_health="ok")
        return 0
    except DurableStoreError:
        _emit_m2b(status="FAILED", code="M2B_STORE_UNAVAILABLE")
        return 1
    finally:
        await cache.close()
        await store.close()


def _run_m2b_serve(*, config: GlodexConfig) -> int:
    """Run the one explicit, fixed-loopback M2b API composition."""

    from glodex.adapters.m2b_m2a_executor import M2bM2aExecutor
    from glodex.adapters.m2b_postgres import M2bPostgresStore
    from glodex.adapters.m2b_redis import M2bRedisCache
    from glodex.api.agent_events import AgentEventProjector
    from glodex.api.durable_agent_app import create_durable_agent_app
    from glodex.bootstrap import SystemClock

    try:
        import uvicorn
    except ImportError:
        _emit_m2b(status="FAILED", code="M2B_SERVER_UNAVAILABLE")
        return 1
    store = M2bPostgresStore()
    cache = M2bRedisCache()
    app = create_durable_agent_app(
        executor=M2bM2aExecutor(config=config, store=store, cache=cache),
        store=store,
        projector=AgentEventProjector(clock=SystemClock()),
        asset_version="m2b-m2a-agent-v1",
        config_fingerprint=config.fingerprint,
        context_cache=cache,
        shutdown_callback=cache.close,
    )
    _emit_m2b(status="STARTING", host="127.0.0.1", port=8766)
    uvicorn.run(app, host="127.0.0.1", port=8766, access_log=False, log_config=None)
    return 0


async def _run_m2c_model_verify(namespace: argparse.Namespace) -> int:
    from glodex.adapters.m2c_model_service import M2cModelServiceClient, M2cModelServiceError

    if not namespace.live:
        _emit_m2c(status="FAILED", code="M2C_LIVE_REQUIRED")
        return 2
    try:
        identity = await M2cModelServiceClient().health()
    except M2cModelServiceError:
        _emit_m2c(status="FAILED", code="M2C_MODEL_UNAVAILABLE")
        return 1
    _emit_m2c(
        status="OK",
        dimension=identity.dimension,
        manifest=identity.manifest_digest[:16],
        model=identity.embedding_model,
    )
    return 0


def _run_m2c_gpu_service(namespace: argparse.Namespace) -> int:
    from glodex.m2c_gpu_service import serve_private_gpu_service

    return serve_private_gpu_service(manifest_path=namespace.manifest)


async def _run_m2b_durable_agent(
    *,
    config: GlodexConfig,
    request: object,
    profile_id: str | None,
) -> int:
    from glodex.adapters.m2b_m2a_executor import M2bM2aExecutor
    from glodex.adapters.m2b_postgres import M2bPostgresStore
    from glodex.adapters.m2b_redis import M2bRedisCache
    from glodex.api.agent_events import AgentEventProjector
    from glodex.application.durable.contracts import DurableRunState, DurableStoreError
    from glodex.application.durable.runtime import DurableAgentCoordinator
    from glodex.bootstrap import SystemClock
    from glodex.contracts import SearchRequest

    if type(request) is not SearchRequest:
        raise TypeError("M2b durable Agent CLI requires an exact SearchRequest")
    store = M2bPostgresStore()
    cache = M2bRedisCache()
    clock = SystemClock()
    coordinator = DurableAgentCoordinator(
        store=store,
        executor=M2bM2aExecutor(config=config, store=store, cache=cache),
        projector=AgentEventProjector(clock=clock),
        asset_version="m2b-m2a-agent-v1",
        config_fingerprint=config.fingerprint,
        context_cache=cache,
    )
    try:
        await store.health()
        submitted = await coordinator.submit(
            request=request,
            thread_id=f"thread-m2b-{uuid.uuid4().hex}",
            profile_id=profile_id,
            profile_revision=(
                0
                if profile_id is None
                else (await store.profile_snapshot(profile_id=profile_id)).revision
            ),
        )
        for _attempt in range(1_200):
            run = await store.load_run(run_id=submitted.run_id)
            if run.state in {
                DurableRunState.COMPLETED,
                DurableRunState.NO_MATCH,
                DurableRunState.FAILED,
                DurableRunState.ABORTED,
            }:
                events = await store.load_events(run_id=run.run_id)
                _emit_m2b(
                    status=run.state.value,
                    event_count=len(events),
                    run_id=run.run_id,
                    safe_code=run.terminal_error_code,
                )
                return (
                    0 if run.state in {DurableRunState.COMPLETED, DurableRunState.NO_MATCH} else 1
                )
            await asyncio.sleep(0.1)
        await coordinator.cancel(run_id=submitted.run_id)
        _emit_m2b(status="FAILED", code="M2B_AGENT_TIMEOUT", run_id=submitted.run_id)
        return 1
    except DurableStoreError:
        _emit_m2b(status="FAILED", code="M2B_STORE_UNAVAILABLE")
        return 1
    finally:
        await coordinator.shutdown()
        await cache.close()
        await store.close()


async def _load_m2a_indexes(config: GlodexConfig) -> object:
    from glodex.adapters.agent_indexes import M1D_DEMO_VERSION, load_agent_indexes
    from glodex.application.agent.ports import ToolPortError

    try:
        return await load_agent_indexes(
            snapshot_root=config.data_dir,
            agent_root=config.data_dir.parent / "agent",
            version=M1D_DEMO_VERSION,
        )
    except ToolPortError:
        raise RuntimeError("M2A_RETRIEVAL_FAILED") from None


async def _run_m2a_index(namespace: argparse.Namespace, config: GlodexConfig) -> int:
    from glodex.adapters.agent_indexes import M1D_DEMO_VERSION, AgentIndexes
    from glodex.adapters.m2a_indexes import M2aIndexError, build_indexes, verify_indexes
    from glodex.adapters.m2a_opensearch import M2aOpenSearch

    if namespace.snapshot != M1D_DEMO_VERSION:
        _emit_m2a(status="FAILED", code="M2A_SNAPSHOT_UNSUPPORTED")
        return 2
    try:
        indexes = await _load_m2a_indexes(config)
        if type(indexes) is not AgentIndexes:
            raise RuntimeError("M2A_RETRIEVAL_FAILED")
        async with M2aOpenSearch() as client:
            result = (
                await build_indexes(client=client, indexes=indexes)
                if namespace.action == "build"
                else await verify_indexes(client=client, indexes=indexes)
            )
        _emit_m2a(
            status="OK",
            action=namespace.action,
            card_count=result.card_count,
            product_count=result.product_count,
            profile_count=result.profile_count,
            version=result.manifest_fingerprint[:16],
        )
        return 0
    except (M2aIndexError, RuntimeError):
        _emit_m2a(status="FAILED", code="M2A_RETRIEVAL_FAILED")
        return 1


async def _run_m2a_profile(namespace: argparse.Namespace, config: GlodexConfig) -> int:
    from glodex.adapters.agent_live_http import build_dashscope_embedding
    from glodex.adapters.m2a_opensearch import M2aOpenSearch
    from glodex.adapters.m2a_profile_store import M2aProfileStore
    from glodex.application.agent.contracts import EmbeddingBatch
    from glodex.application.agent.ports import ToolPortError
    from glodex.application.m2a_profile import M2aProfileError, profile_entry_from_operator

    if namespace.action == "set" and (not namespace.live or namespace.value is None):
        _emit_m2a(status="FAILED", code="M2A_PROFILE_LIVE_REQUIRED")
        return 2
    if namespace.action == "delete" and namespace.entry_id is None:
        _emit_m2a(status="FAILED", code="M2A_PROFILE_ENTRY_REQUIRED")
        return 2
    try:
        async with M2aOpenSearch() as client:
            store = M2aProfileStore(client)
            if namespace.action == "set":
                embedding_port = build_dashscope_embedding()
                embedding = await embedding_port.embed(EmbeddingBatch(texts=(namespace.value,)))
                vector = embedding.vectors[0]
                entry = profile_entry_from_operator(
                    profile_id=namespace.profile,
                    entry_id=namespace.entry_id,
                    value=namespace.value,
                    vector=vector,
                )
                await store.set(entry)
                _emit_m2a(
                    status="OK",
                    action="set",
                    profile=entry.profile_id,
                    entry_id=entry.entry_id,
                )
                return 0
            if namespace.action == "delete":
                deleted = await store.delete(
                    profile_id=namespace.profile,
                    entry_id=namespace.entry_id,
                )
                _emit_m2a(
                    status="OK",
                    action="delete",
                    deleted=deleted,
                    profile=namespace.profile,
                )
                return 0
            entries = await store.list(profile_id=namespace.profile)
            _emit_m2a(
                status="OK",
                action="list",
                entry_ids=[entry.entry_id for entry in entries],
                profile=namespace.profile,
            )
            return 0
    except (M2aProfileError, ToolPortError):
        _emit_m2a(status="FAILED", code="M2A_PROFILE_DEGRADED")
        return 1


async def _load_m2c_indexes(config: GlodexConfig) -> object:
    from glodex.adapters.agent_indexes import M1D_DEMO_VERSION, load_agent_indexes
    from glodex.application.agent.ports import ToolPortError

    try:
        return await load_agent_indexes(
            snapshot_root=config.data_dir,
            agent_root=config.data_dir.parent / "agent",
            version=M1D_DEMO_VERSION,
        )
    except ToolPortError:
        raise RuntimeError("M2C_INDEX_FAILED") from None


async def _run_m2c_index(namespace: argparse.Namespace, config: GlodexConfig) -> int:
    from glodex.adapters.agent_indexes import M1D_DEMO_VERSION, AgentIndexes
    from glodex.adapters.m2a_opensearch import M2aOpenSearch
    from glodex.adapters.m2c_indexes import M2cIndexError, build_indexes, verify_indexes
    from glodex.adapters.m2c_model_service import M2cModelServiceClient, M2cModelServiceError

    if not namespace.live:
        _emit_m2c(status="FAILED", code="M2C_LIVE_REQUIRED")
        return 2
    if namespace.snapshot != M1D_DEMO_VERSION:
        _emit_m2c(status="FAILED", code="M2C_SNAPSHOT_UNSUPPORTED")
        return 2
    try:
        indexes = await _load_m2c_indexes(config)
        if type(indexes) is not AgentIndexes:
            raise RuntimeError("M2C_INDEX_FAILED")
        gpu = M2cModelServiceClient()
        async with M2aOpenSearch() as client:
            result = (
                await build_indexes(client=client, indexes=indexes, gpu=gpu)
                if namespace.action == "build"
                else await verify_indexes(
                    client=client,
                    indexes=indexes,
                    identity=await gpu.health(),
                )
            )
        _emit_m2c(
            status="OK",
            action=namespace.action,
            card_count=result.card_count,
            product_count=result.product_count,
            profile_count=result.profile_count,
            version=result.manifest_fingerprint[:16],
        )
        return 0
    except M2cModelServiceError:
        _emit_m2c(status="FAILED", code="M2C_MODEL_UNAVAILABLE")
        return 1
    except (M2cIndexError, RuntimeError):
        _emit_m2c(status="FAILED", code="M2C_INDEX_FAILED")
        return 1


async def _run_m2c_profile(namespace: argparse.Namespace, _config: GlodexConfig) -> int:
    from glodex.adapters.m2a_opensearch import M2aOpenSearch
    from glodex.adapters.m2c_model_service import M2cModelServiceClient, M2cModelServiceError
    from glodex.adapters.m2c_profile_store import M2cProfileStore
    from glodex.application.m2c_profile import M2cProfileError, profile_entry_from_operator

    if not namespace.live:
        _emit_m2c(status="FAILED", code="M2C_LIVE_REQUIRED")
        return 2
    if namespace.action == "set" and namespace.value is None:
        _emit_m2c(status="FAILED", code="M2C_PROFILE_VALUE_REQUIRED")
        return 2
    if namespace.action == "delete" and namespace.entry_id is None:
        _emit_m2c(status="FAILED", code="M2C_PROFILE_ENTRY_REQUIRED")
        return 2
    try:
        gpu = M2cModelServiceClient()
        identity = await gpu.health()
        async with M2aOpenSearch() as client:
            store = M2cProfileStore(client)
            if namespace.action == "set":
                vectors = await gpu.embed_texts(texts=(namespace.value,), identity=identity)
                entry = profile_entry_from_operator(
                    profile_id=namespace.profile,
                    entry_id=namespace.entry_id,
                    value=namespace.value,
                    vector=vectors[0],
                    model_manifest_digest=identity.manifest_digest,
                )
                await store.set(entry)
                _emit_m2c(
                    status="OK",
                    action="set",
                    entry_id=entry.entry_id,
                    profile=entry.profile_id,
                )
                return 0
            if namespace.action == "delete":
                deleted = await store.delete(
                    profile_id=namespace.profile,
                    entry_id=namespace.entry_id,
                )
                _emit_m2c(
                    status="OK",
                    action="delete",
                    deleted=deleted,
                    profile=namespace.profile,
                )
                return 0
            entries = await store.list(profile_id=namespace.profile, identity=identity)
            _emit_m2c(
                status="OK",
                action="list",
                entry_ids=[entry.entry_id for entry in entries],
                profile=namespace.profile,
            )
            return 0
    except M2cModelServiceError:
        _emit_m2c(status="FAILED", code="M2C_MODEL_UNAVAILABLE")
        return 1
    except M2cProfileError as error:
        _emit_m2c(status="FAILED", code=str(error))
        return 1


async def _execute_m2a_agent(
    *,
    config: GlodexConfig,
    request: object,
    profile_id: str | None,
) -> object:
    from glodex.agent_bootstrap import M2aAgentExecution, build_m2a_agent_service
    from glodex.contracts import SearchRequest

    if type(request) is not SearchRequest:
        raise TypeError("M2a Agent CLI requires an exact SearchRequest")
    service = await build_m2a_agent_service(
        config,
        profile_id=profile_id,
        preflight_query=request.query,
    )
    execution = await service.execute_with_trace(request)
    if type(execution) is not M2aAgentExecution:
        raise TypeError("M2a Agent composition must return exact M2aAgentExecution")
    return execution


async def _execute_m2c_agent(
    *,
    config: GlodexConfig,
    request: object,
    profile_id: str | None,
) -> object:
    from glodex.agent_bootstrap import M2cAgentExecution, build_m2c_agent_service
    from glodex.contracts import SearchRequest

    if type(request) is not SearchRequest:
        raise TypeError("M2c Agent CLI requires an exact SearchRequest")
    service = await build_m2c_agent_service(
        config,
        profile_id=profile_id,
        preflight_query=request.query,
    )
    execution = await service.execute_with_trace(request)
    if type(execution) is not M2cAgentExecution:
        raise TypeError("M2c Agent composition must return exact M2cAgentExecution")
    return execution


async def _run_m2a_esci_eval(namespace: argparse.Namespace) -> int:
    from glodex.adapters.dashscope_rerank import M2aRerankError, build_dashscope_reranker
    from glodex.esci_benchmark import BenchmarkArtifactError
    from glodex.m2a_esci_eval import comparison_summary, evaluate_m2a_rerank

    try:
        result = await evaluate_m2a_rerank(
            artifact_root=namespace.artifact_root,
            reranker=build_dashscope_reranker(),
        )
        rendered = json.dumps(
            comparison_summary(result),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        sys.stdout.write(rendered + "\n")
        return 0
    except BenchmarkArtifactError:
        _emit_m2a(status="FAILED", code="BENCHMARK_ARTIFACT_INVALID")
        return 1
    except M2aRerankError:
        _emit_m2a(status="FAILED", code="M2A_RERANK_DEGRADED")
        return 1


async def _run_m2c_esci_eval(namespace: argparse.Namespace) -> int:
    if not namespace.live:
        _emit_m2c(status="FAILED", code="M2C_LIVE_REQUIRED")
        return 2

    from glodex.adapters.m2c_model_service import (
        M2cModelServiceClient,
        M2cModelServiceError,
        M2cReranker,
    )
    from glodex.esci_benchmark import BenchmarkArtifactError
    from glodex.m2c_esci_eval import comparison_summary, evaluate_m2c_rerank

    try:
        client = M2cModelServiceClient()
        reranker = M2cReranker(client=client, identity=await client.health())
        result = await evaluate_m2c_rerank(
            artifact_root=namespace.artifact_root,
            reranker=reranker,
        )
        rendered = json.dumps(
            comparison_summary(result),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        sys.stdout.write(rendered + "\n")
        return 0
    except BenchmarkArtifactError:
        _emit_m2c(status="FAILED", code="BENCHMARK_ARTIFACT_INVALID")
        return 1
    except M2cModelServiceError:
        _emit_m2c(status="FAILED", code="M2C_MODEL_UNAVAILABLE")
        return 1


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

    if namespace.command == "m2a-eval-esci":
        return asyncio.run(_run_m2a_esci_eval(namespace))

    if namespace.command == "m2c-eval-esci":
        return asyncio.run(_run_m2c_esci_eval(namespace))

    if namespace.command == "m2b-migrate":
        return asyncio.run(_run_m2b_migrate(namespace))

    if namespace.command == "m2b-profile":
        return asyncio.run(_run_m2b_profile(namespace))

    if namespace.command == "m2b-verify":
        return asyncio.run(_run_m2b_verify(namespace))

    if namespace.command == "m2c-model-verify":
        return asyncio.run(_run_m2c_model_verify(namespace))

    if namespace.command == "m2c-gpu-service":
        return _run_m2c_gpu_service(namespace)

    if namespace.command in {"m2c-index", "m2c-profile", "m2c-agent-demo"} and not namespace.live:
        _emit_m2c(status="FAILED", code="M2C_LIVE_REQUIRED")
        return 2

    try:
        config = load_config(explicit_path=namespace.config)
    except ConfigurationError as error:
        rejection = _configuration_rejected(error)
        _emit(rejection)
        return exit_code_for(rejection)

    if namespace.command == "m2a-index":
        return asyncio.run(_run_m2a_index(namespace, config))

    if namespace.command == "m2c-index":
        return asyncio.run(_run_m2c_index(namespace, config))

    if namespace.command == "m2b-serve":
        return _run_m2b_serve(config=config)

    if namespace.command == "m2a-profile":
        return asyncio.run(_run_m2a_profile(namespace, config))

    if namespace.command == "m2c-profile":
        return asyncio.run(_run_m2c_profile(namespace, config))

    if namespace.command == "m2a-agent-demo":
        if namespace.snapshot not in (None, _AGENT_DEMO_SNAPSHOT):
            rejection = _rejected(
                field="snapshot",
                code="M2A_SNAPSHOT_UNSUPPORTED",
                message="M2a Agent Demo supports only m1d-demo-v1",
            )
            _emit(rejection)
            return exit_code_for(rejection)
        request = validate_search_request(_m2a_agent_payload(namespace))
        if isinstance(request, RequestRejected):
            _emit(request)
            return exit_code_for(request)
        from glodex.agent_bootstrap import AgentPreflightError

        try:
            execution = asyncio.run(
                _execute_m2a_agent(
                    config=config,
                    request=request,
                    profile_id=namespace.profile,
                )
            )
        except AgentPreflightError as error:
            message = str(error).partition(": ")[2] or "M2a Agent activation failed."
            rejection = _rejected(field="agent", code=error.code, message=message)
            _emit(rejection)
            return exit_code_for(rejection)
        return _emit_m2a_agent_execution(execution)

    if namespace.command == "m2c-agent-demo":
        if namespace.snapshot not in (None, _AGENT_DEMO_SNAPSHOT):
            rejection = _rejected(
                field="snapshot",
                code="M2C_SNAPSHOT_UNSUPPORTED",
                message="M2c Agent Demo supports only m1d-demo-v1",
            )
            _emit(rejection)
            return exit_code_for(rejection)
        request = validate_search_request(_m2a_agent_payload(namespace))
        if isinstance(request, RequestRejected):
            _emit(request)
            return exit_code_for(request)
        from glodex.agent_bootstrap import AgentPreflightError

        try:
            execution = asyncio.run(
                _execute_m2c_agent(
                    config=config,
                    request=request,
                    profile_id=namespace.profile,
                )
            )
        except AgentPreflightError as error:
            message = str(error).partition(": ")[2] or "M2c Agent activation failed."
            rejection = _rejected(field="agent", code=error.code, message=message)
            _emit(rejection)
            return exit_code_for(rejection)
        return _emit_m2c_agent_execution(execution)

    if namespace.command == "m2b-durable-agent-demo":
        if namespace.snapshot not in (None, _AGENT_DEMO_SNAPSHOT):
            rejection = _rejected(
                field="snapshot",
                code="M2B_SNAPSHOT_UNSUPPORTED",
                message="M2b Agent Demo supports only m1d-demo-v1",
            )
            _emit(rejection)
            return exit_code_for(rejection)
        request = validate_search_request(_m2a_agent_payload(namespace))
        if isinstance(request, RequestRejected):
            _emit(request)
            return exit_code_for(request)
        return asyncio.run(
            _run_m2b_durable_agent(
                config=config,
                request=request,
                profile_id=namespace.profile,
            )
        )

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
