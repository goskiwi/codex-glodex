from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from glodex.adapters.agent_indexes import load_agent_indexes
from glodex.adapters.agent_item_search import DemoItemSource
from glodex.adapters.deterministic_ranker import DeterministicQueryRanker
from glodex.adapters.rule_intent import AgentRuleIntentInterpreter
from glodex.application.agent.catalog import CandidateManifest
from glodex.application.agent.contracts import (
    FULL_TOOL_SET,
    AgentCapabilities,
    AgentEventKind,
    AgentEventScope,
    CallToolAction,
    CategoryInsightSelector,
    ChatFallbackSelector,
    DataMode,
    DispatchSelector,
    DispatchTaskSelector,
    EmbeddingBatch,
    EmbeddingResult,
    EmptySelector,
    EvidenceKind,
    InsightDepth,
    ItemPickerSelector,
    ItemSearchSelector,
    PlannerFallbackReason,
    Platform,
    PriceCompareSelector,
    ToolFailureCode,
    ToolName,
    WebEvidence,
    WebSearchInput,
    WebSearchOutput,
    WebSearchSelector,
)
from glodex.application.agent.ports import ToolPortError
from glodex.application.agent.runtime import (
    FULL_TOOL_REGISTRY,
    AgentRuntimeConfig,
    AgentService,
)
from glodex.application.agent.tools import (
    ShippingRule,
    ToolDependencies,
)
from glodex.application.search_service import SearchService
from glodex.config import GlodexConfig
from glodex.contracts import RunStatus, SearchRequest

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec(
        "GLO-M1D-P0-003",
        "GLO-M1D-P0-005",
        "GLO-M1D-NFR-002",
        "GLO-M1D-NFR-004",
        "GLO-M1D-NFR-006",
    ),
]

ROOT = Path(__file__).parents[3]
UNIT_VECTOR = (1.0, *([0.0] * 1023))


def _dispatch_action(task_count: int) -> CallToolAction:
    return CallToolAction(
        tool_name=ToolName.DISPATCH_TOOL,
        selector_args=DispatchSelector(
            tasks=tuple(
                DispatchTaskSelector(
                    task_id=f"model-task-{index + 1}",
                    demands=f"untrusted demand {index + 1}",
                )
                for index in range(task_count)
            )
        ),
    )


def _child_action(
    tool_name: ToolName,
    *,
    platform: Platform | None = None,
) -> CallToolAction:
    if tool_name is ToolName.WEB_SEARCH:
        selector = WebSearchSelector(evidence_kind=EvidenceKind.REVIEW)
    elif tool_name is ToolName.CATEGORY_INSIGHT:
        selector = CategoryInsightSelector(depth=InsightDepth.QUICK)
    elif tool_name is ToolName.ITEM_SEARCH:
        assert platform is not None
        selector = ItemSearchSelector(platform=platform, top_k=20)
    elif tool_name is ToolName.PRICE_COMPARE:
        selector = PriceCompareSelector(top_n=12)
    elif tool_name is ToolName.SHIPPING_CALC:
        selector = EmptySelector()
    else:
        raise AssertionError(f"unsupported child tool: {tool_name}")
    return CallToolAction(tool_name=tool_name, selector_args=selector)


@dataclass
class _Ids:
    value: str = "agent-run-1"

    def next_run_id(self) -> str:
        return self.value


@dataclass
class _Clock:
    ticks: int = 0

    def now_utc(self) -> datetime:
        return datetime(2026, 7, 29, tzinfo=UTC)

    def monotonic_ns(self) -> int:
        self.ticks += 1
        return self.ticks * 1_000_000


@dataclass
class _Embedding:
    requests: list[EmbeddingBatch] = field(default_factory=list)

    async def embed(self, request: EmbeddingBatch) -> EmbeddingResult:
        self.requests.append(request)
        return EmbeddingResult(
            vectors=tuple(UNIT_VECTOR for _ in request.texts),
        )


