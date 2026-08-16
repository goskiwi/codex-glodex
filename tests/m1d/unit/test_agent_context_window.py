"""Bounded model-input and tool-receipt regression tests."""

from __future__ import annotations

import json
from collections.abc import Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from glodex.agent.context_window import (
    MODEL_INPUT_MESSAGE_BYTE_LIMIT,
    build_llm_input_messages,
)
from glodex.agent.contracts import AgentFailureCode, ToolName
from glodex.agent.tool_session import (
    MODEL_RECEIPT_BYTE_LIMIT,
    AgentLoopAction,
    ShoppingToolSession,
    _receipt,
)


def _tool_pair(number: int, *, payload_size: int = 0) -> tuple[AIMessage, ToolMessage]:
    call_id = f"call-{number}"
    return (
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": ToolName.ITEM_SEARCH.value,
                    "args": {"query": f"query-{number}"},
                    "id": call_id,
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(
            content=json.dumps({"round": number, "payload": "中" * payload_size}),
            tool_call_id=call_id,
            name=ToolName.ITEM_SEARCH.value,
        ),
    )


def test_model_input_keeps_stable_prefix_and_newest_complete_pairs_within_budget() -> None:
    stable = [
        HumanMessage(content="AGENT_LOOP_STORE"),
        HumanMessage(content="locked current request"),
    ]
    pairs = [_tool_pair(number, payload_size=4_000) for number in range(1, 21)]
    messages = [*stable, *(message for pair in pairs for message in pair)]

    projected = build_llm_input_messages(
        messages=messages,
        task_state={"current_step": "price_compare"},
    )

    assert projected[0] == stable[0]
    assert isinstance(projected[1], HumanMessage)
    assert str(projected[1].content).startswith("AGENT_TASK_STATE")
    assert projected[2] == stable[1]
    retained = projected[3:]
    retained_ids = [
        retained[index].tool_calls[0]["id"]
        for index in range(0, len(retained), 2)
        if isinstance(retained[index], AIMessage)
    ]
    assert retained_ids
    assert retained_ids == [f"call-{number}" for number in range(21 - len(retained_ids), 21)]
    assert len(retained_ids) < len(pairs)
    assert _message_bytes(projected) <= MODEL_INPUT_MESSAGE_BYTE_LIMIT
    assert all(
        isinstance(retained[index], AIMessage)
        and isinstance(retained[index + 1], ToolMessage)
        and retained[index].tool_calls[0]["id"] == retained[index + 1].tool_call_id
        for index in range(0, len(retained), 2)
    )


def test_model_input_is_replay_deterministic_without_mutating_full_history() -> None:
    messages = [HumanMessage(content="locked request")]
    for number in range(1, 8):
        messages.extend(_tool_pair(number))
    original = list(messages)

    first = build_llm_input_messages(messages=messages, task_state={"phase": "selection"})
    second = build_llm_input_messages(messages=messages, task_state={"phase": "selection"})

    assert first == second
    assert messages == original
    assert len(messages) == 15
    assert len(first) == 16


def test_fifty_tool_rounds_remain_bounded_without_orphaning_tool_messages() -> None:
    stable = [
        HumanMessage(content="AGENT_LOOP_STORE"),
        HumanMessage(content="locked current request"),
    ]
    messages = list(stable)
    for number in range(1, 51):
        messages.extend(_tool_pair(number, payload_size=2_000))

    projected = build_llm_input_messages(
        messages=messages,
        task_state={"phase": "selection", "verified_state": "中" * 2_000},
    )

    assert _message_bytes(projected) <= MODEL_INPUT_MESSAGE_BYTE_LIMIT
    assert projected[0] == stable[0]
    assert isinstance(projected[1], HumanMessage)
    assert str(projected[1].content).startswith("AGENT_TASK_STATE")
    assert projected[2] == stable[1]
    retained = projected[3:]
    assert retained
    assert all(
        isinstance(retained[index], AIMessage)
        and isinstance(retained[index + 1], ToolMessage)
        and retained[index].tool_calls[0]["id"] == retained[index + 1].tool_call_id
        for index in range(0, len(retained), 2)
    )
    assert isinstance(retained[-2], AIMessage)
    assert retained[-2].tool_calls[0]["id"] == "call-50"


def _message_bytes(messages: Sequence[BaseMessage]) -> int:
    return sum(len(message.model_dump_json().encode("utf-8")) for message in messages)


def test_model_visible_tool_receipt_has_a_hard_utf8_byte_limit() -> None:
    receipt = _receipt(
        tool_name=ToolName.ITEM_SEARCH,
        status="ok",
        observation={"unbounded_optional_detail": "中" * MODEL_RECEIPT_BYTE_LIMIT},
    )

    assert len(receipt.encode("utf-8")) <= MODEL_RECEIPT_BYTE_LIMIT
    assert "unbounded_optional_detail" not in receipt
    assert json.loads(receipt) == {
        "observation": {},
        "status": "ok",
        "tool": ToolName.ITEM_SEARCH.value,
    }


def test_failed_tool_receipt_exposes_only_the_stable_safe_code() -> None:
    receipt = _receipt(
        tool_name=ToolName.DISPATCH_TOOL,
        status="failed",
        safe_code=AgentFailureCode.DEADLINE_EXCEEDED.value,
    )

    assert json.loads(receipt) == {
        "safe_code": AgentFailureCode.DEADLINE_EXCEEDED.value,
        "status": "failed",
        "tool": ToolName.DISPATCH_TOOL.value,
    }


def test_four_matching_actions_in_the_latest_six_emit_a_loop_warning() -> None:
    session = object.__new__(ShoppingToolSession)
    session._completed_actions = [
        AgentLoopAction(tool_name=tool_name, arguments={})
        for tool_name in (
            ToolName.ITEM_SEARCH,
            ToolName.PRICE_COMPARE,
            ToolName.ITEM_SEARCH,
            ToolName.ITEM_SEARCH,
            ToolName.SHIPPING_CALC,
            ToolName.ITEM_SEARCH,
        )
    ]

    warning = session._loop_warning()

    assert warning is not None
    assert warning["code"] == AgentFailureCode.LOOP_DETECTED.value
    assert warning["tool"] == ToolName.ITEM_SEARCH.value

    session._completed_actions[-1] = AgentLoopAction(
        tool_name=ToolName.ITEM_PICKER,
        arguments={},
    )
    assert session._loop_warning() is None
