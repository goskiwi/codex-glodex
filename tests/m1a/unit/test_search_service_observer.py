from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from inspect import Parameter, signature
from pathlib import Path

import pytest
from pydantic import ValidationError

from glodex.application.journal import RunEvent, RunEventKind
from glodex.application.ports import RunEventObserver
from glodex.application.search_service import SearchService
from glodex.config import GlodexConfig
from glodex.contracts import RunStatus, SearchRequest
from glodex.domain.catalog import CatalogBatch
from glodex.domain.eligibility import EligibleProduct
from glodex.domain.intent import InterpretedRequest, PreferredCriterion
from tests.builders import build_catalog_batch

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec(
        "GLO-M1-P0-003",
        "GLO-M1-P0-004",
        "GLO-M1-P0-008",
        "GLO-M1-NFR-001",
        "GLO-M1-NFR-002",
        "GLO-M1-NFR-003",
        "GLO-M1-NFR-004",
    ),
]

EXPECTED_STAGES = (
    "intent",
    "snapshot",
    "aggregation",
    "eligibility",
    "ranking",
    "result_assembly",
)
TERMINAL_KINDS = frozenset(
    {
        RunEventKind.RUN_COMPLETED,
        RunEventKind.RUN_NO_MATCH,
        RunEventKind.RUN_FAILED,
    }
)


@dataclass(slots=True)
class _RunIds:
    calls: int = 0

    def next_run_id(self) -> str:
        self.calls += 1
        return f"run-{self.calls}"


@dataclass(slots=True)
class _Clock:
    now_calls: int = 0
    monotonic_calls: int = 0

    def now_utc(self) -> datetime:
        self.now_calls += 1
        return datetime(2026, 7, 28, tzinfo=UTC)

    def monotonic_ns(self) -> int:
        self.monotonic_calls += 1
        return self.monotonic_calls * 1_000_000


@dataclass(slots=True)
class _Recorder:
    events: list[RunEvent] = field(default_factory=list)

    def on_event(self, event: RunEvent) -> None:
        assert event.sequence == len(self.events) + 1
        self.events.append(event)


@dataclass(slots=True)
class _Intent:
    observer: _Recorder | None = None
    calls: int = 0

    async def interpret(self, request: SearchRequest) -> InterpretedRequest:
        del request
        self.calls += 1
        if self.observer is not None:
            assert tuple((event.kind, event.stage) for event in self.observer.events) == (
                (RunEventKind.RUN_STARTED, "run"),
                (RunEventKind.STAGE_STARTED, "intent"),
            )
        return InterpretedRequest(parser_version="observer-test-v1")


@dataclass(slots=True)
class _Catalog:
    observer: _Recorder | None = None
    calls: int = 0

    async def load(
        self,
        snapshot_version: str,
        *,
        display_currency: str | None = None,
        budget_currency: str | None = None,
    ) -> CatalogBatch:
        del display_currency
        del budget_currency
        self.calls += 1
        if self.observer is not None:
            assert tuple((event.kind, event.stage) for event in self.observer.events[-2:]) == (
                (RunEventKind.STAGE_COMPLETED, "intent"),
                (RunEventKind.STAGE_STARTED, "snapshot"),
            )
        return build_catalog_batch(snapshot_version=snapshot_version)


@dataclass(slots=True)
class _DegradingRanker:
    observer: _Recorder | None = None
    calls: int = 0

    async def rank(
        self,
        query: str,
        preferred: tuple[PreferredCriterion, ...],
        candidates: tuple[EligibleProduct, ...],
    ) -> tuple[object, ...]:
        del query
        del preferred
        del candidates
        self.calls += 1
        if self.observer is not None:
            assert self.observer.events[-1].kind is RunEventKind.STAGE_STARTED
            assert self.observer.events[-1].stage == "ranking"
        raise RuntimeError("force the approved deterministic ranking degradation")


def _service(
    tmp_path: Path,
    *,
    observer: _Recorder | None = None,
) -> tuple[SearchService, _RunIds, _Clock, _Intent, _Catalog, _DegradingRanker]:
    run_ids = _RunIds()
    clock = _Clock()
    intent = _Intent(observer=observer)
    catalog = _Catalog(observer=observer)
    ranker = _DegradingRanker(observer=observer)
    service = SearchService(
        config=GlodexConfig(
            data_dir=tmp_path,
            default_snapshot="m0-v1",
            default_locale="zh-CN",
            default_currency="USD",
            default_top_k=3,
            fingerprint="a" * 64,
        ),
        run_id_provider=run_ids,
        clock=clock,
        intent_interpreter=intent,
        catalog_gateway=catalog,
        query_ranker=ranker,
    )
    return service, run_ids, clock, intent, catalog, ranker


