from __future__ import annotations

from dataclasses import replace

import pytest

from glodex.domain.eligibility import EligibleProduct
from glodex.domain.ranking import (
    LexicalScore,
    degraded_rank,
    isolate_rank_candidates,
    require_unchanged_rank_candidates,
    validate_and_rank,
)
from tests.unit.domain.test_ranking_lexical_v1 import _candidate

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec("GLO-P0-007", "AC-011"),
]


def _score(
    candidate: EligibleProduct,
    *,
    query_score: int,
    coverage: int,
) -> LexicalScore:
    return LexicalScore(
        product_id=candidate.product.product_id,
        query_score=query_score,
        verified_preference_coverage=coverage,
    )


def _with_ordinal(candidate: EligibleProduct, ordinal: int) -> EligibleProduct:
    return replace(
        candidate,
        product=replace(candidate.product, snapshot_ordinal=ordinal),
    )


def test_valid_batch_uses_every_normal_tie_break_and_preserves_candidate_identity() -> None:
    top_score = _candidate(product_id="top-score", item_price="999.00")
    high_coverage = _candidate(product_id="high-coverage", item_price="30.00")
    cheap_b = _candidate(product_id="cheap-b", item_price="10.00")
    cheap_a = _candidate(product_id="cheap-a", item_price="10.00")
    expensive = _candidate(product_id="expensive", item_price="20.00")
    candidates = (expensive, cheap_b, high_coverage, top_score, cheap_a)
    scores = (
        _score(cheap_a, query_score=9, coverage=1),
        _score(top_score, query_score=10, coverage=0),
        _score(expensive, query_score=9, coverage=1),
        _score(high_coverage, query_score=9, coverage=2),
        _score(cheap_b, query_score=9, coverage=1),
    )

    ranked = validate_and_rank(candidates, scores)

    assert ranked == (top_score, high_coverage, cheap_a, cheap_b, expensive)
    assert all(
        ranked_candidate is expected
        for ranked_candidate, expected in zip(
            ranked,
            (top_score, high_coverage, cheap_a, cheap_b, expensive),
            strict=True,
        )
    )
    assert {id(candidate) for candidate in ranked} == {id(candidate) for candidate in candidates}


def test_score_batch_order_cannot_change_the_ranked_result() -> None:
    first = _candidate(product_id="first")
    second = _candidate(product_id="second")
    candidates = (second, first)
    scores = (
        _score(first, query_score=2, coverage=0),
        _score(second, query_score=1, coverage=0),
    )

    assert validate_and_rank(candidates, scores) == validate_and_rank(
        candidates,
        tuple(reversed(scores)),
    )


def test_batch_requires_exact_tuples_and_exact_score_records() -> None:
    candidate = _candidate()
    score = _score(candidate, query_score=1, coverage=0)

    with pytest.raises(TypeError, match="tuple"):
        validate_and_rank([candidate], (score,))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="tuple"):
        validate_and_rank((candidate,), [score])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="LexicalScore"):
        validate_and_rank((candidate,), (object(),))


def test_missing_duplicate_and_extra_score_ids_reject_the_entire_batch() -> None:
    first = _candidate(product_id="first")
    second = _candidate(product_id="second")
    extra = _candidate(product_id="extra")
    candidates = (first, second)

    invalid_batches = (
        (_score(first, query_score=1, coverage=0),),
        (
            _score(first, query_score=1, coverage=0),
            _score(first, query_score=2, coverage=0),
        ),
        (
            _score(first, query_score=1, coverage=0),
            _score(second, query_score=1, coverage=0),
            _score(extra, query_score=99, coverage=99),
        ),
    )

    for scores in invalid_batches:
        before = tuple(id(candidate) for candidate in candidates)
        with pytest.raises(ValueError):
            validate_and_rank(candidates, scores)
        assert tuple(id(candidate) for candidate in candidates) == before


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("query_score", True),
        ("query_score", 1.0),
        ("query_score", float("nan")),
        ("query_score", float("inf")),
        ("query_score", -1),
        ("verified_preference_coverage", False),
        ("verified_preference_coverage", 1.0),
        ("verified_preference_coverage", float("nan")),
        ("verified_preference_coverage", float("inf")),
        ("verified_preference_coverage", -1),
        ("algorithm_version", "lexical-v2"),
    ],
)
def test_constructor_bypass_cannot_smuggle_an_invalid_score_into_a_partial_batch(
    field_name: str,
    invalid_value: object,
) -> None:
    first = _candidate(product_id="first")
    second = _candidate(product_id="second")
    valid = _score(first, query_score=10, coverage=2)
    hostile = _score(second, query_score=20, coverage=3)
    object.__setattr__(hostile, field_name, invalid_value)
    candidates = (first, second)

    with pytest.raises((TypeError, ValueError)):
        validate_and_rank(candidates, (valid, hostile))

    assert candidates == (first, second)
    assert candidates[0] is first
    assert candidates[1] is second


def test_duplicate_candidate_product_ids_fail_closed() -> None:
    first = _candidate(product_id="duplicate")
    second = _candidate(product_id="duplicate")

    with pytest.raises(ValueError, match="candidate"):
        validate_and_rank(
            (first, second),
            (
                _score(first, query_score=1, coverage=0),
                _score(second, query_score=1, coverage=0),
            ),
        )


def test_ranker_candidate_copy_is_deeply_isolated_and_any_change_discards_it() -> None:
    candidate = _candidate(title="trusted title")

    isolated = isolate_rank_candidates((candidate,))

    assert isolated[0] is not candidate
    assert isolated[0].product is not candidate.product
    assert isolated[0].selected_offer is not candidate.selected_offer
    assert any(isolated[0].selected_offer is offer for offer in isolated[0].eligible_offers)
    require_unchanged_rank_candidates((candidate,), isolated)

    object.__setattr__(isolated[0].product, "title", "ranker mutation")

    with pytest.raises(ValueError, match="changed candidate content"):
        require_unchanged_rank_candidates((candidate,), isolated)
    assert candidate.product.title == "trusted title"


@pytest.mark.parametrize("operation", [validate_and_rank, degraded_rank])
def test_candidate_content_is_revalidated_before_normal_or_degraded_ordering(
    operation: object,
) -> None:
    candidate = _candidate()
    score = _score(candidate, query_score=1, coverage=0)
    object.__setattr__(candidate.product, "snapshot_ordinal", True)

    with pytest.raises(TypeError, match="candidate invariants"):
        if operation is validate_and_rank:
            validate_and_rank((candidate,), (score,))
        else:
            degraded_rank((candidate,))


def test_degraded_rank_uses_snapshot_ordinal_then_product_id_and_preserves_identity() -> None:
    ordinal_two = _with_ordinal(_candidate(product_id="c"), 2)
    ordinal_one_b = _with_ordinal(_candidate(product_id="b"), 1)
    ordinal_one_a = _with_ordinal(_candidate(product_id="a"), 1)
    candidates = (ordinal_two, ordinal_one_b, ordinal_one_a)

    ranked = degraded_rank(candidates)

    assert ranked == (ordinal_one_a, ordinal_one_b, ordinal_two)
    assert ranked[0] is ordinal_one_a
    assert ranked[1] is ordinal_one_b
    assert ranked[2] is ordinal_two


def test_degraded_rank_rejects_non_tuple_candidates() -> None:
    candidate = _candidate()

    with pytest.raises(TypeError, match="tuple"):
        degraded_rank([candidate])  # type: ignore[arg-type]
