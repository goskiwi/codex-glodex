from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal

import pytest

from glodex.domain.assembly import (
    FinalInvariantViolation,
    GuardedResults,
    ReasonProjection,
    ResultDraft,
    assemble_result_drafts,
    guard_result_drafts,
    require_guarded_results,
)
from glodex.domain.eligibility import EligibleProduct
from glodex.domain.intent import BudgetMax, InterpretedRequest, SourceSpan
from tests.unit.domain.test_result_assembly import _Pipeline, _pipeline

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec("GLO-P0-009", "GLO-NFR-001", "GLO-NFR-002", "GLO-NFR-003"),
]


def _drafts(pipeline: _Pipeline, *, top_k: int = 2) -> tuple[ResultDraft, ...]:
    return assemble_result_drafts(
        pipeline.ranked,
        pipeline.query,
        pipeline.interpreted,
        pipeline.evidence,
        pipeline.rates,
        top_k=top_k,
    )


def _guard(
    pipeline: _Pipeline,
    drafts: object,
    *,
    ranked: tuple[EligibleProduct, ...] | None = None,
    query: str | None = None,
    interpreted: InterpretedRequest | None = None,
    evidence: object | None = None,
    display_currency: str | None = None,
    snapshot_version: str | None = None,
    top_k: int = 2,
) -> GuardedResults:
    return guard_result_drafts(
        drafts,
        ranked=pipeline.ranked if ranked is None else ranked,
        eligibility_output=pipeline.eligibility,
        query=pipeline.query if query is None else query,
        interpreted=pipeline.interpreted if interpreted is None else interpreted,
        evidence=pipeline.evidence if evidence is None else evidence,  # type: ignore[arg-type]
        exchange_rates=pipeline.rates,
        display_currency=(
            pipeline.display_currency if display_currency is None else display_currency
        ),
        snapshot_version=(
            pipeline.snapshot_version if snapshot_version is None else snapshot_version
        ),
        top_k=top_k,
    )


def test_guard_seals_the_exact_complete_ranked_prefix() -> None:
    pipeline = _pipeline()
    drafts = _drafts(pipeline)

    guarded = _guard(pipeline, drafts)
    checked = require_guarded_results(guarded)

    assert checked is guarded
    assert checked.items == drafts
    assert tuple(item.candidate for item in checked.items) == pipeline.ranked
    assert len({item.candidate.product.product_id for item in checked.items}) == 2


@pytest.mark.parametrize(
    "ranked_factory",
    [
        lambda ranked: (ranked[0],),
        lambda ranked: (ranked[0], ranked[0]),
        lambda ranked: (replace(ranked[0]), ranked[1]),
    ],
)
def test_guard_rejects_deleted_duplicated_or_copied_ranked_candidates(
    ranked_factory: Callable[
        [tuple[EligibleProduct, ...]],
        tuple[EligibleProduct, ...],
    ],
) -> None:
    pipeline = _pipeline()
    drafts = _drafts(pipeline)

    with pytest.raises(FinalInvariantViolation):
        _guard(
            pipeline,
            drafts,
            ranked=ranked_factory(pipeline.ranked),
        )


def test_guard_rejects_over_top_k_or_wrong_prefix_without_truncating() -> None:
    pipeline = _pipeline()
    drafts = _drafts(pipeline)

    with pytest.raises(FinalInvariantViolation):
        _guard(
            pipeline,
            drafts,
            top_k=1,
        )
    with pytest.raises(FinalInvariantViolation):
        _guard(
            pipeline,
            tuple(reversed(drafts)),
        )
    with pytest.raises(FinalInvariantViolation):
        _guard(
            pipeline,
            (drafts[0], drafts[0]),
        )


@pytest.mark.parametrize(
    "missing_evidence_id",
    [
        "ev-product-1-title",
        "ev-product-1-category",
        "ev-product-1-kind",
        "ev-offer-1-inventory",
        "ev-offer-1-item_price",
        "ev-rate-usd",
    ],
)
def test_guard_rejects_missing_product_offer_cost_or_fx_evidence(
    missing_evidence_id: str,
) -> None:
    pipeline = _pipeline()
    drafts = _drafts(pipeline)
    incomplete = tuple(
        item for item in pipeline.evidence if item.evidence_id != missing_evidence_id
    )

    with pytest.raises(FinalInvariantViolation):
        _guard(pipeline, drafts, evidence=incomplete)


