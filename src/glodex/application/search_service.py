"""Async application orchestration and immutable terminal assembly."""

from __future__ import annotations

import asyncio
import re
from copy import deepcopy
from dataclasses import dataclass

from pydantic import TypeAdapter

from glodex.application.issue_mapping import catalog_issue_to_public
from glodex.application.journal import RunJournal, StageResult
from glodex.application.ports import (
    CatalogGateway,
    Clock,
    IntentInterpreter,
    QueryRanker,
    RunEventObserver,
    RunIdProvider,
)
from glodex.config import GlodexConfig
from glodex.contracts import (
    Detail,
    Diagnostics,
    EvidenceSummary,
    FilterReasonCount,
    FilterStageSummary,
    FilterSummary,
    Identifier,
    InterpretedCriterionSummary,
    InterpretedRequestSummary,
    Issue,
    IssueSeverity,
    MoneySummary,
    OfferSummary,
    RunStatus,
    SearchRequest,
    SearchResponse,
    SearchResult,
    SourceSpanSummary,
    StageDiagnostic,
)
from glodex.domain.assembly import (
    GuardedResults,
    ResultDraft,
    assemble_result_drafts,
    guard_result_drafts,
    require_guarded_results,
    result_evidence_closure,
)
from glodex.domain.catalog import CatalogAggregationResult, CatalogBatch, aggregate_catalog_batch
from glodex.domain.eligibility import (
    EligibilityContext,
    EligibleOffer,
    OfferPricingCandidate,
    assemble_eligibility,
    run_offer_gates,
    run_product_gates,
    scorer_input,
)
from glodex.domain.eligibility import (
    FilterSummary as DomainFilterSummary,
)
from glodex.domain.intent import (
    BudgetMax,
    Exclusion,
    InterpretedRequest,
    PreferredCriterion,
    SourceSpan,
    StockRequired,
    TargetCategory,
    validate_interpreted_request,
)
from glodex.domain.issues import CatalogIssue, IssueStage
from glodex.domain.pricing import calculate_landed_cost
from glodex.domain.ranking import (
    degraded_rank,
    isolate_rank_candidates,
    require_unchanged_rank_candidates,
    validate_and_rank,
)

WALKING_SKELETON_STAGE = "walking_skeleton"
WALKING_SKELETON_ALGORITHM = "walking-skeleton-v1"
INTENT_STAGE = "intent"
SNAPSHOT_STAGE = "snapshot"
AGGREGATION_STAGE = "aggregation"
ELIGIBILITY_STAGE = "eligibility"
RANKING_STAGE = "ranking"
RESULT_ASSEMBLY_STAGE = "result_assembly"
PHASE_B_ALGORITHM = "phase-b-v1"
PHASE_C_ALGORITHM = "phase-c-v1"
PHASE_D_ALGORITHM = "phase-d-v1"
RANKER_TIMEOUT_SECONDS = 1.0
_PUBLIC_ISSUE_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
_RUN_ID_ADAPTER = TypeAdapter(Identifier)


@dataclass(frozen=True, slots=True)
class SearchExecution:
    """Atomically paired public response and internal immutable journal."""

    response: SearchResponse
    journal: RunJournal

    def __post_init__(self) -> None:
        if self.response.run_id != self.journal.run_id:
            raise ValueError("response and journal run_id mismatch")
        if self.response.snapshot_version != self.journal.snapshot_version:
            raise ValueError("response and journal snapshot_version mismatch")
        if self.response.status is not self.journal.terminal_status:
            raise ValueError("response and journal terminal status mismatch")


