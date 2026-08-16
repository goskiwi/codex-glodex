"""Offline-only Rubric-as-Reward domain model for complete shopping runs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, Protocol

from glodex.contracts import MAX_RESULT_EVIDENCE_IDS
from glodex.runtime.contracts import validate_identifier

M7_REPORT_SCHEMA_VERSION: Final = "glodex.m7-offline-report.v2"
M7_RUBRIC_SCHEMA_VERSION: Final = "glodex.m7-rubric.v2"
M7_SUMMARY_SCHEMA_VERSION: Final = "glodex.m7-offline-summary.v1"
M7_P1_PENALTY: Final = 10
M7_TRAINING_CANDIDATE_THRESHOLD: Final = 80


class M7P0Rule(StrEnum):
    CATEGORY_CONSTRAINT = "CATEGORY_CONSTRAINT"
    BUDGET_CONSTRAINT = "BUDGET_CONSTRAINT"
    EXCLUSION_CONSTRAINT = "EXCLUSION_CONSTRAINT"
    EVIDENCE_BINDING = "EVIDENCE_BINDING"
    PRIVATE_OUTPUT_GUARD = "PRIVATE_OUTPUT_GUARD"


class M7P1Rule(StrEnum):
    TOOL_ORDER = "TOOL_ORDER"
    OUTPUT_CONTRACT = "OUTPUT_CONTRACT"
    RECOVERY_FLOW = "RECOVERY_FLOW"
    LOOP_BOUND = "LOOP_BOUND"


class M7P2Dimension(StrEnum):
    NEED_COVERAGE = "NEED_COVERAGE"
    SCENARIO_FIT = "SCENARIO_FIT"
    DECISION_VALUE = "DECISION_VALUE"


class M7P2ReasonCode(StrEnum):
    STRONG = "STRONG"
    PARTIAL = "PARTIAL"
    WEAK = "WEAK"


class M7NeedImportance(StrEnum):
    REQUIRED = "REQUIRED"
    PREFERRED = "PREFERRED"


class M7NeedStatus(StrEnum):
    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"


class M7JudgeStatus(StrEnum):
    SCORED = "SCORED"
    P0_FAILED = "P0_FAILED"
    UNSCORED = "UNSCORED"


class M7SafeCode(StrEnum):
    JUDGE_UNAVAILABLE = "JUDGE_UNAVAILABLE"
    JUDGE_INVALID = "JUDGE_INVALID"
    JUDGE_CIRCUIT_OPEN = "JUDGE_CIRCUIT_OPEN"


class M7QualityUnavailable(RuntimeError):
    def __init__(self, code: M7SafeCode) -> None:
        if type(code) is not M7SafeCode:
            raise TypeError("M7 unavailable code is invalid")
        self.code = code
        super().__init__(code.value)


_P0_ORDER: Final = tuple(M7P0Rule)
_P1_ORDER: Final = tuple(M7P1Rule)
_P2_ORDER: Final = tuple(M7P2Dimension)


def _text(value: str, *, name: str, maximum: int) -> None:
    if type(value) is not str or not value.strip() or len(value) > maximum or "\0" in value:
        raise ValueError(f"{name} is invalid")


@dataclass(frozen=True, slots=True)
class M7NeedFact:
    label: str
    kind: str
    importance: M7NeedImportance
    product_statuses: tuple[M7NeedStatus, ...]

    def __post_init__(self) -> None:
        _text(self.label, name="M7 need label", maximum=256)
        _text(self.kind, name="M7 need kind", maximum=128)
        if type(self.importance) is not M7NeedImportance or any(
            type(value) is not M7NeedStatus for value in self.product_statuses
        ):
            raise TypeError("M7 need fact is invalid")


@dataclass(frozen=True, slots=True)
class M7ProductFact:
    product_id: str
    title: str
    category: str
    landed_cost: str
    reason: str
    unknowns: tuple[str, ...]
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        validate_identifier(self.product_id, name="M7 product ID")
        for name, value, maximum in (
            ("title", self.title, 512),
            ("category", self.category, 128),
            ("landed cost", self.landed_cost, 128),
            ("reason", self.reason, 2_000),
        ):
            _text(value, name=f"M7 product {name}", maximum=maximum)
        if not self.evidence_ids or len(self.evidence_ids) > MAX_RESULT_EVIDENCE_IDS:
            raise ValueError("M7 product evidence is invalid")
        for value in (*self.unknowns, *self.evidence_ids):
            _text(value, name="M7 product detail", maximum=512)


@dataclass(frozen=True, slots=True)
class M7TraceFact:
    sequence: int
    kind: str
    operation: str | None = None
    outcome: str | None = None
    safe_code: str | None = None

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or not 1 <= self.sequence <= 96:
            raise ValueError("M7 trace sequence is invalid")
        _text(self.kind, name="M7 trace kind", maximum=64)
        for value in (self.operation, self.outcome, self.safe_code):
            if value is not None:
                _text(value, name="M7 trace value", maximum=128)


@dataclass(frozen=True, slots=True)
class M7EvaluationFacts:
    run_id: str
    query: str
    answer: str
    terminal_state: str
    top_k: int
    needs: tuple[M7NeedFact, ...]
    products: tuple[M7ProductFact, ...]
    tool_names: tuple[str, ...]
    trace: tuple[M7TraceFact, ...]
    category_integrity: bool
    budget_integrity: bool
    exclusion_integrity: bool
    evidence_integrity: bool
    public_output_safe: bool
    output_contract_valid: bool

    def __post_init__(self) -> None:
        validate_identifier(self.run_id, name="M7 run ID")
        _text(self.query, name="M7 query", maximum=2_000)
        _text(self.answer, name="M7 answer", maximum=2_000)
        if self.terminal_state != "COMPLETED" or not 1 <= self.top_k <= 3:
            raise ValueError("M7 terminal facts are invalid")
        if not 1 <= len(self.products) <= 3 or not 1 <= len(self.needs) <= 32:
            raise ValueError("M7 result facts are invalid")
        if any(len(need.product_statuses) != len(self.products) for need in self.needs):
            raise ValueError("M7 need coverage is not product-bound")
        if len(self.tool_names) > 10 or len(set(self.tool_names)) != len(self.tool_names):
            raise ValueError("M7 tool facts are invalid")
        if any(
            type(value) is not bool
            for value in (
                self.category_integrity,
                self.budget_integrity,
                self.exclusion_integrity,
                self.evidence_integrity,
                self.public_output_safe,
                self.output_contract_valid,
            )
        ):
            raise TypeError("M7 integrity facts are invalid")


@dataclass(frozen=True, slots=True)
class M7RubricCriterion:
    dimension: M7P2Dimension
    focus_need_indexes: tuple[int, ...]
    missing_evidence_score_cap: int

    def __post_init__(self) -> None:
        if type(self.dimension) is not M7P2Dimension:
            raise TypeError("M7 rubric dimension is invalid")
        if (
            not self.focus_need_indexes
            or len(set(self.focus_need_indexes)) != len(self.focus_need_indexes)
            or any(type(value) is not int or value < 0 for value in self.focus_need_indexes)
            or type(self.missing_evidence_score_cap) is not int
            or not 1 <= self.missing_evidence_score_cap <= 3
        ):
            raise ValueError("M7 rubric criterion is invalid")


@dataclass(frozen=True, slots=True)
class TypedRubric:
    schema_version: str
    p0_rules: tuple[M7P0Rule, ...]
    p1_rules: tuple[M7P1Rule, ...]
    p2_criteria: tuple[M7RubricCriterion, ...]

    def __post_init__(self) -> None:
        if (
            self.schema_version != M7_RUBRIC_SCHEMA_VERSION
            or self.p0_rules != _P0_ORDER
            or self.p1_rules != _P1_ORDER
            or tuple(value.dimension for value in self.p2_criteria) != _P2_ORDER
        ):
            raise ValueError("M7 rubric shape is invalid")

    def validate_for(self, facts: M7EvaluationFacts) -> None:
        if any(max(value.focus_need_indexes) >= len(facts.needs) for value in self.p2_criteria):
            raise ValueError("M7 rubric references an absent need")
        all_indexes = tuple(range(len(facts.needs)))
        preferred_indexes = tuple(
            index
            for index, need in enumerate(facts.needs)
            if need.importance is M7NeedImportance.PREFERRED
        )
        expected = (
            all_indexes,
            preferred_indexes or all_indexes,
            all_indexes,
        )
        if tuple(value.focus_need_indexes for value in self.p2_criteria) != expected:
            raise ValueError("M7 rubric omits a decision requirement")

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "p0_rules": [value.value for value in self.p0_rules],
            "p1_rules": [value.value for value in self.p1_rules],
            "p2_criteria": [
                {
                    "dimension": value.dimension.value,
                    "focus_need_indexes": list(value.focus_need_indexes),
                    "missing_evidence_score_cap": value.missing_evidence_score_cap,
                }
                for value in self.p2_criteria
            ],
        }


@dataclass(frozen=True, slots=True)
class M7P2Score:
    dimension: M7P2Dimension
    score: int
    reason_code: M7P2ReasonCode

    def __post_init__(self) -> None:
        if (
            type(self.dimension) is not M7P2Dimension
            or type(self.reason_code) is not M7P2ReasonCode
        ):
            raise TypeError("M7 P2 score is invalid")
        if type(self.score) is not int or not 1 <= self.score <= 5:
            raise ValueError("M7 P2 score is invalid")


@dataclass(frozen=True, slots=True)
class M7Verification:
    p0_failures: tuple[M7P0Rule, ...]
    p1_failures: tuple[M7P1Rule, ...]


@dataclass(frozen=True, slots=True)
class M7OfflineSummary:
    run_id: str
    judge_status: M7JudgeStatus
    p0_passed: bool
    p0_failure_count: int
    p1_failure_count: int
    p2_scores: tuple[M7P2Score, ...]
    reward: int | None
    training_candidate: bool
    judge_model: str
    generated_at: datetime

    def __post_init__(self) -> None:
        validate_identifier(self.run_id, name="M7 summary run ID")
        _text(self.judge_model, name="M7 summary judge model", maximum=128)
        if type(self.judge_status) is not M7JudgeStatus:
            raise TypeError("M7 summary judge status is invalid")
        if (
            type(self.p0_passed) is not bool
            or type(self.p0_failure_count) is not int
            or not 0 <= self.p0_failure_count <= len(M7P0Rule)
            or type(self.p1_failure_count) is not int
            or not 0 <= self.p1_failure_count <= len(M7P1Rule)
            or type(self.training_candidate) is not bool
            or self.generated_at.tzinfo is None
        ):
            raise ValueError("M7 summary facts are invalid")
        if self.p0_passed != (self.p0_failure_count == 0):
            raise ValueError("M7 summary P0 state is inconsistent")
        if self.judge_status is M7JudgeStatus.SCORED:
            if (
                tuple(value.dimension for value in self.p2_scores) != _P2_ORDER
                or type(self.reward) is not int
                or not 0 <= self.reward <= 100
            ):
                raise ValueError("M7 scored summary is incomplete")
        elif (
            self.p2_scores
            or (self.judge_status is M7JudgeStatus.P0_FAILED and self.reward != 0)
            or (self.judge_status is M7JudgeStatus.UNSCORED and self.reward is not None)
        ):
            raise ValueError("M7 unscored summary contains a score")
        if self.training_candidate and (
            self.judge_status is not M7JudgeStatus.SCORED
            or not self.p0_passed
            or self.reward is None
            or self.reward < M7_TRAINING_CANDIDATE_THRESHOLD
            or self.p1_failure_count
        ):
            raise ValueError("M7 training summary is inconsistent")


def verify_p0_p1(*, facts: M7EvaluationFacts) -> M7Verification:
    p0 = tuple(
        rule
        for rule, passed in (
            (M7P0Rule.CATEGORY_CONSTRAINT, facts.category_integrity),
            (M7P0Rule.BUDGET_CONSTRAINT, facts.budget_integrity),
            (M7P0Rule.EXCLUSION_CONSTRAINT, facts.exclusion_integrity),
            (M7P0Rule.EVIDENCE_BINDING, facts.evidence_integrity),
            (M7P0Rule.PRIVATE_OUTPUT_GUARD, facts.public_output_safe),
        )
        if not passed
    )
    p1: list[M7P1Rule] = []
    operations = tuple(value.operation for value in facts.trace if value.operation is not None)
    if (
        "item_search" in operations
        and "shopping_summary" in operations
        and operations.index("item_search") > operations.index("shopping_summary")
    ):
        p1.append(M7P1Rule.TOOL_ORDER)
    if not facts.output_contract_valid:
        p1.append(M7P1Rule.OUTPUT_CONTRACT)
    failed = any(value.outcome == "FAILED" for value in facts.trace)
    recovered = any(value.safe_code == "RECOVERED" for value in facts.trace)
    if failed and not recovered:
        p1.append(M7P1Rule.RECOVERY_FLOW)
    if len(facts.trace) > 96 or len(facts.tool_names) > 10:
        p1.append(M7P1Rule.LOOP_BOUND)
    return M7Verification(p0_failures=p0, p1_failures=tuple(p1))


def apply_evidence_caps(
    *, facts: M7EvaluationFacts, rubric: TypedRubric, scores: tuple[M7P2Score, ...]
) -> tuple[M7P2Score, ...]:
    if tuple(value.dimension for value in scores) != _P2_ORDER:
        raise ValueError("M7 scorecard is incomplete")
    rubric.validate_for(facts)
    output: list[M7P2Score] = []
    for score, criterion in zip(scores, rubric.p2_criteria, strict=True):
        focused = tuple(facts.needs[index] for index in criterion.focus_need_indexes)
        cap = 5
        if any(
            need.importance is M7NeedImportance.REQUIRED
            and any(status is M7NeedStatus.UNVERIFIED for status in need.product_statuses)
            for need in focused
        ):
            cap = 1
        else:
            preferred = tuple(
                need for need in focused if need.importance is M7NeedImportance.PREFERRED
            )
            if any(
                all(status is M7NeedStatus.UNVERIFIED for status in need.product_statuses)
                for need in preferred
            ):
                cap = min(cap, criterion.missing_evidence_score_cap)
            elif any(
                any(status is M7NeedStatus.UNVERIFIED for status in need.product_statuses)
                for need in preferred
            ):
                cap = min(cap, 3)
        if (
            score.dimension is M7P2Dimension.DECISION_VALUE
            and len(facts.products) > 1
            and len(facts.answer.strip()) < 80
        ):
            cap = min(cap, 3)
        value = min(score.score, cap)
        reason = (
            M7P2ReasonCode.STRONG
            if value >= 4
            else M7P2ReasonCode.PARTIAL
            if value == 3
            else M7P2ReasonCode.WEAK
        )
        output.append(M7P2Score(score.dimension, value, reason))
    return tuple(output)


def combine_reward(
    *, verification: M7Verification, p2_scores: tuple[M7P2Score, ...]
) -> tuple[int, bool]:
    if verification.p0_failures:
        return 0, False
    if tuple(value.dimension for value in p2_scores) != _P2_ORDER:
        raise ValueError("M7 scorecard is incomplete")
    reward = round(100 * sum(value.score for value in p2_scores) / 15)
    reward = max(0, reward - M7_P1_PENALTY * len(verification.p1_failures))
    return reward, not verification.p1_failures and reward >= M7_TRAINING_CANDIDATE_THRESHOLD


@dataclass(frozen=True, slots=True)
class M7OfflineReport:
    facts: M7EvaluationFacts
    rubric: TypedRubric | None
    verification: M7Verification
    p2_scores: tuple[M7P2Score, ...]
    reward: int | None
    training_candidate: bool
    judge_status: M7JudgeStatus
    judge_model: str
    safe_code: M7SafeCode | None = None
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def summary(self) -> M7OfflineSummary:
        return M7OfflineSummary(
            run_id=self.facts.run_id,
            judge_status=self.judge_status,
            p0_passed=not self.verification.p0_failures,
            p0_failure_count=len(self.verification.p0_failures),
            p1_failure_count=len(self.verification.p1_failures),
            p2_scores=self.p2_scores,
            reward=self.reward,
            training_candidate=self.training_candidate,
            judge_model=self.judge_model,
            generated_at=self.generated_at,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": M7_REPORT_SCHEMA_VERSION,
            "generated_at": self.generated_at.isoformat(),
            "run_id": self.facts.run_id,
            "query": self.facts.query,
            "answer": self.facts.answer,
            "terminal_state": self.facts.terminal_state,
            "products": [
                {
                    "product_id": value.product_id,
                    "title": value.title,
                    "category": value.category,
                    "landed_cost": value.landed_cost,
                    "reason": value.reason,
                    "unknowns": list(value.unknowns),
                    "evidence_ids": list(value.evidence_ids),
                }
                for value in self.facts.products
            ],
            "needs": [
                {
                    "label": value.label,
                    "kind": value.kind,
                    "importance": value.importance.value,
                    "product_statuses": [status.value for status in value.product_statuses],
                }
                for value in self.facts.needs
            ],
            "trace": [
                {
                    "sequence": value.sequence,
                    "kind": value.kind,
                    "operation": value.operation,
                    "outcome": value.outcome,
                    "safe_code": value.safe_code,
                }
                for value in self.facts.trace
            ],
            "rubric": None if self.rubric is None else self.rubric.to_dict(),
            "rubric_fingerprint": None if self.rubric is None else self.rubric.fingerprint,
            "p0": {
                "passed": not self.verification.p0_failures,
                "failures": [value.value for value in self.verification.p0_failures],
            },
            "p1": {"failures": [value.value for value in self.verification.p1_failures]},
            "p2": [
                {
                    "dimension": value.dimension.value,
                    "score": value.score,
                    "reason_code": value.reason_code.value,
                }
                for value in self.p2_scores
            ],
            "reward": self.reward,
            "training_candidate": self.training_candidate,
            "judge": {
                "status": self.judge_status.value,
                "model": self.judge_model,
                "safe_code": None if self.safe_code is None else self.safe_code.value,
            },
        }


class M7RubricGeneratorPort(Protocol):
    async def generate(self, *, facts: M7EvaluationFacts) -> TypedRubric: ...


class M7QualityJudgePort(Protocol):
    async def score(
        self, *, facts: M7EvaluationFacts, rubric: TypedRubric
    ) -> tuple[M7P2Score, ...]: ...


@dataclass(frozen=True, slots=True)
class M7OfflineEvaluator:
    rubric_generator: M7RubricGeneratorPort
    quality_judge: M7QualityJudgePort
    model_version: str

    async def evaluate(self, *, facts: M7EvaluationFacts) -> M7OfflineReport:
        verification = verify_p0_p1(facts=facts)
        rubric: TypedRubric | None = None
        try:
            rubric = await self.rubric_generator.generate(facts=facts)
            rubric.validate_for(facts)
            if verification.p0_failures:
                return M7OfflineReport(
                    facts=facts,
                    rubric=rubric,
                    verification=verification,
                    p2_scores=(),
                    reward=0,
                    training_candidate=False,
                    judge_status=M7JudgeStatus.P0_FAILED,
                    judge_model=self.model_version,
                )
            raw_scores = await self.quality_judge.score(facts=facts, rubric=rubric)
            scores = apply_evidence_caps(facts=facts, rubric=rubric, scores=raw_scores)
            reward, candidate = combine_reward(verification=verification, p2_scores=scores)
            return M7OfflineReport(
                facts=facts,
                rubric=rubric,
                verification=verification,
                p2_scores=scores,
                reward=reward,
                training_candidate=candidate,
                judge_status=M7JudgeStatus.SCORED,
                judge_model=self.model_version,
            )
        except M7QualityUnavailable as error:
            return M7OfflineReport(
                facts=facts,
                rubric=rubric,
                verification=verification,
                p2_scores=(),
                reward=None,
                training_candidate=False,
                judge_status=M7JudgeStatus.UNSCORED,
                judge_model=self.model_version,
                safe_code=error.code,
            )
        except Exception:
            return M7OfflineReport(
                facts=facts,
                rubric=rubric,
                verification=verification,
                p2_scores=(),
                reward=None,
                training_candidate=False,
                judge_status=M7JudgeStatus.UNSCORED,
                judge_model=self.model_version,
                safe_code=M7SafeCode.JUDGE_INVALID,
            )


__all__ = [
    name
    for name in globals()
    if name.startswith("M7")
    or name in {"TypedRubric", "apply_evidence_caps", "combine_reward", "verify_p0_p1"}
]
