"""Offline M7 dynamic rubric, full judge input, and evidence-cap tests."""

# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any, cast

import pytest

from glodex.agent.contracts import SemanticAssertionInput, SemanticAssertionOutput
from glodex.quality.llm_judge import LlmQualityJudge, LlmRubricGenerator
from glodex.quality.offline import assert_category_integrity
from glodex.quality.runtime import (
    M7_RUBRIC_SCHEMA_VERSION,
    M7EvaluationFacts,
    M7JudgeStatus,
    M7NeedFact,
    M7NeedImportance,
    M7NeedStatus,
    M7OfflineEvaluator,
    M7P0Rule,
    M7P1Rule,
    M7P2Dimension,
    M7P2ReasonCode,
    M7P2Score,
    M7ProductFact,
    M7QualityUnavailable,
    M7RubricCriterion,
    M7SafeCode,
    M7TraceFact,
    TypedRubric,
    apply_evidence_caps,
    verify_p0_p1,
)

pytestmark = [pytest.mark.unit, pytest.mark.spec("GLO-M7-P0-002", "GLO-M7-NFR-001")]


def _facts(*, evidence_integrity: bool = True) -> M7EvaluationFacts:
    return M7EvaluationFacts(
        run_id="run-m7-offline-1",
        query="我想要3000左右的手机，续航要好，游戏性能好",
        answer="已找到 2 个商品。",
        terminal_state="COMPLETED",
        top_k=3,
        needs=(
            M7NeedFact(
                "手机",
                "target_category",
                M7NeedImportance.REQUIRED,
                (M7NeedStatus.VERIFIED, M7NeedStatus.VERIFIED),
            ),
            M7NeedFact(
                "续航要好",
                "attribute_preference",
                M7NeedImportance.PREFERRED,
                (M7NeedStatus.VERIFIED, M7NeedStatus.UNVERIFIED),
            ),
            M7NeedFact(
                "游戏性能好",
                "attribute_preference",
                M7NeedImportance.PREFERRED,
                (M7NeedStatus.UNVERIFIED, M7NeedStatus.UNVERIFIED),
            ),
        ),
        products=(
            M7ProductFact(
                "phone-a", "手机 A", "phone", "2999 CNY", "电池信息已核对", (), ("ev-a",)
            ),
            M7ProductFact(
                "phone-b",
                "手机 B",
                "phone",
                "3099 CNY",
                "预算符合",
                ("未证实偏好：续航要好", "未证实偏好：游戏性能好"),
                ("ev-b",),
            ),
        ),
        tool_names=("item_search", "shopping_summary"),
        trace=(
            M7TraceFact(1, "RUN_STARTED"),
            M7TraceFact(2, "OPERATION_FINISHED", "item_search", "SUCCESS"),
            M7TraceFact(3, "OPERATION_FINISHED", "shopping_summary", "SUCCESS"),
        ),
        category_integrity=True,
        budget_integrity=True,
        exclusion_integrity=True,
        evidence_integrity=evidence_integrity,
        public_output_safe=True,
        output_contract_valid=True,
    )


def _rubric() -> TypedRubric:
    return TypedRubric(
        schema_version=M7_RUBRIC_SCHEMA_VERSION,
        p0_rules=tuple(M7P0Rule),
        p1_rules=tuple(M7P1Rule),
        p2_criteria=(
            M7RubricCriterion(M7P2Dimension.NEED_COVERAGE, (0, 1, 2), 2),
            M7RubricCriterion(M7P2Dimension.SCENARIO_FIT, (1, 2), 2),
            M7RubricCriterion(M7P2Dimension.DECISION_VALUE, (0, 1, 2), 2),
        ),
    )