class SearchService:
    """Orchestrate a search while keeping I/O behind injected protocols."""

    def __init__(
        self,
        *,
        config: GlodexConfig,
        run_id_provider: RunIdProvider,
        clock: Clock,
        intent_interpreter: IntentInterpreter,
        catalog_gateway: CatalogGateway,
        query_ranker: QueryRanker,
    ) -> None:
        self.config = config
        self.run_id_provider = run_id_provider
        self.clock = clock
        self.intent_interpreter = intent_interpreter
        self.catalog_gateway = catalog_gateway
        self.query_ranker = query_ranker

    async def search(self, request: SearchRequest) -> SearchResponse:
        """Return the approved public response contract."""

        return (await self.execute(request)).response

    async def execute(self, request: SearchRequest) -> SearchExecution:
        """Execute the complete guarded search pipeline and commit one terminal state."""

        if not isinstance(request, SearchRequest):
            raise TypeError("SearchService requires a validated SearchRequest")

        return await self.execute_run(
            request,
            run_id=self.run_id_provider.next_run_id(),
        )

    async def execute_run(
        self,
        request: SearchRequest,
        *,
        run_id: str,
        observer: RunEventObserver | None = None,
    ) -> SearchExecution:
        """Execute one run whose identifier was allocated by the caller."""

        if not isinstance(request, SearchRequest):
            raise TypeError("SearchService requires a validated SearchRequest")
        checked_run_id = _RUN_ID_ADAPTER.validate_python(run_id, strict=True)
        snapshot_version = request.snapshot_version or self.config.default_snapshot
        journal = RunJournal.new(
            run_id=checked_run_id,
            snapshot_version=snapshot_version,
        )
        journal = journal.start(self.clock.now_utc())
        self._notify_observer(journal, observer)
        stage_diagnostics: list[StageDiagnostic] = []
        filter_stages: list[FilterStageSummary] = []
        public_issues: list[Issue] = []

        journal, started_ns = self._start_stage(
            journal,
            INTENT_STAGE,
            observer=observer,
        )
        try:
            untrusted_intent = await self.intent_interpreter.interpret(request)
        except Exception as error:
            issue = _intent_adapter_issue(error)
            public_issues.append(issue)
            journal, duration_ms = self._complete_stage(
                journal,
                INTENT_STAGE,
                started_ns,
                observer=observer,
                before=1,
                after=0,
                issue_codes=(issue.code,),
            )
            stage_diagnostics.append(StageDiagnostic(stage=INTENT_STAGE, duration_ms=duration_ms))
            filter_stages.append(FilterStageSummary(gate=INTENT_STAGE, before=1, after=0))
            return self._finish(
                journal,
                RunStatus.FAILED,
                snapshot_version=snapshot_version,
                filter_stages=filter_stages,
                stage_diagnostics=stage_diagnostics,
                public_issues=public_issues,
            )

        validation = validate_interpreted_request(request.query, untrusted_intent)
        if not validation.is_valid:
            public_issues.extend(_intent_validation_issue(item) for item in validation.issues)
            issue_codes = tuple(issue.code for issue in public_issues)
            journal, duration_ms = self._complete_stage(
                journal,
                INTENT_STAGE,
                started_ns,
                observer=observer,
                before=1,
                after=0,
                issue_codes=issue_codes,
            )
            stage_diagnostics.append(StageDiagnostic(stage=INTENT_STAGE, duration_ms=duration_ms))
            filter_stages.append(FilterStageSummary(gate=INTENT_STAGE, before=1, after=0))
            return self._finish(
                journal,
                RunStatus.FAILED,
                snapshot_version=snapshot_version,
                filter_stages=filter_stages,
                stage_diagnostics=stage_diagnostics,
                public_issues=public_issues,
            )

        interpreted = validation.interpreted_request
        assert interpreted is not None
        interpreted_summary = _summarize_intent(interpreted)
        journal, duration_ms = self._complete_stage(
            journal,
            INTENT_STAGE,
            started_ns,
            observer=observer,
            before=1,
            after=1,
        )
        stage_diagnostics.append(StageDiagnostic(stage=INTENT_STAGE, duration_ms=duration_ms))
        filter_stages.append(FilterStageSummary(gate=INTENT_STAGE, before=1, after=1))

        budget = next(
            (criterion for criterion in interpreted.required if type(criterion) is BudgetMax),
            None,
        )
        budget_currency = (
            None
            if budget is None
            else budget.currency
            if budget.currency is not None
            else request.display_currency
        )

        journal, started_ns = self._start_stage(
            journal,
            SNAPSHOT_STAGE,
            observer=observer,
        )
        try:
            batch = await self.catalog_gateway.load(
                snapshot_version,
                display_currency=request.display_currency,
                budget_currency=budget_currency,
            )
            if type(batch) is not CatalogBatch:
                raise TypeError("catalog gateway returned an invalid batch")
        except Exception:
            issue = Issue(
                code="catalog.load-failed",
                stage=SNAPSHOT_STAGE,
                message="Catalog snapshot loading failed.",
                severity=IssueSeverity.ERROR,
            )
            public_issues.append(issue)
            journal, duration_ms = self._complete_stage(
                journal,
                SNAPSHOT_STAGE,
                started_ns,
                observer=observer,
                before=0,
                after=0,
                issue_codes=(issue.code,),
            )
            stage_diagnostics.append(StageDiagnostic(stage=SNAPSHOT_STAGE, duration_ms=duration_ms))
            filter_stages.append(FilterStageSummary(gate=SNAPSHOT_STAGE, before=0, after=0))
            return self._finish(
                journal,
                RunStatus.FAILED,
                snapshot_version=snapshot_version,
                interpreted_request=interpreted_summary,
                filter_stages=filter_stages,
                stage_diagnostics=stage_diagnostics,
                public_issues=public_issues,
            )

        if batch.snapshot_version != snapshot_version:
            issue = Issue(
                code="catalog.snapshot-version-mismatch",
                stage=SNAPSHOT_STAGE,
                message="Catalog snapshot version does not match the request.",
                severity=IssueSeverity.ERROR,
            )
            public_issues.append(issue)
            journal, duration_ms = self._complete_stage(
                journal,
                SNAPSHOT_STAGE,
                started_ns,
                observer=observer,
                before=0,
                after=0,
                issue_codes=(issue.code,),
            )
            stage_diagnostics.append(StageDiagnostic(stage=SNAPSHOT_STAGE, duration_ms=duration_ms))
            filter_stages.append(FilterStageSummary(gate=SNAPSHOT_STAGE, before=0, after=0))
            return self._finish(
                journal,
                RunStatus.FAILED,
                snapshot_version=snapshot_version,
                interpreted_request=interpreted_summary,
                filter_stages=filter_stages,
                stage_diagnostics=stage_diagnostics,
                public_issues=public_issues,
            )

        if batch.fatal_issues:
            fatal_issues = tuple(catalog_issue_to_public(item) for item in batch.fatal_issues)
            public_issues.extend(fatal_issues)
            journal, duration_ms = self._complete_stage(
                journal,
                SNAPSHOT_STAGE,
                started_ns,
                observer=observer,
                before=0,
                after=0,
                issue_codes=tuple(issue.code for issue in fatal_issues),
            )
            stage_diagnostics.append(StageDiagnostic(stage=SNAPSHOT_STAGE, duration_ms=duration_ms))
            filter_stages.append(FilterStageSummary(gate=SNAPSHOT_STAGE, before=0, after=0))
            return self._finish(
                journal,
                RunStatus.FAILED,
                snapshot_version=snapshot_version,
                interpreted_request=interpreted_summary,
                filter_stages=filter_stages,
                stage_diagnostics=stage_diagnostics,
                public_issues=public_issues,
            )

        snapshot_issues = tuple(catalog_issue_to_public(item) for item in batch.quarantine_issues)
        public_issues.extend(snapshot_issues)
        valid_product_count = len(batch.products)
        quarantined_product_count = sum(
            issue.stage is IssueStage.PRODUCTS for issue in batch.quarantine_issues
        )
        raw_product_count = valid_product_count + quarantined_product_count
        journal, duration_ms = self._complete_stage(
            journal,
            SNAPSHOT_STAGE,
            started_ns,
            observer=observer,
            before=raw_product_count,
            after=valid_product_count,
            issue_codes=tuple(issue.code.value for issue in batch.quarantine_issues),
        )
        stage_diagnostics.append(StageDiagnostic(stage=SNAPSHOT_STAGE, duration_ms=duration_ms))
        filter_stages.append(
            FilterStageSummary(
                gate=SNAPSHOT_STAGE,
                before=raw_product_count,
                after=valid_product_count,
            )
        )

        journal, started_ns = self._start_stage(
            journal,
            AGGREGATION_STAGE,
            observer=observer,
        )
        try:
            aggregation = aggregate_catalog_batch(batch)
        except Exception:
            issue = Issue(
                code="catalog.aggregation-failed",
                stage=AGGREGATION_STAGE,
                message="Catalog aggregation failed.",
                severity=IssueSeverity.ERROR,
            )
            public_issues.append(issue)
            journal, duration_ms = self._complete_stage(
                journal,
                AGGREGATION_STAGE,
                started_ns,
                observer=observer,
                before=valid_product_count,
                after=0,
                issue_codes=(issue.code,),
            )
            stage_diagnostics.append(
                StageDiagnostic(stage=AGGREGATION_STAGE, duration_ms=duration_ms)
            )
            filter_stages.append(
                FilterStageSummary(
                    gate=AGGREGATION_STAGE,
                    before=valid_product_count,
                    after=0,
                )
            )
            return self._finish(
                journal,
                RunStatus.FAILED,
                snapshot_version=snapshot_version,
                interpreted_request=interpreted_summary,
                filter_stages=filter_stages,
                stage_diagnostics=stage_diagnostics,
                public_issues=public_issues,
            )

        aggregation_issues = _aggregation_only_issues(
            aggregation.quarantine_issues,
            batch.quarantine_issues,
        )
        public_issues.extend(catalog_issue_to_public(item) for item in aggregation_issues)
        canonical_product_count = len(aggregation.products)
        journal, duration_ms = self._complete_stage(
            journal,
            AGGREGATION_STAGE,
            started_ns,
            observer=observer,
            before=valid_product_count,
            after=canonical_product_count,
            issue_codes=tuple(issue.code.value for issue in aggregation_issues),
        )
        stage_diagnostics.append(StageDiagnostic(stage=AGGREGATION_STAGE, duration_ms=duration_ms))
        filter_stages.append(
            FilterStageSummary(
                gate=AGGREGATION_STAGE,
                before=valid_product_count,
                after=canonical_product_count,
            )
        )

        journal, started_ns = self._start_stage(
            journal,
            ELIGIBILITY_STAGE,
            observer=observer,
        )
        try:
            if batch.exchange_rates is None:
                raise ValueError("catalog batch has no exchange-rate table")
            required_currencies = {
                request.display_currency,
                *(() if budget_currency is None else (budget_currency,)),
            }
            if not required_currencies.issubset(batch.exchange_rates.supported_currencies):
                raise ValueError("requested display or budget currency is unsupported")
            context = EligibilityContext.from_interpreted_request(interpreted)
            pricing = tuple(
                OfferPricingCandidate(
                    offer=offer,
                    pricing=calculate_landed_cost(
                        offer.cost_components,
                        batch.exchange_rates,
                        display_currency=request.display_currency,
                        budget_max=None if budget is None else budget.amount,
                        budget_currency=None if budget is None else budget.currency,
                    ),
                )
                for offer in aggregation.offers
            )
            product_output = run_product_gates(
                aggregation.products,
                context,
                batch.evidence,
            )
            offer_output = run_offer_gates(
                product_output,
                aggregation.offers,
                pricing,
                batch.evidence,
                display_currency=request.display_currency,
            )
            eligibility_output = assemble_eligibility(product_output, offer_output)
            domain_filter_summary = eligibility_output.filter_summary
            if domain_filter_summary is None:
                raise TypeError("eligibility assembly returned no filter summary")
        except Exception:
            issue = Issue(
                code="eligibility.pipeline-failed",
                stage=ELIGIBILITY_STAGE,
                message="Pricing or hard-gate evaluation failed.",
                severity=IssueSeverity.ERROR,
            )
            public_issues.append(issue)
            journal, duration_ms = self._complete_stage(
                journal,
                ELIGIBILITY_STAGE,
                started_ns,
                observer=observer,
                before=canonical_product_count,
                after=0,
                issue_codes=(issue.code,),
            )
            stage_diagnostics.append(
                StageDiagnostic(stage=ELIGIBILITY_STAGE, duration_ms=duration_ms)
            )
            filter_stages.append(
                FilterStageSummary(
                    gate=ELIGIBILITY_STAGE,
                    before=canonical_product_count,
                    after=0,
                )
            )
            return self._finish(
                journal,
                RunStatus.FAILED,
                snapshot_version=snapshot_version,
                interpreted_request=interpreted_summary,
                filter_stages=filter_stages,
                stage_diagnostics=stage_diagnostics,
                public_issues=public_issues,
            )

        eligible_count = len(eligibility_output.candidates)
        journal, duration_ms = self._complete_stage(
            journal,
            ELIGIBILITY_STAGE,
            started_ns,
            observer=observer,
            before=canonical_product_count,
            after=eligible_count,
        )
        stage_diagnostics.append(StageDiagnostic(stage=ELIGIBILITY_STAGE, duration_ms=duration_ms))
        filter_stages.extend(_public_filter_stages(domain_filter_summary))
        filter_reason_counts = list(_public_filter_reason_counts(domain_filter_summary))
        if not eligibility_output.candidates:
            return self._finish(
                journal,
                RunStatus.NO_MATCH,
                snapshot_version=snapshot_version,
                interpreted_request=interpreted_summary,
                filter_stages=filter_stages,
                filter_reason_counts=filter_reason_counts,
                stage_diagnostics=stage_diagnostics,
                public_issues=public_issues,
                aggregation=aggregation,
            )

        journal, started_ns = self._start_stage(
            journal,
            RANKING_STAGE,
            observer=observer,
        )
        rankable = scorer_input(eligibility_output)
        candidate_count = len(rankable.candidates)
        try:
            ranker_candidates = isolate_rank_candidates(rankable.candidates)
            ranker_preferred = _isolate_ranker_preferred(interpreted.preferred)
        except Exception:
            issue = Issue(
                code="ranking.candidate-isolation-failed",
                stage=RANKING_STAGE,
                message="Ranking inputs could not be isolated safely.",
                severity=IssueSeverity.ERROR,
            )
            public_issues.append(issue)
            journal, duration_ms = self._complete_stage(
                journal,
                RANKING_STAGE,
                started_ns,
                observer=observer,
                before=candidate_count,
                after=0,
                issue_codes=(issue.code,),
            )
            stage_diagnostics.append(StageDiagnostic(stage=RANKING_STAGE, duration_ms=duration_ms))
            return self._finish(
                journal,
                RunStatus.FAILED,
                snapshot_version=snapshot_version,
                interpreted_request=interpreted_summary,
                filter_stages=filter_stages,
                filter_reason_counts=filter_reason_counts,
                stage_diagnostics=stage_diagnostics,
                public_issues=public_issues,
            )
        ranker_failed = False
        try:
            untrusted_ranking = await asyncio.wait_for(
                self.query_ranker.rank(
                    request.query,
                    ranker_preferred,
                    ranker_candidates,
                ),
                timeout=RANKER_TIMEOUT_SECONDS,
            )
        except Exception:
            ranker_failed = True
            untrusted_ranking = ()

        try:
            verified_after_ranking = scorer_input(eligibility_output).candidates
        except TypeError:
            issue = Issue(
                code="ranking.candidate-tampering",
                stage=RANKING_STAGE,
                message="Ranking mutated an eligibility-protected candidate.",
                severity=IssueSeverity.ERROR,
            )
            public_issues.append(issue)
            journal, duration_ms = self._complete_stage(
                journal,
                RANKING_STAGE,
                started_ns,
                observer=observer,
                before=candidate_count,
                after=0,
                issue_codes=(issue.code,),
            )
            stage_diagnostics.append(StageDiagnostic(stage=RANKING_STAGE, duration_ms=duration_ms))
            return self._finish(
                journal,
                RunStatus.FAILED,
                snapshot_version=snapshot_version,
                interpreted_request=interpreted_summary,
                filter_stages=filter_stages,
                filter_reason_counts=filter_reason_counts,
                stage_diagnostics=stage_diagnostics,
                public_issues=public_issues,
            )

        try:
            require_unchanged_rank_candidates(
                verified_after_ranking,
                ranker_candidates,
            )
            _require_unchanged_ranker_preferred(
                interpreted.preferred,
                ranker_preferred,
            )
        except (TypeError, ValueError):
            ranker_failed = True

        try:
            ranked_candidates = (
                degraded_rank(verified_after_ranking)
                if ranker_failed
                else validate_and_rank(
                    verified_after_ranking,
                    untrusted_ranking,
                )
            )
        except (TypeError, ValueError):
            ranker_failed = True
            ranked_candidates = degraded_rank(verified_after_ranking)
        if ranker_failed:
            public_issues.append(
                Issue(
                    code="ranking.degraded",
                    stage=RANKING_STAGE,
                    message="Ranking failed; stable snapshot order was used.",
                    severity=IssueSeverity.WARNING,
                )
            )
            journal = journal.stage_degraded(
                RANKING_STAGE,
                self.clock.now_utc(),
                result=StageResult(
                    before=candidate_count,
                    after=len(ranked_candidates),
                    degraded=True,
                    issue_codes=("ranking.degraded",),
                ),
            )
            self._notify_observer(journal, observer)
        ranking_issue_codes = tuple(
            issue.code for issue in public_issues if issue.stage == RANKING_STAGE
        )
        journal, duration_ms = self._complete_stage(
            journal,
            RANKING_STAGE,
            started_ns,
            observer=observer,
            before=candidate_count,
            after=len(ranked_candidates),
            issue_codes=ranking_issue_codes,
        )
        stage_diagnostics.append(StageDiagnostic(stage=RANKING_STAGE, duration_ms=duration_ms))

        journal, started_ns = self._start_stage(
            journal,
            RESULT_ASSEMBLY_STAGE,
            observer=observer,
        )
        try:
            exchange_rates = batch.exchange_rates
            if exchange_rates is None:
                raise TypeError("catalog batch has no exchange-rate table")
            result_evidence = result_evidence_closure(
                ranked_candidates,
                batch.evidence,
                exchange_rates,
            )
            drafts = assemble_result_drafts(
                ranked_candidates,
                request.query,
                interpreted,
                result_evidence,
                exchange_rates,
                top_k=request.top_k,
            )
            guarded_results = guard_result_drafts(
                drafts,
                ranked=ranked_candidates,
                eligibility_output=eligibility_output,
                query=request.query,
                interpreted=interpreted,
                evidence=result_evidence,
                exchange_rates=exchange_rates,
                display_currency=request.display_currency,
                snapshot_version=snapshot_version,
                top_k=request.top_k,
            )
            checked_results = require_guarded_results(guarded_results)
            results = tuple(_phase_d_result(item) for item in checked_results.items)
            require_guarded_results(guarded_results)
        except Exception:
            issue = Issue(
                code="result.final-guard-failed",
                stage=RESULT_ASSEMBLY_STAGE,
                message="Final result invariants could not be proven.",
                severity=IssueSeverity.ERROR,
            )
            public_issues.append(issue)
            journal, duration_ms = self._complete_stage(
                journal,
                RESULT_ASSEMBLY_STAGE,
                started_ns,
                observer=observer,
                before=len(ranked_candidates),
                after=0,
                issue_codes=(issue.code,),
            )
            stage_diagnostics.append(
                StageDiagnostic(
                    stage=RESULT_ASSEMBLY_STAGE,
                    duration_ms=duration_ms,
                )
            )
            return self._finish(
                journal,
                RunStatus.FAILED,
                snapshot_version=snapshot_version,
                interpreted_request=interpreted_summary,
                filter_stages=filter_stages,
                filter_reason_counts=filter_reason_counts,
                stage_diagnostics=stage_diagnostics,
                public_issues=public_issues,
            )
        journal, duration_ms = self._complete_stage(
            journal,
            RESULT_ASSEMBLY_STAGE,
            started_ns,
            observer=observer,
            before=len(ranked_candidates),
            after=len(results),
        )
        stage_diagnostics.append(
            StageDiagnostic(
                stage=RESULT_ASSEMBLY_STAGE,
                duration_ms=duration_ms,
            )
        )
        return self._finish(
            journal,
            RunStatus.COMPLETED,
            snapshot_version=snapshot_version,
            interpreted_request=interpreted_summary,
            filter_stages=filter_stages,
            filter_reason_counts=filter_reason_counts,
            stage_diagnostics=stage_diagnostics,
            public_issues=public_issues,
            results=results,
            guarded_results=guarded_results,
        )

    @staticmethod
    def _notify_observer(
        journal: RunJournal,
        observer: RunEventObserver | None,
    ) -> None:
        if observer is not None:
            observer.on_event(journal.events[-1])

    def _start_stage(
        self,
        journal: RunJournal,
        stage: str,
        *,
        observer: RunEventObserver | None,
    ) -> tuple[RunJournal, int]:
        started_ns = self.clock.monotonic_ns()
        journal = journal.stage_started(stage, self.clock.now_utc())
        self._notify_observer(journal, observer)
        return journal, started_ns

    def _complete_stage(
        self,
        journal: RunJournal,
        stage: str,
        started_ns: int,
        *,
        observer: RunEventObserver | None,
        before: int,
        after: int,
        issue_codes: tuple[str, ...] = (),
    ) -> tuple[RunJournal, int]:
        ended_ns = self.clock.monotonic_ns()
        if ended_ns < started_ns:
            raise ValueError("monotonic clock moved backwards")
        duration_ms = (ended_ns - started_ns) // 1_000_000
        journal = journal.stage_completed(
            stage,
            self.clock.now_utc(),
            duration_ms=duration_ms,
            result=StageResult(
                before=before,
                after=after,
                issue_codes=issue_codes,
            ),
        )
        self._notify_observer(journal, observer)
        return journal, duration_ms

    def _finish(
        self,
        journal: RunJournal,
        status: RunStatus,
        *,
        snapshot_version: str,
        interpreted_request: InterpretedRequestSummary | None = None,
        filter_stages: list[FilterStageSummary],
        filter_reason_counts: list[FilterReasonCount] | None = None,
        stage_diagnostics: list[StageDiagnostic],
        public_issues: list[Issue],
        aggregation: CatalogAggregationResult | None = None,
        results: tuple[SearchResult, ...] = (),
        guarded_results: GuardedResults | None = None,
    ) -> SearchExecution:
        if status is not RunStatus.NO_MATCH and aggregation is not None:
            raise ValueError("only a NO_MATCH response may retain aggregation diagnostics")
        if status is RunStatus.COMPLETED:
            checked = require_guarded_results(guarded_results)
            expected_results = tuple(_phase_d_result(item) for item in checked.items)
            if results != expected_results:
                raise ValueError("COMPLETED results must equal the guarded result projection")
        elif guarded_results is not None or results:
            raise ValueError("non-COMPLETED responses cannot retain guarded or public results")
        journal = journal.finish(status, self.clock.now_utc())
        response = SearchResponse(
            run_id=journal.run_id,
            status=status,
            snapshot_version=snapshot_version,
            config_fingerprint=self.config.fingerprint,
            algorithm_version=PHASE_D_ALGORITHM,
            interpreted_request=interpreted_request or InterpretedRequestSummary(),
            results=results,
            filter_summary=FilterSummary(
                stages=tuple(filter_stages),
                reason_counts=tuple(filter_reason_counts or ()),
            ),
            warnings=tuple(public_issues),
            diagnostics=Diagnostics(
                ranker_degraded=any(issue.code == "ranking.degraded" for issue in public_issues),
                stages=tuple(stage_diagnostics),
                issues=tuple(public_issues),
            ),
        )
        return SearchExecution(response=response, journal=journal)


