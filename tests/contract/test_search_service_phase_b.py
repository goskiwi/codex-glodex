from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from glodex.adapters.local_snapshot import LocalSnapshotCatalog
from glodex.application.journal import RunEventKind
from glodex.application.search_service import PHASE_D_ALGORITHM, SearchService
from glodex.bootstrap import submit_search
from glodex.config import GlodexConfig
from glodex.contracts import RequestRejected, RunStatus, SearchRequest
from glodex.domain.catalog import CatalogBatch
from glodex.domain.eligibility import EligibleProduct
from glodex.domain.intent import (
    BudgetMax,
    InterpretedRequest,
    PreferredCriterion,
    SourceSpan,
)
from glodex.domain.issues import (
    CatalogIssue,
    IssueCode,
    IssueDisposition,
    IssueStage,
)
from glodex.domain.ranking import LexicalScore
from tests.builders import build_catalog_batch

pytestmark = [
    pytest.mark.contract,
    pytest.mark.spec("GLO-P0-001", "GLO-P0-002", "GLO-P0-003", "GLO-P0-004"),
]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_ROOT = PROJECT_ROOT / "data" / "snapshots"


@dataclass
class SequentialRunIds:
    calls: int = 0

    def next_run_id(self) -> str:
        self.calls += 1
        return f"run-{self.calls}"


@dataclass
class FixedClock:
    monotonic_calls: int = 0

    def now_utc(self) -> datetime:
        return datetime(2026, 7, 23, 4, 0, tzinfo=UTC)

    def monotonic_ns(self) -> int:
        value = self.monotonic_calls * 1_000_000
        self.monotonic_calls += 1
        return value


@dataclass
class FixedIntent:
    result: InterpretedRequest
    calls: int = 0

    async def interpret(self, request: SearchRequest) -> InterpretedRequest:
        self.calls += 1
        return self.result


@dataclass
class FixedCatalog:
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
class ExplodingRanker:
    calls: int = 0

    async def rank(
        self,
        query: str,
        preferred: tuple[object, ...],
        candidates: tuple[object, ...],
    ) -> tuple[object, ...]:
        self.calls += 1
        raise AssertionError("Phase B must not rank before pricing and hard gates")


@dataclass
class CapturingScoreRanker:
    calls: list[
        tuple[
            str,
            tuple[PreferredCriterion, ...],
            tuple[EligibleProduct, ...],
        ]
    ] = field(default_factory=list)

    async def rank(
        self,
        query: str,
        preferred: tuple[PreferredCriterion, ...],
        candidates: tuple[EligibleProduct, ...],
    ) -> tuple[object, ...]:
        self.calls.append((query, preferred, candidates))
        return tuple(
            LexicalScore(
                product_id=candidate.product.product_id,
                query_score=0,
                verified_preference_coverage=0,
            )
            for candidate in candidates
        )


@dataclass
class HangingRanker:
    calls: int = 0
    cancelled: bool = False

    async def rank(
        self,
        query: str,
        preferred: tuple[PreferredCriterion, ...],
        candidates: tuple[EligibleProduct, ...],
    ) -> tuple[object, ...]:
        self.calls += 1
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("hanging ranker unexpectedly resumed")


def _config(tmp_path: Path) -> GlodexConfig:
    return GlodexConfig(
        data_dir=tmp_path,
        default_snapshot="m0-v1",
        default_locale="zh-CN",
        default_currency="USD",
        default_top_k=3,
        fingerprint="a" * 64,
    )


def _service(
    tmp_path: Path,
    *,
    interpreted: InterpretedRequest | None = None,
    batch: CatalogBatch | None = None,
) -> tuple[SearchService, SequentialRunIds, FixedIntent, FixedCatalog, ExplodingRanker]:
    run_ids = SequentialRunIds()
    intent = FixedIntent(
        interpreted
        or InterpretedRequest(
            parser_version="rules-zh-cn-v1",
        )
    )
    catalog = FixedCatalog(batch or build_catalog_batch())
    ranker = ExplodingRanker()
    return (
        SearchService(
            config=_config(tmp_path),
            run_id_provider=run_ids,
            clock=FixedClock(),
            intent_interpreter=intent,
            catalog_gateway=catalog,
            query_ranker=ranker,
        ),
        run_ids,
        intent,
        catalog,
        ranker,
    )


