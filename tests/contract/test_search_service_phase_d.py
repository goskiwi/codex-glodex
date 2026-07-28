# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

import glodex.application.search_service as search_service_module
from glodex.adapters.deterministic_ranker import DeterministicQueryRanker
from glodex.adapters.local_snapshot import LocalSnapshotCatalog
from glodex.adapters.rule_intent import RuleIntentInterpreter
from glodex.application.journal import RunEventKind, StageResult
from glodex.application.ports import QueryRanker
from glodex.application.search_service import (
    PHASE_D_ALGORITHM,
    RANKING_STAGE,
    RESULT_ASSEMBLY_STAGE,
    SearchExecution,
    SearchService,
)
from glodex.config import GlodexConfig
from glodex.contracts import RunStatus, SearchRequest
from glodex.domain.catalog import CatalogBatch
from glodex.domain.eligibility import EligibleProduct
from glodex.domain.intent import InterpretedRequest, PreferredCriterion

pytestmark = [
    pytest.mark.contract,
    pytest.mark.spec(
        "GLO-P0-007",
        "GLO-P0-009",
        "GLO-P0-010",
        "GLO-P0-011",
        "GLO-NFR-007",
        "GLO-NFR-008",
    ),
]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_ROOT = PROJECT_ROOT / "data" / "snapshots"
RESULT_QUERY = "推荐预算800美元、有库存、便携的笔记本"
NO_MATCH_QUERY = "推荐预算1美元以内的笔记本"


@pytest.fixture(scope="module")
def m0_batch() -> CatalogBatch:
    batch = asyncio.run(
        LocalSnapshotCatalog(SNAPSHOT_ROOT).load(
            "m0-v1",
            display_currency="USD",
            budget_currency="USD",
        )
    )
    assert batch.fatal_issues == ()
    return batch


@dataclass
class _RunIds:
    calls: int = 0

    def next_run_id(self) -> str:
        self.calls += 1
        return f"run-phase-d-{self.calls}"


@dataclass
class _Clock:
    monotonic_calls: int = 0

    def now_utc(self) -> datetime:
        return datetime(2026, 7, 23, 4, 0, tzinfo=UTC)

    def monotonic_ns(self) -> int:
        self.monotonic_calls += 1
        return self.monotonic_calls * 1_000_000


@dataclass
class _IntentSpy:
    fixed: InterpretedRequest | None = None
    calls: int = 0
    outputs: list[InterpretedRequest] = field(default_factory=list)
    delegate: RuleIntentInterpreter = field(default_factory=RuleIntentInterpreter)

    async def interpret(self, request: SearchRequest) -> InterpretedRequest:
        self.calls += 1
        interpreted = (
            self.fixed if self.fixed is not None else await self.delegate.interpret(request)
        )
        self.outputs.append(interpreted)
        return interpreted


@dataclass
class _CatalogSpy:
    batch: CatalogBatch
    calls: list[tuple[str, str | None, str | None]] = field(default_factory=list)

    async def load(
        self,
        snapshot_version: str,
        *,
        display_currency: str | None = None,
        budget_currency: str | None = None,
    ) -> CatalogBatch:
        self.calls.append((snapshot_version, display_currency, budget_currency))
        return self.batch


@dataclass
class _RankerSpy:
    calls: list[
        tuple[
            str,
            tuple[PreferredCriterion, ...],
            tuple[EligibleProduct, ...],
        ]
    ] = field(default_factory=list)
    delegate: DeterministicQueryRanker = field(default_factory=DeterministicQueryRanker)

    async def rank(
        self,
        query: str,
        preferred: tuple[PreferredCriterion, ...],
        candidates: tuple[EligibleProduct, ...],
    ) -> tuple[object, ...]:
        self.calls.append((query, preferred, candidates))
        return await self.delegate.rank(query, preferred, candidates)


@dataclass
class _FaultyRanker:
    mode: str
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
        if self.mode == "raises":
            raise RuntimeError("ranker unavailable")
        if self.mode == "invalid-batch":
            return ()
        raise AssertionError(f"unknown faulty ranker mode: {self.mode}")


@dataclass
class _HostilePreferredRanker:
    attack: str
    calls: int = 0
    delegate: DeterministicQueryRanker = field(default_factory=DeterministicQueryRanker)

    async def rank(
        self,
        query: str,
        preferred: tuple[PreferredCriterion, ...],
        candidates: tuple[EligibleProduct, ...],
    ) -> tuple[object, ...]:
        self.calls += 1
        target = preferred[0]
        if self.attack == "value":
            object.__setattr__(target, "value", "forged-preference")
        elif self.attack == "source-span":
            object.__setattr__(target.source_span, "text", "伪造")
        else:
            raise AssertionError(f"unknown preferred attack: {self.attack}")
        return await self.delegate.rank(query, preferred, candidates)