def test_execute_run_uses_explicit_identifier_and_observes_live_nonterminal_events(
    tmp_path: Path,
) -> None:
    observer = _Recorder()
    checked_observer: RunEventObserver = observer
    service, run_ids, _clock, intent, catalog, ranker = _service(
        tmp_path,
        observer=observer,
    )

    execution = asyncio.run(
        service.execute_run(
            SearchRequest(query="推荐轻薄本"),
            run_id="run:reserved",
            observer=checked_observer,
        )
    )

    assert execution.response.status is RunStatus.COMPLETED
    assert execution.response.run_id == execution.journal.run_id == "run:reserved"
    assert run_ids.calls == 0
    assert (intent.calls, catalog.calls, ranker.calls) == (1, 1, 1)
    assert tuple(observer.events) == execution.journal.events[:-1]
    assert not any(event.kind in TERMINAL_KINDS for event in observer.events)
    assert (
        tuple(event.stage for event in observer.events if event.kind is RunEventKind.STAGE_STARTED)
        == EXPECTED_STAGES
    )
    assert any(event.kind is RunEventKind.STAGE_DEGRADED for event in observer.events)
    assert execution.journal.events[-1].kind is RunEventKind.RUN_COMPLETED


@pytest.mark.parametrize(
    "run_id",
    [
        "",
        "contains whitespace",
        "starts-with-💥",
        "x" * 129,
        1,
        True,
    ],
)
def test_execute_run_rejects_invalid_identifier_before_run_or_downstream(
    tmp_path: Path,
    run_id: object,
) -> None:
    observer = _Recorder()
    service, run_ids, clock, intent, catalog, ranker = _service(tmp_path)

    with pytest.raises(ValidationError):
        asyncio.run(
            service.execute_run(
                SearchRequest(query="推荐轻薄本"),
                run_id=run_id,  # type: ignore[arg-type]
                observer=observer,
            )
        )

    assert run_ids.calls == 0
    assert clock.now_calls == 0
    assert clock.monotonic_calls == 0
    assert (intent.calls, catalog.calls, ranker.calls) == (0, 0, 0)
    assert observer.events == []


def test_execute_run_rejects_non_contract_request_before_validating_run_id(
    tmp_path: Path,
) -> None:
    service, run_ids, clock, intent, catalog, ranker = _service(tmp_path)

    with pytest.raises(TypeError, match="SearchRequest"):
        asyncio.run(
            service.execute_run(
                {"query": "推荐轻薄本"},  # type: ignore[arg-type]
                run_id="invalid run id",
            )
        )

    assert run_ids.calls == 0
    assert clock.now_calls == 0
    assert clock.monotonic_calls == 0
    assert (intent.calls, catalog.calls, ranker.calls) == (0, 0, 0)


class _ObserverFailure(RuntimeError):
    pass


class _ExplodingObserver:
    def on_event(self, event: RunEvent) -> None:
        del event
        raise _ObserverFailure("observer boundary failure")


def test_search_service_does_not_hide_observer_failure(tmp_path: Path) -> None:
    service, run_ids, _clock, intent, catalog, ranker = _service(tmp_path)

    with pytest.raises(_ObserverFailure, match="observer boundary"):
        asyncio.run(
            service.execute_run(
                SearchRequest(query="推荐轻薄本"),
                run_id="run-observer-failure",
                observer=_ExplodingObserver(),
            )
        )

    assert run_ids.calls == 0
    assert (intent.calls, catalog.calls, ranker.calls) == (0, 0, 0)


def test_existing_search_and_execute_signatures_and_provider_behavior_remain_stable(
    tmp_path: Path,
) -> None:
    search_parameters = signature(SearchService.search).parameters
    execute_parameters = signature(SearchService.execute).parameters
    execute_run_parameters = signature(SearchService.execute_run).parameters

    assert tuple(search_parameters) == ("self", "request")
    assert tuple(execute_parameters) == ("self", "request")
    assert tuple(execute_run_parameters) == ("self", "request", "run_id", "observer")
    assert execute_run_parameters["run_id"].kind is Parameter.KEYWORD_ONLY
    assert execute_run_parameters["observer"].kind is Parameter.KEYWORD_ONLY

    service, run_ids, *_unused = _service(tmp_path)
    first = asyncio.run(service.execute(SearchRequest(query="推荐轻薄本")))
    second = asyncio.run(service.search(SearchRequest(query="推荐轻薄本")))

    assert (first.response.run_id, second.run_id) == ("run-1", "run-2")
    assert run_ids.calls == 2