def _public_filter_stages(
    summary: DomainFilterSummary,
) -> tuple[FilterStageSummary, ...]:
    return tuple(
        FilterStageSummary(
            gate=f"{stage.scope.value}.{stage.gate.value}",
            before=stage.before,
            after=stage.after,
        )
        for stage in summary.stages
    )


def _public_filter_reason_counts(
    summary: DomainFilterSummary,
) -> tuple[FilterReasonCount, ...]:
    return tuple(
        FilterReasonCount(
            reason=count.reason,
            product_count=count.product_count,
            offer_count=count.offer_count,
        )
        for count in summary.reason_counts
    )


def _isolate_ranker_preferred(
    preferred: tuple[PreferredCriterion, ...],
) -> tuple[PreferredCriterion, ...]:
    isolated = deepcopy(preferred)
    _require_unchanged_ranker_preferred(preferred, isolated)
    return isolated


def _require_unchanged_ranker_preferred(
    trusted: object,
    isolated: object,
) -> None:
    if type(trusted) is not tuple or type(isolated) is not tuple:
        raise TypeError("ranking preferred inputs must be exact tuples")
    if len(trusted) != len(isolated):
        raise ValueError("ranking preferred input cardinality changed")
    for original, copied in zip(trusted, isolated, strict=True):
        if (
            type(original) is not PreferredCriterion
            or type(copied) is not PreferredCriterion
            or type(original.source_span) is not SourceSpan
            or type(copied.source_span) is not SourceSpan
            or copied is original
            or copied.source_span is original.source_span
            or copied != original
        ):
            raise ValueError("ranking preferred inputs must remain equal and deeply isolated")


