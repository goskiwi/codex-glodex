"""Offline unit evidence for the frozen WebConsole AG-UI projection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from glodex.agent.contracts import (
    AgentAnswer,
    AgentAnswerKind,
    AgentDemoResponse,
    AgentEventScope,
    AgentTraceBullet,
    AgentTraceSource,
    Platform,
    ToolName,
)
from glodex.api.agent_events import (
    AgentResultEvent,
    AgentStartedEvent,
    ChildCheckpointConfirmedEvent,
    ChildFailedEvent,
    ChildHandoffReadyEvent,
    ChildRunStartedEvent,
    ForkJoinedEvent,
    ForkRequestedEvent,
    ModelFinishedEvent,
    ModelStartedEvent,
    ModelStreamingEvent,
    ToolFinishedEvent,
    ToolStartedEvent,
)
from glodex.api.agui import (
    _safe_assistant_deltas,
    initial_state,
    parse_durable_public_event,
    project_event,
    project_payload,
)
from glodex.api.console_contracts import (
    _EVIDENCE_FIELD_LABELS,
    AgUiCustom,
    AgUiRunInput,
    WebConsoleForkState,
    WebConsoleRelayCode,
    WebConsoleTerminalView,
    WebConsoleToolSummaryView,
    _category_label,
    _evidence_source_label,
    _humanize_known_detail,
)
from glodex.contracts import (
    Diagnostics,
    EvidenceSummary,
    FilterSummary,
    InterpretedCriterionSummary,
    InterpretedRequestSummary,
    MoneySummary,
    OfferSummary,
    RunStatus,
    SearchResponse,
    SearchResult,
    SourceSpanSummary,
)
from glodex.runtime.contracts import LoopKind

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec(
        "GLO-WEB_CONSOLE-P0-001",
        "GLO-WEB_CONSOLE-P0-002",
        "GLO-WEB_CONSOLE-P0-005",
        "GLO-WEB_CONSOLE-NFR-002",
        "GLO-WEB_CONSOLE-NFR-003",
    ),
]


@pytest.mark.parametrize(
    ("provider_id", "expected"),
    [
        ("manufacturer-spec-lenovo", "厂商官方规格"),
        ("manufacturer-price-apple", "厂商官方标价"),
        ("public-product-spec-hp", "公开商品规格"),
        ("public-retailer-price-jd", "公开零售挂牌价"),
        ("public-price-report-ithome", "公开价格报道"),
        ("catalog-reference-price-glodex", "商品参考价"),
    ],
)
def test_evidence_source_label_preserves_source_kind(
    provider_id: str,
    expected: str,
) -> None:
    assert _evidence_source_label(provider_id) == expected


def test_evidence_source_label_rejects_legacy_or_unknown_namespace() -> None:
    with pytest.raises(ValueError, match="namespace"):
        _evidence_source_label("manufacturer-lenovo")


def test_every_runtime_product_attribute_has_a_chinese_display_label() -> None:
    root = Path(__file__).resolve().parents[3]
    raw = json.loads((root / "data/digital-interview-v1/products.json").read_text(encoding="utf-8"))
    attribute_names = {name for product in raw["products"] for name in product["attributes"]}
    assert attribute_names <= set(_EVIDENCE_FIELD_LABELS)
    assert all(
        any("\u4e00" <= character <= "\u9fff" for character in label) or label in {"DPI", "HDR"}
        for name, label in _EVIDENCE_FIELD_LABELS.items()
        if name in attribute_names
    )


def test_terminal_projection_uses_taxonomy_label_for_lifestyle_category() -> None:
    assert _category_label("desk_lamp") == "护眼台灯"


def test_preference_detail_humanizes_internal_attribute_names() -> None:
    assert _humanize_known_detail(
        "已核验规格：illuminance：国AA级；color_rendering：Ra98"  # noqa: RUF001
    ) == (
        "已核验规格：照度等级：国AA级；显色指数：Ra98"  # noqa: RUF001
    )


def _input() -> dict[str, object]:
    return {
        "threadId": "thread-web_console-test",
        "runId": "client-run-web_console-test",
        "state": {},
        "messages": [{"id": "message-web_console-test", "role": "user", "content": " 推荐轻薄本 "}],
        "tools": [],
        "context": [],
        "forwardedProps": {
            "locale": "zh-CN",
            "displayCurrency": "CNY",
            "topK": 3,
            "snapshotVersion": "synthetic-interview-commerce-v1",
        },
    }


def _response() -> AgentDemoResponse:
    return AgentDemoResponse(
        run_id="run-web_console-test",
        status=RunStatus.COMPLETED,
        answer=AgentAnswer(kind=AgentAnswerKind.CHAT_FALLBACK, text="Safe terminal answer."),
    )


def _root_tree() -> dict[str, object]:
    return {
        "root_run_id": "run-web_console-test",
        "parent_run_id": None,
        "loop_kind": LoopKind.ROOT,
        "run_depth": 0,
    }


def _child_tree() -> dict[str, object]:
    return {
        "root_run_id": "run-web_console-test",
        "parent_run_id": "run-web_console-test",
        "loop_kind": LoopKind.CHILD,
        "run_depth": 1,
    }


def _shopping_response(*, answer: str, currency: str = "CNY") -> AgentDemoResponse:
    cost = MoneySummary(currency=currency, exact="5799", display="5799.00")
    offer = OfferSummary(
        offer_id="offer-web_console-test",
        provider_id="synthetic-provider-must-not-reach-summary",
        market="GLOBAL",
        landed_cost=cost,
    )
    result = SearchResult(
        product_id="product-web_console-test",
        title="Travel Notebook",
        category="laptop",
        selected_offer=offer,
        eligible_offers=(offer,),
        landed_cost=cost,
        reason="有库存: synthetic-provider-must-not-reach-summary/GLOBAL",
        evidence=(
            EvidenceSummary(
                evidence_id="evidence-web_console-test",
                provider_id="manufacturer-spec-lenovo",
                source_uri="https://example.com/specification",
                field_path="product.title",
                captured_at="2026-08-11T00:00:00+00:00",
            ),
        ),
    )
    search = SearchResponse(
        run_id="run-web_console-test",
        status=RunStatus.COMPLETED,
        snapshot_version="test-v1",
        config_fingerprint="a" * 64,
        algorithm_version="test-v1",
        results=(result,),
        filter_summary=FilterSummary(),
        diagnostics=Diagnostics(),
    )
    return AgentDemoResponse(
        run_id=search.run_id,
        status=RunStatus.COMPLETED,
        answer=AgentAnswer(kind=AgentAnswerKind.SHOPPING_SUMMARY, text=answer),
        search_response=search,
        selected_product_ids=(result.product_id,),
        evidence_ids=("evidence-web_console-test",),
    )


def test_tool_summary_accepts_the_agent_contract_call_limit() -> None:
    projected = WebConsoleToolSummaryView(
        tool_name="item_search",
        call_count=10,
        safe_outcome="SUCCESS",
    )

    assert projected.call_count == 10
    with pytest.raises(ValidationError):
        WebConsoleToolSummaryView(
            tool_name="item_search",
            call_count=11,
            safe_outcome="SUCCESS",
        )


def test_terminal_projects_human_readable_verification_details_without_raw_reason() -> None:
    response = _shopping_response(answer="推荐一款符合需求的商品。")
    assert response.search_response is not None
    original_result = response.search_response.results[0]
    result = SearchResult.model_validate(
        {
            **original_result.model_dump(mode="python"),
            "matched_requirements": ("budget_max",),
            "unknowns": ("偏好证据不足：机身轻便",),  # noqa: RUF001
            "reason": (
                "满足预算：5799.00 CNY；有库存：US; "  # noqa: RUF001
                "匹配偏好: 游戏性能要好 (处理器型号和高刷新率屏幕来自商品标题证据); "
                "偏好证据不足: 机身轻便 (现有规格：weight：320 克；不足以直接验证“机身轻便”。)"  # noqa: RUF001
            ),
        }
    )
    interpreted = InterpretedRequestSummary(
        required=(
            InterpretedCriterionSummary(
                kind="budget_max",
                value="mode=around;target=6000;bounds=5400..6600 CNY",
                source_span=SourceSpanSummary(start=0, end=6, text="6000左右"),
            ),
        ),
        preferred=(
            InterpretedCriterionSummary(
                kind="preferred",
                value="游戏性能要好",
                source_span=SourceSpanSummary(start=7, end=13, text="游戏性能要好"),
            ),
            InterpretedCriterionSummary(
                kind="preferred",
                value="机身轻便",
                source_span=SourceSpanSummary(start=14, end=18, text="机身轻便"),
            ),
        ),
    )
    search = SearchResponse.model_validate(
        {
            **response.search_response.model_dump(mode="python"),
            "interpreted_request": interpreted,
            "results": (result,),
        }
    )
    projected = WebConsoleTerminalView.from_agent_response(
        AgentDemoResponse.model_validate(
            {
                **response.model_dump(mode="python"),
                "search_response": search,
            }
        )
    )

    budget, preference, insufficient = projected.results[0].need_coverage
    assert budget.detail == (
        "到手价 5799.00 CNY，位于允许区间 5400-6600 CNY 内。"  # noqa: RUF001
    )
    assert preference.detail == "处理器型号和高刷新率屏幕来自商品标题证据"
    assert insufficient.status == "INSUFFICIENT"
    assert insufficient.detail == "现有规格：重量：320 克；不足以直接验证“机身轻便”。"  # noqa: RUF001
    assert insufficient.known_facts == ("重量：320 克",)  # noqa: RUF001
    rendered = projected.model_dump_json(by_alias=True)
    assert '"reason"' not in rendered
    assert "synthetic-provider-must-not-reach-summary" not in rendered


def test_run_input_is_exact_and_maps_only_to_search_request() -> None:
    received = AgUiRunInput.model_validate(_input())

    assert received.messages[0].content == "推荐轻薄本"
    assert received.search_request_payload() == {
        "query": "推荐轻薄本",
        "locale": "zh-CN",
        "display_currency": "CNY",
        "top_k": 3,
        "snapshot_version": "synthetic-interview-commerce-v1",
    }
    for mutation in (
        {"parentRunId": "forged"},
        {"tools": [{"name": "browser_tool"}]},
        {"context": [{"description": "private", "value": "private"}]},
        {"state": {"profile": "private"}},
        {"forwardedProps": {"locale": "zh-CN", "model": "forged"}},
        {"forwardedProps": {"snapshotVersion": "m1d-demo-v1"}},
    ):
        candidate = _input() | mutation
        with pytest.raises(ValidationError):
            AgUiRunInput.model_validate(candidate)


def test_browser_cutover_rejects_the_removed_usd_request_shape() -> None:
    candidate = _input()
    candidate["forwardedProps"] = {
        "locale": "zh-CN",
        "displayCurrency": "USD",
        "topK": 3,
        "snapshotVersion": "synthetic-interview-commerce-v1",
    }

    with pytest.raises(ValidationError):
        AgUiRunInput.model_validate(candidate)


def test_terminal_projection_rejects_non_cny_price() -> None:
    with pytest.raises(ValueError, match="only renders CNY"):
        WebConsoleTerminalView.from_agent_response(
            _shopping_response(answer="USD must not reach the page.", currency="USD")
        )


def test_safe_assistant_deltas_preserve_the_whitelisted_terminal_text() -> None:
    summary = "安全终态文本。" * 40

    deltas = _safe_assistant_deltas(summary)

    assert len(deltas) > 1
    assert all(1 <= len(delta) <= 96 for delta in deltas)
    assert "".join(deltas) == summary


def test_projector_maps_lifecycle_tool_and_terminal_without_upstream_payload() -> None:
    state = initial_state(thread_id="thread-web_console-test", run_id="run-web_console-test")
    started = project_event(
        state=state,
        event=AgentStartedEvent(
            thread_id="thread-web_console-test",
            run_id="run-web_console-test",
            **_root_tree(),
            sequence=1,
            timestamp=1,
        ),
    )
    model_started = project_event(
        state=started.state,
        event=ModelStartedEvent(
            thread_id="thread-web_console-test",
            run_id="run-web_console-test",
            **_root_tree(),
            sequence=2,
            timestamp=2,
            scope=AgentEventScope.ROOT,
            round=1,
        ),
    )
    model_streaming = project_event(
        state=model_started.state,
        event=ModelStreamingEvent(
            thread_id="thread-web_console-test",
            run_id="run-web_console-test",
            **_root_tree(),
            sequence=3,
            timestamp=3,
            scope=AgentEventScope.ROOT,
            round=1,
        ),
    )
    model_finished = project_event(
        state=model_streaming.state,
        event=ModelFinishedEvent(
            thread_id="thread-web_console-test",
            run_id="run-web_console-test",
            **_root_tree(),
            sequence=4,
            timestamp=4,
            scope=AgentEventScope.ROOT,
            round=1,
            tool_name=ToolName.CHAT_FALLBACK,
        ),
    )
    tool_started = project_event(
        state=model_finished.state,
        event=ToolStartedEvent(
            thread_id="thread-web_console-test",
            run_id="run-web_console-test",
            **_root_tree(),
            sequence=5,
            timestamp=5,
            scope=AgentEventScope.ROOT,
            tool_name=ToolName.CHAT_FALLBACK,
        ),
    )
    tool_finished = project_event(
        state=tool_started.state,
        event=ToolFinishedEvent(
            thread_id="thread-web_console-test",
            run_id="run-web_console-test",
            **_root_tree(),
            sequence=6,
            timestamp=6,
            scope=AgentEventScope.ROOT,
            tool_name=ToolName.CHAT_FALLBACK,
            safe_code="SUCCESS",
            trace_bullets=(
                AgentTraceBullet(
                    label="研究结果",
                    value="当前请求不进入商品检索",
                    source=AgentTraceSource.VERIFIED_STATE,
                ),
            ),
        ),
    )
    terminal = project_event(
        state=tool_finished.state,
        event=AgentResultEvent(
            thread_id="thread-web_console-test",
            run_id="run-web_console-test",
            **_root_tree(),
            sequence=7,
            timestamp=7,
            scope=AgentEventScope.ROOT,
            status="COMPLETED",
        ),
        terminal_response=_response(),
    )

    assert [event.type for event in started.events] == ["RUN_STARTED", "STATE_SNAPSHOT"]
    assert [event.type for event in model_streaming.events] == ["CUSTOM", "STATE_SNAPSHOT"]
    assert model_streaming.state.trace[-1].title == "模型正在流式理解购物需求"
    assert model_streaming.state.trace[-1].detail == "正在接收模型输出并形成安全的结构化决策"
    streaming_body = "\n".join(
        event.model_dump_json(by_alias=True) for event in model_streaming.events
    )
    assert "token" not in streaming_body
    assert "toolArgs" not in streaming_body
    assert "reasoning" not in streaming_body
    assert [event.type for event in model_finished.events] == [
        "STEP_FINISHED",
        "STEP_STARTED",
        "STATE_SNAPSHOT",
    ]
    assert [event.type for event in tool_finished.events] == [
        "TOOL_CALL_END",
        "STEP_FINISHED",
        "CUSTOM",
        "STATE_SNAPSHOT",
    ]
    tool_custom = tool_finished.events[2]
    assert isinstance(tool_custom, AgUiCustom)
    assert tool_custom.value["schemaVersion"] == "glodex.web-console.event.v5"
    assert [event.type for event in terminal.events] == [
        "STATE_SNAPSHOT",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]
    body = "\n".join(event.model_dump_json(by_alias=True) for event in terminal.events)
    assert "Safe terminal answer." in body
    assert "rawEvent" not in body
    assert "推荐轻薄本" not in body
    assert terminal.state.terminal is not None
    assert terminal.state.schema_version == "glodex.web-console.ui-state.v6"
    assert terminal.state.terminal.content_kind == "CHAT_FALLBACK"
    assert terminal.state.terminal.summary == "Safe terminal answer."
    assert "answer" not in terminal.state.terminal.model_dump(by_alias=True)
    assert [step.phase.value for step in terminal.state.trace] == [
        "THINK",
        "OBSERVE",
        "OBSERVE",
    ]
    assert terminal.state.trace[1].title == "请求类型已确认"
    assert terminal.state.trace[1].bullets[0].source == "VERIFIED_STATE"


def test_root_event_round_trips_after_durable_none_elision() -> None:
    event = AgentStartedEvent(
        thread_id="thread-web_console-test",
        run_id="run-web_console-test",
        **_root_tree(),
        sequence=1,
        timestamp=1,
    )

    encoded = event.model_dump_json(by_alias=True, exclude_none=True)

    assert "parentRunId" not in encoded
    assert parse_durable_public_event(encoded) == event

    missing_schema = json.loads(encoded)
    missing_schema.pop("schemaVersion")
    with pytest.raises(ValueError, match="schema"):
        parse_durable_public_event(json.dumps(missing_schema))

    snake_case = event.model_dump_json(by_alias=False, exclude_none=True)
    with pytest.raises(ValueError, match=r"schema|camelCase"):
        parse_durable_public_event(snake_case)


def test_child_search_retries_replace_the_platform_observation() -> None:
    state = project_event(
        state=initial_state(thread_id="thread-web_console-test", run_id="run-web_console-test"),
        event=AgentStartedEvent(
            thread_id="thread-web_console-test",
            run_id="run-web_console-test",
            **_root_tree(),
            sequence=1,
            timestamp=1,
        ),
    ).state

    for offset, child_run_id, candidate_count in (
        (2, "child-amazon-first", 1),
        (6, "child-amazon-retry", 3),
    ):
        child_fields = {
            "thread_id": f"thread-{child_run_id}",
            "run_id": child_run_id,
            **_child_tree(),
            "scope": AgentEventScope.CHILD,
            "child_id": child_run_id,
            "depth": 1,
        }
        state = project_event(
            state=state,
            event=ModelStartedEvent(
                **child_fields,
                sequence=offset,
                timestamp=offset,
                round=1,
            ),
        ).state
        state = project_event(
            state=state,
            event=ModelFinishedEvent(
                **child_fields,
                sequence=offset + 1,
                timestamp=offset + 1,
                round=1,
                tool_name=ToolName.ITEM_SEARCH,
            ),
        ).state
        state = project_event(
            state=state,
            event=ToolStartedEvent(
                **child_fields,
                sequence=offset + 2,
                timestamp=offset + 2,
                tool_name=ToolName.ITEM_SEARCH,
            ),
        ).state
        state = project_event(
            state=state,
            event=ToolFinishedEvent(
                **child_fields,
                sequence=offset + 3,
                timestamp=offset + 3,
                tool_name=ToolName.ITEM_SEARCH,
                safe_code="SUCCESS",
                platforms=(Platform.AMAZON,),
                candidate_count=candidate_count,
            ),
        ).state

    searches = tuple(step for step in state.trace if step.tool_name == "item_search")
    assert len(searches) == 1
    assert searches[0].owner_run_id == "child-amazon-retry"
    assert searches[0].candidate_count == 3


def test_projector_maps_the_v2_child_lifecycle_on_the_root_aggregate_cursor() -> None:
    digest = "a" * 64
    child_id = "child-web_console-test"
    child_run_id = "child-run-web_console-test"
    started = project_event(
        state=initial_state(thread_id="thread-web_console-test", run_id="run-web_console-test"),
        event=AgentStartedEvent(
            thread_id="thread-web_console-test",
            run_id="run-web_console-test",
            **_root_tree(),
            sequence=1,
            timestamp=1,
        ),
    )
    requested = project_event(
        state=started.state,
        event=ForkRequestedEvent(
            thread_id="thread-web_console-test",
            run_id="run-web_console-test",
            **_root_tree(),
            sequence=2,
            timestamp=2,
            child_id=child_id,
            depth=1,
            child_parent_run_id="run-web_console-test",
            task_scope_digest=digest,
            platforms=(Platform.AMAZON,),
        ),
    )
    child_started = project_event(
        state=requested.state,
        event=ChildRunStartedEvent(
            thread_id="thread-child-web_console-test",
            run_id=child_run_id,
            **_child_tree(),
            sequence=3,
            timestamp=3,
            child_id=child_id,
            depth=1,
            task_scope_digest=digest,
        ),
    )
    confirmed = project_event(
        state=child_started.state,
        event=ChildCheckpointConfirmedEvent(
            thread_id="thread-child-web_console-test",
            run_id=child_run_id,
            **_child_tree(),
            sequence=4,
            timestamp=4,
            child_id=child_id,
            depth=1,
            task_scope_digest=digest,
        ),
    )
    handoff = project_event(
        state=confirmed.state,
        event=ChildHandoffReadyEvent(
            thread_id="thread-child-web_console-test",
            run_id=child_run_id,
            **_child_tree(),
            sequence=5,
            timestamp=5,
            child_id=child_id,
            depth=1,
            task_scope_digest=digest,
            status="COMPLETED",
        ),
    )
    joined = project_event(
        state=handoff.state,
        event=ForkJoinedEvent(
            thread_id="thread-web_console-test",
            run_id="run-web_console-test",
            **_root_tree(),
            sequence=6,
            timestamp=6,
            child_id=child_id,
            depth=1,
            child_parent_run_id="run-web_console-test",
            task_scope_digest=digest,
            status="COMPLETED",
        ),
    )

    assert [event.type for event in requested.events] == [
        "STEP_STARTED",
        "CUSTOM",
        "STATE_SNAPSHOT",
    ]
    assert [event.type for event in child_started.events] == ["CUSTOM", "STATE_SNAPSHOT"]
    assert [event.type for event in confirmed.events] == ["CUSTOM", "STATE_SNAPSHOT"]
    assert [event.type for event in handoff.events] == [
        "STEP_FINISHED",
        "CUSTOM",
        "STATE_SNAPSHOT",
    ]
    assert [event.type for event in joined.events] == ["CUSTOM", "STATE_SNAPSHOT"]
    assert joined.source_cursor == "run-web_console-test:6"
    assert joined.state.forks[0].child_id == child_id
    assert joined.state.forks[0].state is WebConsoleForkState.COMPLETED
    custom = joined.events[0]
    assert custom.value["kind"] == "FORK_JOINED"
    assert custom.value["runId"] == "run-web_console-test"
    assert custom.value["rootRunId"] == "run-web_console-test"
    rendered = "\n".join(event.model_dump_json(by_alias=True) for event in joined.events)
    assert digest not in rendered


def test_projector_exposes_a_v2_child_failure_without_a_child_cursor_leak() -> None:
    digest = "b" * 64
    child_id = "child-failed-web_console-test"
    child_run_id = "child-failed-run-web_console-test"
    started = project_event(
        state=initial_state(thread_id="thread-web_console-test", run_id="run-web_console-test"),
        event=AgentStartedEvent(
            thread_id="thread-web_console-test",
            run_id="run-web_console-test",
            **_root_tree(),
            sequence=1,
            timestamp=1,
        ),
    )
    requested = project_event(
        state=started.state,
        event=ForkRequestedEvent(
            thread_id="thread-web_console-test",
            run_id="run-web_console-test",
            **_root_tree(),
            sequence=2,
            timestamp=2,
            child_id=child_id,
            depth=1,
            child_parent_run_id="run-web_console-test",
            task_scope_digest=digest,
            platforms=(Platform.EBAY,),
        ),
    )
    child_started = project_event(
        state=requested.state,
        event=ChildRunStartedEvent(
            thread_id="thread-child-web_console-test",
            run_id=child_run_id,
            **_child_tree(),
            sequence=3,
            timestamp=3,
            child_id=child_id,
            depth=1,
            task_scope_digest=digest,
        ),
    )
    failed = project_event(
        state=child_started.state,
        event=ChildFailedEvent(
            thread_id="thread-child-web_console-test",
            run_id=child_run_id,
            **_child_tree(),
            sequence=4,
            timestamp=4,
            child_id=child_id,
            depth=1,
            task_scope_digest=digest,
            status="FAILED",
            safe_code="FORK_FAILED",
        ),
    )
    joined = project_event(
        state=failed.state,
        event=ForkJoinedEvent(
            thread_id="thread-web_console-test",
            run_id="run-web_console-test",
            **_root_tree(),
            sequence=5,
            timestamp=5,
            child_id=child_id,
            depth=1,
            child_parent_run_id="run-web_console-test",
            task_scope_digest=digest,
            status="FAILED",
        ),
    )

    assert failed.source_cursor == "run-web_console-test:4"
    assert failed.state.forks[0].state is WebConsoleForkState.FAILED
    assert failed.state.stages[-1].safe_code == "FORK_FAILED"
    assert failed.events[1].value["kind"] == "CHILD_FAILED"
    assert failed.events[1].value["safeCode"] == "FORK_FAILED"
    assert joined.state.forks[0].state is WebConsoleForkState.FAILED


def test_shopping_projection_emits_only_the_short_display_summary() -> None:
    full_answer = (
        "完整逐项比较哨兵。第1项: 对应证据摘要: "
        "有库存: synthetic-provider-must-not-reach-summary/GLOBAL;综合取舍: 第一项。"
    )
    response = _shopping_response(answer=full_answer)
    state = project_event(
        state=initial_state(
            thread_id="thread-web_console-test", run_id="run-web_console-test"
        ).model_copy(),
        event=AgentStartedEvent(
            thread_id="thread-web_console-test",
            run_id="run-web_console-test",
            **_root_tree(),
            sequence=1,
            timestamp=1,
        ),
    ).state
    terminal = project_event(
        state=state,
        event=AgentResultEvent(
            thread_id="thread-web_console-test",
            run_id="run-web_console-test",
            **_root_tree(),
            sequence=2,
            timestamp=2,
            scope=AgentEventScope.ROOT,
            status="COMPLETED",
        ),
        terminal_response=response,
    )

    assert response.answer is not None and response.answer.text == full_answer
    assert terminal.state.terminal is not None
    assert terminal.state.terminal.content_kind == "SHOPPING_RESULTS"
    assert terminal.state.terminal.summary == (
        "找到 1 款满足已验证硬性条件的候选; 未验证的偏好请查看需求覆盖状态。"
    )
    rendered = "\n".join(event.model_dump_json(by_alias=True) for event in terminal.events)
    assert full_answer not in rendered
    assert "完整逐项比较哨兵" not in rendered
    assert "synthetic-provider-must-not-reach-summary" not in rendered
    assert "GLOBAL" not in rendered
    assert '"reason"' not in rendered
    assert '"categoryLabel":"笔记本电脑"' in rendered
    assert '"sourceLabel":"商品来源"' in rendered
    assert rendered.count("找到 1 款满足已验证硬性条件的候选") == 2


def test_terminal_v2_rejects_the_removed_answer_fields() -> None:
    with pytest.raises(ValidationError):
        WebConsoleTerminalView.model_validate(
            {
                "status": "COMPLETED",
                "answerKind": "SHOPPING_SUMMARY",
                "answer": "legacy full answer",
                "results": [],
                "evidence": [],
                "toolSummary": [],
            }
        )


def test_invalid_payload_and_source_gap_fail_closed_without_echoing_payload() -> None:
    state = initial_state(thread_id="thread-web_console-test", run_id="run-web_console-test")
    invalid = project_payload(
        state=state,
        payload='{"type":"TOOL_STARTED","query":"private request"}',
    )

    assert invalid.events[-1].type == "RUN_ERROR"
    assert invalid.events[-1].code == WebConsoleRelayCode.PROJECTION_INVALID.value
    rendered = "\n".join(event.model_dump_json(by_alias=True) for event in invalid.events)
    assert "private request" not in rendered
    assert "query" not in rendered