@pytest.mark.parametrize("field", ["projection", "matched_requirements", "evidence_ids"])
def test_guard_recomputes_every_mutable_draft_projection(field: str) -> None:
    pipeline = _pipeline()
    original = _drafts(pipeline, top_k=1)[0]
    if field == "projection":
        tampered = replace(
            original,
            projection=replace(
                original.projection,
                reason=f"{original.projection.reason}!",
            ),
        )
    elif field == "matched_requirements":
        tampered = replace(original, matched_requirements=("forged",))
    else:
        tampered = replace(
            original,
            evidence_ids=(*original.evidence_ids, "ev-forged"),
        )

    with pytest.raises(FinalInvariantViolation):
        _guard(pipeline, (tampered,), top_k=1)


def test_a_bad_second_draft_causes_atomic_batch_failure() -> None:
    pipeline = _pipeline()
    first, second = _drafts(pipeline)
    bad_second = replace(
        second,
        projection=ReasonProjection(
            reason="forged",
            unknowns=(),
            evidence_ids=second.projection.evidence_ids,
        ),
    )

    with pytest.raises(FinalInvariantViolation):
        _guard(pipeline, (first, bad_second))


def test_guard_rejects_changed_budget_even_when_the_new_span_is_valid() -> None:
    pipeline = _pipeline()
    drafts = _drafts(pipeline)
    original_budget = next(
        item for item in pipeline.interpreted.required if type(item) is BudgetMax
    )
    changed_query = pipeline.query.replace("800", "400")
    changed_budget = replace(
        original_budget,
        amount=Decimal("400"),
        source_span=SourceSpan(
            start=original_budget.source_span.start,
            end=original_budget.source_span.end,
            text="预算400美元",
        ),
    )
    changed_interpreted = replace(
        pipeline.interpreted,
        required=tuple(
            changed_budget if item is original_budget else item
            for item in pipeline.interpreted.required
        ),
    )

    with pytest.raises(FinalInvariantViolation):
        _guard(
            pipeline,
            drafts,
            query=changed_query,
            interpreted=changed_interpreted,
        )


def test_guard_rejects_a_selected_offer_that_is_not_the_exact_minimum_member() -> None:
    pipeline = _pipeline()
    drafts = _drafts(pipeline)
    candidate = pipeline.eligibility.candidates[0]
    object.__setattr__(
        candidate,
        "selected_offer",
        replace(candidate.selected_offer),
    )

    with pytest.raises(FinalInvariantViolation):
        _guard(pipeline, drafts)


@pytest.mark.parametrize(
    ("display_currency", "snapshot_version"),
    [("EUR", None), (None, "m0-v2")],
)
def test_guard_rejects_run_currency_or_snapshot_mismatch(
    display_currency: str | None,
    snapshot_version: str | None,
) -> None:
    pipeline = _pipeline()

    with pytest.raises(FinalInvariantViolation):
        _guard(
            pipeline,
            _drafts(pipeline),
            display_currency=display_currency,
            snapshot_version=snapshot_version,
        )


def test_no_budget_guard_still_requires_landed_cost_closure() -> None:
    pipeline = _pipeline(budget=False)
    drafts = _drafts(pipeline)
    incomplete = tuple(
        item for item in pipeline.evidence if item.evidence_id != "ev-offer-1-shipping"
    )

    with pytest.raises(FinalInvariantViolation):
        _guard(pipeline, drafts, evidence=incomplete)


def test_guarded_results_reject_public_construction_forgery_and_tampering() -> None:
    pipeline = _pipeline()
    guarded = _guard(pipeline, _drafts(pipeline))

    with pytest.raises(TypeError, match="final invariant guard"):
        GuardedResults()

    forged = object.__new__(GuardedResults)
    object.__setattr__(forged, "items", guarded.items)
    with pytest.raises(TypeError, match="sealed"):
        require_guarded_results(forged)

    object.__setattr__(guarded, "items", ())
    with pytest.raises(TypeError, match="sealed"):
        require_guarded_results(guarded)