@dataclass
class _ReactiveSelector:
    fallback_reason: PlannerFallbackReason = PlannerFallbackReason.NON_SHOPPING
    calls: list[object] = field(default_factory=list)

    async def select(self, request: object) -> CallToolAction:
        from glodex.application.agent.contracts import ActionSelectionInput

        assert type(request) is ActionSelectionInput
        self.calls.append(request)
        available = request.observation.available_tools
        if available == (ToolName.PLANNER,):
            return CallToolAction(
                tool_name=ToolName.PLANNER,
                selector_args=EmptySelector(),
            )
        if available == (ToolName.CHAT_FALLBACK,):
            return CallToolAction(
                tool_name=ToolName.CHAT_FALLBACK,
                selector_args=ChatFallbackSelector(reason_code=self.fallback_reason),
            )
        if available == (ToolName.ITEM_SEARCH,):
            return CallToolAction(
                tool_name=ToolName.ITEM_SEARCH,
                selector_args=ItemSearchSelector(
                    platform=request.observation.platforms[0],
                    top_k=20,
                ),
            )
        if ToolName.ITEM_SEARCH in available:
            return CallToolAction(
                tool_name=ToolName.ITEM_SEARCH,
                selector_args=ItemSearchSelector(
                    platform=request.observation.platforms[0],
                    top_k=20,
                ),
            )
        if ToolName.PRICE_COMPARE in available:
            return CallToolAction(
                tool_name=ToolName.PRICE_COMPARE,
                selector_args=PriceCompareSelector(top_n=12),
            )
        if ToolName.SHIPPING_CALC in available:
            return CallToolAction(
                tool_name=ToolName.SHIPPING_CALC,
                selector_args=EmptySelector(),
            )
        if ToolName.DISPATCH_TOOL in available:
            return _dispatch_action(len(request.observation.platforms))
        if available == (ToolName.ITEM_PICKER,):
            return CallToolAction(
                tool_name=ToolName.ITEM_PICKER,
                selector_args=ItemPickerSelector(
                    max_items=request.request.top_k,
                ),
            )
        if available == (ToolName.SHOPPING_SUMMARY,):
            return CallToolAction(
                tool_name=ToolName.SHOPPING_SUMMARY,
                selector_args=EmptySelector(),
            )
        raise AssertionError(f"unexpected tools: {available!r}")


@dataclass
class _BadSelector:
    async def select(self, request: object) -> CallToolAction:
        del request
        return CallToolAction(
            tool_name=ToolName.PRICE_COMPARE,
            selector_args=PriceCompareSelector(top_n=1),
        )


@dataclass
class _NestedSelector:
    platform_count: int = 2

    async def select(self, request: object) -> CallToolAction:
        from glodex.application.agent.contracts import ActionSelectionInput

        assert type(request) is ActionSelectionInput
        observation = request.observation
        available = observation.available_tools
        executed = observation.executed_tools
        if available == (ToolName.PLANNER,):
            return CallToolAction(
                tool_name=ToolName.PLANNER,
                selector_args=EmptySelector(),
            )
        if observation.phase == "CHILD_WORK":
            if available == (ToolName.DISPATCH_TOOL,):
                assert len(observation.platforms) == self.platform_count
                return _dispatch_action(len(observation.platforms))
            if available == (ToolName.ITEM_SEARCH,):
                return _child_action(
                    ToolName.ITEM_SEARCH,
                    platform=observation.platforms[0],
                )
        if ToolName.WEB_SEARCH in available and ToolName.WEB_SEARCH not in executed:
            return _child_action(ToolName.WEB_SEARCH)
        if ToolName.CATEGORY_INSIGHT in available and ToolName.CATEGORY_INSIGHT not in executed:
            return _child_action(ToolName.CATEGORY_INSIGHT)
        if available == (ToolName.DISPATCH_TOOL,):
            return _dispatch_action(1)
        if ToolName.PRICE_COMPARE in available:
            return _child_action(ToolName.PRICE_COMPARE)
        if ToolName.SHIPPING_CALC in available:
            return _child_action(ToolName.SHIPPING_CALC)
        if available == (ToolName.ITEM_PICKER,):
            return CallToolAction(
                tool_name=ToolName.ITEM_PICKER,
                selector_args=ItemPickerSelector(max_items=request.request.top_k),
            )
        if available == (ToolName.SHOPPING_SUMMARY,):
            return CallToolAction(
                tool_name=ToolName.SHOPPING_SUMMARY,
                selector_args=EmptySelector(),
            )
        raise AssertionError(f"unexpected nested action: {available!r}")


