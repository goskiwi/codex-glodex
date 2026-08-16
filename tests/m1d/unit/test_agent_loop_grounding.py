from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any, Never, cast

import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool

import glodex.agent.tool_session as agent_tool_session
from glodex.agent.contracts import (
    FULL_TOOL_SET,
    AgentCapabilities,
    AgentEventKind,
    AgentFailureCode,
    AgentTraceSource,
    CategoryInsightInput,
    CategoryInsightOutput,
    DataMode,
    EmbeddingBatch,
    EmbeddingResult,
    EvidenceKind,
    InsightStatus,
    ItemSearchInput,
    ItemSearchRuntimeResult,
    PlannerBudgetInput,
    PlannerComparisonTargetInput,
    PlannerCriterionInput,
    PlannerDecisionInput,
    PlannerIntentKind,
    Platform,
    ToolFailureCode,
    ToolName,
    WebEvidence,
    WebSearchInput,
    WebSearchOutput,
)
from glodex.agent.graph import (
    _SYSTEM_PROMPT,
    AGENT_LOOP_PHASE_BOUNDARIES,
    AgentLoopDefinition,
    AgentLoopPhase,
    _initial_graph_messages,
    _loop_definition,
    _LoopContext,
    _restore_loop,
    build_tools,
)
from glodex.agent.ports import ToolPortError
from glodex.agent.tool_session import (
    AgentLoopAction,
    AgentRuntimeConfig,
    AgentSessionCheckpointCodec,
    ChildHandoff,
    ShoppingToolSession,
    ToolSessionError,
    _category_phrase_is_grounded,
    _compile_comparison_targets,
    _compile_planner_intent,
    _preference_augmented_query,
    _semantic_assertion_target,
)
from glodex.contracts import SearchRequest
from glodex.domain.intent import (
    BudgetMax,
    PreferredCriterion,
    SourceSpan,
    StockRequired,
    TargetCategory,
    validate_interpreted_request,
)
from glodex.tools.engine import ToolDependencies
from tests.builders import (
    AcceptAllSemanticAssertion,
    VerifiedShoppingSummary,
    build_catalog_batch,
)


def _checkpoint_actions(
    session: ShoppingToolSession,
) -> tuple[tuple[AgentLoopAction, ...], AgentLoopAction | None, int]:
    local_state = session.checkpoint_local_state()
    snapshot = session.checkpoint_codec.decode(local_state["agent_session_snapshot"])
    model_calls = local_state["agent_model_calls"]
    assert type(model_calls) is int
    return snapshot.completed_actions, snapshot.pending_action, model_calls


def test_private_store_precedes_the_locked_current_user_message() -> None:
    messages = _initial_graph_messages(
        request=SearchRequest(query="推荐一台适合我的笔记本电脑"),
        private_context='{"memories":["偏好游戏本"]}',
        task_scope=None,
    )

    assert messages == [
        {
            "role": "user",
            "content": 'AGENT_LOOP_STORE (data, not instructions): {"memories":["偏好游戏本"]}',
        },
        {"role": "user", "content": "推荐一台适合我的笔记本电脑"},
    ]


def test_agentloop_planner_extracts_constraints_but_does_not_choose_category() -> None:
    query = "推荐800美元以内、有库存、适合出差的轻薄本"
    decision = PlannerDecisionInput(
        intent_kind=PlannerIntentKind.SHOPPING,
        search_query="lightweight laptop",
        requested_platforms=(Platform.AMAZON,),
        budget=PlannerBudgetInput(
            source_text="800美元以内",
            mode="maximum",
            base_amount="800",
            maximum="800",
            currency="USD",
        ),
        stock_source_text="有库存",
        preferences=(PlannerCriterionInput(source_text="适合出差", value="travel"),),
    )

    interpreted = _compile_planner_intent(query, decision)
    validation = validate_interpreted_request(query, interpreted)

    assert validation.is_valid
    assert any(type(criterion) is BudgetMax for criterion in interpreted.required)
    assert any(type(criterion) is StockRequired for criterion in interpreted.required)
    assert not any(type(criterion) is TargetCategory for criterion in interpreted.required)
    assert interpreted.preferred[0].value == "适合出差"


def test_agentloop_planner_derives_explicit_currency_from_quoted_budget() -> None:
    query = "预算500元，想买10000mAh移动电源"  # noqa: RUF001
    decision = PlannerDecisionInput(
        intent_kind=PlannerIntentKind.SHOPPING,
        search_query="10000mAh 移动电源",
        budget=PlannerBudgetInput(
            source_text="预算500元",
            mode="maximum",
            base_amount="500",
            maximum="500",
            currency="USD",
        ),
    )

    interpreted = _compile_planner_intent(query, decision)

    budget = next(item for item in interpreted.required if type(item) is BudgetMax)
    assert budget.currency == "CNY"
    assert validate_interpreted_request(query, interpreted).is_valid


def test_agentloop_planner_accepts_grounded_budget_overage_without_inferred_currency() -> None:
    query = "通勤双肩包，可装16寸电脑，预算1500左右，可以超出100-200"  # noqa: RUF001
    decision = PlannerDecisionInput(
        intent_kind=PlannerIntentKind.SHOPPING,
        search_query="commuter laptop backpack",
        budget=PlannerBudgetInput(
            source_text="预算1500左右，可以超出100-200",  # noqa: RUF001
            mode="maximum",
            base_amount="1500",
            allowance_amounts=("100", "200"),
            maximum="1700",
            currency="CNY",
        ),
        preferences=(
            PlannerCriterionInput(source_text="通勤双肩包", value="commuter backpack"),
            PlannerCriterionInput(source_text="可装16寸电脑", value="fits 16-inch laptop"),
        ),
    )

    interpreted = _compile_planner_intent(query, decision)

    assert validate_interpreted_request(query, interpreted).is_valid
    budget = next(item for item in interpreted.required if type(item) is BudgetMax)
    assert budget.target_amount == Decimal("1700")
    assert budget.currency is None


