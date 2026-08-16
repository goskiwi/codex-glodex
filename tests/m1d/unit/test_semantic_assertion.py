"""Semantic assertion protocol and identity-closure tests."""

from __future__ import annotations

import asyncio
import json

import pytest

from glodex.agent.contracts import (
    CandidateAttribute,
    SemanticAssertionCandidate,
    SemanticAssertionInput,
    ToolFailureCode,
)
from glodex.agent.ports import ToolPortError
from glodex.agent.semantic_assertion import _SYSTEM_PROMPT, DeepSeekSemanticAssertion


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


def _request() -> SemanticAssertionInput:
    return SemanticAssertionInput(
        query="推荐安卓手机，不要手机配件",  # noqa: RUF001
        category="安卓手机",
        components=("Phone", "smartphone"),
        candidates=(
            SemanticAssertionCandidate(
                candidate_id="phone-1",
                title="Android Smartphone 5G",
                attributes=(CandidateAttribute(name="category", value="手机"),),
            ),
            SemanticAssertionCandidate(
                candidate_id="rig-1",
                title="Smartphone Video Rig with Tripod and Phone Grip",
            ),
        ),
    )


def test_category_components_are_documented_as_non_exhaustive() -> None:
    assert "non-exhaustive category-knowledge examples" in _SYSTEM_PROMPT
    assert "not an allowlist" in _SYSTEM_PROMPT


def test_semantic_assertion_returns_only_observed_target_product_ids() -> None:
    payloads: list[dict[str, object]] = []

    async def transport(payload: bytes) -> bytes:
        payloads.append(json.loads(payload))
        return _provider_response(
            {
                "decisions": [
                    {"candidate_id": "phone-1", "is_target_product": True},
                    {"candidate_id": "rig-1", "is_target_product": False},
                ]
            }
        )

    result = asyncio.run(
        DeepSeekSemanticAssertion(transport, model_name="deepseek-v4-flash").verify(_request())
    )

    assert result.relevant_candidate_ids == ("phone-1",)
    assert payloads[0]["thinking"] == {"type": "disabled"}
    user_payload = json.loads(payloads[0]["messages"][1]["content"])
    assert user_payload["candidates"][0]["attributes"] == [{"name": "category", "value": "手机"}]


@pytest.mark.parametrize(
    "decisions",
    [
        [{"candidate_id": "phone-1", "is_target_product": True}],
        [
            {"candidate_id": "invented", "is_target_product": True},
            {"candidate_id": "rig-1", "is_target_product": False},
        ],
        [
            {"candidate_id": "rig-1", "is_target_product": False},
            {"candidate_id": "phone-1", "is_target_product": True},
        ],
    ],
)
def test_semantic_assertion_rejects_incomplete_invented_or_reordered_ids(
    decisions: list[dict[str, object]],
) -> None:
    async def transport(_payload: bytes) -> bytes:
        return _provider_response({"decisions": decisions})

    with pytest.raises(ToolPortError) as captured:
        asyncio.run(
            DeepSeekSemanticAssertion(transport, model_name="deepseek-v4-flash").verify(_request())
        )

    assert captured.value.code is ToolFailureCode.SEMANTIC_ASSERTION_INVALID