@dataclass
class _ChildWorkSelector(_ReactiveSelector):
    child_tool: ToolName = ToolName.ITEM_SEARCH

    async def select(self, request: object) -> CallToolAction:
        from glodex.application.agent.contracts import ActionSelectionInput

        assert type(request) is ActionSelectionInput
        observation = request.observation
        available = observation.available_tools
        executed = observation.executed_tools
        if observation.phase == "CHILD_WORK":
            assert len(available) == 1
            return _child_action(
                available[0],
                platform=(None if not observation.platforms else observation.platforms[0]),
            )
        if (
            self.child_tool is ToolName.WEB_SEARCH
            and ToolName.WEB_SEARCH in available
            and ToolName.WEB_SEARCH not in executed
        ):
            return _dispatch_action(1)
        if (
            self.child_tool is ToolName.CATEGORY_INSIGHT
            and ToolName.CATEGORY_INSIGHT in available
            and ToolName.CATEGORY_INSIGHT not in executed
        ):
            if ToolName.WEB_SEARCH in available:
                return _child_action(ToolName.WEB_SEARCH)
            return _dispatch_action(1)
        if (
            self.child_tool is ToolName.ITEM_SEARCH
            and ToolName.DISPATCH_TOOL in available
            and observation.phase == "EVIDENCE_OR_ITEM"
        ):
            return _dispatch_action(len(observation.platforms))
        if self.child_tool is ToolName.PRICE_COMPARE and ToolName.PRICE_COMPARE in available:
            return _dispatch_action(1)
        if self.child_tool is ToolName.SHIPPING_CALC and ToolName.SHIPPING_CALC in available:
            return _dispatch_action(1)
        return await super().select(request)


@dataclass
class _OversizedPickerSelector(_ReactiveSelector):
    async def select(self, request: object) -> CallToolAction:
        from glodex.application.agent.contracts import ActionSelectionInput

        assert type(request) is ActionSelectionInput
        if request.observation.available_tools == (ToolName.ITEM_PICKER,):
            return CallToolAction(
                tool_name=ToolName.ITEM_PICKER,
                selector_args=ItemPickerSelector(max_items=3),
            )
        return await super().select(request)


@dataclass
class _BlockingSelector:
    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def select(self, request: object) -> CallToolAction:
        del request
        self.started.set()
        await self.release.wait()
        return CallToolAction(
            tool_name=ToolName.PLANNER,
            selector_args=EmptySelector(),
        )


@dataclass
class _OptionalSelector(_ReactiveSelector):
    async def select(self, request: object) -> CallToolAction:
        from glodex.application.agent.contracts import ActionSelectionInput

        assert type(request) is ActionSelectionInput
        available = request.observation.available_tools
        executed = request.observation.executed_tools
        if ToolName.CATEGORY_INSIGHT in available and ToolName.CATEGORY_INSIGHT not in executed:
            self.calls.append(request)
            return CallToolAction(
                tool_name=ToolName.CATEGORY_INSIGHT,
                selector_args=CategoryInsightSelector(depth=InsightDepth.QUICK),
            )
        if ToolName.WEB_SEARCH in available and ToolName.WEB_SEARCH not in executed:
            self.calls.append(request)
            return CallToolAction(
                tool_name=ToolName.WEB_SEARCH,
                selector_args=WebSearchSelector(evidence_kind=EvidenceKind.REVIEW),
            )
        return await super().select(request)