def test_agentloop_planner_preserves_around_budget_and_verbatim_preference() -> None:
    query = "我想要3000左右的手机，游戏性能要好"  # noqa: RUF001
    decision = PlannerDecisionInput(
        intent_kind=PlannerIntentKind.SHOPPING,
        search_query="gaming smartphone",
        budget=PlannerBudgetInput(
            source_text="3000左右",
            mode="around",
            base_amount="3000",
            lower_bound="2700",
            maximum="3300",
        ),
        preferences=(
            PlannerCriterionInput(
                source_text="游戏性能要好",
                value="gaming_performance",
            ),
        ),
    )

    interpreted = _compile_planner_intent(query, decision)

    assert validate_interpreted_request(query, interpreted).is_valid
    budget = next(item for item in interpreted.required if type(item) is BudgetMax)
    assert budget.mode == "around"
    assert budget.target_amount == Decimal("3000")
    assert budget.lower_bound == Decimal("2700")
    assert budget.upper_bound == Decimal("3300")
    assert budget.calculation is None
    assert interpreted.preferred[0].value == "游戏性能要好"


def test_category_scope_cannot_invent_a_product_subtype() -> None:
    query = "我想要10000左右的电脑，游戏性能要好"  # noqa: RUF001

    assert _category_phrase_is_grounded(query, "电脑")
    assert not _category_phrase_is_grounded(query, "gaming desktop computer")
    assert not _category_phrase_is_grounded(query, "游戏电脑")


def test_generated_retrieval_wording_cannot_become_a_semantic_constraint() -> None:
    assert (
        _semantic_assertion_target(
            category="电脑",
            target_query="gaming computer desktop PC",
            comparison_targets=(),
        )
        == "电脑"
    )


def test_exact_comparison_target_remains_the_semantic_constraint() -> None:
    targets = _compile_comparison_targets(
        "比较 Realme 5 Pro 和 Realme 5",
        PlannerDecisionInput(
            intent_kind=PlannerIntentKind.SHOPPING,
            search_query="Realme phones",
            comparison_targets=(
                PlannerComparisonTargetInput(source_text="Realme 5 Pro"),
                PlannerComparisonTargetInput(source_text="Realme 5"),
            ),
        ),
    )

    assert (
        _semantic_assertion_target(
            category="手机",
            target_query="Realme 5 Pro",
            comparison_targets=targets,
        )
        == "Realme 5 Pro"
    )


def test_agentloop_planner_rejects_an_omitted_coordinated_preference() -> None:
    query = "我想要3000左右的手机，游戏性能和续航要好"  # noqa: RUF001
    incomplete = PlannerDecisionInput(
        intent_kind=PlannerIntentKind.SHOPPING,
        search_query="游戏手机",
        preferences=(PlannerCriterionInput(source_text="续航要好", value="续航要好"),),
    )

    with pytest.raises(ValueError, match="omitted a coordinated preference"):
        _compile_planner_intent(query, incomplete)

    complete = incomplete.model_copy(
        update={
            "preferences": (
                PlannerCriterionInput(source_text="游戏性能", value="游戏性能"),
                PlannerCriterionInput(source_text="续航要好", value="续航要好"),
            )
        }
    )

    interpreted = _compile_planner_intent(query, complete)

    assert tuple(item.value for item in interpreted.preferred) == ("游戏性能", "续航要好")


def test_agentloop_prompt_splits_compact_multi_purpose_preferences() -> None:
    assert "适合旅行剪视频" in _SYSTEM_PROMPT
    assert "preferences ``旅行`` and ``剪视频``" in _SYSTEM_PROMPT


def test_item_search_query_keeps_explicit_current_turn_preferences() -> None:
    preferred = (
        PreferredCriterion(
            value="旅行",
            source_span=SourceSpan(start=7, end=9, text="旅行"),
        ),
        PreferredCriterion(
            value="剪视频",
            source_span=SourceSpan(start=9, end=12, text="剪视频"),
        ),
        PreferredCriterion(
            value="轻一点",
            source_span=SourceSpan(start=26, end=29, text="轻一点"),
        ),
    )

    assert _preference_augmented_query("笔记本", preferred) == "笔记本 旅行 剪视频 轻一点"
    assert _preference_augmented_query("旅行笔记本", preferred) == "旅行笔记本 剪视频 轻一点"


def test_agentloop_planner_rejects_maximum_mode_for_around_source() -> None:
    query = "我想要3000左右的手机"
    decision = PlannerDecisionInput(
        intent_kind=PlannerIntentKind.SHOPPING,
        search_query="smartphone",
        budget=PlannerBudgetInput(
            source_text="3000左右",
            mode="maximum",
            base_amount="3000",
            maximum="3000",
        ),
    )

    interpreted = _compile_planner_intent(query, decision)

    assert not validate_interpreted_request(query, interpreted).is_valid


def test_agentloop_planner_accepts_a_separately_stated_absolute_cap() -> None:
    query = "找无线降噪耳机，预算500，最高可以到700"  # noqa: RUF001
    decision = PlannerDecisionInput(
        intent_kind=PlannerIntentKind.SHOPPING,
        search_query="wireless noise cancelling headphones",
        budget=PlannerBudgetInput(
            source_text="预算500，最高可以到700",  # noqa: RUF001
            mode="maximum",
            base_amount="500",
            allowance_amounts=("200",),
            maximum="700",
        ),
    )

    interpreted = _compile_planner_intent(query, decision)

    assert validate_interpreted_request(query, interpreted).is_valid
    budget = next(item for item in interpreted.required if type(item) is BudgetMax)
    assert budget.target_amount == Decimal("700")
    assert budget.calculation is not None
    assert budget.calculation.explicit_maximum == Decimal("700")


def test_agentloop_planner_drops_private_store_preference_not_grounded_in_current_query() -> None:
    decision = PlannerDecisionInput(
        intent_kind=PlannerIntentKind.SHOPPING,
        search_query="laptop",
        preferences=(
            PlannerCriterionInput(
                source_text="我长期偏好高性能、独立显卡",
                value="高性能游戏本",
            ),
            PlannerCriterionInput(source_text="适合我的", value="高性能游戏本"),
        ),
    )

    interpreted = _compile_planner_intent("推荐一台适合我的笔记本电脑", decision)

    assert tuple(item.value for item in interpreted.preferred) == ("适合我的",)


def test_agentloop_planner_grounds_each_explicit_comparison_target() -> None:
    query = "比较 Realme 5 Pro 和 Realme 5, 优先续航"
    decision = PlannerDecisionInput(
        intent_kind=PlannerIntentKind.SHOPPING,
        search_query="Realme 5 Pro Realme 5",
        comparison_targets=(
            PlannerComparisonTargetInput(source_text="Realme 5 Pro"),
            PlannerComparisonTargetInput(source_text="Realme 5"),
        ),
    )

    targets = _compile_comparison_targets(query, decision)

    assert tuple(target.search_query for target in targets) == (
        "Realme 5 Pro",
        "Realme 5",
    )
    assert tuple(target.source_span.text for target in targets) == (
        "Realme 5 Pro",
        "Realme 5",
    )


