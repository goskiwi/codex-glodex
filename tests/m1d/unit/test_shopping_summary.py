# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import pytest

from glodex.agent.contracts import (
    PickedAttribute,
    PickedItem,
    Platform,
    PreferenceCriterionAssessment,
    PreferenceMatchStatus,
    ShippingStatus,
    ShoppingNarrationInput,
)
from glodex.agent.shopping_summary import DeepSeekShoppingSummary


def _request() -> ShoppingNarrationInput:
    return ShoppingNarrationInput(
        user_query="优先选择续航更长、拍照更好的手机",
        display_currency="CNY",
        picks=(
            PickedItem(
                candidate_id="amazon.phone-1",
                title="Phone One",
                platform=Platform.AMAZON,
                landed_cost_cny=Decimal("2999.00"),
                shipping_status=ShippingStatus.EXACT,
                attributes=(
                    PickedAttribute(
                        name="battery_capacity",
                        value="5000 mAh",
                        evidence_ids=("ev-battery-1",),
                    ),
                    PickedAttribute(
                        name="camera_resolution",
                        value="50 MP",
                        evidence_ids=("ev-camera-1",),
                    ),
                ),
                score=420,
                reasons=("到手价 2999.00 CNY", "2 项商品属性具有来源证据"),
            ),
        ),
    )


def _envelope(content: object, *, finish_reason: str = "stop") -> bytes:
    return json.dumps(
        {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish_reason,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(content, ensure_ascii=False),
                    },
                }
            ]
        },
        ensure_ascii=False,
    ).encode()


def test_deepseek_summary_receives_only_bounded_picks_and_returns_final_text() -> None:
    payloads: list[dict[str, object]] = []

    async def transport(payload: bytes) -> bytes:
        parsed = json.loads(payload)
        payloads.append(parsed)
        return _envelope(
            {"final_text": ("第1项具备已验证的 5000 mAh 电池和 50 MP 相机, 到手价为 2999.00 CNY。")}
        )

    answer = asyncio.run(
        DeepSeekShoppingSummary(transport, model_name="deepseek-v4-flash").summarize(_request())
    )

    assert "5000 mAh" in answer
    assert "50 MP" in answer
    assert len(payloads) == 1
    assert payloads[0]["tool_choice"] == "none"
    user_content = payloads[0]["messages"][1]["content"]  # type: ignore[index]
    supplied = json.loads(user_content)
    assert set(supplied) == {"user_query", "display_currency", "picks"}
    assert supplied["picks"][0]["attributes"][0]["evidence_ids"] == ["ev-battery-1"]


def test_deepseek_summary_marks_all_incomplete_matches_as_tradeoff_alternatives() -> None:
    request = _request()
    pick = request.picks[0].model_copy(
        update={
            "preference_assessments": (
                PreferenceCriterionAssessment(
                    preference="适合旅行剪视频",
                    status=PreferenceMatchStatus.UNKNOWN,
                    evidence_ids=("ev-battery-1",),
                    reason="续航已知，但剪辑能力缺少直接证据",
                ),
            )
        }
    )
    request = request.model_copy(update={"picks": (pick,)})

    async def transport(_payload: bytes) -> bytes:
        return _envelope({"final_text": "第1项到手价为 2999 CNY。"})

    answer = asyncio.run(
        DeepSeekShoppingSummary(transport, model_name="deepseek-v4-flash").summarize(request)
    )

    assert answer.startswith("没有候选同时获得全部偏好的直接证据")
    assert "权衡备选" in answer
    assert "第1项" in answer


@pytest.mark.parametrize(
    "response",
    (
        _envelope({"final_text": "第1项价格为 100 USD。"}),
        _envelope({"final_text": "价格为 2999 CNY。"}),
        _envelope({"final_text": "第1项价格为 2999 CNY。", "extra": True}),
        _envelope({"final_text": "第1项价格为 2999 CNY。"}, finish_reason="length"),
    ),
)
def test_deepseek_summary_falls_back_for_unbounded_or_incomplete_output(response: bytes) -> None:
    async def transport(_payload: bytes) -> bytes:
        return response

    answer = asyncio.run(
        DeepSeekShoppingSummary(transport, model_name="deepseek-v4-flash").summarize(_request())
    )

    assert answer.startswith("已按验证后的人民币到手价排序。")
    assert "第1项 (Amazon): 到手价 2999.00 CNY" in answer


def test_deepseek_summary_uses_typed_fallback_when_provider_is_unavailable() -> None:
    async def transport(_payload: bytes) -> bytes:
        raise TimeoutError

    answer = asyncio.run(
        DeepSeekShoppingSummary(transport, model_name="deepseek-v4-flash").summarize(_request())
    )

    assert answer.startswith("已按验证后的人民币到手价排序。")
    assert "第1项 (Amazon): 到手价 2999.00 CNY" in answer


def test_deepseek_summary_retries_one_invalid_response_without_echoing_it() -> None:
    payloads: list[dict[str, object]] = []

    async def transport(payload: bytes) -> bytes:
        payloads.append(json.loads(payload))
        if len(payloads) == 1:
            return _envelope({"final_text": "价格为 2999 CNY。"})
        return _envelope({"final_text": "第1项到手价为 2999 CNY。"})

    answer = asyncio.run(
        DeepSeekShoppingSummary(transport, model_name="deepseek-v4-flash").summarize(_request())
    )

    assert answer == "第1项到手价为 2999 CNY。"
    assert len(payloads) == 2
    second_messages = payloads[1]["messages"]
    assert type(second_messages) is list
    assert len(second_messages) == 3
    assert "价格为 2999 CNY。" not in json.dumps(second_messages, ensure_ascii=False)
