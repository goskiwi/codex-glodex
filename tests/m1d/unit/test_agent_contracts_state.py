from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest
from pydantic import ValidationError

from glodex.adapters.rule_intent import RuleIntentInterpreter
from glodex.application.agent.contracts import (
    BUSINESS_TOOL_SET,
    FULL_TOOL_SET,
    TERMINAL_TOOLS,
    AgentCapabilities,
    CallToolAction,
    Candidate,
    CandidateAttribute,
    DataMode,
    EmptySelector,
    ForkResult,
    ForkStatus,
    ItemSearchSelector,
    NestedForkReturn,
    Platform,
    ToolName,
    WebSearchOutput,
)
from glodex.application.agent.state import (
    AgentBudgetLedger,
    AgentToolState,
    BudgetExceeded,
    canonical_json_bytes,
)
from glodex.contracts import SearchRequest

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec(
        "GLO-M1D-P0-002",
        "GLO-M1D-NFR-002",
        "GLO-M1D-NFR-004",
        "GLO-M1D-NFR-006",
    ),
]


def _candidate() -> Candidate:
    return Candidate(
        candidate_id="amazon.phone-1",
        item_id="phone-1",
        platform=Platform.AMAZON,
        title="Phone 1",
        price=Decimal("699.00"),
        currency="USD",
        attributes=(CandidateAttribute(name="color", value="black"),),
        source_ref="amazon-phone-1",
        record_ref="amazon-phone-1",
    )


def test_inventory_and_terminal_sets_are_exact_and_dispatch_has_no_business_slot() -> None:
    assert BUSINESS_TOOL_SET == (
        ToolName.PLANNER,
        ToolName.CHAT_FALLBACK,
        ToolName.WEB_SEARCH,
        ToolName.CATEGORY_INSIGHT,
        ToolName.ITEM_SEARCH,
        ToolName.ITEM_PICKER,
        ToolName.PRICE_COMPARE,
        ToolName.SHIPPING_CALC,
        ToolName.SHOPPING_SUMMARY,
    )
    assert (*BUSINESS_TOOL_SET, ToolName.DISPATCH_TOOL) == FULL_TOOL_SET
    assert TERMINAL_TOOLS == (
        ToolName.SHOPPING_SUMMARY,
        ToolName.CHAT_FALLBACK,
    )
    assert len(BUSINESS_TOOL_SET) == 9
    assert len(FULL_TOOL_SET) == 10


def test_action_selector_is_strict_frozen_and_bound_to_its_tool() -> None:
    action = CallToolAction(
        tool_name=ToolName.ITEM_SEARCH,
        selector_args=ItemSearchSelector(platform=Platform.EBAY, top_k=20),
    )

    assert action.selector_args.platform is Platform.EBAY
    with pytest.raises(ValidationError):
        CallToolAction.model_validate(
            {
                "tool_name": ToolName.ITEM_SEARCH,
                "selector_args": {"platform": Platform.EBAY, "top_k": "20"},
            }
        )
    with pytest.raises(ValidationError):
        CallToolAction(
            tool_name=ToolName.ITEM_SEARCH,
            selector_args=EmptySelector(),
        )
    with pytest.raises(ValidationError):
        CallToolAction.model_validate(
            {
                "tool_name": ToolName.ITEM_SEARCH,
                "selector_args": {
                    "platform": Platform.EBAY,
                    "top_k": 20,
                    "query": "forged",
                },
            }
        )
    with pytest.raises(ValidationError):
        action.tool_name = ToolName.PLANNER  # type: ignore[misc]


def test_candidate_projection_rejects_coercion_unsafe_urls_and_oversized_attributes() -> None:
    candidate = _candidate()
    assert candidate.image_url is None

    with pytest.raises(ValidationError):
        Candidate.model_validate(
            {
                **candidate.model_dump(),
                "price": "699.00",
            }
        )
    with pytest.raises(ValidationError):
        Candidate(
            **{
                **candidate.model_dump(),
                "image_url": "http://example.test/image.png",
            }
        )
    with pytest.raises(ValidationError):
        Candidate(
            **{
                **candidate.model_dump(),
                "attributes": tuple(
                    CandidateAttribute(name=f"a-{index}", value="v") for index in range(17)
                ),
            }
        )


def test_canonical_json_is_exact_for_decimal_and_budget_caps_are_one_more() -> None:
    payload = canonical_json_bytes(
        {
            "candidate": _candidate(),
            "amount": Decimal("699.00"),
        }
    )
    assert b'"amount":"699.00"' in payload
    assert payload == canonical_json_bytes(
        {
            "amount": Decimal("699.00"),
            "candidate": _candidate(),
        }
    )

    ledger = AgentBudgetLedger()
    for _ in range(10):
        ledger = ledger.consume_root_model_action()
    assert ledger.root_model_actions == 10
    with pytest.raises(BudgetExceeded):
        ledger.consume_root_model_action()

    ledger = AgentBudgetLedger().add_observation_bytes(8 * 1024)
    assert ledger.observation_bytes == 8 * 1024
    with pytest.raises(BudgetExceeded):
        ledger.add_observation_bytes(8 * 1024 + 1)
    ledger = AgentBudgetLedger()
    for _ in range(4):
        ledger = ledger.add_observation_bytes(8 * 1024)
    with pytest.raises(BudgetExceeded):
        ledger.add_observation_bytes(1)
    with pytest.raises(FrozenInstanceError):
        ledger.root_model_actions = 0  # type: ignore[misc]


def test_state_keeps_required_baseline_and_nested_fork_results_typed() -> None:
    request = SearchRequest(query="推荐预算800美元的笔记本")
    interpreted = asyncio.run(RuleIntentInterpreter().interpret(request))
    grandchild = ForkResult(
        child_id="child-2",
        depth=2,
        goal="retrieve evidence",
        status=ForkStatus.COMPLETED,
        tool_name=ToolName.WEB_SEARCH,
        task_id="task-2",
        safe_code=None,
        typed_result=WebSearchOutput(),
        model_calls=1,
        tool_calls=1,
        return_calls=1,
    )
    nested = NestedForkReturn(results=(grandchild,))
    parent_fork = ForkResult(
        child_id="child-1",
        depth=1,
        goal="delegate evidence",
        status=ForkStatus.COMPLETED,
        tool_name=ToolName.DISPATCH_TOOL,
        task_id="task-1",
        safe_code=None,
        typed_result=nested,
        model_calls=1,
        tool_calls=1,
        return_calls=1,
    )
    state = AgentToolState(
        request=request,
        interpreted_request=interpreted,
        required_baseline=interpreted,
        capabilities=AgentCapabilities(
            data_mode=DataMode.DEMO_SNAPSHOT,
            available_platforms=(Platform.AMAZON,),
        ),
        fork_results=(parent_fork,),
    )

    assert state.required_baseline is interpreted
    assert state.fork_results == (parent_fork,)
    with pytest.raises(ValueError, match="depth-one"):
        ForkResult(
            child_id="child-2",
            depth=2,
            goal="illegal nested dispatch",
            status=ForkStatus.COMPLETED,
            tool_name=ToolName.DISPATCH_TOOL,
            task_id="task-2",
            safe_code=None,
            typed_result=nested,
            model_calls=1,
            tool_calls=1,
            return_calls=1,
        )