def _phase_d_result(draft: ResultDraft) -> SearchResult:
    """Project one final-guarded domain result into the public DTO."""

    if type(draft) is not ResultDraft:
        raise TypeError("public result projection requires an exact ResultDraft")
    ResultDraft.__post_init__(draft)
    candidate = draft.candidate
    offer_summaries = tuple(
        _phase_d_offer_summary(eligible) for eligible in candidate.eligible_offers
    )
    selected_index = next(
        index
        for index, eligible in enumerate(candidate.eligible_offers)
        if eligible is candidate.selected_offer
    )
    selected_summary = offer_summaries[selected_index]
    evidence = tuple(EvidenceSummary(evidence_id=evidence_id) for evidence_id in draft.evidence_ids)
    return SearchResult(
        product_id=candidate.product.product_id,
        title=candidate.product.title,
        category=candidate.product.category,
        selected_offer=selected_summary,
        eligible_offers=offer_summaries,
        landed_cost=selected_summary.landed_cost,
        matched_requirements=draft.matched_requirements,
        unknowns=draft.projection.unknowns,
        reason=draft.projection.reason,
        evidence=evidence,
    )


def _phase_d_offer_summary(eligible: EligibleOffer) -> OfferSummary:
    offer = eligible.offer
    landed_cost = eligible.landed_cost
    return OfferSummary(
        offer_id=offer.offer_id,
        provider_id=offer.provider_id,
        market=offer.market,
        landed_cost=MoneySummary(
            currency=landed_cost.display_currency,
            exact=landed_cost.display_exact_json,
            display=landed_cost.display_json,
        ),
    )