def test_pre_run_rejection_still_allocates_no_run_and_calls_no_adapter(
    tmp_path: Path,
) -> None:
    service, run_ids, intent, catalog, ranker = _service(tmp_path)

    outcome = asyncio.run(submit_search({"query": "   "}, service))

    assert isinstance(outcome, RequestRejected)
    assert run_ids.calls == 0
    assert intent.calls == 0
    assert catalog.calls == []
    assert ranker.calls == 0


def test_forged_intent_span_fails_before_catalog_io(tmp_path: Path) -> None:
    query = "预算1000元的笔记本"
    forged = InterpretedRequest(
        required=(
            BudgetMax(
                amount=Decimal("1"),
                currency="CNY",
                source_span=SourceSpan(start=0, end=7, text="预算1000元"),
            ),
        ),
        parser_version="malicious-v1",
    )
    service, run_ids, intent, catalog, ranker = _service(
        tmp_path,
        interpreted=forged,
    )

    execution = asyncio.run(service.execute(SearchRequest(query=query)))

    assert execution.response.status is RunStatus.FAILED
    assert execution.response.run_id == "run-1"
    assert execution.response.diagnostics.issues[0].code == ("intent.budget-amount-span-mismatch")
    assert execution.response.results == ()
    assert execution.journal.terminal_status is RunStatus.FAILED
    assert run_ids.calls == 1
    assert intent.calls == 1
    assert catalog.calls == []
    assert ranker.calls == 0


def test_snapshot_fatal_is_a_run_internal_failure_with_no_partial_result(
    tmp_path: Path,
) -> None:
    issue = CatalogIssue(
        code=IssueCode.MANIFEST_MISSING,
        stage=IssueStage.MANIFEST,
        disposition=IssueDisposition.FATAL,
        message="Snapshot manifest is missing.",
    )
    fatal = CatalogBatch(snapshot_version="m0-v1", fatal_issues=(issue,))
    service, run_ids, intent, catalog, ranker = _service(tmp_path, batch=fatal)

    response = asyncio.run(service.search(SearchRequest(query="推荐笔记本")))

    assert response.status is RunStatus.FAILED
    assert response.run_id == "run-1"
    assert response.diagnostics.issues[0].code == IssueCode.MANIFEST_MISSING.value
    assert response.results == ()
    assert run_ids.calls == 1
    assert intent.calls == 1
    assert catalog.calls == [("m0-v1", "USD", None)]
    assert ranker.calls == 0


def test_unsupported_display_currency_is_a_run_internal_failure_with_no_partial_result() -> None:
    run_ids = SequentialRunIds()
    intent = FixedIntent(InterpretedRequest(parser_version="rules-zh-cn-v1"))
    ranker = ExplodingRanker()
    service = SearchService(
        config=_config(SNAPSHOT_ROOT),
        run_id_provider=run_ids,
        clock=FixedClock(),
        intent_interpreter=intent,
        catalog_gateway=LocalSnapshotCatalog(SNAPSHOT_ROOT),
        query_ranker=ranker,
    )

    execution = asyncio.run(
        service.execute(
            SearchRequest(
                query="推荐笔记本",
                display_currency="JPY",
            )
        )
    )

    response = execution.response
    assert response.status is RunStatus.FAILED
    assert response.run_id == "run-1"
    assert response.snapshot_version == "m0-v1"
    assert response.results == ()
    assert tuple(issue.code for issue in response.diagnostics.issues) == (
        IssueCode.CURRENCY_UNSUPPORTED.value,
    )
    assert execution.journal.terminal_status is RunStatus.FAILED
    assert tuple(
        event.stage
        for event in execution.journal.events
        if event.kind is RunEventKind.STAGE_STARTED
    ) == ("intent", "snapshot")
    assert run_ids.calls == 1
    assert intent.calls == 1
    assert ranker.calls == 0


