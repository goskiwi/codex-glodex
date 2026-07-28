from __future__ import annotations

import asyncio
import inspect

import pytest

from glodex.adapters.deterministic_ranker import DeterministicQueryRanker
from glodex.domain.ranking import LexicalScore
from tests.unit.domain.test_ranking_lexical_v1 import _candidate, _preferred

pytestmark = [
    pytest.mark.contract,
    pytest.mark.spec("GLO-P0-007", "AC-011"),
]


def test_deterministic_ranker_exposes_the_new_async_score_batch_contract() -> None:
    first = _candidate(product_id="first", title="match")
    second = _candidate(product_id="second", category="match")
    preferred = (_preferred(value="canonical-value", text="match"),)
    ranker = DeterministicQueryRanker()

    assert inspect.iscoroutinefunction(ranker.rank)
    scores = asyncio.run(ranker.rank("match", preferred, (first, second)))

    assert type(scores) is tuple
    assert all(type(score) is LexicalScore for score in scores)
    assert scores == ranker.score_batch("match", preferred, (first, second))
    assert tuple(score.product_id for score in scores) == ("first", "second")


def test_deterministic_ranker_is_repeatable_stateless_and_does_not_reorder_candidates() -> None:
    first = _candidate(product_id="first", title="match")
    second = _candidate(product_id="second", title="unrelated")
    candidates = (second, first)
    ranker = DeterministicQueryRanker()

    first_scores = asyncio.run(ranker.rank("match", (), candidates))
    second_scores = asyncio.run(ranker.rank("match", (), candidates))

    assert first_scores == second_scores
    assert tuple(score.product_id for score in first_scores) == ("second", "first")
    assert candidates[0] is second
    assert candidates[1] is first
    assert not hasattr(ranker, "__dict__")


def test_deterministic_ranker_rejects_a_non_tuple_candidate_batch_atomically() -> None:
    candidate = _candidate()
    ranker = DeterministicQueryRanker()

    with pytest.raises(TypeError, match="tuple"):
        asyncio.run(ranker.rank("match", (), [candidate]))  # type: ignore[arg-type]
