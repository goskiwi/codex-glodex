from __future__ import annotations

import asyncio
import inspect
from dataclasses import FrozenInstanceError

import pytest

from glodex.agent.rule_intent import RuleIntentInterpreter
from glodex.application.eligibility_evaluator import (
    EligibilityEvaluation,
    evaluate_eligibility,
)
from glodex.contracts import SearchRequest
from glodex.domain.catalog import aggregate_catalog_batch
from tests.builders import build_catalog_batch

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec("GLO-M1D-P0-004", "GLO-M1D-NFR-003"),
]


def test_evaluator_is_the_single_pure_hard_gate_composition() -> None:
    request = SearchRequest(query="推荐预算800美元、有库存的笔记本")
    interpreted = asyncio.run(RuleIntentInterpreter().interpret(request))
    batch = build_catalog_batch()
    aggregation = aggregate_catalog_batch(batch)

    evaluation = evaluate_eligibility(
        aggregation,
        batch,
        interpreted,
        display_currency=request.display_currency,
    )

    assert isinstance(evaluation, EligibilityEvaluation)
    assert tuple(candidate.offer for candidate in evaluation.pricing) == aggregation.offers
    assert tuple(
        candidate.product.product_id for candidate in evaluation.eligibility_output.candidates
    ) == ("product-1",)
    assert evaluation.filter_summary is evaluation.eligibility_output.filter_summary
    with pytest.raises(FrozenInstanceError):
        evaluation.pricing = ()  # type: ignore[misc]

    parameters = inspect.signature(evaluate_eligibility).parameters
    assert tuple(parameters) == (
        "aggregation",
        "catalog_batch",
        "interpreted_request",
        "display_currency",
    )
    assert "budget" not in parameters
    assert "context" not in parameters
    assert "shipping" not in parameters


def test_evaluator_preserves_no_match_reasons_and_stage_counts() -> None:
    request = SearchRequest(query="推荐预算1美元以内的笔记本")
    interpreted = asyncio.run(RuleIntentInterpreter().interpret(request))
    batch = build_catalog_batch()

    evaluation = evaluate_eligibility(
        aggregate_catalog_batch(batch),
        batch,
        interpreted,
        display_currency="USD",
    )

    assert evaluation.eligibility_output.candidates == ()
    reason_counts = {
        count.reason: (count.product_count, count.offer_count)
        for count in evaluation.filter_summary.reason_counts
    }
    assert reason_counts["offer.over-budget"] == (1, 1)
    assert reason_counts["product.no-eligible-offer"] == (1, 0)
    assert evaluation.filter_summary.stages[-1].before == 1
    assert evaluation.filter_summary.stages[-1].after == 0


def test_evaluator_rejects_unsupported_display_currency_before_gates() -> None:
    request = SearchRequest(
        query="推荐预算800美元、有库存的笔记本",
        display_currency="EUR",
    )
    interpreted = asyncio.run(RuleIntentInterpreter().interpret(request))
    batch = build_catalog_batch()

    with pytest.raises(ValueError, match="unsupported"):
        evaluate_eligibility(
            aggregate_catalog_batch(batch),
            batch,
            interpreted,
            display_currency="EUR",
        )
