"""Explicit bounded root/child AgentLoop for the fixed M1d composition."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import date
from types import MappingProxyType
from typing import cast

from glodex.application.agent.catalog import (
    CandidateEligibility,
    CandidateManifest,
    CandidateStore,
    build_fx_evaluation_view,
    evaluate_candidate_pool,
)
from glodex.application.agent.contracts import (
    FULL_TOOL_SET,
    ActionSelectionInput,
    AgentAnswer,
    AgentAnswerKind,
    AgentCapabilities,
    AgentDemoResponse,
    AgentEventKind,
    AgentEventScope,
    AgentExecution,
    AgentFailureCode,
    AgentRunEvent,
    AgentRunRecord,
    AgentToolSummary,
    CallToolAction,
    CategoryInsightInput,
    CategoryInsightOutput,
    CategoryInsightSelector,
    ChatFallbackOutput,
    ChatFallbackSelector,
    DataMode,
    DispatchSelector,
    EmbeddingBatch,
    EmbeddingResult,
    EmptySelector,
    ForkResult,
    ForkStatus,
    InsightStatus,
    ItemPickerOutput,
    ItemPickerSelector,
    ItemSearchInput,
    ItemSearchRuntimeResult,
    ItemSearchSelector,
    NestedForkReturn,
    PlannerInput,
    PlannerIntentKind,
    PlannerOutput,
    Platform,
    PriceCompareOutput,
    PriceCompareSelector,
    SafeObservation,
    ShippingCalcOutput,
    ShoppingSummaryOutput,
    ToolFailureCode,
    ToolName,
    WebSearchInput,
    WebSearchOutput,
    WebSearchSelector,
)
from glodex.application.agent.ports import (
    ActionSelector,
    AgentEventObserver,
    EmbeddingPort,
    ToolPortError,
)
from glodex.application.agent.state import (
    AGENT_DEADLINE_SECONDS,
    CHILD_DEADLINE_SECONDS,
    TOOL_RESULT_BYTE_LIMIT,
    AgentBudgetLedger,
    AgentPhase,
    AgentTerminal,
    AgentToolState,
    BudgetExceeded,
    RuntimeResources,
    canonical_json_bytes,
    require_byte_limit,
)
from glodex.application.agent.tools import (
    BUSINESS_TOOL_REGISTRY,
    ChatFallbackInput,
    ItemPickerInput,
    ItemSearchToolInput,
    PriceCompareInput,
    SearchServiceFactory,
    ShippingCalcInput,
    ShippingRule,
    ShoppingSummaryInput,
    ToolDependencies,
    execute_business_tool,
)
from glodex.application.ports import IntentInterpreter, RunIdProvider
from glodex.contracts import RunStatus, SearchRequest, SearchResponse
from glodex.domain.catalog import CatalogBatch
from glodex.domain.intent import TargetCategory

type CandidateManifestFactory = Callable[
    [tuple[ItemSearchRuntimeResult, ...]],
    CandidateManifest,
]
type ResourceDrainer = Callable[[], Awaitable[None]]
type ChildToolResult = (
    WebSearchOutput
    | CategoryInsightOutput
    | ItemSearchRuntimeResult
    | PriceCompareOutput
    | ShippingCalcOutput
    | NestedForkReturn
)


class AgentRuntimeError(RuntimeError):
    """One safe runtime failure without dynamic detail."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class AgentRuntimeConfig:
    capabilities: AgentCapabilities
    index_version: str
    ruleset_version: str
    calculation_date: str
    shipping_rules: tuple[ShippingRule, ...]
    fx_source_batch: CatalogBatch

    def __post_init__(self) -> None:
        if type(self.capabilities) is not AgentCapabilities:
            raise TypeError("runtime capabilities must be exact AgentCapabilities")
        if type(self.index_version) is not str or not self.index_version:
            raise ValueError("runtime index_version must be non-empty")
        if type(self.ruleset_version) is not str or not self.ruleset_version:
            raise ValueError("runtime ruleset_version must be non-empty")
        if type(self.calculation_date) is not str:
            raise TypeError("runtime calculation_date must be a string")
        try:
            if date.fromisoformat(self.calculation_date).isoformat() != self.calculation_date:
                raise ValueError
        except ValueError:
            raise ValueError("runtime calculation_date must be strict YYYY-MM-DD") from None
        if type(self.shipping_rules) is not tuple or any(
            type(rule) is not ShippingRule for rule in self.shipping_rules
        ):
            raise TypeError("runtime shipping_rules must contain exact rules")
        if type(self.fx_source_batch) is not CatalogBatch:
            raise TypeError("runtime FX source must be an exact CatalogBatch")


@dataclass(frozen=True, slots=True)
class _ForkScope:
    child_id: str
    task_id: str
    goal: str
    depth: int
    allowed_tool: ToolName
    platform: Platform | None = None
    nested_scopes: tuple[_ForkScope, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.child_id) is not str
            or not self.child_id
            or type(self.task_id) is not str
            or not self.task_id
            or type(self.goal) is not str
            or not self.goal
        ):
            raise ValueError("fork scope identifiers and goal must be non-empty")
        if type(self.depth) is not int or not 1 <= self.depth <= 2:
            raise ValueError("fork scope depth must be one or two")
        allowed = {
            ToolName.WEB_SEARCH,
            ToolName.CATEGORY_INSIGHT,
            ToolName.ITEM_SEARCH,
            ToolName.PRICE_COMPARE,
            ToolName.SHIPPING_CALC,
            ToolName.DISPATCH_TOOL,
        }
        if self.allowed_tool not in allowed:
            raise ValueError("fork scope tool is not child-safe")
        if self.allowed_tool is ToolName.ITEM_SEARCH and self.platform is None:
            raise ValueError("item-search scope requires a trusted platform")
        if self.allowed_tool is not ToolName.ITEM_SEARCH and self.platform is not None:
            raise ValueError("only item-search scope may bind a platform")
        if self.allowed_tool is ToolName.DISPATCH_TOOL:
            if self.depth >= 2 or not self.nested_scopes:
                raise ValueError("nested dispatch requires depth-one trusted scopes")
            if any(scope.depth != self.depth + 1 for scope in self.nested_scopes):
                raise ValueError("nested scopes must advance depth exactly once")
        elif self.nested_scopes:
            raise ValueError("work scopes cannot carry nested scopes")


