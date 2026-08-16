"""Stateless local adapter for the ``lexical-v1`` domain scorer."""

from __future__ import annotations

from glodex.domain.eligibility import EligibleProduct
from glodex.domain.intent import PreferredCriterion
from glodex.domain.ranking import (
    LEXICAL_ALGORITHM_VERSION,
    LexicalScore,
    score_eligible_product,
)


class DeterministicQueryRanker:
    """Expose deterministic single-candidate and batch scoring without I/O."""

    __slots__ = ()

    algorithm_version = LEXICAL_ALGORITHM_VERSION

    def score(
        self,
        query: str,
        preferred: tuple[PreferredCriterion, ...],
        candidate: EligibleProduct,
    ) -> LexicalScore:
        return score_eligible_product(query, preferred, candidate)

    def score_batch(
        self,
        query: str,
        preferred: tuple[PreferredCriterion, ...],
        candidates: tuple[EligibleProduct, ...],
    ) -> tuple[LexicalScore, ...]:
        if type(candidates) is not tuple or any(
            type(candidate) is not EligibleProduct for candidate in candidates
        ):
            raise TypeError("candidates must be a tuple of exact EligibleProduct values")
        return tuple(self.score(query, preferred, candidate) for candidate in candidates)

    async def rank(
        self,
        query: str,
        preferred: tuple[PreferredCriterion, ...],
        candidates: tuple[EligibleProduct, ...],
    ) -> tuple[object, ...]:
        """Return one untrusted-facing score record per input candidate."""

        return self.score_batch(query, preferred, candidates)


__all__ = ["DeterministicQueryRanker"]