def test_budget_without_explicit_currency_inherits_display_and_fails_if_unsupported(
    tmp_path: Path,
) -> None:
    query = "预算800以内的笔记本"
    interpreted = InterpretedRequest(
        required=(
            BudgetMax(
                amount=Decimal("800"),
                currency=None,
                source_span=SourceSpan(start=0, end=7, text="预算800以内"),
            ),
        ),
        parser_version="rules-zh-cn-v1",
    )
    service, _, _, catalog, ranker = _service(
        tmp_path,
        interpreted=interpreted,
    )

    response = asyncio.run(
        service.search(
            SearchRequest(
                query=query,
                display_currency="EUR",
            )
        )
    )

    assert response.status is RunStatus.FAILED
    assert response.algorithm_version == PHASE_D_ALGORITHM
    assert response.interpreted_request.required[0].value == "800"
    assert response.diagnostics.issues[-1].code == "eligibility.pipeline-failed"
    assert catalog.calls == [("m0-v1", "EUR", "EUR")]
    assert ranker.calls == 0


def test_gateway_cannot_return_a_different_snapshot_version(tmp_path: Path) -> None:
    service, _, _, _, ranker = _service(
        tmp_path,
        batch=build_catalog_batch(snapshot_version="m0-v2"),
    )

    execution = asyncio.run(
        service.execute(SearchRequest(query="推荐笔记本", snapshot_version="m0-v1"))
    )

    assert execution.response.status is RunStatus.FAILED
    assert execution.response.diagnostics.issues[0].code == ("catalog.snapshot-version-mismatch")
    assert execution.response.snapshot_version == "m0-v1"
    assert execution.journal.snapshot_version == "m0-v1"
    assert execution.journal.terminal_status is RunStatus.FAILED
    assert ranker.calls == 0


def test_snapshot_quarantine_and_aggregation_are_attributed_to_distinct_stages(
    tmp_path: Path,
) -> None:
    batch = asyncio.run(
        LocalSnapshotCatalog(SNAPSHOT_ROOT).load(
            "m0-v1",
            display_currency="USD",
        )
    )
    service, _, _, _, _ = _service(tmp_path, batch=batch)

    execution = asyncio.run(service.execute(SearchRequest(query="推荐笔记本")))

    completed = {
        event.stage: event.result
        for event in execution.journal.events
        if event.kind is RunEventKind.STAGE_COMPLETED
    }
    snapshot_result = completed["snapshot"]
    aggregation_result = completed["aggregation"]
    assert snapshot_result is not None
    assert aggregation_result is not None
    assert (snapshot_result.before, snapshot_result.after) == (39, 36)
    assert len(snapshot_result.issue_codes) == 7
    assert (aggregation_result.before, aggregation_result.after) == (36, 34)
    assert aggregation_result.issue_codes == ()
    catalog_warnings = tuple(
        warning for warning in execution.response.warnings if warning.stage != "ranking"
    )
    assert len(catalog_warnings) == 7
    assert execution.response.warnings[-1].code == "ranking.degraded"


@pytest.mark.spec("GLO-P0-007", "AC-011")
def test_search_service_passes_preferred_and_accepts_one_complete_score_batch(
    tmp_path: Path,
) -> None:
    query = "轻薄笔记本"
    preferred = (
        PreferredCriterion(
            value="lightweight",
            source_span=SourceSpan(start=0, end=2, text="轻薄"),
        ),
    )
    batch = asyncio.run(
        LocalSnapshotCatalog(SNAPSHOT_ROOT).load(
            "m0-v1",
            display_currency="USD",
        )
    )
    service, _, _, _, _ = _service(
        tmp_path,
        interpreted=InterpretedRequest(
            preferred=preferred,
            parser_version="rules-zh-cn-v1",
        ),
        batch=batch,
    )
    ranker = CapturingScoreRanker()
    service.query_ranker = ranker

    response = asyncio.run(service.search(SearchRequest(query=query)))

    assert response.status is RunStatus.COMPLETED
    assert response.diagnostics.ranker_degraded is False
    assert not any(issue.code == "ranking.degraded" for issue in response.warnings)
    assert len(ranker.calls) == 1
    ranked_query, ranked_preferred, candidates = ranker.calls[0]
    assert ranked_query == query
    assert ranked_preferred == preferred
    assert candidates


