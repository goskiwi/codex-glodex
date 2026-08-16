"""JSON-only command-line entrypoint for Glodex."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Never

from glodex.config import load_config


class CliUsageError(ValueError):
    """An argparse failure that can be rendered through the public JSON contract."""


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise CliUsageError(message)


def _build_parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(prog="glodex")
    subparsers = parser.add_subparsers(dest="command", required=True)

    storage = subparsers.add_parser("storage", help="manage local durable storage")
    storage_subparsers = storage.add_subparsers(dest="storage_action", required=True)
    storage_migrate = storage_subparsers.add_parser("migrate")
    storage_migrate.add_argument("--live", action="store_true", required=True)
    storage_verify = storage_subparsers.add_parser("verify")
    storage_verify.add_argument("--live", action="store_true", required=True)

    operations = subparsers.add_parser("operations", help="manage operational retention")
    operations_subparsers = operations.add_subparsers(dest="operations_action", required=True)
    operations_retain = operations_subparsers.add_parser("retain")
    operations_retain.add_argument("--live", action="store_true", required=True)

    quality_evaluation = subparsers.add_parser(
        "quality-evaluation", help="run an offline quality evaluation"
    )
    quality_subparsers = quality_evaluation.add_subparsers(
        dest="quality_evaluation_action", required=True
    )
    quality_run = quality_subparsers.add_parser("run")
    quality_run.add_argument("--live", action="store_true", required=True)
    quality_run.add_argument("--run-id", required=True)
    quality_run.add_argument("--output", type=Path, required=True)
    quality_run.add_argument("--overwrite", action="store_true")

    model_prices = subparsers.add_parser("model-prices", help="manage model price tables")
    model_prices_subparsers = model_prices.add_subparsers(dest="model_prices_action", required=True)
    model_prices_import = model_prices_subparsers.add_parser("import")
    model_prices_import.add_argument("--live", action="store_true", required=True)
    model_prices_import.add_argument("--model", required=True)
    model_prices_import.add_argument("--price-table-version", required=True)
    model_prices_import.add_argument("--currency", required=True)
    model_prices_import.add_argument("--input-micro-units-per-token", required=True, type=int)
    model_prices_import.add_argument("--output-micro-units-per-token", required=True, type=int)
    model_prices_import.add_argument("--source-label", required=True)

    model_service = subparsers.add_parser("model-service", help="manage the private model service")
    model_service_subparsers = model_service.add_subparsers(
        dest="model_service_action", required=True
    )
    model_verify = model_service_subparsers.add_parser("verify")
    model_verify.add_argument("--live", action="store_true")
    model_serve = model_service_subparsers.add_parser("serve")
    model_serve.add_argument("--manifest", type=Path, required=True)

    product_index = subparsers.add_parser("product-index", help="build or verify product indexes")
    product_index_subparsers = product_index.add_subparsers(dest="action", required=True)
    for action in ("build", "verify"):
        index_action = product_index_subparsers.add_parser(action)
        index_action.add_argument("--binding", type=Path, help="path to the product-index binding")
        index_action.add_argument("--live", action="store_true", required=True)

    agent_api = subparsers.add_parser("agent-api", help="serve the durable shopping Agent API")
    agent_api_subparsers = agent_api.add_subparsers(dest="agent_api_action", required=True)
    agent_api_serve = agent_api_subparsers.add_parser("serve")
    agent_api_serve.add_argument("--config", type=Path, help="path to glodex.toml")
    agent_api_serve.add_argument("--live", action="store_true", required=True)
    agent_api_serve.add_argument("--price-table-version")
    agent_api_serve.add_argument("--semantic-query-canary", action="store_true")

    web_console = subparsers.add_parser("web-console", help="serve the browser console")
    web_console_subparsers = web_console.add_subparsers(dest="web_console_action", required=True)
    web_console_serve = web_console_subparsers.add_parser("serve")
    web_console_serve.add_argument("--live", action="store_true", required=True)

    return parser


def _emit_durable(*, status: str, code: str | None = None, **values: object) -> None:
    """Emit only safe Durable operator facts; no memory value/vector or DSN is public."""

    payload: dict[str, object] = {"status": status, **values}
    if code is not None:
        payload["code"] = code
    rendered = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    sys.stdout.write(rendered + "\n")


def _emit_model_service(*, status: str, code: str | None = None, **values: object) -> None:
    """Emit only retrieval model identity summaries, never private manifest details."""

    payload: dict[str, object] = {"status": status, **values}
    if code is not None:
        payload["code"] = code
    rendered = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    sys.stdout.write(rendered + "\n")


async def _run_storage_migrate(namespace: argparse.Namespace) -> int:
    from glodex.infrastructure.postgres import DurablePostgresStore
    from glodex.runtime.contracts import DurableStoreError

    if not namespace.live:
        _emit_durable(status="FAILED", code="DURABLE_LIVE_REQUIRED")
        return 2
    store = DurablePostgresStore()
    try:
        applied = await store.migrate()
        await store.health()
        _emit_durable(status="OK", action="migrate", migrations_applied=applied)
        return 0
    except DurableStoreError:
        _emit_durable(status="FAILED", code="DURABLE_STORE_UNAVAILABLE")
        return 1
    finally:
        await store.close()


async def _run_storage_verify(namespace: argparse.Namespace) -> int:
    from glodex.infrastructure.postgres import DurablePostgresStore
    from glodex.infrastructure.redis import DurableRedisCache
    from glodex.runtime.contracts import (
        DURABLE_SCHEMA_VERSION,
        DurableCacheValue,
        DurableStoreError,
        cache_key,
    )

    if not namespace.live:
        _emit_durable(status="FAILED", code="DURABLE_LIVE_REQUIRED")
        return 2
    store = DurablePostgresStore()
    cache = DurableRedisCache()
    try:
        await store.health()
        key = cache_key(namespace="context", material={"command": "storage-verify"})
        value = DurableCacheValue(
            namespace="context",
            version=DURABLE_SCHEMA_VERSION,
            identities=("storage-verify",),
            metadata=(("verified", 1),),
        )
        await cache.set(key=key, value=value, ttl_seconds=300)
        cache_ok = (await cache.get(key=key, namespace="context")) == value
        _emit_durable(status="OK", cache_round_trip=cache_ok, postgres_health="ok")
        return 0
    except DurableStoreError:
        _emit_durable(status="FAILED", code="DURABLE_STORE_UNAVAILABLE")
        return 1
    finally:
        await cache.close()
        await store.close()


async def _run_operations_retain(namespace: argparse.Namespace) -> int:
    """Run only explicit, fixed-policy M6 retention; it never runs in request paths."""

    from datetime import UTC, datetime

    from glodex.infrastructure.postgres import DurablePostgresStore
    from glodex.runtime.contracts import DurableStoreError

    if not namespace.live:
        _emit_durable(status="FAILED", code="DURABLE_LIVE_REQUIRED")
        return 2
    store = DurablePostgresStore()
    try:
        report = await store.prune_m6_retention(now=datetime.now(UTC))
        _emit_durable(
            status="OK",
            action="m6_retain",
            deleted_event_count=report.deleted_event_count,
            deleted_metric_bucket_count=report.deleted_metric_bucket_count,
            deleted_trace_count=report.deleted_trace_count,
        )
        return 0
    except DurableStoreError:
        _emit_durable(status="FAILED", code="DURABLE_STORE_UNAVAILABLE")
        return 1
    finally:
        await store.close()


async def _run_quality_evaluation(namespace: argparse.Namespace) -> int:
    """Run the full judge outside request handling and persist its private report."""

    from glodex.agent.semantic_assertion import DeepSeekSemanticAssertion
    from glodex.infrastructure.postgres import DurablePostgresStore
    from glodex.llm.config import LlmConfigurationError, load_llm_configuration
    from glodex.llm.openai_compatible_http import build_json_completion_transport
    from glodex.quality.llm_judge import LlmQualityJudge, LlmRubricGenerator
    from glodex.quality.offline import assert_category_integrity, facts_from_durable_run
    from glodex.quality.runtime import M7JudgeStatus, M7OfflineEvaluator
    from glodex.runtime.contracts import DurableStoreError

    if not namespace.live:
        _emit_durable(status="FAILED", code="DURABLE_LIVE_REQUIRED")
        return 2
    output = namespace.output.resolve()
    if output.suffix.lower() != ".json" or not output.parent.is_dir():
        _emit_durable(status="FAILED", code="M7_OUTPUT_INVALID")
        return 2
    if output.exists() and not namespace.overwrite:
        _emit_durable(status="FAILED", code="M7_OUTPUT_EXISTS")
        return 2
    store = DurablePostgresStore()
    try:
        run = await store.load_run(run_id=namespace.run_id)
        try:
            trace = await store.load_m6_trace(run_id=namespace.run_id)
        except DurableStoreError as error:
            if str(error) != "DURABLE_M6_TRACE_NOT_FOUND":
                raise
            trace = None
        configuration = load_llm_configuration()
        transport = build_json_completion_transport(configuration=configuration)
        facts = await assert_category_integrity(
            facts=facts_from_durable_run(run=run, trace=trace),
            semantic_assertion=DeepSeekSemanticAssertion(
                transport,
                model_name=configuration.model_name,
            ),
        )
        report = await M7OfflineEvaluator(
            rubric_generator=LlmRubricGenerator(transport, model_name=configuration.model_name),
            quality_judge=LlmQualityJudge(transport, model_name=configuration.model_name),
            model_version=configuration.model_name,
        ).evaluate(facts=facts)
        rendered = json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        mode = "w" if namespace.overwrite else "x"
        with output.open(mode, encoding="utf-8") as handle:
            handle.write(rendered)
        await store.store_m7_offline_summary(summary=report.summary())
        _emit_durable(
            status="OK" if report.judge_status is not M7JudgeStatus.UNSCORED else "FAILED",
            action="m7_evaluate",
            run_id=namespace.run_id,
            judge_status=report.judge_status.value,
            reward=report.reward,
            output=str(output),
        )
        return 0 if report.judge_status is not M7JudgeStatus.UNSCORED else 1
    except (DurableStoreError, LlmConfigurationError, ValueError):
        _emit_durable(status="FAILED", code="M7_EVALUATION_UNAVAILABLE")
        return 1
    finally:
        await store.close()


async def _run_model_prices_import(namespace: argparse.Namespace) -> int:
    """Store a user-supplied, versioned price row without contacting any Provider."""

    from glodex.infrastructure.postgres import DurablePostgresStore
    from glodex.observability.runtime import ModelPriceTableEntry
    from glodex.runtime.contracts import DurableStoreError

    if not namespace.live:
        _emit_durable(status="FAILED", code="DURABLE_LIVE_REQUIRED")
        return 2
    try:
        entry = ModelPriceTableEntry(
            model=namespace.model,
            price_table_version=namespace.price_table_version,
            currency=namespace.currency,
            input_micro_units_per_token=namespace.input_micro_units_per_token,
            output_micro_units_per_token=namespace.output_micro_units_per_token,
            source_label=namespace.source_label,
        )
    except (TypeError, ValueError):
        _emit_durable(status="FAILED", code="DURABLE_M6_PRICE_TABLE_INVALID")
        return 2
    store = DurablePostgresStore()
    try:
        await store.upsert_m6_price_table(entry=entry)
        _emit_durable(status="OK", action="m6_price_import", model=entry.model)
        return 0
    except DurableStoreError:
        _emit_durable(status="FAILED", code="DURABLE_STORE_UNAVAILABLE")
        return 1
    finally:
        await store.close()


async def _run_model_service_verify(namespace: argparse.Namespace) -> int:
    from glodex.retrieval.model_service import RetrievalModelClient, RetrievalModelError

    if not namespace.live:
        _emit_model_service(status="FAILED", code="RETRIEVAL_MODEL_LIVE_REQUIRED")
        return 2
    try:
        identity = await RetrievalModelClient().health()
    except RetrievalModelError:
        _emit_model_service(status="FAILED", code="RETRIEVAL_MODEL_UNAVAILABLE")
        return 1
    _emit_model_service(
        status="OK",
        dimension=identity.dimension,
        manifest=identity.manifest_digest[:16],
        model=identity.embedding_model,
    )
    return 0


def _run_product_index(namespace: argparse.Namespace) -> int:
    """Delegate product-index operations to the one current-product builder."""

    script = (
        Path(__file__).resolve().parents[2]
        / "opensearch"
        / "current-product"
        / "build_current_product_index.py"
    )
    if not script.is_file():
        _emit_model_service(status="FAILED", code="PRODUCT_INDEX_UNAVAILABLE")
        return 1
    command = [sys.executable, str(script), "--action", namespace.action]
    if namespace.binding is not None:
        command.extend(("--binding", str(namespace.binding)))
    completed = subprocess.run(command, check=False)
    return completed.returncode


def _run_model_service_serve(namespace: argparse.Namespace) -> int:
    """Run the private model service with its explicit manifest."""

    from glodex.retrieval.gpu_service import serve_private_gpu_service

    try:
        return serve_private_gpu_service(manifest_path=namespace.manifest)
    except Exception:
        _emit_model_service(status="FAILED", code="MODEL_SERVICE_UNAVAILABLE")
        return 1


def _run_web_console() -> int:
    """Serve the fixed loopback browser console."""

    from glodex.api.console_app import create_web_console_app

    try:
        import uvicorn
    except ImportError:
        _emit_durable(status="FAILED", code="WEB_CONSOLE_UNAVAILABLE")
        return 1
    static_root = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    app = create_web_console_app(static_root=static_root)
    _emit_durable(status="STARTING", host="127.0.0.1", port=8767)
    uvicorn.run(app, host="127.0.0.1", port=8767, access_log=False, log_config=None)
    return 0


def _run_agent_api(namespace: argparse.Namespace) -> int:
    """Preflight and serve the one live durable shopping Agent composition."""

    try:
        import uvicorn

        from glodex.composition.agent_api import build_agent_api

        config = load_config(explicit_path=namespace.config)
        app = asyncio.run(build_agent_api(config=config))
    except Exception:
        _emit_durable(status="FAILED", code="AGENT_API_PREFLIGHT_FAILED")
        return 1
    _emit_durable(status="STARTING", host="127.0.0.1", port=8766)
    uvicorn.run(app, host="127.0.0.1", port=8766, access_log=False, log_config=None)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse once, run the async application once, and emit one JSON document."""

    arguments = tuple(sys.argv[1:] if argv is None else argv)
    try:
        namespace = _build_parser().parse_args(arguments)
    except CliUsageError as error:
        _emit_durable(status="FAILED", code="CLI_USAGE", message=str(error))
        return 2

    if namespace.command == "storage":
        if namespace.storage_action == "migrate":
            return asyncio.run(_run_storage_migrate(namespace))
        return asyncio.run(_run_storage_verify(namespace))

    if namespace.command == "operations":
        return asyncio.run(_run_operations_retain(namespace))

    if namespace.command == "quality-evaluation":
        return asyncio.run(_run_quality_evaluation(namespace))

    if namespace.command == "model-prices":
        return asyncio.run(_run_model_prices_import(namespace))

    if namespace.command == "model-service":
        if namespace.model_service_action == "serve":
            return _run_model_service_serve(namespace)
        return asyncio.run(_run_model_service_verify(namespace))

    if namespace.command == "product-index":
        return _run_product_index(namespace)

    if namespace.command == "agent-api":
        return _run_agent_api(namespace)

    if namespace.command == "web-console":
        return _run_web_console()

    raise AssertionError(f"unhandled command: {namespace.command}")


__all__ = ["main"]