@dataclass(slots=True)
class _SharedLedger:
    value: AgentBudgetLedger = field(default_factory=AgentBudgetLedger)

    def root_model(self) -> None:
        self.value = self.value.consume_root_model_action()

    def child_model(self) -> None:
        self.value = self.value.consume_child_model_action()

    def root_tool(self) -> None:
        self.value = self.value.consume_root_tool_execution()

    def child_run(self) -> None:
        self.value = self.value.consume_child_run()

    def business(self, tool_name: ToolName) -> None:
        self.value = self.value.consume_business_tool(tool_name)

    def dispatch(self) -> None:
        self.value = self.value.consume_dispatch()

    def tavily(self) -> None:
        self.value = self.value.consume_tavily()

    def dashscope(self) -> None:
        self.value = self.value.consume_dashscope()

    def ebay(self) -> None:
        self.value = self.value.consume_ebay_auth()
        self.value = self.value.consume_ebay_browse()

    def observation(self, byte_count: int) -> None:
        self.value = self.value.add_observation_bytes(byte_count)


@dataclass(slots=True)
class _ToolTracker:
    counts: dict[ToolName, int]
    outcomes: dict[ToolName, str]

    @classmethod
    def empty(cls) -> _ToolTracker:
        return cls(counts={}, outcomes={})

    def record(self, tool_name: ToolName, outcome: str) -> None:
        self.counts[tool_name] = self.counts.get(tool_name, 0) + 1
        previous = self.outcomes.get(tool_name)
        if previous is None or (previous == "SUCCESS" and outcome != "SUCCESS"):
            self.outcomes[tool_name] = outcome
        elif previous != "SUCCESS" and outcome != "SUCCESS":
            self.outcomes[tool_name] = min(previous, outcome)

    def summary(self) -> tuple[AgentToolSummary, ...]:
        return tuple(
            AgentToolSummary(
                tool_name=tool_name,
                call_count=self.counts[tool_name],
                safe_outcome=self.outcomes[tool_name],
            )
            for tool_name in FULL_TOOL_SET
            if tool_name in self.counts
        )


