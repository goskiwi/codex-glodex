"""Generic preference assessment protocol tests."""

# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
import json

import pytest

from glodex.agent.contracts import (
    PickedAttribute,
    PreferenceAssessmentInput,
    PreferenceCandidate,
    PreferenceMatchStatus,
    ToolFailureCode,
)
from glodex.agent.ports import ToolPortError
from glodex.agent.preference_assessment import DeepSeekPreferenceAssessment


def _provider_response(content: object) -> bytes:
    return json.dumps(
        {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(content, ensure_ascii=False),
                    },
                }
            ]
        },
        ensure_ascii=False,
    ).encode()


def _request() -> PreferenceAssessmentInput:
    return PreferenceAssessmentInput(
        preferences=("降噪要好", "佩戴轻便"),
        candidates=(
            PreferenceCandidate(
                candidate_id="headphone-1",
                title="Wireless Headphones",
                title_evidence_id="ev-title",
                attributes=(
                    PickedAttribute(
                        name="noise_cancelling",
                        value="hybrid ANC",
                        evidence_ids=("ev-anc",),
                    ),
                    PickedAttribute(
                        name="weight",
                        value="245 g",
                        evidence_ids=("ev-weight",),
                    ),
                ),
            ),
        ),
    )


def test_assessment_handles_arbitrary_preferences_with_closed_evidence() -> None:
    async def transport(_payload: bytes) -> bytes:
        return _provider_response(
            {
                "candidates": [
                    {
                        "candidate_id": "headphone-1",
                        "score": 88,
                        "assessments": [
                            {
                                "preference": "降噪要好",
                                "status": "MATCHED",
                                "evidence_ids": ["ev-anc"],
                                "reason": "商品属性明确记录 hybrid ANC",
                            },
                            {
                                "preference": "佩戴轻便",
                                "status": "UNKNOWN",
                                "evidence_ids": ["ev-weight"],
                                "reason": "有重量事实但没有用户可接受阈值",
                            },
                        ],
                    }
                ]
            }
        )

    result = asyncio.run(
        DeepSeekPreferenceAssessment(transport, model_name="deepseek-v4-flash").assess(_request())
    )

    assert result.candidates[0].score == 88
    assert result.candidates[0].assessments[0].status is PreferenceMatchStatus.MATCHED
    assert result.candidates[0].assessments[1].status is PreferenceMatchStatus.UNKNOWN
    assert result.candidates[0].assessments[0].reason == (
        "已核验规格：noise_cancelling：hybrid ANC"
    )
    assert result.candidates[0].assessments[1].reason == (
        "现有规格：weight：245 g；不足以直接验证“佩戴轻便”。"
    )


def test_assessment_supplies_current_date_and_contemporary_relative_quality_policy() -> None:
    captured: dict[str, object] = {}

    async def transport(payload: bytes) -> bytes:
        captured.update(json.loads(payload))
        return _provider_response(
            {
                "candidates": [
                    {
                        "candidate_id": "headphone-1",
                        "score": 0,
                        "assessments": [
                            {
                                "preference": "降噪要好",
                                "status": "UNKNOWN",
                                "evidence_ids": [],
                                "reason": "缺少与当前同类商品基线可比较的数据",
                            },
                            {
                                "preference": "佩戴轻便",
                                "status": "UNKNOWN",
                                "evidence_ids": ["ev-weight"],
                                "reason": "只有重量事实，缺少当前同类基线",
                            },
                        ],
                    }
                ]
            }
        )

    asyncio.run(
        DeepSeekPreferenceAssessment(transport, model_name="deepseek-v4-flash").assess(_request())
    )

    messages = captured["messages"]
    assert isinstance(messages, list)
    system = messages[0]["content"]
    user = json.loads(messages[1]["content"])
    assert "contemporary baseline" in system
    assert "use-case suitability preference" in system
    assert "cover every material dimension" in system
    assert "UNKNOWN does not mean that all evidence must be discarded" in system
    assert "battery capacity" in system
    assert user["evaluation_date"].count("-") == 2
    assert user["input"]["preferences"] == ["降噪要好", "佩戴轻便"]


def test_assessment_rejects_invented_evidence() -> None:
    async def transport(_payload: bytes) -> bytes:
        return _provider_response(
            {
                "candidates": [
                    {
                        "candidate_id": "headphone-1",
                        "score": 100,
                        "assessments": [
                            {
                                "preference": "降噪要好",
                                "status": "MATCHED",
                                "evidence_ids": ["invented"],
                                "reason": "伪造证据",
                            },
                            {
                                "preference": "佩戴轻便",
                                "status": "UNKNOWN",
                                "evidence_ids": [],
                                "reason": "无法判断",
                            },
                        ],
                    }
                ]
            }
        )

    with pytest.raises(ToolPortError) as captured:
        asyncio.run(
            DeepSeekPreferenceAssessment(transport, model_name="deepseek-v4-flash").assess(
                _request()
            )
        )

    assert captured.value.code is ToolFailureCode.PROVIDER_RESPONSE_INVALID


def test_assessment_downgrades_provider_negative_conclusion_for_soft_preference() -> None:
    async def transport(_payload: bytes) -> bytes:
        return _provider_response(
            {
                "candidates": [
                    {
                        "candidate_id": "headphone-1",
                        "score": 0,
                        "assessments": [
                            {
                                "preference": "降噪要好",
                                "status": "NOT_MATCHED",
                                "evidence_ids": ["ev-anc"],
                                "reason": "规格不如另一个候选，因此否定",
                            },
                            {
                                "preference": "佩戴轻便",
                                "status": "UNKNOWN",
                                "evidence_ids": [],
                                "reason": "无法判断",
                            },
                        ],
                    }
                ]
            }
        )

    result = asyncio.run(
        DeepSeekPreferenceAssessment(transport, model_name="deepseek-v4-flash").assess(_request())
    )

    assessment = result.candidates[0].assessments[0]
    assert assessment.status is PreferenceMatchStatus.UNKNOWN
    assert assessment.evidence_ids == ("ev-anc",)
    assert assessment.reason == (
        "现有规格：noise_cancelling：hybrid ANC；不足以直接验证“降噪要好”。"
    )