def test_agentloop_planner_rejects_ungrounded_comparison_target() -> None:
    decision = PlannerDecisionInput(
        intent_kind=PlannerIntentKind.SHOPPING,
        search_query="Realme 5 Pro Realme 5",
        comparison_targets=(PlannerComparisonTargetInput(source_text="Realme 6"),),
    )

    with pytest.raises(ValueError, match="must occur"):
        _compile_comparison_targets("比较 Realme 5 Pro 和 Realme 5", decision)


def test_native_tool_schema_makes_category_grounding_an_observed_loop_action() -> None:
    placeholder = cast(Any, None)
    root_session = cast(
        Any,
        type("RootSession", (), {"native_tools": lambda self: FULL_TOOL_SET})(),
    )
    tools = {
        tool.name: tool
        for tool in build_tools(
            root_session,
            coordinator=placeholder,
            loop=placeholder,
        )
    }
    assert "dispatch_tool" in tools
    assert "parallel_dispatch_tool" in tools
    assert tuple(tools) == tuple(tool_name.value for tool_name in FULL_TOOL_SET)

    planner_schema = tools["planner"].args_schema.model_json_schema()  # type: ignore[union-attr]
    category_schema = tools["category_insight"].args_schema.model_json_schema()  # type: ignore[union-attr]
    item_schema = tools["item_search"].args_schema.model_json_schema()  # type: ignore[union-attr]

    assert set(planner_schema["properties"]) == {"decision"}
    decision_schema = planner_schema["$defs"]["PlannerDecisionInput"]
    assert set(decision_schema["required"]) == set(decision_schema["properties"])
    budget_schema = planner_schema["$defs"]["PlannerBudgetInput"]
    assert set(budget_schema["required"]) == set(budget_schema["properties"])
    assert set(category_schema["properties"]) == {"category", "depth"}
    assert set(item_schema["properties"]) == {"query", "platform"}
    assert "next_tools" not in _SYSTEM_PROMPT
    assert "Search-query terms do not consume preferences" in tools["planner"].description
    assert "Never submit one combined preference" in _SYSTEM_PROMPT
    assert "never synthesize ``A are good``" in _SYSTEM_PROMPT
    assert "budgets are upper bounds, not a checklist" in _SYSTEM_PROMPT
    assert "failed sibling does not by itself justify" in _SYSTEM_PROMPT
    for tag in (
        "<role>",
        "<workflow>",
        "<tool_policy>",
        "<termination>",
        "<output_format>",
        "<constraints>",
    ):
        assert tag in _SYSTEM_PROMPT


def test_agentloop_four_phases_map_to_native_react_boundaries() -> None:
    assert tuple(AGENT_LOOP_PHASE_BOUNDARIES) == (
        AgentLoopPhase.THINK,
        AgentLoopPhase.ACT,
        AgentLoopPhase.OBSERVE,
        AgentLoopPhase.REFLECT,
    )
    assert "model" in AGENT_LOOP_PHASE_BOUNDARIES[AgentLoopPhase.THINK]
    assert "tool" in AGENT_LOOP_PHASE_BOUNDARIES[AgentLoopPhase.ACT]
    assert "receipt" in AGENT_LOOP_PHASE_BOUNDARIES[AgentLoopPhase.OBSERVE]
    assert "model" in AGENT_LOOP_PHASE_BOUNDARIES[AgentLoopPhase.REFLECT]


def test_agentloop_definition_centralizes_the_four_instance_properties() -> None:
    definition = _loop_definition(
        loop=_LoopContext(
            run_id="run-root",
            thread_id="thread-root",
            root_run_id="run-root",
        ),
        checkpoint_namespace="run-root.checkpoint-2",
    )

    assert type(definition) is AgentLoopDefinition
    assert definition.thread_id == "thread-root"
    assert definition.checkpoint_namespace == "run-root.checkpoint-2"
    assert definition.graph_thread_id == "thread-root.run-root.checkpoint-2"
    assert definition.tool_names == FULL_TOOL_SET
    assert definition.system_prompt == _SYSTEM_PROMPT


def test_scoped_child_advertises_the_identical_native_schema() -> None:
    session = cast(
        Any,
        type("ScopedSession", (), {"native_tools": lambda self: FULL_TOOL_SET})(),
    )
    placeholder = cast(Any, None)

    tools = build_tools(session, coordinator=placeholder, loop=placeholder)

    assert tuple(tool.name for tool in tools) == tuple(tool.value for tool in FULL_TOOL_SET)
    dispatch_schema = tools[-2].args_schema.model_json_schema()  # type: ignore[union-attr]
    demand = dispatch_schema["$defs"]["ForkDemandInput"]["properties"]
    assert "estimated_tool_calls" in demand


def test_planner_accepts_native_json_arguments_and_compiles_exact_runtime_types() -> None:
    decision = PlannerDecisionInput.model_validate(
        {
            "intent_kind": "SHOPPING",
            "search_query": "lightweight laptop",
            "requested_platforms": ["amazon", "walmart"],
            "budget": {
                "source_text": "5000元以内",
                "mode": "maximum",
                "base_amount": "5000",
                "maximum": "5000",
                "currency": "CNY",
            },
            "exclusions": [],
            "preferences": [{"source_text": "轻薄", "value": "lightweight"}],
        }
    )

    assert decision.intent_kind is PlannerIntentKind.SHOPPING
    assert decision.requested_platforms == (Platform.AMAZON, Platform.WALMART)
    assert type(decision.requested_platforms) is tuple
    assert type(decision.preferences) is tuple