@pytest.mark.spec("GLO-P0-007", "AC-011")
def test_ranker_timeout_cancels_the_batch_and_records_deterministic_degradation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = asyncio.run(
        LocalSnapshotCatalog(SNAPSHOT_ROOT).load(
            "m0-v1",
            display_currency="USD",
        )
    )
    service, _, _, _, _ = _service(tmp_path, batch=batch)
    ranker = HangingRanker()
    service.query_ranker = ranker
    monkeypatch.setattr(
        "glodex.application.search_service.RANKER_TIMEOUT_SECONDS",
        0.001,
    )

    response = asyncio.run(service.search(SearchRequest(query="推荐笔记本")))

    assert response.status is RunStatus.COMPLETED
    assert response.results
    assert response.diagnostics.ranker_degraded is True
    assert response.warnings[-1].code == "ranking.degraded"
    assert ranker.calls == 1
    assert ranker.cancelled is True


def test_oversized_domain_issue_is_mapped_without_escaping_the_run(
    tmp_path: Path,
) -> None:
    issue = CatalogIssue(
        code=IssueCode.MANIFEST_INVALID,
        stage=IssueStage.MANIFEST,
        disposition=IssueDisposition.FATAL,
        message="x" * 2_001,
    )
    fatal = CatalogBatch(snapshot_version="m0-v1", fatal_issues=(issue,))
    service, _, _, _, _ = _service(tmp_path, batch=fatal)

    execution = asyncio.run(service.execute(SearchRequest(query="推荐笔记本")))

    assert execution.response.status is RunStatus.FAILED
    public_issue = execution.response.diagnostics.issues[0]
    assert public_issue.code == IssueCode.MANIFEST_INVALID.value
    assert len(public_issue.message) <= 2_000
    assert "x" * 100 not in public_issue.message
    assert execution.journal.terminal_status is RunStatus.FAILED


def test_untrusted_adapter_error_code_cannot_break_public_issue_mapping(
    tmp_path: Path,
) -> None:
    class UnsafeCode:
        value = "intent." + ("x" * 500)

    class UnsafeIntentError(RuntimeError):
        code = UnsafeCode()

    class ExplodingIntent:
        async def interpret(self, request: SearchRequest) -> InterpretedRequest:
            raise UnsafeIntentError

    service, _, _, catalog, _ = _service(tmp_path)
    service.intent_interpreter = ExplodingIntent()

    execution = asyncio.run(service.execute(SearchRequest(query="推荐笔记本")))

    assert execution.response.status is RunStatus.FAILED
    assert execution.response.diagnostics.issues[0].code == ("intent.interpretation-failed")
    assert execution.journal.terminal_status is RunStatus.FAILED
    assert catalog.calls == []


def test_hostile_discriminator_equality_cannot_escape_the_run(tmp_path: Path) -> None:
    class AlwaysEqualDiscriminator:
        def __ne__(self, other: object) -> bool:
            return False

    preferred = PreferredCriterion(
        value="lightweight",
        source_span=SourceSpan(start=0, end=2, text="轻薄"),
    )
    object.__setattr__(preferred, "kind", AlwaysEqualDiscriminator())
    service, _, _, catalog, ranker = _service(
        tmp_path,
        interpreted=InterpretedRequest(
            preferred=(preferred,),
            parser_version="malicious-v1",
        ),
    )

    execution = asyncio.run(service.execute(SearchRequest(query="轻薄")))

    assert execution.response.status is RunStatus.FAILED
    assert execution.response.run_id == "run-1"
    assert execution.response.diagnostics.issues[0].code == ("intent.discriminator-mismatch")
    assert execution.response.results == ()
    assert execution.journal.terminal_status is RunStatus.FAILED
    assert catalog.calls == []
    assert ranker.calls == 0