def _service(
    batch: CatalogBatch,
    *,
    ranker: QueryRanker | None = None,
    interpreted: InterpretedRequest | None = None,
) -> tuple[SearchService, _RunIds, _IntentSpy, _CatalogSpy, QueryRanker]:
    run_ids = _RunIds()
    intent = _IntentSpy(fixed=interpreted)
    catalog = _CatalogSpy(batch)
    checked_ranker = ranker or _RankerSpy()
    service = SearchService(
        config=GlodexConfig(
            data_dir=SNAPSHOT_ROOT,
            default_snapshot="m0-v1",
            default_locale="zh-CN",
            default_currency="USD",
            default_top_k=3,
            fingerprint="a" * 64,
        ),
        run_id_provider=run_ids,
        clock=_Clock(),
        intent_interpreter=intent,
        catalog_gateway=catalog,
        query_ranker=checked_ranker,
    )
    return service, run_ids, intent, catalog, checked_ranker


def _completed_stage(execution: SearchExecution, stage: str) -> StageResult:
    matching = tuple(
        event
        for event in execution.journal.events
        if event.kind is RunEventKind.STAGE_COMPLETED and event.stage == stage
    )
    assert len(matching) == 1
    result = matching[0].result
    assert result is not None
    return result


def _terminal_events(execution: SearchExecution) -> tuple[RunEventKind, ...]:
    terminal_kinds = {
        RunEventKind.RUN_COMPLETED,
        RunEventKind.RUN_NO_MATCH,
        RunEventKind.RUN_FAILED,
    }
    return tuple(event.kind for event in execution.journal.events if event.kind in terminal_kinds)


def test_completed_response_uses_phase_d_reason_unknowns_evidence_and_stage(
    m0_batch: CatalogBatch,
) -> None:
    service, run_ids, intent, catalog, ranker = _service(m0_batch)

    execution = asyncio.run(
        service.execute(
            SearchRequest(
                query=RESULT_QUERY,
                top_k=2,
            )
        )
    )

    response = execution.response
    assert response.status is RunStatus.COMPLETED
    assert response.algorithm_version == PHASE_D_ALGORITHM == "phase-d-v1"
    assert len(response.results) == 2
    assert all("满足预算：" in result.reason for result in response.results)
    assert all("有库存：" in result.reason for result in response.results)
    assert all(result.reason != "Passed all fixed hard constraints." for result in response.results)
    assert all(result.unknowns == ("未证实偏好：便携",) for result in response.results)
    for result in response.results:
        evidence_ids = tuple(item.evidence_id for item in result.evidence)
        assert evidence_ids
        assert len(evidence_ids) == len(set(evidence_ids))
        assert "ev-rate-usd" in evidence_ids
        assert any(evidence_id.endswith("-item_price") for evidence_id in evidence_ids)

    assert (
        tuple(item.stage for item in response.diagnostics.stages).count(RESULT_ASSEMBLY_STAGE) == 1
    )
    assert RESULT_ASSEMBLY_STAGE not in {stage.gate for stage in response.filter_summary.stages}
    result_events = tuple(
        event.kind for event in execution.journal.events if event.stage == RESULT_ASSEMBLY_STAGE
    )
    assert result_events == (
        RunEventKind.STAGE_STARTED,
        RunEventKind.STAGE_COMPLETED,
    )
    assert execution.journal.terminal_status is response.status
    assert _terminal_events(execution) == (RunEventKind.RUN_COMPLETED,)
    assert run_ids.calls == intent.calls == len(catalog.calls) == 1
    assert isinstance(ranker, _RankerSpy)
    assert len(ranker.calls) == 1


def test_top_k_one_two_three_are_exact_prefixes_with_result_stage_funnel(
    m0_batch: CatalogBatch,
) -> None:
    service, _, _, _, _ = _service(m0_batch)
    product_ids: dict[int, tuple[str, ...]] = {}

    for top_k in (1, 2, 3):
        execution = asyncio.run(
            service.execute(
                SearchRequest(
                    query=RESULT_QUERY,
                    top_k=top_k,
                )
            )
        )

        assert execution.response.status is RunStatus.COMPLETED
        product_ids[top_k] = tuple(result.product_id for result in execution.response.results)
        assert len(product_ids[top_k]) == top_k
        ranking = _completed_stage(execution, RANKING_STAGE)
        result_assembly = _completed_stage(execution, RESULT_ASSEMBLY_STAGE)
        assert result_assembly.before == ranking.after
        assert result_assembly.after == top_k

    assert product_ids[1] == product_ids[2][:1] == product_ids[3][:1]
    assert product_ids[2] == product_ids[3][:2]


def test_no_match_does_not_call_ranker_or_start_result_assembly(
    m0_batch: CatalogBatch,
) -> None:
    service, _, _, _, ranker = _service(m0_batch)

    execution = asyncio.run(
        service.execute(
            SearchRequest(
                query=NO_MATCH_QUERY,
                top_k=3,
            )
        )
    )

    assert execution.response.status is RunStatus.NO_MATCH
    assert execution.response.results == ()
    assert execution.journal.terminal_status is RunStatus.NO_MATCH
    assert isinstance(ranker, _RankerSpy)
    assert ranker.calls == []
    stages = tuple(event.stage for event in execution.journal.events)
    assert RANKING_STAGE not in stages
    assert RESULT_ASSEMBLY_STAGE not in stages
    assert RESULT_ASSEMBLY_STAGE not in {
        diagnostic.stage for diagnostic in execution.response.diagnostics.stages
    }
    assert _terminal_events(execution) == (RunEventKind.RUN_NO_MATCH,)