@dataclass
class _Web:
    calls: int = 0

    async def search(self, request: WebSearchInput) -> WebSearchOutput:
        self.calls += 1
        return WebSearchOutput(
            evidence=(
                WebEvidence(
                    source_id="web-review-1",
                    title="Independent review",
                    url_domain="reviews.example",
                    snippet="A bounded review summary.",
                    source_type=request.evidence_kind,
                ),
            )
        )


@dataclass
class _BarrierItems:
    delegate: DemoItemSource
    expected: int
    arrived: int = 0
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def search(
        self,
        request: object,
        *,
        query_vector: tuple[float, ...] | None,
    ) -> object:
        from glodex.application.agent.contracts import ItemSearchInput

        assert type(request) is ItemSearchInput
        self.arrived += 1
        if self.arrived == self.expected:
            self.release.set()
        await asyncio.wait_for(self.release.wait(), timeout=1)
        return await self.delegate.search(request, query_vector=query_vector)


@dataclass
class _FailingItems:
    delegate: DemoItemSource
    failed_platform: Platform

    async def search(
        self,
        request: object,
        *,
        query_vector: tuple[float, ...] | None,
    ) -> object:
        from glodex.application.agent.contracts import ItemSearchInput

        assert type(request) is ItemSearchInput
        if request.platform is self.failed_platform:
            raise ToolPortError(ToolFailureCode.PROVIDER_UNAVAILABLE)
        return await self.delegate.search(request, query_vector=query_vector)


@dataclass
class _Observer:
    events: list[object] = field(default_factory=list)
    fail: bool = False

    def on_event(self, event: object) -> None:
        self.events.append(event)
        if self.fail:
            raise RuntimeError("projection failed")


def _search_factory(gateway: object) -> SearchService:
    from glodex.application.agent.catalog import InMemoryCatalogGateway

    assert type(gateway) is InMemoryCatalogGateway
    return SearchService(
        config=GlodexConfig(
            data_dir=ROOT / "data" / "snapshots",
            default_snapshot="m1d-demo-v1",
            default_locale="zh-CN",
            default_currency="CNY",
            default_top_k=3,
            fingerprint="a" * 64,
        ),
        run_id_provider=_Ids(),
        clock=_Clock(),
        intent_interpreter=AgentRuleIntentInterpreter(),
        catalog_gateway=gateway,
        query_ranker=DeterministicQueryRanker(),
    )


async def _service(
    selector: object,
    *,
    barrier: bool = False,
    fail_platform: Platform | None = None,
    drainer: object | None = None,
) -> tuple[AgentService, _Embedding, _ReactiveSelector | None]:
    indexes = await load_agent_indexes(
        snapshot_root=ROOT / "data" / "snapshots",
        agent_root=ROOT / "data" / "agent",
    )
    source = DemoItemSource(indexes)
    item_source: object
    if barrier:
        item_source = _BarrierItems(source, expected=4)
    elif fail_platform is not None:
        item_source = _FailingItems(source, fail_platform)
    else:
        item_source = source
    web = _Web()

    def manifest_factory(results: tuple[object, ...]) -> CandidateManifest:
        from glodex.application.agent.contracts import ItemSearchRuntimeResult

        assert all(type(result) is ItemSearchRuntimeResult for result in results)
        typed = tuple(result for result in results if type(result) is ItemSearchRuntimeResult)
        return CandidateManifest(
            data_mode=DataMode.DEMO_SNAPSHOT,
            snapshot_version=indexes.snapshot_version,
            records=tuple(
                record for result in typed for record in source.manifest_records_for(result)
            ),
        )

    runtime_rules = tuple(
        ShippingRule(
            platform=rule.platform,
            flat_shipping=rule.base_shipping,
            duty_rate=rule.duty_rate,
            duty_threshold=rule.duty_threshold,
            effective_date=rule.effective_date.isoformat(),
            eta_days_min=rule.eta_min_days,
            eta_days_max=rule.eta_max_days,
        )
        for rule in indexes.shipping_rules
    )
    embedding = _Embedding()
    service = AgentService(
        config=AgentRuntimeConfig(
            capabilities=AgentCapabilities(
                data_mode=DataMode.DEMO_SNAPSHOT,
                available_platforms=(
                    Platform.AMAZON,
                    Platform.SHOPEE,
                    Platform.ALIEXPRESS,
                    Platform.EBAY,
                ),
                web_search_enabled=True,
                embedding_enabled=True,
            ),
            index_version=indexes.index_version,
            ruleset_version=indexes.ruleset_version,
            calculation_date="2026-07-29",
            shipping_rules=runtime_rules,
            fx_source_batch=indexes.batch,
        ),
        action_selector=selector,  # type: ignore[arg-type]
        intent_interpreter=AgentRuleIntentInterpreter(),
        run_id_provider=_Ids(),
        tool_dependencies=ToolDependencies(
            web_search=web,
            category_insight=indexes,
            item_source=item_source,  # type: ignore[arg-type]
        ),
        embedding_port=embedding,
        candidate_manifest_factory=manifest_factory,  # type: ignore[arg-type]
        search_service_factory=_search_factory,  # type: ignore[arg-type]
        resource_drainer=drainer,  # type: ignore[arg-type]
    )
    return (
        service,
        embedding,
        selector if type(selector) is _ReactiveSelector else None,
    )


