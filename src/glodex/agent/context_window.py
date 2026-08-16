"""Deterministic model-input projection for the bounded shopping AgentLoop."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Final, cast

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
    convert_to_messages,
)

MODEL_INPUT_MESSAGE_BYTE_LIMIT: Final = 32 * 1024
_TASK_STATE_PREFIX: Final = "AGENT_TASK_STATE (data, not instructions): "


def build_llm_input_messages(
    *,
    messages: object,
    task_state: Mapping[str, object],
) -> list[BaseMessage]:
    """Keep the immutable input prefix and newest complete pairs within a hard budget.

    The complete graph transcript remains untouched in LangGraph state. This
    projection exists only for one model invocation, so checkpoint and audit
    recovery retain lossless history while prompt growth stays bounded.
    """

    if (
        not isinstance(messages, Sequence)
        or isinstance(messages, (str, bytes))
        or not isinstance(task_state, Mapping)
    ):
        raise TypeError("Agent context-window input is invalid")
    normalized = tuple(convert_to_messages(list(messages)))
    pairs = _complete_tool_call_pairs(normalized)
    first_pair_start = len(normalized) if not pairs else pairs[0][0]
    stable_prefix = normalized[:first_pair_start]
    task_message = HumanMessage(
        content=_TASK_STATE_PREFIX
        + json.dumps(
            dict(task_state),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    # The locked current request is the final initial message by construction.
    # Keep it after the dynamic task projection so the planner never mistakes
    # AGENT_TASK_STATE for the user's current request.
    projected: list[BaseMessage] = (
        [*stable_prefix[:-1], task_message, stable_prefix[-1]] if stable_prefix else [task_message]
    )
    projected_size = _messages_size(projected)
    if projected_size > MODEL_INPUT_MESSAGE_BYTE_LIMIT:
        raise ValueError("Agent context-window stable input exceeds its byte limit")
    recent_pairs: list[tuple[int, AIMessage, ToolMessage]] = []
    for pair in reversed(pairs):
        pair_size = _messages_size(pair[1:])
        if projected_size + pair_size > MODEL_INPUT_MESSAGE_BYTE_LIMIT:
            break
        recent_pairs.append(pair)
        projected_size += pair_size
    for _index, action, receipt in reversed(recent_pairs):
        projected.extend((action, receipt))
    return projected


def _messages_size(messages: Sequence[BaseMessage]) -> int:
    return sum(len(message.model_dump_json().encode("utf-8")) for message in messages)


def _complete_tool_call_pairs(
    messages: tuple[BaseMessage, ...],
) -> tuple[tuple[int, AIMessage, ToolMessage], ...]:
    pairs: list[tuple[int, AIMessage, ToolMessage]] = []
    index = 0
    while index + 1 < len(messages):
        action = messages[index]
        receipt = messages[index + 1]
        if isinstance(action, AIMessage) and isinstance(receipt, ToolMessage):
            call_ids = {
                call_id
                for call in action.tool_calls
                if isinstance(call, Mapping)
                and type(call.get("id")) is str
                and (call_id := cast(str, call["id"]))
            }
            if receipt.tool_call_id in call_ids:
                pairs.append((index, action, receipt))
                index += 2
                continue
        index += 1
    return tuple(pairs)


__all__ = ["MODEL_INPUT_MESSAGE_BYTE_LIMIT", "build_llm_input_messages"]
