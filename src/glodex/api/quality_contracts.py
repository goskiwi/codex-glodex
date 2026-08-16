"""Owner-scoped browser projection of an already-generated offline M7 report."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field
from pydantic.alias_generators import to_camel

from glodex.api.contracts import ApiDTO


class M7SummaryDTO(ApiDTO):
    model_config = ApiDTO.model_config | {"alias_generator": to_camel, "populate_by_name": True}


class M7P2SummaryView(M7SummaryDTO):
    dimension: Literal["NEED_COVERAGE", "SCENARIO_FIT", "DECISION_VALUE"]
    score: Annotated[int, Field(strict=True, ge=1, le=5)]


class M7OfflineSummaryView(M7SummaryDTO):
    schema_version: Literal["glodex.m7-offline-summary.v1"] = "glodex.m7-offline-summary.v1"
    run_id: str
    judge_status: Literal["SCORED", "P0_FAILED", "UNSCORED"]
    p0_passed: bool
    p0_failure_count: Annotated[int, Field(strict=True, ge=0, le=5)]
    p1_failure_count: Annotated[int, Field(strict=True, ge=0, le=4)]
    p2_scores: tuple[M7P2SummaryView, ...]
    reward: Annotated[int, Field(strict=True, ge=0, le=100)] | None
    training_candidate: bool
    judge_model: str
    generated_at: str


__all__ = ["M7OfflineSummaryView", "M7P2SummaryView"]