def test_full_registry_is_exactly_nine_business_tools_plus_real_dispatch() -> None:
    assert tuple(FULL_TOOL_REGISTRY) == FULL_TOOL_SET
    assert len(FULL_TOOL_REGISTRY) == 10
    assert callable(FULL_TOOL_REGISTRY[ToolName.DISPATCH_TOOL])


def test_single_platform_agent_loop_reaches_canonical_summary_and_cleans_up() -> None:
    async def scenario() -> None:
        selector = _ReactiveSelector()
        service, embedding, _ = await _service(selector)
        execution = await service.execute(
            SearchRequest(
                query="在 amazon 推荐手机",
                display_currency="CNY",
                top_k=1,
            )
        )

        assert execution.response.status is RunStatus.COMPLETED
        assert execution.response.search_response is not None
        assert execution.response.selected_product_ids
        assert execution.response.evidence_ids
        assert len(embedding.requests) == 1
        assert embedding.requests[0].texts == ("在 amazon 推荐手机",)
        assert tuple(item.tool_name for item in execution.response.tool_summary) == (
            ToolName.PLANNER,
            ToolName.ITEM_SEARCH,
            ToolName.ITEM_PICKER,
            ToolName.PRICE_COMPARE,
            ToolName.SHIPPING_CALC,
            ToolName.SHOPPING_SUMMARY,
        )
        assert execution.record.events[0].kind is AgentEventKind.AGENT_STARTED
        assert execution.record.events[-1].kind is AgentEventKind.AGENT_RESULT

    asyncio.run(scenario())


def test_four_platform_dispatch_is_concurrent_ordered_and_one_embedding_batch() -> None:
    async def scenario() -> None:
        selector = _ReactiveSelector()
        service, embedding, _ = await _service(selector, barrier=True)
        execution = await service.execute(
            SearchRequest(
                query="比较 amazon shopee aliexpress ebay 的手机",
                display_currency="CNY",
                top_k=3,
            )
        )

        assert execution.response.status is RunStatus.COMPLETED
        assert execution.record.child_runs == 4
        assert len(embedding.requests) == 1
        item_summary = next(
            item
            for item in execution.response.tool_summary
            if item.tool_name is ToolName.ITEM_SEARCH
        )
        assert item_summary.call_count == 4
        child_events = tuple(
            event for event in execution.record.events if event.scope is AgentEventScope.CHILD
        )
        assert (
            len([event for event in child_events if event.kind is AgentEventKind.MODEL_STARTED])
            == 4
        )
        for child_id in {event.child_id for event in child_events}:
            bracket = tuple(
                index
                for index, event in enumerate(execution.record.events)
                if event.child_id == child_id
            )
            assert execution.record.events[bracket[0]].kind is AgentEventKind.FORK_STARTED
            assert execution.record.events[bracket[-1]].kind is AgentEventKind.FORK_FINISHED

    asyncio.run(scenario())