def _summarize_intent(interpreted: InterpretedRequest) -> InterpretedRequestSummary:
    return InterpretedRequestSummary(
        required=tuple(_summarize_criterion(item) for item in interpreted.required),
        preferred=tuple(_summarize_criterion(item) for item in interpreted.preferred),
        parser_version=interpreted.parser_version,
    )


def _summarize_criterion(
    criterion: BudgetMax | TargetCategory | StockRequired | Exclusion | PreferredCriterion,
) -> InterpretedCriterionSummary:
    if isinstance(criterion, BudgetMax):
        value = format(criterion.amount, "f")
        if criterion.currency is not None:
            value = f"{value} {criterion.currency}"
    elif isinstance(criterion, TargetCategory):
        value = criterion.category
    elif isinstance(criterion, StockRequired):
        value = "true"
    elif isinstance(criterion, (Exclusion, PreferredCriterion)):
        value = criterion.value
    else:
        raise TypeError("unsupported intent criterion")
    return InterpretedCriterionSummary(
        kind=criterion.kind,
        value=value,
        source_span=SourceSpanSummary(
            start=criterion.source_span.start,
            end=criterion.source_span.end,
            text=criterion.source_span.text,
        ),
    )


def _intent_adapter_issue(error: Exception) -> Issue:
    try:
        raw_code = getattr(error, "code", None)
        code_value = getattr(raw_code, "value", None)
    except Exception:
        code_value = None
    code = "intent.interpretation-failed"
    if (
        type(code_value) is str
        and code_value.startswith("intent.")
        and _PUBLIC_ISSUE_CODE.fullmatch(code_value) is not None
    ):
        code = code_value
    return Issue(
        code=code,
        stage=INTENT_STAGE,
        message="Request intent could not be interpreted safely.",
        severity=IssueSeverity.ERROR,
    )


def _intent_validation_issue(issue: object) -> Issue:
    code = getattr(getattr(issue, "code", None), "value", None)
    message = getattr(issue, "message", None)
    location = getattr(issue, "location", None)
    if type(code) is not str or type(message) is not str or type(location) is not str:
        raise TypeError("invalid intent validation issue")
    return Issue(
        code=code,
        stage=INTENT_STAGE,
        message=message,
        severity=IssueSeverity.ERROR,
        details=(Detail(key="location", value=location),),
    )


def _aggregation_only_issues(
    combined: tuple[CatalogIssue, ...],
    snapshot: tuple[CatalogIssue, ...],
) -> tuple[CatalogIssue, ...]:
    remaining = list(combined)
    for existing in snapshot:
        try:
            remaining.remove(existing)
        except ValueError:
            continue
    return tuple(remaining)


__all__ = [
    "PHASE_B_ALGORITHM",
    "PHASE_C_ALGORITHM",
    "PHASE_D_ALGORITHM",
    "RESULT_ASSEMBLY_STAGE",
    "WALKING_SKELETON_ALGORITHM",
    "WALKING_SKELETON_STAGE",
    "SearchExecution",
    "SearchService",
]
