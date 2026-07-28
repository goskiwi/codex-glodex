from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from glodex.application.journal import RunEventKind
from glodex.application.search_service import (
    PHASE_D_ALGORITHM,
    SearchExecution,
    SearchService,
)
from glodex.application.state import RunState
from glodex.bootstrap import submit_search
from glodex.config import GlodexConfig
from glodex.contracts import (
    RequestRejected,
    RunStatus,
    SearchRequest,
    SearchResponse,
)
from glodex.domain.catalog import CatalogBatch
from glodex.domain.intent import InterpretedRequest
from tests.builders import build_catalog_batch

pytestmark = [
    pytest.mark.contract,
    pytest.mark.spec(
        "GLO-P0-001",
        "GLO-P0-010",
        "GLO-P0-011",
        "GLO-NFR-007",
        "GLO-NFR-008",
    ),
]


@dataclass
class SequentialRunIds:
    calls: int = 0

    def next_run_id(self) -> str:
        self.calls += 1
        return f"run-{self.calls}"


@dataclass
class FixedClock:
    now_calls: int = 0
    monotonic_calls: int = 0

    def now_utc(self) -> datetime:
        self.now_calls += 1
        return datetime(2026, 7, 23, 4, 0, tzinfo=UTC)

    def monotonic_ns(self) -> int:
        self.monotonic_calls += 1
        run_number, reading = divmod(self.monotonic_calls - 1, 2)
        return run_number * 4_000_000 + (1_000_000, 3_500_000)[reading]


@dataclass
class FixedIntent:
    calls: int = 0

    async def interpret(self, request: SearchRequest) -> InterpretedRequest:
        self.calls += 1
        return InterpretedRequest(parser_version="rules-zh-cn-v1")


@dataclass
class FixedCatalog:
    calls: int = 0

    async def load(
        self,
        snapshot_version: str,
        *,
        display_currency: str | None = None,
        budget_currency: str | None = None,
    ) -> CatalogBatch:
        self.calls += 1
        return build_catalog_batch(snapshot_version=snapshot_version)


@dataclass
class ExplodingRanker:
    calls: int = 0

    async def rank(
        self,
        query: str,
        preferred: tuple[object, ...],
        candidates: tuple[object, ...],
    ) -> tuple[object, ...]:
        self.calls += 1
        raise AssertionError("Phase A must not fake ranking")


def make_service(
    tmp_path: Path,
) -> tuple[
    SearchService,
    SequentialRunIds,
    FixedClock,
    FixedIntent,
    FixedCatalog,
    ExplodingRanker,
]:
    config = GlodexConfig(
        data_dir=tmp_path,
        default_snapshot="m0-v1",
        default_locale="zh-CN",
        default_currency="USD",
        default_top_k=3,
        fingerprint="a" * 64,
    )
    run_ids = SequentialRunIds()
    clock = FixedClock()
    intent = FixedIntent()
    catalog = FixedCatalog()
    ranker = ExplodingRanker()
    service = SearchService(
        config=config,
        run_id_provider=run_ids,
        clock=clock,
        intent_interpreter=intent,
        catalog_gateway=catalog,
        query_ranker=ranker,
    )
    return service, run_ids, clock, intent, catalog, ranker


def test_valid_request_runs_phase_d_and_preserves_response_journal_atomicity(
    tmp_path: Path,
) -> None:
    service, run_ids, clock, intent, catalog, ranker = make_service(tmp_path)
    request = SearchRequest(query="推荐轻薄本")

    execution = asyncio.run(service.execute(request))

    assert execution.response.status is RunStatus.COMPLETED
    assert len(execution.response.results) == 1
    assert execution.response.snapshot_version == "m0-v1"
    assert execution.response.algorithm_version == PHASE_D_ALGORITHM
    assert execution.response.warnings[-1].code == "ranking.degraded"
    assert execution.response.diagnostics.stages[0].duration_ms == 2
    assert execution.journal.state is RunState.COMPLETED
    assert execution.journal.terminal_status is execution.response.status
    assert tuple(
        event.stage
        for event in execution.journal.events
        if event.kind is RunEventKind.STAGE_STARTED
    ) == (
        "intent",
        "snapshot",
        "aggregation",
        "eligibility",
        "ranking",
        "result_assembly",
    )
    assert execution.journal.events[-1].kind is RunEventKind.RUN_COMPLETED
    assert run_ids.calls == 1
    assert clock.now_calls == 15
    assert clock.monotonic_calls == 12
    assert (intent.calls, catalog.calls, ranker.calls) == (1, 1, 1)


def test_invalid_payload_is_rejected_before_a_run_or_downstream_call(
    tmp_path: Path,
) -> None:
    service, run_ids, clock, intent, catalog, ranker = make_service(tmp_path)

    outcome = asyncio.run(submit_search({"query": "   "}, service))

    assert isinstance(outcome, RequestRejected)
    assert "run_id" not in outcome.model_dump()
    assert run_ids.calls == 0
    assert clock.now_calls == 0
    assert clock.monotonic_calls == 0
    assert (intent.calls, catalog.calls, ranker.calls) == (0, 0, 0)


def test_search_returns_public_response_and_allocates_unique_run_ids(
    tmp_path: Path,
) -> None:
    service, run_ids, *_unused = make_service(tmp_path)
    request = SearchRequest(query="推荐轻薄本")

    first = asyncio.run(service.search(request))
    second = asyncio.run(service.search(request))

    assert isinstance(first, SearchResponse)
    assert (first.run_id, second.run_id) == ("run-1", "run-2")
    assert run_ids.calls == 2


def test_execution_rejects_response_and_journal_mismatch(tmp_path: Path) -> None:
    service, *_unused = make_service(tmp_path)
    execution = asyncio.run(service.execute(SearchRequest(query="推荐轻薄本")))
    mismatched_response = execution.response.model_copy(update={"run_id": "run-other"})

    with pytest.raises(ValueError, match="run_id"):
        SearchExecution(response=mismatched_response, journal=execution.journal)


def test_search_service_rejects_non_contract_input_before_run_id(tmp_path: Path) -> None:
    service, run_ids, *_unused = make_service(tmp_path)

    with pytest.raises(TypeError, match="SearchRequest"):
        asyncio.run(service.search({"query": "推荐轻薄本"}))  # type: ignore[arg-type]

    assert run_ids.calls == 0