def test_typed_empty_eligibility_is_the_only_no_match_path() -> None:
    async def scenario() -> None:
        service, _embedding, _ = await _service(_ReactiveSelector())
        execution = await service.execute(
            SearchRequest(
                query="在 amazon 推荐预算 1 元的手机",
                display_currency="CNY",
                top_k=1,
            )
        )

        assert execution.response.status is RunStatus.NO_MATCH
        assert execution.response.search_response is not None
        assert execution.response.selected_product_ids == ()
        assert execution.record.events[-1].status == "NO_MATCH"

    asyncio.run(scenario())


def test_fallback_is_local_terminal_and_invalid_phase_fails_without_tool_execution() -> None:
    async def scenario() -> None:
        fallback_service, embedding, _ = await _service(_ReactiveSelector())
        fallback = await fallback_service.execute(SearchRequest(query="你好"))
        assert fallback.response.status is RunStatus.COMPLETED
        assert fallback.response.search_response is None
        assert embedding.requests == []
        assert tuple(item.tool_name for item in fallback.response.tool_summary) == (
            ToolName.PLANNER,
            ToolName.CHAT_FALLBACK,
        )

        failed_service, _embedding, _ = await _service(_BadSelector())
        failed = await failed_service.execute(SearchRequest(query="推荐手机"))
        assert failed.response.status is RunStatus.FAILED
        assert failed.response.answer is None
        assert failed.response.tool_summary == ()
        assert failed.record.terminal_code == "INVALID_PHASE"
        assert failed.record.events[-1].kind is AgentEventKind.AGENT_ERROR

    asyncio.run(scenario())


def test_optional_category_and_web_steps_feed_one_bounded_embedding_session() -> None:
    async def scenario() -> None:
        selector = _OptionalSelector()
        service, embedding, _ = await _service(selector)
        execution = await service.execute(
            SearchRequest(
                query="在 amazon 推荐手机",
                display_currency="CNY",
                top_k=1,
            )
        )

        assert execution.response.status is RunStatus.COMPLETED
        assert tuple(item.tool_name for item in execution.response.tool_summary) == (
            ToolName.PLANNER,
            ToolName.WEB_SEARCH,
            ToolName.CATEGORY_INSIGHT,
            ToolName.ITEM_SEARCH,
            ToolName.ITEM_PICKER,
            ToolName.PRICE_COMPARE,
            ToolName.SHIPPING_CALC,
            ToolName.SHOPPING_SUMMARY,
        )
        assert len(execution.response.web_evidence) == 1
        assert len(embedding.requests) == 1
        assert embedding.requests[0].texts == (
            "phone 在 amazon 推荐手机",
            "在 amazon 推荐手机",
        )

    asyncio.run(scenario())