def test_provider_parallel_calls_are_rejected_without_an_extra_action() -> None:
    async def scenario() -> None:
        session = ShoppingToolSession(
            request=SearchRequest(query="推荐轻薄本"),
            run_id="serialize-provider-tools",
            config=AgentRuntimeConfig(
                capabilities=AgentCapabilities(
                    data_mode=DataMode.SYNTHETIC_INTERVIEW,
                    available_platforms=(Platform.AMAZON,),
                    web_search_enabled=True,
                    embedding_enabled=True,
                ),
                index_version="current-product-v3",
                ruleset_version="current-commerce-v1",
                calculation_date="2026-08-09",
                shipping_rules=(),
                fx_source_batch=build_catalog_batch(),
            ),
            tool_dependencies=ToolDependencies(
                semantic_assertion=AcceptAllSemanticAssertion(),
                shopping_summary=VerifiedShoppingSummary(),
            ),
            embedding_port=None,
            candidate_manifest_factory=cast(Any, lambda _results: None),
            search_service_factory=cast(Any, lambda _gateway, _intent: None),
            checkpoint_codec=AgentSessionCheckpointCodec(b"0" * 32),
        )
        await session.open()
        decision = PlannerDecisionInput(
            intent_kind=PlannerIntentKind.SHOPPING,
            search_query="lightweight laptop",
        )
        session.model_started()
        session.model_finished([{"name": "planner", "args": {}}])
        await session.invoke(ToolName.PLANNER, decision_json=decision.model_dump_json())

        planner_event = session.events[-1]
        assert planner_event.kind is AgentEventKind.TOOL_FINISHED
        trace_facts = [
            (bullet.label, bullet.value, bullet.source) for bullet in planner_event.trace_bullets
        ]
        assert trace_facts == [
            ("检索平台", "amazon", AgentTraceSource.VERIFIED_STATE),
        ]

        session.model_started()
        session.model_finished(
            [
                {"name": "category_insight", "args": {"depth": "deep"}},
                {"name": "web_search", "args": {"evidence_kind": "guide"}},
            ]
        )
        rejected = json.loads(await session.invoke(ToolName.WEB_SEARCH, evidence_kind="guide"))
        actions, pending, model_calls = _checkpoint_actions(session)

        assert rejected["status"] == "failed"
        assert session.failure_code == "INVALID_ACTION"
        assert [action.tool_name for action in actions] == [ToolName.PLANNER]
        assert pending is None
        assert model_calls == 2
        local_state = session.checkpoint_local_state()
        encoded_local_state = json.dumps(local_state, ensure_ascii=False)
        assert set(local_state) == {"agent_model_calls", "agent_session_snapshot"}
        assert "出差" not in encoded_local_state
        assert "出差".encode().hex() not in encoded_local_state
        await session.close()

    asyncio.run(scenario())


def test_dispatch_failure_is_observed_but_exact_retry_is_blocked_before_fork() -> None:
    async def scenario() -> None:
        session = ShoppingToolSession(
            request=SearchRequest(query="推荐轻薄本"),
            run_id="recoverable-dispatch",
            config=AgentRuntimeConfig(
                capabilities=AgentCapabilities(
                    data_mode=DataMode.SYNTHETIC_INTERVIEW,
                    available_platforms=(Platform.AMAZON,),
                    web_search_enabled=False,
                    embedding_enabled=True,
                ),
                index_version="current-product-v3",
                ruleset_version="current-commerce-v1",
                calculation_date="2026-08-09",
                shipping_rules=(),
                fx_source_batch=build_catalog_batch(),
            ),
            tool_dependencies=ToolDependencies(
                semantic_assertion=AcceptAllSemanticAssertion(),
                shopping_summary=VerifiedShoppingSummary(),
            ),
            embedding_port=None,
            candidate_manifest_factory=cast(Any, lambda _results: None),
            search_service_factory=cast(Any, lambda _gateway, _intent: None),
            checkpoint_codec=AgentSessionCheckpointCodec(b"0" * 32),
            dispatch_enabled=True,
        )
        await session.open()
        decision = PlannerDecisionInput(
            intent_kind=PlannerIntentKind.SHOPPING,
            search_query="lightweight laptop",
        )
        session.model_started()
        session.model_finished([{"name": "planner", "args": {}}])
        await session.invoke(ToolName.PLANNER, decision_json=decision.model_dump_json())

        fork_calls = 0

        async def failed_fork() -> tuple[ChildHandoff, ...]:
            nonlocal fork_calls
            fork_calls += 1
            raise ToolSessionError(AgentFailureCode.DEADLINE_EXCEEDED.value)

        dispatch_args = {
            "demand": {
                "objective": "INVESTIGATE",
                "reason": "DEEP_CHAIN",
                "platforms": ["amazon"],
                "context_refs": [],
                "estimated_tool_calls": 3,
            }
        }
        session.model_started()
        session.model_finished([{"name": "dispatch_tool", "args": dispatch_args}])
        receipt = json.loads(
            await session.invoke_dispatch(failed_fork, tool_name=ToolName.DISPATCH_TOOL)
        )

        assert receipt["status"] == "failed"
        assert receipt["safe_code"] == AgentFailureCode.DEADLINE_EXCEEDED.value
        initial_failure_code = session.failure_code
        assert initial_failure_code is None
        actions, pending, model_calls = _checkpoint_actions(session)
        assert [action.tool_name for action in actions] == [
            ToolName.PLANNER,
            ToolName.DISPATCH_TOOL,
        ]
        assert pending is None
        assert model_calls == 2

        session.model_started()
        retry_failure_code = session.failure_code
        assert retry_failure_code is None
        assert session.events[-1].kind is AgentEventKind.MODEL_STARTED
        session.model_streaming()
        session.model_streaming()
        latest_event = session.events[-1]
        assert latest_event.kind is AgentEventKind.MODEL_STREAMING
        assert sum(event.kind is AgentEventKind.MODEL_STREAMING for event in session.events) == 1
        session.model_finished([{"name": "dispatch_tool", "args": dispatch_args}])
        repeated = json.loads(
            await session.invoke_dispatch(failed_fork, tool_name=ToolName.DISPATCH_TOOL)
        )
        assert repeated["status"] == "failed"
        assert repeated["safe_code"] == AgentFailureCode.LOOP_DETECTED.value
        assert session.failure_code == AgentFailureCode.LOOP_DETECTED.value
        assert fork_calls == 1
        await session.close()

    asyncio.run(scenario())