@pytest.mark.parametrize("mode", ["raises", "invalid-batch"])
def test_ranker_failure_or_illegal_batch_completes_with_visible_whole_batch_degradation(
    m0_batch: CatalogBatch,
    mode: str,
) -> None:
    ranker = _FaultyRanker(mode=mode)
    service, _, _, _, _ = _service(m0_batch, ranker=ranker)

    execution = asyncio.run(
        service.execute(
            SearchRequest(
                query=RESULT_QUERY,
                top_k=2,
            )
        )
    )

    response = execution.response
    assert response.status is RunStatus.COMPLETED
    assert len(response.results) == 2
    assert response.diagnostics.ranker_degraded is True
    assert any(issue.code == "ranking.degraded" for issue in response.diagnostics.issues)
    degraded = tuple(
        event for event in execution.journal.events if event.kind is RunEventKind.STAGE_DEGRADED
    )
    assert len(degraded) == 1
    assert degraded[0].stage == RANKING_STAGE
    assert degraded[0].result is not None
    assert degraded[0].result.degraded is True
    assert degraded[0].result.issue_codes == ("ranking.degraded",)
    assert _completed_stage(execution, RESULT_ASSEMBLY_STAGE).after == 2
    assert ranker.calls == 1


@pytest.mark.parametrize("attack", ["value", "source-span"])
def test_hostile_ranker_cannot_mutate_original_preferred_summary_or_reason(
    m0_batch: CatalogBatch,
    attack: str,
) -> None:
    interpreted = asyncio.run(RuleIntentInterpreter().interpret(SearchRequest(query=RESULT_QUERY)))
    original_preferred = interpreted.preferred[0]
    original_span = original_preferred.source_span
    ranker = _HostilePreferredRanker(attack=attack)
    service, _, intent, _, _ = _service(
        m0_batch,
        ranker=ranker,
        interpreted=interpreted,
    )

    execution = asyncio.run(
        service.execute(
            SearchRequest(
                query=RESULT_QUERY,
                top_k=2,
            )
        )
    )

    response = execution.response
    assert response.status is RunStatus.COMPLETED
    assert response.diagnostics.ranker_degraded is True
    assert intent.outputs == [interpreted]
    assert interpreted.preferred[0] is original_preferred
    assert original_preferred.value == "portable"
    assert original_preferred.source_span is original_span
    assert original_span.text == "便携"
    public_preferred = response.interpreted_request.preferred
    assert len(public_preferred) == 1
    assert public_preferred[0].value == "portable"
    assert public_preferred[0].source_span.text == "便携"
    assert all(result.unknowns == ("未证实偏好：便携",) for result in response.results)
    assert all(
        "forged" not in result.reason
        and "伪造" not in result.reason
        and "forged" not in " ".join(result.unknowns)
        and "伪造" not in " ".join(result.unknowns)
        for result in response.results
    )
    assert any(
        event.kind is RunEventKind.STAGE_DEGRADED and event.stage == RANKING_STAGE
        for event in execution.journal.events
    )
    assert ranker.calls == 1


@pytest.mark.parametrize(
    "failing_seam",
    ["assemble_result_drafts", "guard_result_drafts"],
)
def test_result_assembly_or_guard_failure_is_atomic_and_has_one_contiguous_terminal(
    m0_batch: CatalogBatch,
    monkeypatch: pytest.MonkeyPatch,
    failing_seam: str,
) -> None:
    def fail_closed(*args: object, **kwargs: object) -> None:
        del args
        del kwargs
        raise RuntimeError("injected final assembly failure")

    monkeypatch.setattr(search_service_module, failing_seam, fail_closed)
    service, _, _, _, _ = _service(m0_batch)

    execution = asyncio.run(
        service.execute(
            SearchRequest(
                query=RESULT_QUERY,
                top_k=2,
            )
        )
    )

    response = execution.response
    assert response.status is RunStatus.FAILED
    assert response.results == ()
    assert response.algorithm_version == PHASE_D_ALGORITHM
    assert response.diagnostics.issues[-1].code == "result.final-guard-failed"
    assert response.diagnostics.issues[-1].stage == RESULT_ASSEMBLY_STAGE
    assert execution.journal.terminal_status is RunStatus.FAILED
    assert _terminal_events(execution) == (RunEventKind.RUN_FAILED,)
    assert execution.journal.events[-1].kind is RunEventKind.RUN_FAILED
    assert tuple(event.sequence for event in execution.journal.events) == tuple(
        range(1, len(execution.journal.events) + 1)
    )
    result_stage = _completed_stage(execution, RESULT_ASSEMBLY_STAGE)
    assert result_stage.before is not None
    assert result_stage.before > 0
    assert result_stage.after == 0
    assert result_stage.issue_codes == ("result.final-guard-failed",)
    assert RESULT_ASSEMBLY_STAGE not in {stage.gate for stage in response.filter_summary.stages}