def test_dynamic_rubric_cannot_omit_a_user_need() -> None:
    rubric = TypedRubric(
        schema_version=M7_RUBRIC_SCHEMA_VERSION,
        p0_rules=tuple(M7P0Rule),
        p1_rules=tuple(M7P1Rule),
        p2_criteria=(
            M7RubricCriterion(M7P2Dimension.NEED_COVERAGE, (0,), 2),
            M7RubricCriterion(M7P2Dimension.SCENARIO_FIT, (1, 2), 2),
            M7RubricCriterion(M7P2Dimension.DECISION_VALUE, (0, 1, 2), 2),
        ),
    )

    with pytest.raises(ValueError, match="omits a decision requirement"):
        rubric.validate_for(_facts())


def test_category_p0_requires_positive_semantic_verification_for_every_product() -> None:
    class _Verifier:
        async def verify(self, request: SemanticAssertionInput) -> SemanticAssertionOutput:
            return SemanticAssertionOutput(
                relevant_candidate_ids=(request.candidates[0].candidate_id,)
            )

    verified = asyncio.run(
        assert_category_integrity(
            facts=replace(_facts(), category_integrity=True),
            semantic_assertion=_Verifier(),
        )
    )

    assert verified.category_integrity is False
    assert verify_p0_p1(facts=verified).p0_failures == (M7P0Rule.CATEGORY_CONSTRAINT,)


class _Generator:
    async def generate(self, *, facts: M7EvaluationFacts) -> TypedRubric:
        assert facts.products[0].title == "手机 A"
        return _rubric()


class _OptimisticJudge:
    async def score(
        self, *, facts: M7EvaluationFacts, rubric: TypedRubric
    ) -> tuple[M7P2Score, ...]:
        assert facts.query.startswith("我想要") and rubric.p2_criteria[0].focus_need_indexes == (
            0,
            1,
            2,
        )
        return tuple(M7P2Score(dimension, 5, M7P2ReasonCode.STRONG) for dimension in M7P2Dimension)


@pytest.mark.acceptance
@pytest.mark.spec("GLO-M7-P0-003", "GLO-M7-P0-004", "GLO-M7-P0-005", "M7-AC-002")
def test_unverified_decision_critical_needs_cap_an_optimistic_judge() -> None:
    async def scenario() -> None:
        report = await M7OfflineEvaluator(_Generator(), _OptimisticJudge(), "test-model").evaluate(
            facts=_facts()
        )
        assert report.judge_status is M7JudgeStatus.SCORED
        assert tuple(value.score for value in report.p2_scores) == (2, 2, 2)
        assert report.reward == 40
        assert report.training_candidate is False
        summary = report.summary()
        assert summary.run_id == report.facts.run_id
        assert summary.p0_passed is True
        assert summary.p1_failure_count == 0
        assert tuple(value.score for value in summary.p2_scores) == (2, 2, 2)
        assert not hasattr(summary, "query")
        assert not hasattr(summary, "rubric")
        assert not hasattr(summary, "trace")
        payload = cast(dict[str, Any], report.to_dict())
        assert payload["query"] == _facts().query
        assert payload["products"][1]["unknowns"] == [
            "未证实偏好：续航要好",
            "未证实偏好：游戏性能好",
        ]

    asyncio.run(scenario())


def test_evidence_bound_comparison_lifts_only_the_short_answer_decision_cap() -> None:
    facts = _facts()
    verified_facts = replace(
        facts,
        needs=tuple(
            replace(
                need,
                product_statuses=tuple(M7NeedStatus.VERIFIED for _ in need.product_statuses),
            )
            for need in facts.needs
        ),
    )
    optimistic = tuple(
        M7P2Score(dimension, 5, M7P2ReasonCode.STRONG) for dimension in M7P2Dimension
    )

    short = apply_evidence_caps(
        facts=replace(verified_facts, answer="已找到 2 个符合条件的商品。"),
        rubric=_rubric(),
        scores=optimistic,
    )
    comparison = (
        "已找到 2 个通过全部硬性条件的商品。第1项满足手机、续航要好、游戏性能好，"
        "对应证据为已核对的电池与性能参数，待确认项为无；第2项同样满足全部需求，"
        "对应证据为电池容量与处理器参数。综合取舍：第1项证据更丰富，第2项价格更低。"
    )
    structured = apply_evidence_caps(
        facts=replace(verified_facts, answer=comparison),
        rubric=_rubric(),
        scores=optimistic,
    )

    assert len(comparison) >= 80
    assert tuple(score.score for score in short) == (5, 5, 3)
    assert tuple(score.score for score in structured) == (5, 5, 5)


