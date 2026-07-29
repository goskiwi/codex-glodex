"""Composition root and the pre-run validation boundary."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime

from glodex.adapters.deterministic_ranker import DeterministicQueryRanker
from glodex.adapters.local_snapshot import LocalSnapshotCatalog
from glodex.adapters.rule_intent import RuleIntentInterpreter
from glodex.application.ports import (
    CatalogGateway,
    Clock,
    IntentInterpreter,
    QueryRanker,
    RunIdProvider,
)
from glodex.application.search_service import SearchService
from glodex.config import GlodexConfig
from glodex.contracts import (
    RequestRejected,
    SearchResponse,
    validate_search_request,
)


class UuidRunIdProvider:
    """Local run IDs; their nondeterminism is excluded from semantic comparisons."""

    def next_run_id(self) -> str:
        return f"run-{uuid.uuid4().hex}"


class SystemClock:
    """System implementation of the injected application clock."""

    def now_utc(self) -> datetime:
        return datetime.now(UTC)

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


def build_service(
    config: GlodexConfig,
    *,
    run_id_provider: RunIdProvider | None = None,
    clock: Clock | None = None,
    intent_interpreter: IntentInterpreter | None = None,
    catalog_gateway: CatalogGateway | None = None,
    query_ranker: QueryRanker | None = None,
    required_baseline_interpreter: IntentInterpreter | None = None,
) -> SearchService:
    """Build the service without importing adapters into the application layer."""

    return SearchService(
        config=config,
        run_id_provider=run_id_provider or UuidRunIdProvider(),
        clock=clock or SystemClock(),
        intent_interpreter=intent_interpreter or RuleIntentInterpreter(),
        catalog_gateway=catalog_gateway or LocalSnapshotCatalog(config.data_dir),
        query_ranker=query_ranker or DeterministicQueryRanker(),
        required_baseline_interpreter=required_baseline_interpreter,
    )


def build_live_intent_service(
    config: GlodexConfig,
    *,
    run_id_provider: RunIdProvider | None = None,
    clock: Clock | None = None,
) -> SearchService:
    """Preflight and compose the one explicitly activated live Intent service."""

    from glodex.adapters.deepseek_http import build_deepseek_transport
    from glodex.adapters.deepseek_intent import DeepSeekIntentInterpreter

    return build_service(
        config,
        run_id_provider=run_id_provider,
        clock=clock,
        intent_interpreter=DeepSeekIntentInterpreter(build_deepseek_transport()),
        required_baseline_interpreter=RuleIntentInterpreter(),
    )


async def submit_search(
    payload: object,
    service: SearchService,
) -> SearchResponse | RequestRejected:
    """Reject malformed payloads before the service can allocate a run ID."""

    request = validate_search_request(payload)
    if isinstance(request, RequestRejected):
        return request
    return await service.search(request)


__all__ = [
    "SystemClock",
    "UuidRunIdProvider",
    "build_live_intent_service",
    "build_service",
    "submit_search",
]