def test_oversized_tool_result_is_recoverable_and_allows_a_narrowed_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        session = ShoppingToolSession(
            request=SearchRequest(query="推荐轻薄本"),
            run_id="recoverable-tool-result",
            config=AgentRuntimeConfig(
                capabilities=AgentCapabilities(
                    data_mode=DataMode.SYNTHETIC_INTERVIEW,
                    available_platforms=(Platform.AMAZON,),
                    web_search_enabled=False,
                    embedding_enabled=True,
                ),
                index_version="current-product-v3",
                ruleset_version="current-commerce-v1",
                calculation_date="2026-08-09",
                shipping_rules=(),
                fx_source_batch=build_catalog_batch(),
            ),
            tool_dependencies=ToolDependencies(
                semantic_assertion=AcceptAllSemanticAssertion(),
                shopping_summary=VerifiedShoppingSummary(),
            ),
            embedding_port=None,
            candidate_manifest_factory=cast(Any, lambda _results: None),
            search_service_factory=cast(Any, lambda _gateway, _intent: None),
            checkpoint_codec=AgentSessionCheckpointCodec(b"0" * 32),
        )
        await session.open()
        decision = PlannerDecisionInput(
            intent_kind=PlannerIntentKind.SHOPPING,
            search_query="lightweight laptop",
        )
        original_execute = agent_tool_session.execute_business_tool

        async def oversized_result(*_args: object, **_kwargs: object) -> Never:
            raise ToolPortError(ToolFailureCode.TOOL_RESULT_TOO_LARGE)

        monkeypatch.setattr(agent_tool_session, "execute_business_tool", oversized_result)
        session.model_started()
        session.model_finished([{"name": "planner", "args": {}}])
        receipt = json.loads(
            await session.invoke(ToolName.PLANNER, decision_json=decision.model_dump_json())
        )

        assert receipt["status"] == "rejected"
        assert receipt["safe_code"] == ToolFailureCode.TOOL_RESULT_TOO_LARGE.value
        assert session.failure_code is None
        actions, pending, model_calls = _checkpoint_actions(session)
        assert [action.tool_name for action in actions] == [ToolName.PLANNER]
        assert pending is None and model_calls == 1

        monkeypatch.setattr(agent_tool_session, "execute_business_tool", original_execute)
        session.model_started()
        session.model_finished([{"name": "planner", "args": {}}])
        retry = json.loads(
            await session.invoke(ToolName.PLANNER, decision_json=decision.model_dump_json())
        )

        assert retry["status"] == "ok"
        assert session.failure_code is None
        assert session._state is not None and session._state.plan is not None
        await session.close()

    asyncio.run(scenario())


def test_rejected_tool_observation_survives_checkpoint_resume() -> None:
    async def scenario() -> None:
        request = SearchRequest(query="推荐轻薄本")
        config = AgentRuntimeConfig(
            capabilities=AgentCapabilities(
                data_mode=DataMode.SYNTHETIC_INTERVIEW,
                available_platforms=(Platform.AMAZON,),
                web_search_enabled=False,
                embedding_enabled=True,
            ),
            index_version="current-product-v3",
            ruleset_version="current-commerce-v1",
            calculation_date="2026-08-09",
            shipping_rules=(),
            fx_source_batch=build_catalog_batch(),
        )
        dependencies = ToolDependencies(
            semantic_assertion=AcceptAllSemanticAssertion(),
            shopping_summary=VerifiedShoppingSummary(),
        )
        codec = AgentSessionCheckpointCodec(b"0" * 32)

        def new_session(run_id: str) -> ShoppingToolSession:
            return ShoppingToolSession(
                request=request,
                run_id=run_id,
                config=config,
                tool_dependencies=dependencies,
                embedding_port=None,
                candidate_manifest_factory=cast(Any, lambda _results: None),
                search_service_factory=cast(Any, lambda _gateway, _intent: None),
                checkpoint_codec=codec,
            )

        session = new_session("rejected-before-resume")
        await session.open()
        session.model_started()
        session.model_finished(
            [
                {
                    "name": "planner",
                    "args": {"decision": {"intent_kind": "SHOPPING"}},
                }
            ]
        )
        rejected = json.loads(await session.invoke(ToolName.PLANNER, decision_json="not-json"))
        local_state = session.checkpoint_local_state()

        assert rejected["status"] == "rejected"
        snapshot = codec.decode(local_state["agent_session_snapshot"])
        assert [action.tool_name for action in snapshot.completed_actions] == [ToolName.PLANNER]
        assert json.loads(snapshot.completed_receipts[0])["status"] == "rejected"
        assert snapshot.tool_executions == 0

        restored = new_session("rejected-before-resume")
        await restored.open()
        placeholder = cast(Any, object())
        messages = await _restore_loop(
            session=restored,
            tools=build_tools(restored, coordinator=placeholder, loop=placeholder),
            initial_messages=[HumanMessage(content=request.query)],
            local_state=local_state,
        )

        assert isinstance(messages[-1], ToolMessage)
        assert json.loads(str(messages[-1].content))["status"] == "rejected"

        decision = PlannerDecisionInput(
            intent_kind=PlannerIntentKind.SHOPPING,
            search_query="lightweight laptop",
        )
        restored.model_started()
        restored.model_finished(
            [
                {
                    "name": "planner",
                    "args": {"decision": decision.model_dump(mode="json")},
                }
            ]
        )
        corrected = json.loads(
            await restored.invoke(ToolName.PLANNER, decision_json=decision.model_dump_json())
        )

        assert corrected["status"] == "ok"
        assert restored.failure_code is None
        await session.close()
        await restored.close()

    asyncio.run(scenario())