@dataclass(slots=True)
class _EmbeddingSession:
    port: EmbeddingPort | None
    ledger: _SharedLedger
    resources: RuntimeResources
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    async def vectors_for(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        async with self._lock:
            existing = dict(self.resources.query_vectors)
            if all(text in existing for text in texts):
                return tuple(existing[text] for text in texts)
            if existing:
                raise AgentRuntimeError(AgentFailureCode.INVALID_PHASE.value)
            if self.port is None:
                raise AgentRuntimeError(ToolFailureCode.PROVIDER_UNAVAILABLE.value)
            try:
                self.ledger.dashscope()
                result = await self.port.embed(EmbeddingBatch(texts=texts))
            except BudgetExceeded as error:
                raise AgentRuntimeError(error.code) from None
            except ToolPortError as error:
                raise AgentRuntimeError(error.code.value) from None
            if type(result) is not EmbeddingResult or len(result.vectors) != len(texts):
                raise AgentRuntimeError(ToolFailureCode.PROVIDER_RESPONSE_INVALID.value)
            self.resources.query_vectors = tuple(zip(texts, result.vectors, strict=True))
            return result.vectors


@dataclass(slots=True)
class _RunContext:
    run_id: str
    state: AgentToolState
    resources: RuntimeResources
    ledger: _SharedLedger
    tracker: _ToolTracker
    embeddings: _EmbeddingSession
    observer: AgentEventObserver | None
    events: list[AgentRunEvent]
    eligibility: CandidateEligibility | None = None
    preserved_search: SearchResponse | None = None


class AgentService:
    """One explicit root while-loop plus bounded child task groups."""

    def __init__(
        self,
        *,
        config: AgentRuntimeConfig,
        action_selector: ActionSelector,
        intent_interpreter: IntentInterpreter,
        run_id_provider: RunIdProvider,
        tool_dependencies: ToolDependencies,
        embedding_port: EmbeddingPort | None,
        candidate_manifest_factory: CandidateManifestFactory,
        search_service_factory: SearchServiceFactory,
        resource_drainer: ResourceDrainer | None = None,
    ) -> None:
        if type(config) is not AgentRuntimeConfig:
            raise TypeError("Agent service requires exact AgentRuntimeConfig")
        if type(tool_dependencies) is not ToolDependencies:
            raise TypeError("Agent service requires exact ToolDependencies")
        if not callable(candidate_manifest_factory) or not callable(search_service_factory):
            raise TypeError("Agent service factories must be callable")
        if resource_drainer is not None and not callable(resource_drainer):
            raise TypeError("resource drainer must be callable")
        self._config = config
        self._action_selector = action_selector
        self._intent_interpreter = intent_interpreter
        self._run_id_provider = run_id_provider
        self._tool_dependencies = tool_dependencies
        self._embedding_port = embedding_port
        self._candidate_manifest_factory = candidate_manifest_factory
        self._search_service_factory = search_service_factory
        self._resource_drainer = resource_drainer

    async def execute(self, request: SearchRequest) -> AgentExecution:
        return await self.execute_run(
            request,
            run_id=self._run_id_provider.next_run_id(),
            observer=None,
        )

    async def execute_run(
        self,
        request: SearchRequest,
        *,
        run_id: str,
        observer: AgentEventObserver | None = None,
    ) -> AgentExecution:
        if type(request) is not SearchRequest:
            raise TypeError("Agent request must be an exact SearchRequest")
        if type(run_id) is not str or not run_id:
            raise ValueError("Agent run_id must be non-empty")

        events: list[AgentRunEvent] = []
        resources = RuntimeResources()
        ledger = _SharedLedger()
        tracker = _ToolTracker.empty()
        response: AgentDemoResponse | None = None
        failure_code: str | None = None
        context: _RunContext | None = None
        self._emit(
            events,
            observer,
            AgentRunEvent(kind=AgentEventKind.AGENT_STARTED, run_id=run_id),
        )
        try:
            async with asyncio.timeout(AGENT_DEADLINE_SECONDS):
                interpreted = await self._intent_interpreter.interpret(request)
                state = AgentToolState(
                    request=request,
                    interpreted_request=interpreted,
                    required_baseline=interpreted,
                    capabilities=self._config.capabilities,
                )
                context = _RunContext(
                    run_id=run_id,
                    state=state,
                    resources=resources,
                    ledger=ledger,
                    tracker=tracker,
                    embeddings=_EmbeddingSession(
                        port=self._embedding_port,
                        ledger=ledger,
                        resources=resources,
                    ),
                    observer=observer,
                    events=events,
                )
                try:
                    response = await self._run_root(context)
                except _TerminalResult as terminal:
                    response = terminal.response
        except asyncio.CancelledError:
            await self._cleanup(resources)
            raise
        except TimeoutError:
            failure_code = AgentFailureCode.DEADLINE_EXCEEDED.value
        except AgentRuntimeError as error:
            failure_code = error.code
        except BudgetExceeded:
            failure_code = AgentFailureCode.BUDGET_EXCEEDED.value
        except ToolPortError as error:
            failure_code = error.code.value
        except Exception:
            failure_code = AgentFailureCode.INTERNAL_ERROR.value

        cleanup_failed = await self._cleanup(resources)
        if cleanup_failed:
            response = None
            failure_code = AgentFailureCode.INTERNAL_ERROR.value
        if response is None:
            failure_code = failure_code or AgentFailureCode.INTERNAL_ERROR.value
            response = AgentDemoResponse(
                run_id=run_id,
                status=RunStatus.FAILED,
                search_response=None if context is None else context.preserved_search,
                tool_summary=tracker.summary(),
            )
            self._emit(
                events,
                observer,
                AgentRunEvent(
                    kind=AgentEventKind.AGENT_ERROR,
                    run_id=run_id,
                    status="FAILED",
                    safe_code=failure_code,
                ),
            )
        else:
            self._emit(
                events,
                observer,
                AgentRunEvent(
                    kind=AgentEventKind.AGENT_RESULT,
                    run_id=run_id,
                    status=response.status.value,
                ),
            )
        summary = tracker.summary()
        if response.tool_summary != summary:
            response = response.model_copy(update={"tool_summary": summary})
            response = AgentDemoResponse.model_validate(response.model_dump())
        record = AgentRunRecord(
            run_id=run_id,
            status=response.status,
            model_calls=ledger.value.tree_deepseek_calls,
            tool_calls=sum(item.call_count for item in summary),
            child_runs=ledger.value.child_runs,
            terminal_code=failure_code if response.status is RunStatus.FAILED else None,
            tool_summary=summary,
            events=tuple(events),
        )
        return AgentExecution(response=response, record=record)

    async def _run_root(self, context: _RunContext) -> AgentDemoResponse:
        while context.state.phase is not AgentPhase.TERMINAL:
            available = self._available_root_tools(context)
            observation = self._observation(context, available)
            try:
                context.ledger.observation(len(canonical_json_bytes(observation)))
                context.ledger.root_model()
            except BudgetExceeded as error:
                raise AgentRuntimeError(error.code) from None
            round_number = context.ledger.value.root_model_actions
            self._emit(
                context.events,
                context.observer,
                AgentRunEvent(
                    kind=AgentEventKind.MODEL_STARTED,
                    run_id=context.run_id,
                    round=round_number,
                ),
            )
            try:
                action = await self._action_selector.select(
                    ActionSelectionInput(
                        request=context.state.request,
                        observation=observation,
                    )
                )
            except ToolPortError as error:
                raise AgentRuntimeError(error.code.value) from None
            if type(action) is not CallToolAction:
                raise AgentRuntimeError(AgentFailureCode.MODEL_INVALID.value)
            self._emit(
                context.events,
                context.observer,
                AgentRunEvent(
                    kind=AgentEventKind.MODEL_FINISHED,
                    run_id=context.run_id,
                    round=round_number,
                    tool_name=action.tool_name,
                ),
            )
            if action.tool_name not in available:
                raise AgentRuntimeError(AgentFailureCode.INVALID_PHASE.value)
            self._record_action_signature(context, action)
            await self._execute_root_action(context, action)

        raise AgentRuntimeError(AgentFailureCode.INTERNAL_ERROR.value)

    async def _execute_root_action(
        self,
        context: _RunContext,
        action: CallToolAction,
    ) -> None:
        tool_name = action.tool_name
        try:
            context.ledger.root_tool()
        except BudgetExceeded as error:
            raise AgentRuntimeError(error.code) from None
        self._emit(
            context.events,
            context.observer,
            AgentRunEvent(
                kind=AgentEventKind.TOOL_STARTED,
                run_id=context.run_id,
                tool_name=tool_name,
            ),
        )
        try:
            if tool_name is ToolName.DISPATCH_TOOL:
                if type(action.selector_args) is not DispatchSelector:
                    raise AgentRuntimeError(AgentFailureCode.INVALID_ACTION.value)
                context.ledger.dispatch()
                results = await self._dispatch(context, action.selector_args)
                require_byte_limit(
                    results,
                    maximum=TOOL_RESULT_BYTE_LIMIT,
                    code=ToolFailureCode.TOOL_RESULT_TOO_LARGE.value,
                )
                await self._apply_dispatch_results(context, results)
                result: object = results
            else:
                context.ledger.business(tool_name)
                self._reserve_provider_budget(context, tool_name)
                tool_input = await self._root_tool_input(context, action)
                result = await execute_business_tool(
                    tool_name,
                    tool_input,
                    self._tool_dependencies,
                )
                await self._apply_root_result(context, tool_name, result)
        except asyncio.CancelledError:
            context.tracker.record(
                tool_name,
                AgentFailureCode.RUN_ABORTED.value,
            )
            self._tool_finished(
                context,
                tool_name,
                AgentFailureCode.RUN_ABORTED.value,
            )
            raise
        except BudgetExceeded as error:
            context.tracker.record(tool_name, error.code)
            self._tool_finished(context, tool_name, error.code)
            raise AgentRuntimeError(error.code) from None
        except ToolPortError as error:
            context.tracker.record(tool_name, error.code.value)
            self._tool_finished(context, tool_name, error.code.value)
            raise AgentRuntimeError(error.code.value) from None
        except AgentRuntimeError as error:
            context.tracker.record(tool_name, error.code)
            self._tool_finished(context, tool_name, error.code)
            raise
        except (TypeError, ValueError):
            code = AgentFailureCode.INVALID_ACTION.value
            context.tracker.record(tool_name, code)
            self._tool_finished(context, tool_name, code)
            raise AgentRuntimeError(code) from None
        context.tracker.record(tool_name, "SUCCESS")
        self._tool_finished(context, tool_name, "SUCCESS")

        if type(result) is ChatFallbackOutput:
            response = AgentDemoResponse(
                run_id=context.run_id,
                status=RunStatus.COMPLETED,
                answer=AgentAnswer(
                    kind=AgentAnswerKind.CHAT_FALLBACK,
                    text=result.answer,
                ),
                tool_summary=context.tracker.summary(),
            )
            context.state = replace(
                context.state,
                phase=AgentPhase.TERMINAL,
                terminal=AgentTerminal.COMPLETED,
                ledger=context.ledger.value,
            )
            return await self._return_terminal(response)
        if type(result) is ShoppingSummaryOutput:
            context.preserved_search = result.search_response
            evidence_ids = tuple(
                dict.fromkeys(
                    evidence.evidence_id
                    for search_result in result.search_response.results
                    for evidence in search_result.evidence
                )
            )
            response = AgentDemoResponse(
                run_id=context.run_id,
                status=result.search_response.status,
                answer=AgentAnswer(
                    kind=AgentAnswerKind.SHOPPING_SUMMARY,
                    text=result.answer,
                ),
                search_response=result.search_response,
                selected_product_ids=result.selected_product_ids,
                evidence_ids=evidence_ids,
                web_evidence=(
                    () if context.state.web_result is None else context.state.web_result.evidence
                ),
                landed_cost_advisories=(
                    ()
                    if context.state.shipping_result is None
                    else context.state.shipping_result.advisories
                ),
                tool_summary=context.tracker.summary(),
            )
            context.state = replace(
                context.state,
                phase=AgentPhase.TERMINAL,
                terminal=AgentTerminal(result.status),
                ledger=context.ledger.value,
            )
            return await self._return_terminal(response)

    async def _return_terminal(self, response: AgentDemoResponse) -> None:
        raise _TerminalResult(response)

    async def _apply_root_result(
        self,
        context: _RunContext,
        tool_name: ToolName,
        result: object,
    ) -> None:
        state = context.state
        if tool_name is ToolName.PLANNER and type(result) is PlannerOutput:
            context.state = replace(
                state,
                plan=result,
                phase=AgentPhase.EVIDENCE_OR_ITEM,
                ledger=context.ledger.value,
            )
        elif tool_name is ToolName.WEB_SEARCH and type(result) is WebSearchOutput:
            context.state = replace(
                state,
                web_result=result,
                ledger=context.ledger.value,
            )
        elif tool_name is ToolName.CATEGORY_INSIGHT and type(result) is CategoryInsightOutput:
            context.state = replace(
                state,
                category_result=result,
                ledger=context.ledger.value,
            )
        elif tool_name is ToolName.ITEM_SEARCH and type(result) is ItemSearchRuntimeResult:
            self._install_candidate_pool(context, (result,))
            context.state = replace(
                state,
                phase=AgentPhase.NEEDS_PRICE_COMPARE,
                item_results=(result,),
                ledger=context.ledger.value,
            )
        elif tool_name is ToolName.PRICE_COMPARE and type(result) is PriceCompareOutput:
            context.state = replace(
                state,
                phase=AgentPhase.NEEDS_SHIPPING,
                price_result=result,
                ledger=context.ledger.value,
            )
        elif tool_name is ToolName.SHIPPING_CALC and type(result) is ShippingCalcOutput:
            store = context.resources.candidate_store
            if store is None:
                raise AgentRuntimeError(AgentFailureCode.INVALID_PHASE.value)
            eligibility = evaluate_candidate_pool(
                store.pool,
                state.interpreted_request,
                display_currency=state.request.display_currency,
            )
            context.eligibility = eligibility
            context.state = replace(
                state,
                phase=AgentPhase.NEEDS_PICKER,
                shipping_result=result,
                publication_eligible_ids=eligibility.eligible_candidate_ids,
                ledger=context.ledger.value,
            )
        elif tool_name is ToolName.ITEM_PICKER and type(result) is ItemPickerOutput:
            context.state = replace(
                state,
                phase=AgentPhase.NEEDS_SUMMARY,
                picker_result=result,
                ledger=context.ledger.value,
            )
        elif tool_name in {ToolName.CHAT_FALLBACK, ToolName.SHOPPING_SUMMARY}:
            return
        else:
            raise AgentRuntimeError(AgentFailureCode.INTERNAL_ERROR.value)

    async def _apply_dispatch_results(
        self,
        context: _RunContext,
        results: tuple[ForkResult, ...],
    ) -> None:
        """Apply only typed work selected by the trusted runtime scope."""

        item_results: tuple[ItemSearchRuntimeResult, ...] | None = None
        if all(result.tool_name is ToolName.ITEM_SEARCH for result in results):
            item_results = tuple(
                cast(ItemSearchRuntimeResult, result.typed_result) for result in results
            )
        elif len(results) == 1 and results[0].tool_name is ToolName.DISPATCH_TOOL:
            nested = results[0].typed_result
            if type(nested) is not NestedForkReturn or any(
                result.tool_name is not ToolName.ITEM_SEARCH for result in nested.results
            ):
                raise AgentRuntimeError(AgentFailureCode.FORK_FAILED.value)
            item_results = tuple(
                cast(ItemSearchRuntimeResult, result.typed_result) for result in nested.results
            )
        elif len(results) == 1:
            child = results[0]
            if child.tool_name not in {
                ToolName.WEB_SEARCH,
                ToolName.CATEGORY_INSIGHT,
                ToolName.PRICE_COMPARE,
                ToolName.SHIPPING_CALC,
            }:
                raise AgentRuntimeError(AgentFailureCode.FORK_FAILED.value)
            await self._apply_root_result(
                context,
                child.tool_name,
                child.typed_result,
            )
            context.state = replace(
                context.state,
                fork_results=(*context.state.fork_results, *results),
                ledger=context.ledger.value,
            )
            return
        else:
            raise AgentRuntimeError(AgentFailureCode.FORK_FAILED.value)

        if not item_results:
            raise AgentRuntimeError(AgentFailureCode.FORK_FAILED.value)
        self._install_candidate_pool(context, item_results)
        context.state = replace(
            context.state,
            phase=AgentPhase.NEEDS_PRICE_COMPARE,
            item_results=item_results,
            fork_results=(*context.state.fork_results, *results),
            ledger=context.ledger.value,
        )

    async def _root_tool_input(
        self,
        context: _RunContext,
        action: CallToolAction,
    ) -> object:
        state = context.state
        tool_name = action.tool_name
        if tool_name is ToolName.PLANNER:
            return PlannerInput(
                request=state.request,
                interpreted_request=state.interpreted_request,
                required_baseline=state.required_baseline,
                capabilities=state.capabilities,
            )
        if tool_name is ToolName.CHAT_FALLBACK:
            if (
                type(action.selector_args) is not ChatFallbackSelector
                or state.plan is None
                or state.plan.fallback_reason is not action.selector_args.reason_code
            ):
                raise AgentRuntimeError(AgentFailureCode.INVALID_ACTION.value)
            return ChatFallbackInput(reason_code=action.selector_args.reason_code)
        if tool_name is ToolName.WEB_SEARCH:
            if type(action.selector_args) is not WebSearchSelector:
                raise AgentRuntimeError(AgentFailureCode.INVALID_ACTION.value)
            return WebSearchInput(
                query=state.request.query[:512],
                evidence_kind=action.selector_args.evidence_kind,
                max_results=8,
            )
        if tool_name is ToolName.CATEGORY_INSIGHT:
            category = self._target_category(state)
            selector = action.selector_args
            if type(selector) is not CategoryInsightSelector:
                raise AgentRuntimeError(AgentFailureCode.INVALID_ACTION.value)
            category_text = f"{category} {state.request.query}"
            texts: tuple[str, ...] = (category_text,)
            if state.capabilities.data_mode is DataMode.DEMO_SNAPSHOT:
                texts = tuple(dict.fromkeys((category_text, state.request.query)))
            vectors = await context.embeddings.vectors_for(texts)
            return CategoryInsightInput(
                category=category,
                depth=selector.depth,
                query=state.request.query,
                query_vector=vectors[0],
                index_version=self._config.index_version,
            )
        if tool_name is ToolName.ITEM_SEARCH:
            if type(action.selector_args) is not ItemSearchSelector:
                raise AgentRuntimeError(AgentFailureCode.INVALID_ACTION.value)
            vector = await self._item_query_vector(context)
            return ItemSearchToolInput(
                request=ItemSearchInput(
                    query=state.request.query,
                    platform=action.selector_args.platform,
                    top_k=action.selector_args.top_k,
                ),
                data_mode=state.capabilities.data_mode,
                query_vector=vector,
            )
        store = context.resources.candidate_store
        if store is None:
            raise AgentRuntimeError(AgentFailureCode.INVALID_PHASE.value)
        if tool_name is ToolName.PRICE_COMPARE:
            if type(action.selector_args) is not PriceCompareSelector:
                raise AgentRuntimeError(AgentFailureCode.INVALID_ACTION.value)
            return PriceCompareInput(
                pool=store.pool,
                top_n=action.selector_args.top_n,
            )
        if tool_name is ToolName.SHIPPING_CALC:
            if type(action.selector_args) is not EmptySelector or state.price_result is None:
                raise AgentRuntimeError(AgentFailureCode.INVALID_ACTION.value)
            return ShippingCalcInput(
                pool=store.pool,
                price_points=state.price_result,
                rules=self._config.shipping_rules,
                ruleset_version=self._config.ruleset_version,
                calculation_date=self._config.calculation_date,
            )
        if tool_name is ToolName.ITEM_PICKER:
            if (
                type(action.selector_args) is not ItemPickerSelector
                or context.eligibility is None
                or state.price_result is None
                or state.shipping_result is None
                or action.selector_args.max_items > state.request.top_k
            ):
                raise AgentRuntimeError(AgentFailureCode.INVALID_ACTION.value)
            return ItemPickerInput(
                eligibility=context.eligibility,
                prices=state.price_result,
                shipping=state.shipping_result,
                preferred=state.interpreted_request.preferred,
                category_insight=(
                    state.category_result
                    if state.category_result is not None
                    and state.category_result.status is InsightStatus.FOUND
                    else None
                ),
                max_items=action.selector_args.max_items,
            )
        if tool_name is ToolName.SHOPPING_SUMMARY:
            if (
                type(action.selector_args) is not EmptySelector
                or context.eligibility is None
                or state.picker_result is None
            ):
                raise AgentRuntimeError(AgentFailureCode.INVALID_ACTION.value)
            return ShoppingSummaryInput(
                agent_run_id=context.run_id,
                request=state.request,
                interpreted_request=state.interpreted_request,
                eligibility=context.eligibility,
                picker=state.picker_result,
                fx_source_batch=self._config.fx_source_batch,
                search_service_factory=self._search_service_factory,
            )
        raise AgentRuntimeError(AgentFailureCode.INVALID_ACTION.value)

    async def _dispatch(
        self,
        context: _RunContext,
        selector: DispatchSelector,
    ) -> tuple[ForkResult, ...]:
        plan = context.state.plan
        if plan is None or plan.intent_kind is not PlannerIntentKind.SHOPPING:
            raise AgentRuntimeError(AgentFailureCode.INVALID_ACTION.value)

        state = context.state
        task_count = len(selector.tasks)
        if state.phase is AgentPhase.EVIDENCE_OR_ITEM:
            if len(plan.platforms) >= 2 and task_count == len(plan.platforms):
                scopes = tuple(
                    _ForkScope(
                        child_id=f"child-item-{index + 1}",
                        task_id=f"item-{platform.value}",
                        goal=f"trusted {platform.value} item search",
                        depth=1,
                        allowed_tool=ToolName.ITEM_SEARCH,
                        platform=platform,
                    )
                    for index, platform in enumerate(plan.platforms)
                )
            elif task_count == 1:
                if state.web_result is None and state.capabilities.web_search_enabled:
                    scopes = (
                        _ForkScope(
                            child_id="child-web-search",
                            task_id="web-search",
                            goal="trusted web evidence search",
                            depth=1,
                            allowed_tool=ToolName.WEB_SEARCH,
                        ),
                    )
                elif state.category_result is None and state.capabilities.embedding_enabled:
                    scopes = (
                        _ForkScope(
                            child_id="child-category-insight",
                            task_id="category-insight",
                            goal="trusted category insight retrieval",
                            depth=1,
                            allowed_tool=ToolName.CATEGORY_INSIGHT,
                        ),
                    )
                elif 2 <= len(plan.platforms) <= 3:
                    nested_scopes = tuple(
                        _ForkScope(
                            child_id=f"child-item-{index + 2}",
                            task_id=f"item-{platform.value}",
                            goal=f"trusted {platform.value} item search",
                            depth=2,
                            allowed_tool=ToolName.ITEM_SEARCH,
                            platform=platform,
                        )
                        for index, platform in enumerate(plan.platforms)
                    )
                    scopes = (
                        _ForkScope(
                            child_id="child-dispatch-1",
                            task_id="nested-item-dispatch",
                            goal="trusted nested item dispatch",
                            depth=1,
                            allowed_tool=ToolName.DISPATCH_TOOL,
                            nested_scopes=nested_scopes,
                        ),
                    )
                else:
                    raise AgentRuntimeError(AgentFailureCode.INVALID_ACTION.value)
            else:
                raise AgentRuntimeError(AgentFailureCode.INVALID_ACTION.value)
        elif state.phase is AgentPhase.NEEDS_PRICE_COMPARE and task_count == 1:
            scopes = (
                _ForkScope(
                    child_id="child-price-compare",
                    task_id="price-compare",
                    goal="trusted price comparison",
                    depth=1,
                    allowed_tool=ToolName.PRICE_COMPARE,
                ),
            )
        elif state.phase is AgentPhase.NEEDS_SHIPPING and task_count == 1:
            scopes = (
                _ForkScope(
                    child_id="child-shipping-calc",
                    task_id="shipping-calc",
                    goal="trusted shipping calculation",
                    depth=1,
                    allowed_tool=ToolName.SHIPPING_CALC,
                ),
            )
        else:
            raise AgentRuntimeError(AgentFailureCode.INVALID_ACTION.value)
        return await self._run_dispatch(context, scopes)

    async def _run_dispatch(
        self,
        context: _RunContext,
        scopes: tuple[_ForkScope, ...],
    ) -> tuple[ForkResult, ...]:
        if not scopes or len(scopes) > 4:
            raise AgentRuntimeError(AgentFailureCode.INVALID_ACTION.value)
        for _scope in scopes:
            try:
                context.ledger.child_run()
            except BudgetExceeded as error:
                raise AgentRuntimeError(error.code) from None
        results: list[ForkResult | None] = [None] * len(scopes)

        async def run_one(index: int, scope: _ForkScope) -> None:
            results[index] = await self._run_child(context, scope)

        async with asyncio.TaskGroup() as group:
            for index, scope in enumerate(scopes):
                group.create_task(
                    run_one(index, scope),
                    name=f"glodex-agent-{scope.child_id}",
                )
        completed = tuple(cast(ForkResult, result) for result in results)
        if any(result.status is not ForkStatus.COMPLETED for result in completed):
            raise AgentRuntimeError(AgentFailureCode.FORK_FAILED.value)
        return completed

    async def _run_child(
        self,
        context: _RunContext,
        scope: _ForkScope,
    ) -> ForkResult:
        self._emit(
            context.events,
            context.observer,
            AgentRunEvent(
                kind=AgentEventKind.FORK_STARTED,
                run_id=context.run_id,
                child_id=scope.child_id,
                depth=scope.depth,
            ),
        )
        model_calls = 0
        tool_calls = 0
        child_tool = scope.allowed_tool
        tool_finished = False
        try:
            async with asyncio.timeout(CHILD_DEADLINE_SECONDS):
                context.ledger.child_model()
                model_calls = 1
                child_platforms = (
                    (scope.platform,)
                    if scope.platform is not None
                    else tuple(
                        nested_scope.platform
                        for nested_scope in scope.nested_scopes
                        if nested_scope.platform is not None
                    )
                )
                child_observation = SafeObservation(
                    round=1,
                    phase="CHILD_WORK",
                    available_tools=(scope.allowed_tool,),
                    platforms=child_platforms,
                )
                context.ledger.observation(len(canonical_json_bytes(child_observation)))
                self._emit(
                    context.events,
                    context.observer,
                    AgentRunEvent(
                        kind=AgentEventKind.MODEL_STARTED,
                        run_id=context.run_id,
                        scope=AgentEventScope.CHILD,
                        child_id=scope.child_id,
                        depth=scope.depth,
                        round=1,
                    ),
                )
                action = await self._action_selector.select(
                    ActionSelectionInput(
                        request=context.state.request,
                        observation=child_observation,
                    )
                )
                if type(action) is not CallToolAction or action.tool_name is not scope.allowed_tool:
                    raise AgentRuntimeError(AgentFailureCode.INVALID_ACTION.value)
                child_tool = action.tool_name
                self._emit(
                    context.events,
                    context.observer,
                    AgentRunEvent(
                        kind=AgentEventKind.MODEL_FINISHED,
                        run_id=context.run_id,
                        scope=AgentEventScope.CHILD,
                        child_id=scope.child_id,
                        depth=scope.depth,
                        round=1,
                        tool_name=action.tool_name,
                    ),
                )
                self._emit(
                    context.events,
                    context.observer,
                    AgentRunEvent(
                        kind=AgentEventKind.TOOL_STARTED,
                        run_id=context.run_id,
                        scope=AgentEventScope.CHILD,
                        child_id=scope.child_id,
                        depth=scope.depth,
                        tool_name=action.tool_name,
                    ),
                )
                tool_calls = 1
                if action.tool_name is ToolName.DISPATCH_TOOL:
                    if type(action.selector_args) is not DispatchSelector or len(
                        action.selector_args.tasks
                    ) != len(scope.nested_scopes):
                        raise AgentRuntimeError(AgentFailureCode.INVALID_ACTION.value)
                    context.ledger.dispatch()
                    nested = await self._run_dispatch(context, scope.nested_scopes)
                    typed_result: ChildToolResult = NestedForkReturn(results=nested)
                else:
                    context.ledger.business(action.tool_name)
                    self._reserve_provider_budget(context, action.tool_name)
                    tool_input = await self._child_tool_input(context, scope, action)
                    typed_result = cast(
                        ChildToolResult,
                        await execute_business_tool(
                            action.tool_name,
                            tool_input,
                            self._tool_dependencies,
                        ),
                    )
                context.tracker.record(action.tool_name, "SUCCESS")
                self._emit(
                    context.events,
                    context.observer,
                    AgentRunEvent(
                        kind=AgentEventKind.TOOL_FINISHED,
                        run_id=context.run_id,
                        scope=AgentEventScope.CHILD,
                        child_id=scope.child_id,
                        depth=scope.depth,
                        tool_name=action.tool_name,
                        safe_code="SUCCESS",
                    ),
                )
                tool_finished = True
                result = ForkResult(
                    child_id=scope.child_id,
                    depth=scope.depth,
                    goal=scope.goal,
                    status=ForkStatus.COMPLETED,
                    tool_name=action.tool_name,
                    task_id=scope.task_id,
                    safe_code=None,
                    typed_result=typed_result,
                    model_calls=1,
                    tool_calls=1,
                    return_calls=1,
                )
        except asyncio.CancelledError:
            if tool_calls and not tool_finished:
                context.tracker.record(
                    child_tool,
                    AgentFailureCode.RUN_ABORTED.value,
                )
                self._emit(
                    context.events,
                    context.observer,
                    AgentRunEvent(
                        kind=AgentEventKind.TOOL_FINISHED,
                        run_id=context.run_id,
                        scope=AgentEventScope.CHILD,
                        child_id=scope.child_id,
                        depth=scope.depth,
                        tool_name=child_tool,
                        safe_code=AgentFailureCode.RUN_ABORTED.value,
                    ),
                )
            self._emit(
                context.events,
                context.observer,
                AgentRunEvent(
                    kind=AgentEventKind.FORK_FINISHED,
                    run_id=context.run_id,
                    child_id=scope.child_id,
                    depth=scope.depth,
                    status="ABORTED",
                    safe_code=AgentFailureCode.RUN_ABORTED.value,
                ),
            )
            raise
        except Exception as error:
            safe_code = (
                error.code
                if isinstance(error, AgentRuntimeError)
                else error.code
                if isinstance(error, BudgetExceeded)
                else error.code.value
                if isinstance(error, ToolPortError)
                else AgentFailureCode.DEADLINE_EXCEEDED.value
                if isinstance(error, TimeoutError)
                else AgentFailureCode.FORK_FAILED.value
            )
            if tool_calls and not tool_finished:
                context.tracker.record(child_tool, safe_code)
                self._emit(
                    context.events,
                    context.observer,
                    AgentRunEvent(
                        kind=AgentEventKind.TOOL_FINISHED,
                        run_id=context.run_id,
                        scope=AgentEventScope.CHILD,
                        child_id=scope.child_id,
                        depth=scope.depth,
                        tool_name=child_tool,
                        safe_code=safe_code,
                    ),
                )
            tool_failure = (
                ToolFailureCode(safe_code)
                if safe_code in ToolFailureCode._value2member_map_
                else ToolFailureCode.INVALID_PRECONDITION
            )
            result = ForkResult(
                child_id=scope.child_id,
                depth=scope.depth,
                goal=scope.goal,
                status=ForkStatus.FAILED,
                tool_name=child_tool,
                task_id=scope.task_id,
                safe_code=tool_failure,
                typed_result=None,
                model_calls=model_calls,
                tool_calls=tool_calls,
                return_calls=1,
            )
        self._emit(
            context.events,
            context.observer,
            AgentRunEvent(
                kind=AgentEventKind.FORK_FINISHED,
                run_id=context.run_id,
                child_id=scope.child_id,
                depth=scope.depth,
                status=result.status.value,
                safe_code=(None if result.safe_code is None else result.safe_code.value),
            ),
        )
        return result

    async def _child_tool_input(
        self,
        context: _RunContext,
        scope: _ForkScope,
        action: CallToolAction,
    ) -> object:
        if action.tool_name is ToolName.ITEM_SEARCH:
            if (
                type(action.selector_args) is not ItemSearchSelector
                or action.selector_args.platform is not scope.platform
            ):
                raise AgentRuntimeError(AgentFailureCode.INVALID_ACTION.value)
            return ItemSearchToolInput(
                request=ItemSearchInput(
                    query=context.state.request.query,
                    platform=scope.platform,
                    top_k=action.selector_args.top_k,
                ),
                data_mode=context.state.capabilities.data_mode,
                query_vector=await self._item_query_vector(context),
            )
        if action.tool_name in {
            ToolName.WEB_SEARCH,
            ToolName.CATEGORY_INSIGHT,
            ToolName.PRICE_COMPARE,
            ToolName.SHIPPING_CALC,
        }:
            return await self._root_tool_input(context, action)
        raise AgentRuntimeError(AgentFailureCode.INVALID_ACTION.value)

    def _install_candidate_pool(
        self,
        context: _RunContext,
        raw_results: tuple[ItemSearchRuntimeResult, ...],
    ) -> None:
        manifest = self._candidate_manifest_factory(raw_results)
        if (
            type(manifest) is not CandidateManifest
            or manifest.data_mode is not context.state.capabilities.data_mode
        ):
            raise AgentRuntimeError(ToolFailureCode.ITEM_SOURCE_INVALID.value)
        results = raw_results
        if manifest.data_mode is DataMode.LIVE_MARKETPLACE:
            results = tuple(
                ItemSearchRuntimeResult(
                    platform=result.platform,
                    candidates=result.candidates,
                    platform_sub_batch=build_fx_evaluation_view(
                        result.platform_sub_batch,
                        self._config.fx_source_batch,
                    ),
                    total_recall=result.total_recall,
                    truncated=result.truncated,
                )
                for result in raw_results
            )
        store = CandidateStore(manifest)
        store.merge(results)
        context.resources.install_candidate_store(store)

    async def _item_query_vector(
        self,
        context: _RunContext,
    ) -> tuple[float, ...] | None:
        if context.state.capabilities.data_mode is DataMode.LIVE_MARKETPLACE:
            return None
        return (await context.embeddings.vectors_for((context.state.request.query,)))[0]

    def _available_root_tools(self, context: _RunContext) -> tuple[ToolName, ...]:
        state = context.state
        if state.phase is AgentPhase.NEEDS_PLAN:
            return (ToolName.PLANNER,)
        if state.phase is AgentPhase.EVIDENCE_OR_ITEM:
            plan = state.plan
            if plan is None:
                raise AgentRuntimeError(AgentFailureCode.INVALID_PHASE.value)
            if plan.intent_kind is PlannerIntentKind.UNSUPPORTED_OR_NON_SHOPPING:
                return (ToolName.CHAT_FALLBACK,)
            available: list[ToolName] = []
            if state.web_result is None and state.capabilities.web_search_enabled:
                available.append(ToolName.WEB_SEARCH)
            if state.category_result is None and state.capabilities.embedding_enabled:
                available.append(ToolName.CATEGORY_INSIGHT)
            available.append(
                ToolName.ITEM_SEARCH if len(plan.platforms) == 1 else ToolName.DISPATCH_TOOL
            )
            return tuple(available)
        if state.phase is AgentPhase.NEEDS_PRICE_COMPARE:
            return (ToolName.PRICE_COMPARE, ToolName.DISPATCH_TOOL)
        if state.phase is AgentPhase.NEEDS_SHIPPING:
            return (ToolName.SHIPPING_CALC, ToolName.DISPATCH_TOOL)
        if state.phase is AgentPhase.NEEDS_PICKER:
            return (ToolName.ITEM_PICKER,)
        if state.phase is AgentPhase.NEEDS_SUMMARY:
            return (ToolName.SHOPPING_SUMMARY,)
        return ()

    def _observation(
        self,
        context: _RunContext,
        available: tuple[ToolName, ...],
    ) -> SafeObservation:
        store = context.resources.candidate_store
        candidates = () if store is None else store.pool.candidates
        return SafeObservation(
            round=context.ledger.value.root_model_actions,
            phase=context.state.phase.value,
            available_tools=available,
            executed_tools=tuple(tool for tool in FULL_TOOL_SET if tool in context.tracker.counts),
            platforms=(() if context.state.plan is None else context.state.plan.platforms),
            candidate_count=len(candidates),
            top_candidate_ids=tuple(candidate.candidate_id for candidate in candidates[:3]),
            publication_eligible_ids=context.state.publication_eligible_ids[:3],
            safe_codes=(),
        )

    def _record_action_signature(
        self,
        context: _RunContext,
        action: CallToolAction,
    ) -> None:
        fingerprint = {
            "phase": context.state.phase.value,
            "plan": (
                None
                if context.state.plan is None
                else tuple(platform.value for platform in context.state.plan.platforms)
            ),
            "candidate_count": (
                0
                if context.resources.candidate_store is None
                else len(context.resources.candidate_store.pool.candidates)
            ),
            "web_complete": context.state.web_result is not None,
            "category_complete": context.state.category_result is not None,
            "eligible": context.state.publication_eligible_ids,
            "picker": (
                ()
                if context.state.picker_result is None
                else context.state.picker_result.selected_candidate_ids
            ),
        }
        digest = hashlib.sha256(
            canonical_json_bytes(
                {
                    "action": action,
                    "state": fingerprint,
                }
            )
        ).hexdigest()
        if digest in context.state.action_signatures:
            raise AgentRuntimeError(AgentFailureCode.LOOP_DETECTED.value)
        context.state = replace(
            context.state,
            action_signatures=(*context.state.action_signatures, digest),
        )

    def _reserve_provider_budget(
        self,
        context: _RunContext,
        tool_name: ToolName,
    ) -> None:
        if tool_name is ToolName.WEB_SEARCH:
            context.ledger.tavily()
        elif (
            tool_name is ToolName.ITEM_SEARCH
            and context.state.capabilities.data_mode is DataMode.LIVE_MARKETPLACE
        ):
            context.ledger.ebay()

    def _target_category(self, state: AgentToolState) -> str:
        category = next(
            (
                criterion.category
                for criterion in state.interpreted_request.required
                if type(criterion) is TargetCategory
            ),
            None,
        )
        if category is None:
            raise AgentRuntimeError(AgentFailureCode.INVALID_PHASE.value)
        return category

    def _tool_finished(
        self,
        context: _RunContext,
        tool_name: ToolName,
        outcome: str,
    ) -> None:
        self._emit(
            context.events,
            context.observer,
            AgentRunEvent(
                kind=AgentEventKind.TOOL_FINISHED,
                run_id=context.run_id,
                tool_name=tool_name,
                safe_code=outcome,
            ),
        )

    async def _cleanup(self, resources: RuntimeResources) -> bool:
        failed = False
        if self._resource_drainer is not None:
            try:
                await self._resource_drainer()
            except Exception:
                failed = True
        resources.clear()
        return failed

    @staticmethod
    def _emit(
        events: list[AgentRunEvent],
        observer: AgentEventObserver | None,
        event: AgentRunEvent,
    ) -> None:
        events.append(event)
        if observer is not None:
            with suppress(Exception):
                observer.on_event(event)


class _TerminalResult(BaseException):
    def __init__(self, response: AgentDemoResponse) -> None:
        self.response = response


FULL_TOOL_REGISTRY = MappingProxyType(
    {
        **BUSINESS_TOOL_REGISTRY,
        ToolName.DISPATCH_TOOL: AgentService._dispatch,
    }
)


__all__ = [
    "FULL_TOOL_REGISTRY",
    "AgentRuntimeConfig",
    "AgentRuntimeError",
    "AgentService",
]
