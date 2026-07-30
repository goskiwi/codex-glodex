"""Contract tests for the fixed DeepSeek Agent action selector."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import httpx
import pytest

from glodex.adapters.deepseek_agent import DeepSeekActionSelector
from glodex.adapters.deepseek_http import build_deepseek_transport
from glodex.adapters.deepseek_intent import DeepSeekIntentError
from glodex.application.agent.contracts import (
    FULL_TOOL_SET,
    ActionSelectionInput,
    CallToolAction,
    CategoryInsightSelector,
    ChatFallbackSelector,
    DispatchSelector,
    DispatchTaskSelector,
    EmptySelector,
    EvidenceKind,
    InsightDepth,
    ItemPickerSelector,
    ItemSearchSelector,
    PlannerFallbackReason,
    Platform,
    PriceCompareSelector,
    SafeObservation,
    ToolFailureCode,
    ToolName,
    WebSearchSelector,
)
from glodex.application.agent.ports import ToolPortError
from glodex.contracts import SearchRequest
from glodex.domain.intent import IntentIssueCode

pytestmark = [
    pytest.mark.contract,
    pytest.mark.spec(
        "GLO-M1D-P0-001",
        "GLO-M1D-P0-002",
        "GLO-M1D-P0-003",
        "GLO-M1D-P0-006",
        "GLO-M1D-NFR-004",
        "GLO-M1D-NFR-005",
    ),
]

_MISSING = object()


@dataclass
class _RecordingTransport:
    responses: list[object]
    calls: list[bytes] = field(default_factory=list)

    async def __call__(self, payload: bytes) -> bytes:
        self.calls.append(payload)
        if not self.responses:
            raise AssertionError("unexpected DeepSeek call")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response  # type: ignore[return-value]


def _envelope(
    content: object,
    *,
    index: object = 0,
    finish_reason: object = "stop",
    tool_calls: object = _MISSING,
    function_call: object = _MISSING,
    reasoning_content: object = _MISSING,
) -> bytes:
    message: dict[str, object] = {"content": content}
    if tool_calls is not _MISSING:
        message["tool_calls"] = tool_calls
    if function_call is not _MISSING:
        message["function_call"] = function_call
    if reasoning_content is not _MISSING:
        message["reasoning_content"] = reasoning_content
    return json.dumps(
        {
            "id": "provider-metadata-is-not-business-data",
            "choices": [
                {
                    "index": index,
                    "finish_reason": finish_reason,
                    "message": message,
                }
            ],
            "usage": {"total_tokens": 1},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


def _provider_action(tool_name: object, selector_args: object) -> bytes:
    return _envelope(
        json.dumps(
            {"tool_name": tool_name, "selector_args": selector_args},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def _selection_input(
    *,
    round_number: int = 0,
    phase: str = "initial",
    available_tools: tuple[ToolName, ...] = FULL_TOOL_SET,
    executed_tools: tuple[ToolName, ...] = (),
    platforms: tuple[Platform, ...] = (),
    candidate_count: int = 0,
    top_candidate_ids: tuple[str, ...] = (),
    publication_eligible_ids: tuple[str, ...] = (),
    safe_codes: tuple[str, ...] = (),
) -> ActionSelectionInput:
    return ActionSelectionInput(
        request=SearchRequest(
            query="  推荐手机  ",
            locale="zh-CN",
            display_currency="CNY",
            top_k=3,
            snapshot_version="m1d-demo-v1",
        ),
        observation=SafeObservation(
            round=round_number,
            phase=phase,
            available_tools=available_tools,
            executed_tools=executed_tools,
            platforms=platforms,
            candidate_count=candidate_count,
            top_candidate_ids=top_candidate_ids,
            publication_eligible_ids=publication_eligible_ids,
            safe_codes=safe_codes,
        ),
    )


def _assert_invalid(response: object) -> ToolPortError:
    transport = _RecordingTransport([response])
    selector = DeepSeekActionSelector(transport)

    with pytest.raises(ToolPortError) as raised:
        asyncio.run(selector.select(_selection_input()))

    assert raised.value.code is ToolFailureCode.PROVIDER_RESPONSE_INVALID
    assert str(raised.value) == ToolFailureCode.PROVIDER_RESPONSE_INVALID.value
    assert len(transport.calls) == 1
    return raised.value


def test_fixed_payload_and_real_next_round_observation_allowlist() -> None:
    transport = _RecordingTransport(
        [
            _provider_action("planner", {}),
            _provider_action("item_search", {"platform": "amazon", "top_k": 20}),
        ]
    )
    selector = DeepSeekActionSelector(transport)

    first = asyncio.run(selector.select(_selection_input()))
    second = asyncio.run(
        selector.select(
            _selection_input(
                round_number=1,
                phase="item_search",
                available_tools=(ToolName.CATEGORY_INSIGHT, ToolName.ITEM_SEARCH),
                executed_tools=(ToolName.PLANNER,),
                platforms=(Platform.AMAZON,),
                candidate_count=2,
                top_candidate_ids=("amazon.phone-1", "amazon.phone-2"),
                publication_eligible_ids=("amazon.phone-1",),
                safe_codes=("PLAN_READY",),
            )
        )
    )

    assert first == CallToolAction(
        tool_name=ToolName.PLANNER,
        selector_args=EmptySelector(),
    )
    assert second == CallToolAction(
        tool_name=ToolName.ITEM_SEARCH,
        selector_args=ItemSearchSelector(platform=Platform.AMAZON, top_k=20),
    )
    assert len(transport.calls) == 2

    first_payload = json.loads(transport.calls[0])
    second_payload = json.loads(transport.calls[1])
    for payload in (first_payload, second_payload):
        assert set(payload) == {
            "model",
            "messages",
            "stream",
            "thinking",
            "response_format",
            "temperature",
            "max_tokens",
            "tool_choice",
        }
        assert payload["model"] == "deepseek-v4-flash"
        assert payload["stream"] is False
        assert payload["thinking"] == {"type": "disabled"}
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["temperature"] == 0
        assert payload["max_tokens"] == 1024
        assert payload["tool_choice"] == "none"
        assert "tools" not in payload
        assert "reasoning_effort" not in payload
        assert [message["role"] for message in payload["messages"]] == [
            "system",
            "user",
        ]
        assert "json" in payload["messages"][0]["content"].lower()
        assert "deepseek-v4-flash" not in payload["messages"][1]["content"]

    assert first_payload["messages"][0] == second_payload["messages"][0]
    system_instruction = first_payload["messages"][0]["content"]
    for tool_name in FULL_TOOL_SET:
        assert tool_name.value in system_instruction
    for forbidden_internal_action in (
        "return_fork_result",
        "eligibility",
        "merge",
    ):
        assert forbidden_internal_action not in system_instruction
    first_user = json.loads(first_payload["messages"][1]["content"])
    assert first_user == {
        "query": "推荐手机",
        "locale": "zh-CN",
        "display_currency": "CNY",
    }
    assert "observation" not in first_user
    assert "top_k" not in first_user
    assert "snapshot_version" not in first_user

    second_user = json.loads(second_payload["messages"][1]["content"])
    assert set(second_user) == {
        "query",
        "locale",
        "display_currency",
        "observation",
    }
    assert second_user["observation"] == {
        "round": 1,
        "phase": "item_search",
        "available_tools": ["category_insight", "item_search"],
        "executed_tools": ["planner"],
        "platforms": ["amazon"],
        "candidate_count": 2,
        "top_candidate_ids": ["amazon.phone-1", "amazon.phone-2"],
        "publication_eligible_ids": ["amazon.phone-1"],
        "safe_codes": ["PLAN_READY"],
    }


def test_selector_composes_with_the_existing_bounded_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=_provider_action("planner", {}),
            request=request,
        )

    transport = build_deepseek_transport(http_transport=httpx.MockTransport(handler))
    selector = DeepSeekActionSelector(transport)

    action = asyncio.run(selector.select(_selection_input()))

    assert action.tool_name is ToolName.PLANNER
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert str(requests[0].url) == "https://api.deepseek.com/chat/completions"


@pytest.mark.parametrize(
    ("tool_name", "selector_args", "expected"),
    [
        ("planner", {}, CallToolAction(tool_name=ToolName.PLANNER, selector_args=EmptySelector())),
        (
            "chat_fallback",
            {"reason_code": "NON_SHOPPING"},
            CallToolAction(
                tool_name=ToolName.CHAT_FALLBACK,
                selector_args=ChatFallbackSelector(reason_code=PlannerFallbackReason.NON_SHOPPING),
            ),
        ),
        (
            "web_search",
            {"evidence_kind": "review"},
            CallToolAction(
                tool_name=ToolName.WEB_SEARCH,
                selector_args=WebSearchSelector(evidence_kind=EvidenceKind.REVIEW),
            ),
        ),
        (
            "category_insight",
            {"depth": "deep"},
            CallToolAction(
                tool_name=ToolName.CATEGORY_INSIGHT,
                selector_args=CategoryInsightSelector(depth=InsightDepth.DEEP),
            ),
        ),
        (
            "item_search",
            {"platform": "ebay", "top_k": 50},
            CallToolAction(
                tool_name=ToolName.ITEM_SEARCH,
                selector_args=ItemSearchSelector(platform=Platform.EBAY, top_k=50),
            ),
        ),
        (
            "price_compare",
            {"top_n": 30},
            CallToolAction(
                tool_name=ToolName.PRICE_COMPARE,
                selector_args=PriceCompareSelector(top_n=30),
            ),
        ),
        (
            "shipping_calc",
            {},
            CallToolAction(
                tool_name=ToolName.SHIPPING_CALC,
                selector_args=EmptySelector(),
            ),
        ),
        (
            "item_picker",
            {"max_items": 3},
            CallToolAction(
                tool_name=ToolName.ITEM_PICKER,
                selector_args=ItemPickerSelector(max_items=3),
            ),
        ),
        (
            "shopping_summary",
            {},
            CallToolAction(
                tool_name=ToolName.SHOPPING_SUMMARY,
                selector_args=EmptySelector(),
            ),
        ),
        (
            "dispatch_tool",
            {
                "tasks": [
                    {"task_id": "task-amazon", "demands": "search amazon"},
                    {"task_id": "task-ebay", "demands": "search ebay"},
                ]
            },
            CallToolAction(
                tool_name=ToolName.DISPATCH_TOOL,
                selector_args=DispatchSelector(
                    tasks=(
                        DispatchTaskSelector(
                            task_id="task-amazon",
                            demands="search amazon",
                        ),
                        DispatchTaskSelector(
                            task_id="task-ebay",
                            demands="search ebay",
                        ),
                    )
                ),
            ),
        ),
    ],
)
def test_strict_parser_accepts_each_single_action_shape(
    tool_name: str,
    selector_args: object,
    expected: CallToolAction,
) -> None:
    selector = DeepSeekActionSelector(
        _RecordingTransport([_provider_action(tool_name, selector_args)])
    )

    action = asyncio.run(selector.select(_selection_input()))

    assert action == expected


@pytest.mark.parametrize(
    "content",
    [
        "I am done.",
        json.dumps(
            [
                {"tool_name": "planner", "selector_args": {}},
                {"tool_name": "planner", "selector_args": {}},
            ]
        ),
        json.dumps(
            {
                "actions": [
                    {"tool_name": "planner", "selector_args": {}},
                    {"tool_name": "planner", "selector_args": {}},
                ]
            }
        ),
        json.dumps({"tool_name": "planner"}),
        json.dumps({"tool_name": "planner", "selector_args": {}, "final": "unsafe"}),
        json.dumps({"tool_name": "unknown_tool", "selector_args": {}}),
        json.dumps({"tool_name": "planner", "selector_args": []}),
        json.dumps({"tool_name": "planner", "selector_args": {"extra": True}}),
        json.dumps({"tool_name": "item_search", "selector_args": {"platform": "ebay"}}),
        json.dumps(
            {
                "tool_name": "item_search",
                "selector_args": {"platform": "ebay", "top_k": "20"},
            }
        ),
        json.dumps(
            {
                "tool_name": "item_search",
                "selector_args": {"platform": "ebay", "top_k": True},
            }
        ),
        json.dumps(
            {
                "tool_name": "item_search",
                "selector_args": {"platform": "ebay", "top_k": 0},
            }
        ),
        json.dumps(
            {
                "tool_name": "item_search",
                "selector_args": {"platform": "ebay", "top_k": 51},
            }
        ),
        json.dumps(
            {
                "tool_name": "price_compare",
                "selector_args": {"top_n": 31},
            }
        ),
        json.dumps(
            {
                "tool_name": "item_picker",
                "selector_args": {"max_items": 4},
            }
        ),
        json.dumps(
            {
                "tool_name": "item_search",
                "selector_args": {
                    "platform": "ebay",
                    "top_k": 20,
                    "query": "forged",
                },
            }
        ),
        json.dumps(
            {
                "tool_name": "dispatch_tool",
                "selector_args": {"tasks": []},
            }
        ),
        json.dumps(
            {
                "tool_name": "dispatch_tool",
                "selector_args": {
                    "tasks": [{"task_id": f"task-{index}", "demands": "one"} for index in range(5)]
                },
            }
        ),
        json.dumps(
            {
                "tool_name": "dispatch_tool",
                "selector_args": {
                    "tasks": [
                        {"task_id": "same", "demands": "one"},
                        {"task_id": "same", "demands": "two"},
                    ]
                },
            }
        ),
        json.dumps(
            {
                "tool_name": "dispatch_tool",
                "selector_args": {"tasks": [{"task_id": "bad id", "demands": "one"}]},
            }
        ),
        json.dumps(
            {
                "tool_name": "dispatch_tool",
                "selector_args": {
                    "tasks": [
                        {
                            "task_id": "task-1",
                            "demands": "x" * 257,
                        }
                    ]
                },
            }
        ),
        json.dumps(
            {
                "tool_name": "dispatch_tool",
                "selector_args": {
                    "tasks": [
                        {
                            "task_id": "task-1",
                            "demands": "one",
                            "platform": "amazon",
                        }
                    ]
                },
            }
        ),
        '{"tool_name":"planner","tool_name":"item_search","selector_args":{}}',
        '{"tool_name":"item_search","selector_args":{"platform":"ebay","top_k":NaN}}',
        '{"tool_name":"planner","selector_args":{}}{"tool_name":"planner","selector_args":{}}',
    ],
)
def test_rejects_multiple_natural_unknown_missing_coerced_or_oversized_actions(
    content: str,
) -> None:
    _assert_invalid(_envelope(content))


@pytest.mark.parametrize(
    "response",
    [
        b"not-json",
        b'{"choices":[]}',
        b'{"choices":[{},{}]}',
        _envelope('{"tool_name":"planner","selector_args":{}}', index=True),
        _envelope('{"tool_name":"planner","selector_args":{}}', index=1),
        _envelope(
            '{"tool_name":"planner","selector_args":{}}',
            finish_reason="length",
        ),
        _envelope(""),
        _envelope("   "),
        _envelope(None),
        _envelope(
            '{"tool_name":"planner","selector_args":{}}',
            tool_calls=[{"function": {"name": "planner"}}],
        ),
        _envelope(
            '{"tool_name":"planner","selector_args":{}}',
            tool_calls=None,
        ),
        _envelope(
            '{"tool_name":"planner","selector_args":{}}',
            function_call=None,
        ),
        _envelope(
            '{"tool_name":"planner","selector_args":{}}',
            reasoning_content="hidden chain of thought",
        ),
        b'{"choices":[],"choices":[]}',
        b'{"choices":"not-a-list"}',
        b'{"choices":[1]}',
        b'{"choices":[{"index":0,"finish_reason":"stop","message":{}}]}',
        b"\xff",
        bytearray(b"{}"),
    ],
)
def test_rejects_invalid_finish_envelope_native_tools_reasoning_and_non_bytes(
    response: object,
) -> None:
    _assert_invalid(response)


def test_rejects_one_more_than_the_decoded_body_cap_before_parsing() -> None:
    _assert_invalid(b"x" * 65_537)


def test_accepts_a_valid_envelope_at_the_exact_decoded_body_cap() -> None:
    envelope = json.loads(_provider_action("planner", {}))
    envelope["padding"] = ""
    base = json.dumps(envelope, separators=(",", ":")).encode()
    envelope["padding"] = "x" * (65_536 - len(base))
    response = json.dumps(envelope, separators=(",", ":")).encode()
    assert len(response) == 65_536
    selector = DeepSeekActionSelector(_RecordingTransport([response]))

    action = asyncio.run(selector.select(_selection_input()))

    assert action == CallToolAction(
        tool_name=ToolName.PLANNER,
        selector_args=EmptySelector(),
    )


def test_empty_native_tool_and_reasoning_fields_are_accepted() -> None:
    response = _envelope(
        '{"tool_name":"planner","selector_args":{}}',
        tool_calls=[],
        reasoning_content=None,
    )
    selector = DeepSeekActionSelector(_RecordingTransport([response]))

    action = asyncio.run(selector.select(_selection_input()))

    assert action.tool_name is ToolName.PLANNER


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (RuntimeError("secret provider body"), ToolFailureCode.PROVIDER_UNAVAILABLE),
        (
            DeepSeekIntentError(IntentIssueCode.PROVIDER_UNAVAILABLE),
            ToolFailureCode.PROVIDER_UNAVAILABLE,
        ),
        (
            DeepSeekIntentError(IntentIssueCode.PROVIDER_RESPONSE_INVALID),
            ToolFailureCode.PROVIDER_RESPONSE_INVALID,
        ),
    ],
)
def test_maps_transport_failures_to_stable_secret_free_tool_errors(
    error: BaseException,
    expected: ToolFailureCode,
) -> None:
    selector = DeepSeekActionSelector(_RecordingTransport([error]))

    with pytest.raises(ToolPortError) as raised:
        asyncio.run(selector.select(_selection_input()))

    assert raised.value.code is expected
    assert str(raised.value) == expected.value
    assert "secret" not in str(raised.value)
    assert "推荐手机" not in str(raised.value)


def test_does_not_swallow_task_cancellation() -> None:
    selector = DeepSeekActionSelector(_RecordingTransport([asyncio.CancelledError()]))

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(selector.select(_selection_input()))


def test_constructor_and_select_reject_programming_misuse_before_transport() -> None:
    with pytest.raises(TypeError):
        DeepSeekActionSelector(object())  # type: ignore[arg-type]

    transport = _RecordingTransport([_provider_action("planner", {})])
    selector = DeepSeekActionSelector(transport)
    with pytest.raises(TypeError):
        asyncio.run(selector.select(object()))  # type: ignore[arg-type]
    assert transport.calls == []
