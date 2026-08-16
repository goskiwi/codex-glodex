"""Behavioral evidence for bounded concurrent homogeneous AgentLoop forks."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Never, Self, cast

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from glodex.agent.catalog import CandidateManifest, InMemoryCatalogGateway, ManifestRecord
from glodex.agent.contracts import (
    FULL_TOOL_SET,
    AgentCapabilities,
    AgentEventKind,
    AgentFailureCode,
    Candidate,
    CandidateAttribute,
    CategoryInsightOutput,
    DataMode,
    EmbeddingBatch,
    EmbeddingResult,
    ForkDemand,
    ForkObjectiveCode,
    ForkReasonCode,
    InsightStatus,
    ItemSearchInput,
    ItemSearchRuntimeResult,
    PlannerDecisionInput,
    PlannerIntentKind,
    Platform,
    ToolName,
)
from glodex.agent.graph import (
    ReActAgentService,
    _ForkCoordinator,
    _loop_deadline_seconds,
    _LoopContext,
)
from glodex.agent.state import CHILD_AGENT_DEADLINE_SECONDS, ROOT_AGENT_DEADLINE_SECONDS
from glodex.agent.tool_session import (
    AgentRuntimeConfig,
    AgentSessionCheckpointCodec,
    ChildHandoff,
    ShoppingToolSession,
    ToolSessionError,
    _category_insight_reference,
)
from glodex.application.search_service import SearchService
from glodex.config import GlodexConfig
from glodex.contracts import RunStatus, SearchRequest
from glodex.domain.catalog import OfferIdentity
from glodex.domain.intent import InterpretedRequest
from glodex.retrieval.deterministic_ranker import DeterministicQueryRanker
from glodex.tools.engine import ToolDependencies
from tests.builders import (
    AcceptAllSemanticAssertion,
    VerifiedShoppingSummary,
    build_catalog_batch,
)


class _RunIds:
    def next_run_id(self) -> str:
        return "unused-run-id"


class _ConcurrentForkCoordinator(_ForkCoordinator):
    started: int = 0
    both_started: asyncio.Event

    async def _run_child(self, plan: object) -> ChildHandoff:
        self.started += 1
        if self.started == 2:
            self.both_started.set()
        await asyncio.wait_for(self.both_started.wait(), timeout=0.25)
        loop = plan.loop  # type: ignore[attr-defined]
        return ChildHandoff(child_run_id=loop.run_id, status="COMPLETED")


class _ScriptedForkModel(FakeMessagesListChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any) -> Self:
        del tools, kwargs
        return self


class _Embeddings:
    async def embed(self, request: EmbeddingBatch) -> EmbeddingResult:
        vector = (1.0, *(0.0 for _ in range(1_023)))
        return EmbeddingResult(vectors=tuple(vector for _ in request.texts))


class _ConcurrentItems:
    def __init__(self, *, required_started: int = 2) -> None:
        self.required_started = required_started
        self.started = 0
        self.both_started = asyncio.Event()

    async def search(
        self,
        request: ItemSearchInput,
        *,
        query_vector: tuple[float, ...] | None,
        preference_vector: tuple[float, ...] | None,
    ) -> ItemSearchRuntimeResult:
        assert query_vector is not None
        assert preference_vector is None
        self.started += 1
        if self.started == self.required_started:
            self.both_started.set()
        await asyncio.wait_for(self.both_started.wait(), timeout=0.5)
        candidate_id = f"{request.platform.value}.product-1"
        return ItemSearchRuntimeResult(
            platform=request.platform,
            target_query=request.query,
            retrieval_query=request.query,
            candidates=(
                Candidate(
                    candidate_id=candidate_id,
                    item_id="product-1",
                    platform=request.platform,
                    title="TravelBook 14",
                    price=Decimal("699.00"),
                    currency="USD",
                    attributes=(CandidateAttribute(name="weight", value="1.2kg"),),
                    source_ref="offer-1",
                    record_ref=f"record-{request.platform.value}",
                ),
            ),
            platform_sub_batch=build_catalog_batch(),
            total_recall=1,
            returned_before_semantic_filter=1,
            truncated=False,
        )


def _unused_manifest(_results: object) -> Never:
    raise AssertionError("fork allocation must not build a candidate manifest")


def _unused_search_service(_gateway: object, _intent: object) -> Never:
    raise AssertionError("fork allocation must not build SearchService")


class _CategoryInsights:
    def __init__(self, result: CategoryInsightOutput) -> None:
        self.result = result

    async def retrieve(self, _request: object) -> CategoryInsightOutput:
        return self.result


@dataclass
class _Clock:
    calls: int = 0

    def now_utc(self) -> datetime:
        return datetime(2026, 8, 9, tzinfo=UTC)

    def monotonic_ns(self) -> int:
        self.calls += 1
        return self.calls * 1_000_000


@dataclass(frozen=True)
class _BoundIntent:
    interpreted_request: InterpretedRequest

    async def interpret(self, _request: SearchRequest) -> InterpretedRequest:
        return self.interpreted_request


def _candidate_manifest(
    results: tuple[ItemSearchRuntimeResult, ...],
) -> CandidateManifest:
    return CandidateManifest(
        data_mode=DataMode.SYNTHETIC_INTERVIEW,
        snapshot_version="m0-v1",
        records=tuple(
            ManifestRecord(
                record_ref=candidate.record_ref,
                source_ref=candidate.source_ref,
                item_id=candidate.item_id,
                platform=candidate.platform,
                product_id="product-1",
                offer_identities=(OfferIdentity(provider_id="provider-a", offer_id="offer-1"),),
                provider_ids=("provider-a",),
            )
            for result in results
            for candidate in result.candidates
        ),
    )


def _search_service(
    gateway: InMemoryCatalogGateway,
    interpreted_request: InterpretedRequest,
) -> SearchService:
    return SearchService(
        config=GlodexConfig(
            data_dir=Path("data/snapshots"),
            default_snapshot="m0-v1",
            default_locale="zh-CN",
            default_currency="USD",
            default_top_k=3,
            fingerprint="a" * 64,
        ),
        run_id_provider=_RunIds(),
        clock=_Clock(),
        intent_interpreter=_BoundIntent(interpreted_request),
        catalog_gateway=gateway,
        query_ranker=DeterministicQueryRanker(),
    )


def test_child_loop_has_a_shorter_independent_deadline() -> None:
    assert _loop_deadline_seconds(depth=0) == ROOT_AGENT_DEADLINE_SECONDS == 240
    assert _loop_deadline_seconds(depth=1) == CHILD_AGENT_DEADLINE_SECONDS == 90
    assert _loop_deadline_seconds(depth=2) == CHILD_AGENT_DEADLINE_SECONDS


def test_exact_item_search_repeat_fails_before_a_second_side_effect() -> None:
    async def scenario() -> None:
        items = _ConcurrentItems(required_started=1)
        session = ShoppingToolSession(
            request=SearchRequest(query="在 Amazon 推荐轻薄本"),
            run_id="exact-repeat",
            config=AgentRuntimeConfig(
                capabilities=AgentCapabilities(
                    data_mode=DataMode.SYNTHETIC_INTERVIEW,
                    available_platforms=(Platform.AMAZON,),
                    web_search_enabled=False,
                    embedding_enabled=True,
                ),
                index_version="repeat-index",
                ruleset_version="repeat-rules",
                calculation_date="2026-08-09",
                shipping_rules=(),
                fx_source_batch=build_catalog_batch(),
            ),
            tool_dependencies=ToolDependencies(
                semantic_assertion=AcceptAllSemanticAssertion(),
                shopping_summary=VerifiedShoppingSummary(),
                item_source=items,
            ),
            embedding_port=_Embeddings(),
            candidate_manifest_factory=_unused_manifest,
            search_service_factory=_unused_search_service,
            checkpoint_codec=AgentSessionCheckpointCodec(b"0" * 32),
        )
        await session.open()
        decision = PlannerDecisionInput(
            intent_kind=PlannerIntentKind.SHOPPING,
            search_query="轻薄本",
            requested_platforms=(Platform.AMAZON,),
        )
        session.model_started()
        session.model_finished([{"name": "planner", "args": {}}])
        await session.invoke(ToolName.PLANNER, decision_json=decision.model_dump_json())
        assert session._state is not None
        session._state = replace(
            session._state,
            category_result=CategoryInsightOutput(
                status=InsightStatus.FOUND,
                category="轻薄本",
                components=("轻薄办公本",),
                confidence=Decimal("0.9"),
            ),
        )

        action = {"name": "item_search", "args": {"query": "轻薄本", "platform": "amazon"}}
        session.model_started()
        session.model_finished([action])
        await session.invoke(ToolName.ITEM_SEARCH, query="轻薄本", platform="amazon")
        assert items.started == 1

        session.model_started()
        session.model_finished([action])
        receipt = await session.invoke(
            ToolName.ITEM_SEARCH,
            query="轻薄本",
            platform="amazon",
        )

        assert '"safe_code":"LOOP_DETECTED"' in receipt
        assert session.failure_code == AgentFailureCode.LOOP_DETECTED.value
        assert items.started == 1
        await session.close()

    asyncio.run(scenario())


def test_parallel_dispatch_starts_independent_child_loops_concurrently() -> None:
    async def scenario() -> None:
        request = SearchRequest(query="比较 Amazon 和 eBay 的轻薄本")
        config = AgentRuntimeConfig(
            capabilities=AgentCapabilities(
                data_mode=DataMode.SYNTHETIC_INTERVIEW,
                available_platforms=(Platform.AMAZON, Platform.EBAY),
                web_search_enabled=False,
                embedding_enabled=True,
            ),
            index_version="parallel-test-index",
            ruleset_version="parallel-test-rules",
            calculation_date="2026-08-09",
            shipping_rules=(),
            fx_source_batch=build_catalog_batch(),
        )
        service = ReActAgentService(
            config=config,
            run_id_provider=_RunIds(),
            tool_dependencies=ToolDependencies(
                semantic_assertion=AcceptAllSemanticAssertion(),
                shopping_summary=VerifiedShoppingSummary(),
            ),
            embedding_port=None,
            candidate_manifest_factory=_unused_manifest,
            search_service_factory=_unused_search_service,
            checkpointer_provider=InMemorySaver,
            session_checkpoint_codec=AgentSessionCheckpointCodec(b"0" * 32),
            model_factory=lambda _tools: object(),
        )
        root = _LoopContext(
            run_id="parallel-root",
            thread_id="parallel-thread",
            root_run_id="parallel-root",
        )
        session = service._new_session(
            request=request,
            loop=root,
            observer=None,
            user_context=None,
        )
        await session.open()
        decision = PlannerDecisionInput(
            intent_kind=PlannerIntentKind.SHOPPING,
            search_query="lightweight laptop",
            requested_platforms=(Platform.AMAZON, Platform.EBAY),
        )
        session.model_started()
        session.model_finished([{"name": "planner", "args": {}}])
        await session.invoke(ToolName.PLANNER, decision_json=decision.model_dump_json())
        assert session._state is not None
        session._state = replace(
            session._state,
            category_result=CategoryInsightOutput(
                status=InsightStatus.FOUND,
                category="轻薄本",
                components=("轻薄办公本", "商务笔记本"),
                confidence=Decimal("0.9"),
            ),
        )
        inherited = session.child_handoff(status="FAILED", safe_code="MODEL_INVALID")
        assert inherited.category_result is not None
        assert ToolName.CATEGORY_INSIGHT not in inherited.completed_tools

        coordinator = _ConcurrentForkCoordinator(
            service=service,
            request=request,
            root_loop=root,
            root_session=session,
            runtime_context=None,
            user_context=None,
        )
        coordinator.both_started = asyncio.Event()

        recursive_demand = ForkDemand(
            objective=ForkObjectiveCode.INVESTIGATE,
            reason=ForkReasonCode.DEEP_CHAIN,
            platforms=(Platform.AMAZON,),
            context_refs=(),
            estimated_tool_calls=3,
        )
        child_plan = coordinator._child_plan(
            parent_loop=root,
            demand=recursive_demand,
            context_seed=session.child_context_seed(
                context_refs=(),
                allowed_platforms=(Platform.AMAZON,),
            ),
        )
        child_session = service._new_session(
            request=request,
            loop=child_plan.loop,
            observer=None,
            user_context=None,
        )
        await child_session.open()
        assert child_session.native_tools() == FULL_TOOL_SET
        assert child_plan.loop.task_scope is not None
        assert child_plan.loop.task_scope.allow_nested_fork
        assert child_session._can_offer_dispatch(child_session._require_state())

        grandchild_plan = coordinator._child_plan(
            parent_loop=child_plan.loop,
            demand=recursive_demand,
            context_seed=child_session.child_context_seed(
                context_refs=(),
                allowed_platforms=(Platform.AMAZON,),
            ),
        )
        grandchild_session = service._new_session(
            request=request,
            loop=grandchild_plan.loop,
            observer=None,
            user_context=None,
        )
        await grandchild_session.open()
        assert grandchild_session.native_tools() == FULL_TOOL_SET
        assert grandchild_plan.loop.task_scope is not None
        assert not grandchild_plan.loop.task_scope.allow_nested_fork
        assert not grandchild_session._can_offer_dispatch(grandchild_session._require_state())
        await grandchild_session.close()
        await child_session.close()

        handoffs = await coordinator.dispatch(
            parent_loop=root,
            parent_session=session,
            demands=(
                ForkDemand(
                    objective=ForkObjectiveCode.COMPARE,
                    reason=ForkReasonCode.PARALLEL,
                    platforms=(Platform.AMAZON,),
                    context_refs=(),
                    estimated_tool_calls=2,
                ),
                ForkDemand(
                    objective=ForkObjectiveCode.COMPARE,
                    reason=ForkReasonCode.PARALLEL,
                    platforms=(Platform.EBAY,),
                    context_refs=(),
                    estimated_tool_calls=2,
                ),
            ),
            parallel=True,
        )

        assert coordinator.started == 2
        assert tuple(handoff.status for handoff in handoffs) == ("COMPLETED", "COMPLETED")
        assert handoffs[0].child_run_id != handoffs[1].child_run_id
        await session.close()

    asyncio.run(scenario())


def test_parallel_dispatch_runs_real_homogeneous_child_graphs_and_merges_handoffs() -> None:
    async def scenario() -> None:
        request = SearchRequest(query="比较 Amazon 和 eBay 的轻薄本")
        items = _ConcurrentItems()
        models = iter(
            (
                _ScriptedForkModel(
                    responses=[
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "item_search",
                                    "args": {"query": "轻薄本", "platform": "amazon"},
                                    "id": "amazon-search",
                                }
                            ],
                        ),
                        AIMessage(
                            content="",
                            tool_calls=[
                                {"name": "shopping_summary", "args": {}, "id": "amazon-handoff"}
                            ],
                        ),
                    ]
                ),
                _ScriptedForkModel(
                    responses=[
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "item_search",
                                    "args": {"query": "轻薄本", "platform": "ebay"},
                                    "id": "ebay-search",
                                }
                            ],
                        ),
                        AIMessage(
                            content="",
                            tool_calls=[
                                {"name": "shopping_summary", "args": {}, "id": "ebay-handoff"}
                            ],
                        ),
                    ]
                ),
            )
        )
        seen_schemas: list[tuple[str, ...]] = []

        def model_factory(tools: Any) -> object:
            seen_schemas.append(tuple(tool.name for tool in tools))
            return next(models)

        config = AgentRuntimeConfig(
            capabilities=AgentCapabilities(
                data_mode=DataMode.SYNTHETIC_INTERVIEW,
                available_platforms=(Platform.AMAZON, Platform.EBAY),
                web_search_enabled=False,
                embedding_enabled=True,
            ),
            index_version="real-parallel-index",
            ruleset_version="real-parallel-rules",
            calculation_date="2026-08-09",
            shipping_rules=(),
            fx_source_batch=build_catalog_batch(),
        )
        service = ReActAgentService(
            config=config,
            run_id_provider=_RunIds(),
            tool_dependencies=ToolDependencies(
                semantic_assertion=AcceptAllSemanticAssertion(),
                shopping_summary=VerifiedShoppingSummary(),
                item_source=items,
            ),
            embedding_port=_Embeddings(),
            candidate_manifest_factory=_unused_manifest,
            search_service_factory=_unused_search_service,
            checkpointer_provider=InMemorySaver,
            session_checkpoint_codec=AgentSessionCheckpointCodec(b"0" * 32),
            model_factory=model_factory,
        )
        root = _LoopContext(
            run_id="real-parallel-root",
            thread_id="real-parallel-thread",
            root_run_id="real-parallel-root",
        )
        session = service._new_session(
            request=request,
            loop=root,
            observer=None,
            user_context=None,
        )
        await session.open()
        decision = PlannerDecisionInput(
            intent_kind=PlannerIntentKind.SHOPPING,
            search_query="轻薄本",
            requested_platforms=(Platform.AMAZON, Platform.EBAY),
        )
        session.model_started()
        session.model_finished([{"name": "planner", "args": {}}])
        await session.invoke(ToolName.PLANNER, decision_json=decision.model_dump_json())
        assert session._state is not None
        session._state = replace(
            session._state,
            category_result=CategoryInsightOutput(
                status=InsightStatus.FOUND,
                category="轻薄本",
                components=("轻薄办公本",),
                confidence=Decimal("0.9"),
            ),
        )
        context_ref = session.fork_context()[1][0]
        coordinator = _ForkCoordinator(
            service=service,
            request=request,
            root_loop=root,
            root_session=session,
            runtime_context=None,
            user_context=None,
        )

        handoffs = await coordinator.dispatch(
            parent_loop=root,
            parent_session=session,
            demands=tuple(
                ForkDemand(
                    objective=ForkObjectiveCode.COMPARE,
                    reason=ForkReasonCode.PARALLEL,
                    platforms=(platform,),
                    context_refs=(context_ref,),
                    estimated_tool_calls=2,
                )
                for platform in (Platform.AMAZON, Platform.EBAY)
            ),
            parallel=True,
        )
        session._merge_child_handoffs(handoffs)

        assert items.started == 2
        assert tuple(handoff.status for handoff in handoffs) == ("COMPLETED", "COMPLETED")
        assert handoffs[0].child_run_id != handoffs[1].child_run_id
        assert seen_schemas == [
            tuple(tool.value for tool in FULL_TOOL_SET),
            tuple(tool.value for tool in FULL_TOOL_SET),
        ]
        assert {result.platform for result in session._require_state().item_results} == {
            Platform.AMAZON,
            Platform.EBAY,
        }
        await session.close()

    asyncio.run(scenario())


def test_four_platform_fork_merge_and_terminal_result_complete_end_to_end() -> None:
    async def scenario() -> None:
        request = SearchRequest(
            query="比较 Amazon、Shopee、AliExpress 和 eBay 的轻薄本",
            display_currency="USD",
            top_k=1,
        )
        category_result = CategoryInsightOutput(
            status=InsightStatus.FOUND,
            category="轻薄本",
            components=("轻薄办公本",),
            confidence=Decimal("0.9"),
        )
        category_ref = _category_insight_reference(category_result)
        decision = PlannerDecisionInput(
            intent_kind=PlannerIntentKind.SHOPPING,
            search_query="轻薄本",
            requested_platforms=(
                Platform.AMAZON,
                Platform.SHOPEE,
                Platform.ALIEXPRESS,
                Platform.EBAY,
            ),
        )
        demands = [
            {
                "objective": "COMPARE",
                "reason": "PARALLEL",
                "platforms": [platform.value],
                "context_refs": [category_ref],
                "estimated_tool_calls": 2,
            }
            for platform in (
                Platform.AMAZON,
                Platform.SHOPEE,
                Platform.ALIEXPRESS,
                Platform.EBAY,
            )
        ]
        root_model = _ScriptedForkModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "planner",
                            "args": {"decision": decision.model_dump(mode="json")},
                            "id": "root-plan",
                        }
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "category_insight",
                            "args": {"category": "轻薄本", "depth": "quick"},
                            "id": "root-category",
                        }
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "parallel_dispatch_tool",
                            "args": {"demands": demands},
                            "id": "root-parallel-fork",
                        }
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[{"name": "price_compare", "args": {}, "id": "root-price"}],
                ),
                AIMessage(
                    content="",
                    tool_calls=[{"name": "shipping_calc", "args": {}, "id": "root-shipping"}],
                ),
                AIMessage(
                    content="",
                    tool_calls=[{"name": "item_picker", "args": {}, "id": "root-picker"}],
                ),
                AIMessage(
                    content="",
                    tool_calls=[{"name": "shopping_summary", "args": {}, "id": "root-summary"}],
                ),
            ]
        )

        def child_model(platform: Platform) -> _ScriptedForkModel:
            return _ScriptedForkModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "item_search",
                                "args": {"query": "轻薄本", "platform": platform.value},
                                "id": f"{platform.value}-search",
                            }
                        ],
                    ),
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "shopping_summary",
                                "args": {},
                                "id": f"{platform.value}-handoff",
                            }
                        ],
                    ),
                ]
            )

        models = iter(
            (
                root_model,
                child_model(Platform.AMAZON),
                child_model(Platform.SHOPEE),
                child_model(Platform.ALIEXPRESS),
                child_model(Platform.EBAY),
            )
        )
        seen_schemas: list[tuple[str, ...]] = []

        def model_factory(tools: Any) -> object:
            seen_schemas.append(tuple(tool.name for tool in tools))
            return next(models)

        items = _ConcurrentItems(required_started=4)
        service = ReActAgentService(
            config=AgentRuntimeConfig(
                capabilities=AgentCapabilities(
                    data_mode=DataMode.SYNTHETIC_INTERVIEW,
                    available_platforms=(
                        Platform.AMAZON,
                        Platform.SHOPEE,
                        Platform.ALIEXPRESS,
                        Platform.EBAY,
                    ),
                    web_search_enabled=False,
                    embedding_enabled=True,
                ),
                index_version="root-fork-terminal-index",
                ruleset_version="root-fork-terminal-rules",
                calculation_date="2026-08-09",
                shipping_rules=(),
                fx_source_batch=build_catalog_batch(),
            ),
            run_id_provider=_RunIds(),
            tool_dependencies=ToolDependencies(
                semantic_assertion=AcceptAllSemanticAssertion(),
                shopping_summary=VerifiedShoppingSummary(),
                category_insight=_CategoryInsights(category_result),
                item_source=items,
            ),
            embedding_port=_Embeddings(),
            candidate_manifest_factory=_candidate_manifest,
            search_service_factory=_search_service,
            checkpointer_provider=InMemorySaver,
            session_checkpoint_codec=AgentSessionCheckpointCodec(b"0" * 32),
            model_factory=model_factory,
        )

        execution = await service.execute_run(request, run_id="root-fork-terminal")

        assert execution.response.status is RunStatus.COMPLETED
        assert execution.response.search_response is not None
        assert execution.response.selected_product_ids
        assert items.started == 4
        assert seen_schemas == [
            tuple(tool.value for tool in FULL_TOOL_SET),
            tuple(tool.value for tool in FULL_TOOL_SET),
            tuple(tool.value for tool in FULL_TOOL_SET),
            tuple(tool.value for tool in FULL_TOOL_SET),
            tuple(tool.value for tool in FULL_TOOL_SET),
        ]
        assert (
            sum(event.kind is AgentEventKind.FORK_REQUESTED for event in execution.record.events)
            == 4
        )
        assert (
            sum(event.kind is AgentEventKind.FORK_JOINED for event in execution.record.events) == 4
        )
        assert execution.record.events[-1].kind is AgentEventKind.AGENT_RESULT

    asyncio.run(scenario())


def test_fork_reasons_are_structurally_enforced() -> None:
    coordinator = object.__new__(_ForkCoordinator)

    with pytest.raises(ToolSessionError, match="INVALID_ACTION"):
        coordinator._validate_demands(
            demands=(
                ForkDemand(
                    objective=ForkObjectiveCode.INVESTIGATE,
                    reason=ForkReasonCode.DEEP_CHAIN,
                    platforms=(Platform.AMAZON,),
                    context_refs=(),
                    estimated_tool_calls=2,
                ),
            ),
            planned_platforms=(Platform.AMAZON,),
            visible_refs=(),
            parallel=False,
        )

    with pytest.raises(ToolSessionError, match="INVALID_ACTION"):
        coordinator._validate_demands(
            demands=(
                ForkDemand(
                    objective=ForkObjectiveCode.INVESTIGATE,
                    reason=ForkReasonCode.CONTEXT_ISOLATION,
                    platforms=(Platform.AMAZON,),
                    context_refs=(),
                    estimated_tool_calls=2,
                ),
            ),
            planned_platforms=(Platform.AMAZON,),
            visible_refs=(),
            parallel=False,
        )

    coordinator._validate_demands(
        demands=(
            ForkDemand(
                objective=ForkObjectiveCode.INVESTIGATE,
                reason=ForkReasonCode.DEEP_CHAIN,
                platforms=(Platform.AMAZON,),
                context_refs=(),
                estimated_tool_calls=3,
            ),
        ),
        planned_platforms=(Platform.AMAZON,),
        visible_refs=(),
        parallel=False,
    )


def test_child_graph_recursively_forks_a_grandchild_and_merges_typed_handoffs() -> None:
    async def scenario() -> None:
        request = SearchRequest(query="在 Amazon 深入调查轻薄本")
        items = _ConcurrentItems(required_started=1)
        child_model = _ScriptedForkModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "dispatch_tool",
                            "args": {
                                "demand": {
                                    "objective": "INVESTIGATE",
                                    "reason": "DEEP_CHAIN",
                                    "platforms": ["amazon"],
                                    "context_refs": [],
                                    "estimated_tool_calls": 3,
                                }
                            },
                            "id": "child-dispatch",
                        }
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[{"name": "shopping_summary", "args": {}, "id": "child-handoff"}],
                ),
            ]
        )
        grandchild_model = _ScriptedForkModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "item_search",
                            "args": {"query": "轻薄本", "platform": "amazon"},
                            "id": "grandchild-search",
                        }
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "shopping_summary",
                            "args": {},
                            "id": "grandchild-handoff",
                        }
                    ],
                ),
            ]
        )
        models = iter((child_model, grandchild_model))
        seen_schemas: list[tuple[str, ...]] = []

        def model_factory(tools: Any) -> object:
            seen_schemas.append(tuple(tool.name for tool in tools))
            return next(models)

        config = AgentRuntimeConfig(
            capabilities=AgentCapabilities(
                data_mode=DataMode.SYNTHETIC_INTERVIEW,
                available_platforms=(Platform.AMAZON,),
                web_search_enabled=False,
                embedding_enabled=True,
            ),
            index_version="recursive-index",
            ruleset_version="recursive-rules",
            calculation_date="2026-08-09",
            shipping_rules=(),
            fx_source_batch=build_catalog_batch(),
        )
        service = ReActAgentService(
            config=config,
            run_id_provider=_RunIds(),
            tool_dependencies=ToolDependencies(
                semantic_assertion=AcceptAllSemanticAssertion(),
                shopping_summary=VerifiedShoppingSummary(),
                item_source=items,
            ),
            embedding_port=_Embeddings(),
            candidate_manifest_factory=_unused_manifest,
            search_service_factory=_unused_search_service,
            checkpointer_provider=InMemorySaver,
            session_checkpoint_codec=AgentSessionCheckpointCodec(b"0" * 32),
            model_factory=model_factory,
        )
        root = _LoopContext(
            run_id="recursive-root",
            thread_id="recursive-thread",
            root_run_id="recursive-root",
        )
        session = service._new_session(
            request=request,
            loop=root,
            observer=None,
            user_context=None,
        )
        await session.open()
        decision = PlannerDecisionInput(
            intent_kind=PlannerIntentKind.SHOPPING,
            search_query="轻薄本",
            requested_platforms=(Platform.AMAZON,),
        )
        session.model_started()
        session.model_finished([{"name": "planner", "args": {}}])
        await session.invoke(ToolName.PLANNER, decision_json=decision.model_dump_json())
        assert session._state is not None
        session._state = replace(
            session._state,
            category_result=CategoryInsightOutput(
                status=InsightStatus.FOUND,
                category="轻薄本",
                components=("轻薄办公本",),
                confidence=Decimal("0.9"),
            ),
        )
        context_ref = session.fork_context()[1][0]
        first_child_response = cast(AIMessage, child_model.responses[0])
        first_child_response.tool_calls[0]["args"]["demand"]["context_refs"] = [context_ref]
        coordinator = _ForkCoordinator(
            service=service,
            request=request,
            root_loop=root,
            root_session=session,
            runtime_context=None,
            user_context=None,
        )

        handoffs = await coordinator.dispatch(
            parent_loop=root,
            parent_session=session,
            demands=(
                ForkDemand(
                    objective=ForkObjectiveCode.INVESTIGATE,
                    reason=ForkReasonCode.DEEP_CHAIN,
                    platforms=(Platform.AMAZON,),
                    context_refs=(context_ref,),
                    estimated_tool_calls=3,
                ),
            ),
            parallel=False,
        )
        session._merge_child_handoffs(handoffs)

        assert len(handoffs) == 1
        diagnostic = "\n".join(
            repr((event.kind, event.run_id, event.tool_name, event.safe_code, event.status))
            for event in session._events
            if event.safe_code is not None
            or event.status is not None
            or event.kind is AgentEventKind.TOOL_FINISHED
        )
        assert handoffs[0].status == "COMPLETED", f"{handoffs[0]!r}\n{diagnostic}"
        assert items.started == 1
        assert handoffs[0].item_results[0].platform is Platform.AMAZON
        assert session._require_state().item_results[0].platform is Platform.AMAZON
        assert seen_schemas == [
            tuple(tool.value for tool in FULL_TOOL_SET),
            tuple(tool.value for tool in FULL_TOOL_SET),
        ]
        assert coordinator._child_count == 2
        await session.close()

    asyncio.run(scenario())
