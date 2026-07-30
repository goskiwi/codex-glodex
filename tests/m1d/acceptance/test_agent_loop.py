from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from glodex.adapters.deepseek_agent import DeepSeekActionSelector
from glodex.api.agent_app import create_agent_app
from glodex.application.agent.contracts import (
    FULL_TOOL_SET,
    ToolName,
)
from glodex.contracts import RunStatus, SearchRequest
from tests.m1d.unit.test_agent_runtime import (
    _ChildWorkSelector,
    _Embedding,
    _NestedSelector,
    _OptionalSelector,
    _ReactiveSelector,
    _service,
)

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.spec(
        "GLO-M1D-P0-003",
        "GLO-M1D-P0-005",
        "GLO-M1D-NFR-004",
        "GLO-M1D-NFR-005",
    ),
]


@dataclass
class _PublicRunIds:
    def next_run_id(self) -> str:
        return "agent-run-1"


@dataclass
class _ProviderShapedAgent:
    invalid: bool = False
    include_optional: bool = False
    picker_max_items: int = 3
    requests: list[dict[str, object]] = field(default_factory=list)

    async def __call__(self, payload: bytes) -> bytes:
        request = json.loads(payload)
        assert type(request) is dict
        self.requests.append(request)
        if self.invalid:
            content = "I have finished shopping for you."
        else:
            user = json.loads(request["messages"][1]["content"])
            content = json.dumps(
                self._action(user),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        return json.dumps(
            {
                "id": "provider-response-id",
                "model": "deepseek-v4-flash",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": content,
                        },
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
            separators=(",", ":"),
        ).encode()

    def _action(self, user: dict[str, object]) -> dict[str, object]:
        observation = user.get("observation")
        if observation is None:
            return {"tool_name": "planner", "selector_args": {}}
        assert type(observation) is dict
        available = observation["available_tools"]
        assert type(available) is list
        executed = observation["executed_tools"]
        assert type(executed) is list
        if self.include_optional:
            if "category_insight" in available and "category_insight" not in executed:
                return {
                    "tool_name": "category_insight",
                    "selector_args": {"depth": "quick"},
                }
            if "web_search" in available and "web_search" not in executed:
                return {
                    "tool_name": "web_search",
                    "selector_args": {"evidence_kind": "review"},
                }
        if available == ["chat_fallback"]:
            return {
                "tool_name": "chat_fallback",
                "selector_args": {"reason_code": "NON_SHOPPING"},
            }
        if available == ["item_search"]:
            platforms = observation["platforms"]
            assert type(platforms) is list and len(platforms) == 1
            return {
                "tool_name": "item_search",
                "selector_args": {
                    "platform": platforms[0],
                    "top_k": 20,
                },
            }
        if "price_compare" in available:
            return {
                "tool_name": "price_compare",
                "selector_args": {"top_n": 12},
            }
        if "shipping_calc" in available:
            return {
                "tool_name": "shipping_calc",
                "selector_args": {},
            }
        if "dispatch_tool" in available:
            platforms = observation["platforms"]
            assert type(platforms) is list
            return {
                "tool_name": "dispatch_tool",
                "selector_args": {
                    "tasks": [
                        {
                            "task_id": f"task-{index + 1}",
                            "demands": f"search {platform}",
                        }
                        for index, platform in enumerate(platforms)
                    ]
                },
            }
        if "item_search" in available:
            platforms = observation["platforms"]
            assert type(platforms) is list and len(platforms) == 1
            return {
                "tool_name": "item_search",
                "selector_args": {
                    "platform": platforms[0],
                    "top_k": 20,
                },
            }
        exact_actions = {
            ("price_compare",): {
                "tool_name": "price_compare",
                "selector_args": {"top_n": 12},
            },
            ("shipping_calc",): {
                "tool_name": "shipping_calc",
                "selector_args": {},
            },
            ("item_picker",): {
                "tool_name": "item_picker",
                "selector_args": {"max_items": self.picker_max_items},
            },
            ("shopping_summary",): {
                "tool_name": "shopping_summary",
                "selector_args": {},
            },
        }
        return exact_actions[tuple(available)]


async def _public_run(
    selector: object,
    request: SearchRequest,
    *,
    barrier: bool = False,
) -> tuple[dict[str, Any], str, _Embedding]:
    service, embedding, _ = await _service(selector, barrier=barrier)
    app = create_agent_app(
        service=service,
        run_id_provider=_PublicRunIds(),
    )
    request_payload = request.model_dump(mode="json")
    request_payload["snapshot_version"] = "m1d-demo-v1"

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(
            app=app,
            raise_app_exceptions=False,
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            accepted = await client.post(
                "/api/v1/agent-runs",
                json={
                    "thread_id": "thread-public-acceptance",
                    "request": request_payload,
                },
            )
            assert accepted.status_code == 202
            await app.state.coordinator.wait_idle()
            run_id = accepted.json()["run_id"]
            status = await client.get(f"/api/v1/agent-runs/{run_id}")
            replay = await client.get(
                f"/api/v1/agent-runs/{run_id}/events",
                headers={"Accept": "text/event-stream"},
            )

    assert status.status_code == 200
    assert replay.status_code == 200
    body = status.json()
    response = body["response"]
    assert isinstance(response, dict)
    return response, replay.text, embedding


@pytest.mark.spec(
    "GLO-M1D-P0-002",
    "M1D-AC-002",
    "GLO-M1D-NFR-002",
)
def test_public_agent_api_wires_nine_real_tools_and_dispatch() -> None:
    async def scenario() -> None:
        full_path, full_events, _embedding = await _public_run(
            _OptionalSelector(),
            SearchRequest(
                query="比较 amazon shopee aliexpress ebay 的手机",
                display_currency="CNY",
                top_k=3,
            ),
            barrier=True,
        )
        fallback, fallback_events, _embedding = await _public_run(
            _ReactiveSelector(),
            SearchRequest(query="你好"),
        )

        assert full_path["status"] == RunStatus.COMPLETED.value
        assert fallback["status"] == RunStatus.COMPLETED.value
        successful_tools = {
            ToolName(item["tool_name"])
            for response in (full_path, fallback)
            for item in response["tool_summary"]
            if item["safe_outcome"] == "SUCCESS"
        }
        assert successful_tools == frozenset(FULL_TOOL_SET)
        public_events = full_events + fallback_events
        assert all(tool_name.value in public_events for tool_name in FULL_TOOL_SET)

    asyncio.run(scenario())


@pytest.mark.spec("M1D-AC-003")
def test_provider_shaped_deepseek_drives_one_complete_canonical_run() -> None:
    async def scenario() -> None:
        provider = _ProviderShapedAgent(
            include_optional=True,
            picker_max_items=1,
        )
        response, events, embedding = await _public_run(
            DeepSeekActionSelector(provider),
            SearchRequest(
                query="在 amazon 推荐手机",
                display_currency="CNY",
                top_k=1,
            ),
        )

        assert response["status"] == RunStatus.COMPLETED.value
        assert response["search_response"] is not None
        assert len(provider.requests) == events.count('"type": "MODEL_STARTED"')
        assert all(
            json.loads(request["messages"][1]["content"])["query"] == "在 amazon 推荐手机"
            for request in provider.requests
        )
        assert len(embedding.requests) == 1
        assert tuple(item["tool_name"] for item in response["tool_summary"]) == (
            ToolName.PLANNER.value,
            ToolName.WEB_SEARCH.value,
            ToolName.CATEGORY_INSIGHT.value,
            ToolName.ITEM_SEARCH.value,
            ToolName.ITEM_PICKER.value,
            ToolName.PRICE_COMPARE.value,
            ToolName.SHIPPING_CALC.value,
            ToolName.SHOPPING_SUMMARY.value,
        )
        assert '"type": "AGENT_RESULT"' in events
        event_ids = [
            line.removeprefix("id: ") for line in events.splitlines() if line.startswith("id: ")
        ]
        assert event_ids == [f"agent-run-1:{sequence}" for sequence in range(1, len(event_ids) + 1)]

    asyncio.run(scenario())


@pytest.mark.spec("M1D-AC-005")
def test_provider_shaped_four_platform_dispatch_has_four_typed_children() -> None:
    async def scenario() -> None:
        provider = _ProviderShapedAgent()
        response, events, embedding = await _public_run(
            DeepSeekActionSelector(provider),
            SearchRequest(
                query="比较 amazon shopee aliexpress ebay 的手机",
                display_currency="CNY",
                top_k=3,
            ),
            barrier=True,
        )

        assert response["status"] == RunStatus.COMPLETED.value
        assert len(embedding.requests) == 1
        assert (
            next(
                item["call_count"]
                for item in response["tool_summary"]
                if item["tool_name"] == ToolName.ITEM_SEARCH.value
            )
            == 4
        )
        assert events.count('"type": "FORK_STARTED"') == 4
        assert events.count('"type": "FORK_FINISHED"') == 4

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "child_tool",
    [
        ToolName.WEB_SEARCH,
        ToolName.CATEGORY_INSIGHT,
        ToolName.ITEM_SEARCH,
        ToolName.PRICE_COMPARE,
        ToolName.SHIPPING_CALC,
    ],
    ids=lambda tool: tool.value,
)
def test_public_agent_api_reaches_each_trusted_child_work_tool(
    child_tool: ToolName,
) -> None:
    async def scenario() -> None:
        query = "比较 amazon ebay 的手机"
        response, events, embedding = await _public_run(
            _ChildWorkSelector(child_tool=child_tool),
            SearchRequest(
                query=query,
                display_currency="CNY",
                top_k=2,
            ),
        )

        assert response["status"] == RunStatus.COMPLETED.value
        assert response["search_response"] is not None
        child_summary = next(
            item for item in response["tool_summary"] if item["tool_name"] == child_tool.value
        )
        assert child_summary["safe_outcome"] == "SUCCESS"
        assert '"scope": "child"' in events and f'"toolName": "{child_tool.value}"' in events
        assert "untrusted demand" not in events
        assert len(embedding.requests) == 1
        if child_tool is ToolName.CATEGORY_INSIGHT:
            assert embedding.requests[0].texts == (
                f"phone {query}",
                query,
            )
        else:
            assert embedding.requests[0].texts == (query,)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("query", "platform_count", "expected_children"),
    [
        ("比较 amazon ebay 的手机", 2, 3),
        ("比较 amazon shopee ebay 的手机", 3, 4),
    ],
)
def test_public_agent_api_reaches_depth_two_and_publishes_nested_items(
    query: str,
    platform_count: int,
    expected_children: int,
) -> None:
    async def scenario() -> None:
        response, events, embedding = await _public_run(
            _NestedSelector(platform_count=platform_count),
            SearchRequest(
                query=query,
                display_currency="CNY",
                top_k=2,
            ),
        )

        assert response["status"] == RunStatus.COMPLETED.value
        assert response["search_response"] is not None
        assert response["search_response"]["results"]
        payloads = tuple(
            json.loads(line.removeprefix("data: "))
            for line in events.splitlines()
            if line.startswith("data: ")
        )
        fork_depths = tuple(
            payload["depth"] for payload in payloads if payload["type"] == "FORK_STARTED"
        )
        assert len(fork_depths) == expected_children
        assert fork_depths.count(1) == 1
        assert fork_depths.count(2) == platform_count
        assert (
            next(
                item["call_count"]
                for item in response["tool_summary"]
                if item["tool_name"] == ToolName.DISPATCH_TOOL.value
            )
            == 2
        )
        assert (
            next(
                item["call_count"]
                for item in response["tool_summary"]
                if item["tool_name"] == ToolName.ITEM_SEARCH.value
            )
            == platform_count
        )
        assert len(embedding.requests) == 1

    asyncio.run(scenario())