def test_child_failure_is_atomic_and_cleanup_precedes_the_single_error_terminal() -> None:
    async def scenario() -> None:
        drained = False

        async def drain() -> None:
            nonlocal drained
            drained = True

        selector = _ReactiveSelector()
        service, embedding, _ = await _service(
            selector,
            fail_platform=Platform.SHOPEE,
            drainer=drain,
        )
        observer = _Observer(fail=True)
        execution = await service.execute_run(
            SearchRequest(
                query="比较 amazon shopee aliexpress ebay 的手机",
                display_currency="CNY",
                top_k=3,
            ),
            run_id="agent-run-failed",
            observer=observer,  # type: ignore[arg-type]
        )

        assert drained
        assert execution.response.status is RunStatus.FAILED
        assert execution.response.search_response is None
        assert execution.response.selected_product_ids == ()
        assert execution.record.terminal_code == "FORK_FAILED"
        assert execution.record.events[-1].kind is AgentEventKind.AGENT_ERROR
        assert (
            len(
                [
                    event
                    for event in execution.record.events
                    if event.kind is AgentEventKind.AGENT_ERROR
                ]
            )
            == 1
        )
        assert len(embedding.requests) == 1
        item_summary = next(
            item
            for item in execution.response.tool_summary
            if item.tool_name is ToolName.ITEM_SEARCH
        )
        assert item_summary.call_count == 4
        assert observer.events[-1] == execution.record.events[-1]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("query", "platform_count", "expected_children"),
    [
        ("比较 amazon ebay 的手机", 2, 3),
        ("比较 amazon shopee ebay 的手机", 3, 4),
    ],
)
def test_public_service_reaches_trusted_depth_two_and_merges_nested_items(
    query: str,
    platform_count: int,
    expected_children: int,
) -> None:
    async def scenario() -> None:
        service, embedding, _ = await _service(_NestedSelector(platform_count=platform_count))
        execution = await service.execute(
            SearchRequest(
                query=query,
                display_currency="CNY",
                top_k=2,
            )
        )

        assert execution.response.status is RunStatus.COMPLETED
        assert execution.response.search_response is not None
        assert execution.record.child_runs == expected_children
        dispatch_summary = next(
            item
            for item in execution.response.tool_summary
            if item.tool_name is ToolName.DISPATCH_TOOL
        )
        assert dispatch_summary.call_count == 2
        item_summary = next(
            item
            for item in execution.response.tool_summary
            if item.tool_name is ToolName.ITEM_SEARCH
        )
        assert item_summary.call_count == platform_count
        fork_depths = tuple(
            event.depth
            for event in execution.record.events
            if event.kind is AgentEventKind.FORK_STARTED
        )
        assert fork_depths.count(1) == 1
        assert fork_depths.count(2) == platform_count
        assert len(embedding.requests) == 1

    asyncio.run(scenario())


def test_four_platform_single_task_cannot_open_a_fifth_nested_child() -> None:
    async def scenario() -> None:
        service, _embedding, _ = await _service(_NestedSelector(platform_count=4))
        execution = await service.execute(
            SearchRequest(
                query="比较 amazon shopee aliexpress ebay 的手机",
                display_currency="CNY",
                top_k=3,
            )
        )

        assert execution.response.status is RunStatus.FAILED
        assert execution.record.child_runs == 0
        assert execution.record.terminal_code == "INVALID_ACTION"
        assert not any(
            event.kind is AgentEventKind.FORK_STARTED for event in execution.record.events
        )

    asyncio.run(scenario())


def test_picker_rejects_model_count_above_request_top_k() -> None:
    async def scenario() -> None:
        service, _embedding, _ = await _service(_OversizedPickerSelector())
        execution = await service.execute(
            SearchRequest(
                query="在 amazon 推荐手机",
                display_currency="CNY",
                top_k=1,
            )
        )

        assert execution.response.status is RunStatus.FAILED
        assert execution.response.search_response is None
        assert execution.record.terminal_code == "INVALID_ACTION"
        picker = next(
            item
            for item in execution.response.tool_summary
            if item.tool_name is ToolName.ITEM_PICKER
        )
        assert picker.safe_outcome == "INVALID_ACTION"

    asyncio.run(scenario())


def test_cancellation_drains_owned_resources_before_propagating_abort() -> None:
    async def scenario() -> None:
        drained = asyncio.Event()

        async def drain() -> None:
            drained.set()

        selector = _BlockingSelector()
        service, _embedding, _ = await _service(selector, drainer=drain)
        task = asyncio.create_task(
            service.execute_run(
                SearchRequest(query="推荐笔记本"),
                run_id="agent-run-cancelled",
            )
        )
        await asyncio.wait_for(selector.started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert drained.is_set()

    asyncio.run(scenario())