def test_low_confidence_category_requires_independent_evidence_before_item_search() -> None:
    class Categories:
        async def retrieve(self, request: CategoryInsightInput) -> CategoryInsightOutput:
            return CategoryInsightOutput(
                status=InsightStatus.FOUND,
                category=request.category,
                components=("轻薄办公本",),
                confidence=Decimal("0.4"),
            )

    class Web:
        async def search(self, request: WebSearchInput) -> WebSearchOutput:
            return WebSearchOutput(
                evidence=(
                    WebEvidence(
                        source_id="web-category-guide",
                        title="Laptop category guide",
                        url_domain="example.test",
                        snippet="Independent category context.",
                        source_type=request.evidence_kind,
                    ),
                )
            )

    async def scenario() -> None:
        session = ShoppingToolSession(
            request=SearchRequest(query="推荐轻薄本"),
            run_id="low-confidence-category",
            config=AgentRuntimeConfig(
                capabilities=AgentCapabilities(
                    data_mode=DataMode.SYNTHETIC_INTERVIEW,
                    available_platforms=(Platform.AMAZON,),
                    web_search_enabled=True,
                    embedding_enabled=True,
                ),
                index_version="current-product-v3",
                ruleset_version="current-commerce-v1",
                calculation_date="2026-08-12",
                shipping_rules=(),
                fx_source_batch=build_catalog_batch(),
            ),
            tool_dependencies=ToolDependencies(
                semantic_assertion=AcceptAllSemanticAssertion(),
                shopping_summary=VerifiedShoppingSummary(),
                category_insight=Categories(),
                web_search=Web(),
            ),
            embedding_port=None,
            candidate_manifest_factory=cast(Any, lambda _results: None),
            search_service_factory=cast(Any, lambda _gateway, _intent: None),
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
        session.model_started()
        session.model_finished(
            [{"name": "category_insight", "args": {"category": "轻薄本", "depth": "quick"}}]
        )
        await session.invoke(ToolName.CATEGORY_INSIGHT, category="轻薄本", depth="quick")

        arguments = {"platform": "amazon", "query": "轻薄本"}
        assert session._unmet_preconditions(ToolName.ITEM_SEARCH, arguments) == (
            "independent_category_evidence",
        )

        session.model_started()
        session.model_finished([{"name": "web_search", "args": {"evidence_kind": "guide"}}])
        await session.invoke(ToolName.WEB_SEARCH, evidence_kind=EvidenceKind.GUIDE.value)
        assert session._unmet_preconditions(ToolName.ITEM_SEARCH, arguments) == ()
        await session.close()

    asyncio.run(scenario())


def test_no_insight_uses_web_cold_start_before_item_search() -> None:
    vector = (1.0, *(0.0 for _ in range(1_023)))
    observed_searches: list[ItemSearchInput] = []

    class Categories:
        async def retrieve(self, request: CategoryInsightInput) -> CategoryInsightOutput:
            return CategoryInsightOutput(
                status=InsightStatus.NO_INSIGHT,
                category=request.category,
                confidence=Decimal("0"),
            )

    class Web:
        async def search(self, request: WebSearchInput) -> WebSearchOutput:
            return WebSearchOutput(
                evidence=(
                    WebEvidence(
                        source_id="web-cold-start-guide",
                        title="Travel storage guide",
                        url_domain="example.test",
                        snippet="Run-local evidence for an uncatalogued category.",
                        source_type=request.evidence_kind,
                    ),
                )
            )

    class Embeddings:
        async def embed(self, request: EmbeddingBatch) -> EmbeddingResult:
            return EmbeddingResult(vectors=tuple(vector for _ in request.texts))

    class Items:
        async def search(
            self,
            request: ItemSearchInput,
            *,
            query_vector: tuple[float, ...] | None,
            preference_vector: tuple[float, ...] | None,
        ) -> ItemSearchRuntimeResult:
            assert query_vector == vector
            assert preference_vector is None
            observed_searches.append(request)
            return ItemSearchRuntimeResult(
                platform=request.platform,
                target_query=request.query,
                retrieval_query=request.query,
                candidates=(),
                platform_sub_batch=build_catalog_batch(),
                total_recall=0,
                returned_before_semantic_filter=0,
                truncated=False,
            )

    async def scenario() -> None:
        session = ShoppingToolSession(
            request=SearchRequest(query="推荐旅行收纳用品"),
            run_id="category-cold-start",
            config=AgentRuntimeConfig(
                capabilities=AgentCapabilities(
                    data_mode=DataMode.SYNTHETIC_INTERVIEW,
                    available_platforms=(Platform.AMAZON,),
                    web_search_enabled=True,
                    embedding_enabled=True,
                ),
                index_version="current-product-v3",
                ruleset_version="current-commerce-v1",
                calculation_date="2026-08-12",
                shipping_rules=(),
                fx_source_batch=build_catalog_batch(),
            ),
            tool_dependencies=ToolDependencies(
                semantic_assertion=AcceptAllSemanticAssertion(),
                shopping_summary=VerifiedShoppingSummary(),
                category_insight=Categories(),
                web_search=Web(),
                item_source=Items(),
            ),
            embedding_port=Embeddings(),
            candidate_manifest_factory=cast(Any, lambda _results: None),
            search_service_factory=cast(Any, lambda _gateway, _intent: None),
            checkpoint_codec=AgentSessionCheckpointCodec(b"0" * 32),
        )
        await session.open()
        decision = PlannerDecisionInput(
            intent_kind=PlannerIntentKind.SHOPPING,
            search_query="旅行收纳用品",
            requested_platforms=(Platform.AMAZON,),
        )
        session.model_started()
        session.model_finished([{"name": "planner", "args": {}}])
        await session.invoke(ToolName.PLANNER, decision_json=decision.model_dump_json())
        session.model_started()
        session.model_finished(
            [{"name": "category_insight", "args": {"category": "旅行收纳用品", "depth": "quick"}}]
        )
        await session.invoke(
            ToolName.CATEGORY_INSIGHT,
            category="旅行收纳用品",
            depth="quick",
        )

        item_arguments = {"platform": "amazon", "query": "旅行收纳用品"}
        assert session._unmet_preconditions(ToolName.ITEM_SEARCH, item_arguments) == (
            "cold_start_evidence",
        )
        assert (
            session._unmet_preconditions(
                ToolName.WEB_SEARCH,
                {"evidence_kind": "guide"},
            )
            == ()
        )

        session.model_started()
        session.model_finished([{"name": "web_search", "args": {"evidence_kind": "guide"}}])
        await session.invoke(ToolName.WEB_SEARCH, evidence_kind="guide")

        assert session._unmet_preconditions(ToolName.ITEM_SEARCH, item_arguments) == ()
        assert session._unmet_preconditions(ToolName.CHAT_FALLBACK, {}) == ("fallback_condition",)

        session.model_started()
        session.model_finished(
            [{"name": "item_search", "args": {"platform": "amazon", "query": "旅行收纳用品"}}]
        )
        receipt = json.loads(
            await session.invoke(
                ToolName.ITEM_SEARCH,
                platform="amazon",
                query="旅行收纳用品",
            )
        )

        assert receipt["status"] == "ok"
        assert len(observed_searches) == 1
        assert observed_searches[0].category == "旅行收纳用品"
        await session.close()

    asyncio.run(scenario())


def test_provider_duplicate_tool_calls_are_rejected() -> None:
    async def scenario() -> None:
        session = ShoppingToolSession(
            request=SearchRequest(query="比较 Realme 5 Pro 和 Realme 5"),
            run_id="serialize-duplicate-tools",
            config=AgentRuntimeConfig(
                capabilities=AgentCapabilities(
                    data_mode=DataMode.SYNTHETIC_INTERVIEW,
                    available_platforms=(Platform.AMAZON,),
                    web_search_enabled=False,
                    embedding_enabled=True,
                ),
                index_version="current-product-v3",
                ruleset_version="current-commerce-v1",
                calculation_date="2026-08-09",
                shipping_rules=(),
                fx_source_batch=build_catalog_batch(),
            ),
            tool_dependencies=ToolDependencies(
                semantic_assertion=AcceptAllSemanticAssertion(),
                shopping_summary=VerifiedShoppingSummary(),
            ),
            embedding_port=None,
            candidate_manifest_factory=cast(Any, lambda _results: None),
            search_service_factory=cast(Any, lambda _gateway, _intent: None),
            checkpoint_codec=AgentSessionCheckpointCodec(b"0" * 32),
        )
        await session.open()
        decision = PlannerDecisionInput(
            intent_kind=PlannerIntentKind.SHOPPING,
            search_query="Realme 5 Pro Realme 5",
            comparison_targets=(
                PlannerComparisonTargetInput(source_text="Realme 5 Pro"),
                PlannerComparisonTargetInput(source_text="Realme 5"),
            ),
        )
        session.model_started()
        session.model_finished(
            [
                {"name": "planner", "args": {}},
                {"name": "planner", "args": {}},
                {"name": "planner", "args": {}},
            ]
        )

        rejected = json.loads(
            await session.invoke(ToolName.PLANNER, decision_json=decision.model_dump_json())
        )

        assert rejected["status"] == "failed"
        assert session.failure_code == "INVALID_ACTION"
        actions, pending, model_calls = _checkpoint_actions(session)
        assert actions == ()
        assert pending is None
        assert model_calls == 1
        await session.close()

    asyncio.run(scenario())


def test_item_search_rejects_a_card_id_and_keeps_the_loop_available_to_reflect() -> None:
    vector = (1.0, *(0.0 for _ in range(1_023)))
    observed_searches: list[ItemSearchInput] = []

    class Embeddings:
        async def embed(self, request: EmbeddingBatch) -> EmbeddingResult:
            return EmbeddingResult(vectors=tuple(vector for _ in request.texts))

    class Categories:
        async def retrieve(self, request: CategoryInsightInput) -> CategoryInsightOutput:
            del request
            return CategoryInsightOutput(
                status=InsightStatus.FOUND,
                category="canonical-laptop",
                components=("轻薄办公本", "商务笔记本"),
                confidence=Decimal("0.91"),
            )

    class Items:
        async def search(
            self,
            request: ItemSearchInput,
            *,
            query_vector: tuple[float, ...] | None,
            preference_vector: tuple[float, ...] | None,
        ) -> ItemSearchRuntimeResult:
            assert query_vector == vector
            assert preference_vector is None
            observed_searches.append(request)
            return ItemSearchRuntimeResult(
                platform=request.platform,
                target_query=request.query,
                retrieval_query=request.query,
                candidates=(),
                platform_sub_batch=build_catalog_batch(),
                total_recall=0,
                returned_before_semantic_filter=0,
                truncated=False,
            )

    def unused_manifest(_results: object) -> Never:
        raise AssertionError("candidate manifest must not be built in this test")

    def unused_search_service(_gateway: object, _intent: object) -> Never:
        raise AssertionError("search service must not be built in this test")

    async def scenario() -> None:
        session = ShoppingToolSession(
            request=SearchRequest(query="推荐出差用轻薄本"),
            run_id="agent-loop-grounding",
            config=AgentRuntimeConfig(
                capabilities=AgentCapabilities(
                    data_mode=DataMode.SYNTHETIC_INTERVIEW,
                    available_platforms=(Platform.AMAZON,),
                    web_search_enabled=False,
                    embedding_enabled=True,
                ),
                index_version="current-product-v3",
                ruleset_version="current-commerce-v1",
                calculation_date="2026-08-09",
                shipping_rules=(),
                fx_source_batch=build_catalog_batch(),
            ),
            tool_dependencies=ToolDependencies(
                semantic_assertion=AcceptAllSemanticAssertion(),
                shopping_summary=VerifiedShoppingSummary(),
                category_insight=Categories(),
                item_source=Items(),
            ),
            embedding_port=Embeddings(),
            candidate_manifest_factory=unused_manifest,
            search_service_factory=unused_search_service,
            checkpoint_codec=AgentSessionCheckpointCodec(b"0" * 32),
            dispatch_enabled=True,
        )
        await session.open()
        decision = PlannerDecisionInput(
            intent_kind=PlannerIntentKind.SHOPPING,
            search_query="lightweight laptop",
        )

        session.model_started()
        session.model_finished([{"name": "planner", "args": {}}])
        await session.invoke(ToolName.PLANNER, decision_json=decision.model_dump_json())

        session.model_started()
        session.model_finished(
            [{"name": "category_insight", "args": {"category": "轻薄本", "depth": "quick"}}]
        )
        await session.invoke(ToolName.CATEGORY_INSIGHT, category="轻薄本", depth="quick")
        assert session._state is not None
        assert session._state.category_result is not None
        assert session._state.category_result.category == "轻薄本"

        actions, pending, model_calls = _checkpoint_actions(session)
        assert [action.tool_name for action in actions] == [
            ToolName.PLANNER,
            ToolName.CATEGORY_INSIGHT,
        ]
        assert pending is None
        assert model_calls == 2

        # The model-facing native schema is stable and complete. Runtime
        # guards validate one requested side effect at a time and never publish
        # a state-derived queue of allowed next tools.
        assert session.native_tools() == FULL_TOOL_SET
        assert not hasattr(session, "next_tools")

        session.model_started()
        session.model_finished([{"name": "price_compare", "args": {}}])
        premature_price = json.loads(await session.invoke(ToolName.PRICE_COMPARE))

        assert premature_price["status"] == "rejected"
        assert premature_price["observation"]["reason"] == "PRECONDITION_NOT_MET"
        assert premature_price["observation"]["unmet_facts"] == ["trusted_candidates"]
        assert "next_tools" not in premature_price
        assert session.failure_code is None

        async def completed_child() -> tuple[ChildHandoff, ...]:
            return (
                ChildHandoff(
                    child_run_id="child-failed-platform",
                    status="FAILED",
                    safe_code="ITEM_SOURCE_INVALID",
                ),
                ChildHandoff(
                    child_run_id="child-grounding",
                    status="COMPLETED",
                    completed_tools=(ToolName.ITEM_SEARCH, ToolName.SHOPPING_SUMMARY),
                ),
            )

        dispatch_arguments = {
            "objective": "INVESTIGATE",
            "reason": "DEEP_CHAIN",
            "platforms": ["amazon"],
            "context_refs": [],
            "estimated_tool_calls": 3,
        }
        session.model_started()
        session.model_finished([{"name": "dispatch_tool", "args": {"demand": dispatch_arguments}}])
        await session.invoke_dispatch(completed_child, tool_name=ToolName.DISPATCH_TOOL)
        actions, pending, _model_calls = _checkpoint_actions(session)
        assert actions[-1].tool_name is ToolName.DISPATCH_TOOL
        assert pending is None
        item_search_summary = next(
            item for item in session._tool_summary() if item.tool_name is ToolName.ITEM_SEARCH
        )
        assert item_search_summary.call_count == 1

        session.model_started()
        session.model_finished(
            [
                {
                    "name": "item_search",
                    "args": {
                        "platform": "amazon",
                        "query": "tablet",
                        "category_card_id": "electronics.tablet",
                    },
                }
            ]
        )
        rejected = json.loads(
            await session.invoke(
                ToolName.ITEM_SEARCH,
                platform="amazon",
                query="tablet",
                category_card_id="electronics.tablet",
            )
        )

        assert rejected["status"] == "rejected"
        assert rejected["observation"]["unmet_facts"] == ["native_tool_schema"]
        assert "next_tools" not in rejected
        assert session.failure_code is None
        assert session._tool_counts[ToolName.ITEM_SEARCH] == 1

        session.model_started()
        session.model_finished(
            [
                {
                    "name": "item_search",
                    "args": {"query": "laptop backpack", "platform": "amazon"},
                }
            ]
        )
        receipt = json.loads(
            await session.invoke(
                ToolName.ITEM_SEARCH,
                query="laptop backpack",
                platform="amazon",
            )
        )

        assert receipt["status"] == "ok"
        assert observed_searches[0].query == "laptop backpack"
        assert observed_searches[0].top_k == 3
        assert session._state is not None
        assert session._state.item_results[0].target_query == "轻薄本"
        assert session._state.item_results[0].retrieval_query == "laptop backpack"

        session.model_started()
        session.model_finished(
            [
                {
                    "name": "item_search",
                    "args": {"query": "laptop backpack", "platform": "amazon"},
                }
            ]
        )
        repeated = json.loads(
            await session.invoke(
                ToolName.ITEM_SEARCH,
                query="laptop backpack",
                platform="amazon",
            )
        )
        assert repeated["status"] == "failed"
        assert repeated["safe_code"] == AgentFailureCode.LOOP_DETECTED.value
        assert len(observed_searches) == 1

    asyncio.run(scenario())


def test_agentloop_resume_executes_a_confirmed_pending_action_before_thinking_again() -> None:
    @tool("planner")
    async def planner(decision: dict[str, object]) -> str:
        """Replay a persisted model-selected planning action."""

        assert decision == {"intent_kind": "SHOPPING"}
        return '{"status":"ok"}'

    class RestoredSession:
        def __init__(self) -> None:
            self.observer: object | None = object()
            self.failed = False
            self.model_starts = 0
            self.tool_calls: list[object] = []
            self.restored_model_calls: int | None = None

        def model_started(self) -> None:
            self.model_starts += 1

        def model_finished(self, calls: object) -> None:
            self.tool_calls.append(calls)

        def restore_model_call_count(self, value: int) -> None:
            self.restored_model_calls = value

        def restore_checkpoint_snapshot(
            self,
            value: object,
        ) -> tuple[tuple[AgentLoopAction, ...], AgentLoopAction | None, tuple[str, ...]]:
            assert value == {"schema": "test", "pages": [["state"]]}
            return (
                (),
                AgentLoopAction(
                    tool_name=ToolName.PLANNER,
                    arguments={"decision": {"intent_kind": "SHOPPING"}},
                ),
                (),
            )

    local_state = {
        "agent_model_calls": 1,
        "agent_session_snapshot": {"schema": "test", "pages": [["state"]]},
    }

    async def scenario() -> None:
        session = RestoredSession()
        messages = await _restore_loop(
            session=cast(Any, session),
            tools=(planner,),
            initial_messages=[HumanMessage(content="推荐轻薄本")],
            local_state=local_state,
        )

        assert session.model_starts == 1
        assert session.restored_model_calls == 1
        assert len(session.tool_calls) == 1
        assert isinstance(messages[-1], ToolMessage)
        assert messages[-1].content == '{"status":"ok"}'

    asyncio.run(scenario())


def test_agentloop_resume_does_not_replay_a_completed_tool_action() -> None:
    @tool("item_search")
    async def item_search(query: str, platform: str) -> str:
        """A completed remote action must never be invoked during recovery."""

        del query, platform
        raise AssertionError("completed item_search was replayed")

    class RestoredSession:
        observer: object | None = object()
        failed = False
        restored_model_calls: int | None = None

        def restore_checkpoint_snapshot(
            self,
            value: object,
        ) -> tuple[tuple[AgentLoopAction, ...], AgentLoopAction | None, tuple[str, ...]]:
            assert value == {"schema": "test", "pages": [["state"]]}
            return (
                (
                    AgentLoopAction(
                        tool_name=ToolName.ITEM_SEARCH,
                        arguments={"query": "lightweight laptop", "platform": "amazon"},
                    ),
                ),
                None,
                ('{"status":"ok","tool":"item_search"}',),
            )

        def restore_model_call_count(self, value: int) -> None:
            self.restored_model_calls = value

    local_state = {
        "agent_model_calls": 1,
        "agent_session_snapshot": {"schema": "test", "pages": [["state"]]},
    }

    async def scenario() -> None:
        session = RestoredSession()
        messages = await _restore_loop(
            session=cast(Any, session),
            tools=(item_search,),
            initial_messages=[HumanMessage(content="推荐轻薄本")],
            local_state=local_state,
        )
        assert session.restored_model_calls == 1
        assert isinstance(messages[-1], ToolMessage)
        assert messages[-1].content == '{"status":"ok","tool":"item_search"}'

    asyncio.run(scenario())