@pytest.mark.spec("M1D-AC-004", "M1D-AC-006")
def test_provider_shaped_no_match_fallback_and_invalid_model_are_distinct() -> None:
    async def scenario() -> None:
        no_match_provider = _ProviderShapedAgent(picker_max_items=1)
        no_match, no_match_events, _embedding = await _public_run(
            DeepSeekActionSelector(no_match_provider),
            SearchRequest(
                query="在 amazon 推荐预算 1 元的手机",
                display_currency="CNY",
                top_k=1,
            ),
        )
        assert no_match["status"] == RunStatus.NO_MATCH.value
        assert no_match["search_response"] is not None
        assert '"type": "AGENT_RESULT"' in no_match_events

        fallback_provider = _ProviderShapedAgent()
        fallback, fallback_events, _embedding = await _public_run(
            DeepSeekActionSelector(fallback_provider),
            SearchRequest(query="你好"),
        )
        assert fallback["status"] == RunStatus.COMPLETED.value
        assert fallback["search_response"] is None
        assert '"type": "AGENT_RESULT"' in fallback_events

        invalid_provider = _ProviderShapedAgent(invalid=True)
        secret = "m1d-private-secret-sentinel"
        full_query = f"推荐手机 {secret} /private/operator/path prompt-sentinel"
        failed, failed_events, _embedding = await _public_run(
            DeepSeekActionSelector(invalid_provider),
            SearchRequest(query=full_query),
        )
        assert failed["status"] == RunStatus.FAILED.value
        assert failed["answer"] is None
        assert failed["search_response"] is None
        assert '"type": "AGENT_ERROR"' in failed_events
        assert len(invalid_provider.requests) == 1
        public_text = json.dumps(failed, ensure_ascii=False) + failed_events
        for sensitive in (
            secret,
            full_query,
            "/private/operator/path",
            "prompt-sentinel",
        ):
            assert sensitive not in public_text

    asyncio.run(scenario())