@pytest.mark.spec("GLO-M7-P0-003", "M7-AC-002")
def test_p0_failure_is_zero_and_skips_judge() -> None:
    class _NeverJudge:
        async def score(self, **_: object) -> tuple[M7P2Score, ...]:
            raise AssertionError("P0 failure must not call the judge")

    async def scenario() -> None:
        report = await M7OfflineEvaluator(_Generator(), _NeverJudge(), "test-model").evaluate(
            facts=_facts(evidence_integrity=False)
        )
        assert report.judge_status is M7JudgeStatus.P0_FAILED
        assert report.reward == 0 and report.p2_scores == ()
        assert report.verification.p0_failures == (M7P0Rule.EVIDENCE_BINDING,)

    asyncio.run(scenario())


@pytest.mark.acceptance
@pytest.mark.spec("GLO-M7-P0-004", "M7-AC-003")
def test_invalid_judge_is_unscored_not_zero_or_a_fabricated_fallback() -> None:
    class _InvalidJudge:
        async def score(self, **_: object) -> tuple[M7P2Score, ...]:
            raise M7QualityUnavailable(M7SafeCode.JUDGE_INVALID)

    async def scenario() -> None:
        report = await M7OfflineEvaluator(_Generator(), _InvalidJudge(), "test-model").evaluate(
            facts=_facts()
        )
        assert report.judge_status is M7JudgeStatus.UNSCORED
        assert report.reward is None and report.p2_scores == ()
        assert report.safe_code is M7SafeCode.JUDGE_INVALID

    asyncio.run(scenario())


@pytest.mark.acceptance
@pytest.mark.spec("GLO-M7-P0-005", "M7-AC-001")
def test_llm_receives_dynamic_needs_and_complete_product_evidence() -> None:
    async def scenario() -> None:
        requests: list[dict[str, Any]] = []
        replies = iter(
            (
                _provider_reply(_rubric().to_dict()),
                _provider_reply(
                    {
                        "scores": [
                            {"dimension": "NEED_COVERAGE", "score": 2, "reason_code": "WEAK"},
                            {"dimension": "SCENARIO_FIT", "score": 2, "reason_code": "WEAK"},
                            {"dimension": "DECISION_VALUE", "score": 2, "reason_code": "WEAK"},
                        ]
                    }
                ),
            )
        )

        async def transport(payload: bytes) -> bytes:
            requests.append(json.loads(payload))
            return next(replies)

        rubric = await LlmRubricGenerator(transport, model_name="test-model").generate(
            facts=_facts()
        )
        scores = await LlmQualityJudge(transport, model_name="test-model").score(
            facts=_facts(), rubric=rubric
        )
        assert tuple(value.score for value in scores) == (2, 2, 2)
        rubric_input = json.loads(requests[0]["messages"][1]["content"])
        judge_input = json.loads(requests[1]["messages"][1]["content"])
        assert rubric_input["needs"][2]["label"] == "游戏性能好"
        assert judge_input["evaluation"]["products"][1]["landed_cost"] == "3099 CNY"
        assert judge_input["evaluation"]["products"][1]["unknowns"]
        assert judge_input["evaluation"]["trace"][1]["operation"] == "item_search"

    asyncio.run(scenario())


def _provider_reply(content: dict[str, object]) -> bytes:
    return json.dumps(
        {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": json.dumps(content)},
                }
            ]
        },
        separators=(",", ":"),
    ).encode()
