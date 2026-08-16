"""Assign current products to leaf Category Cards."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from glodex.interview_catalog.category_policy import CategoryPolicyError, CategoryPolicySet

_FALLBACK_CARD_ID = "general.general-merchandise"
_MIN_VECTOR_SCORE = 0.25
_MIN_VECTOR_MARGIN = 0.01


@dataclass(frozen=True, slots=True)
class CategoryAssignment:
    card_id: str
    method: str
    score: float


class CategoryClassifier:
    """Classify by trusted source category or existing Item-vector similarity."""

    def __init__(
        self,
        policies: CategoryPolicySet,
        *,
        card_vectors: Mapping[str, Sequence[float]] | None = None,
    ) -> None:
        self._policies = policies
        self._source_categories = {
            _normalize(alias): card.card_id
            for card in policies.cards
            for alias in card.category_aliases
        }
        self._card_vectors = {
            card_id: _normalized(tuple(float(value) for value in vector))
            for card_id, vector in (card_vectors or {}).items()
        }
        unknown = set(self._card_vectors).difference(policies.by_id)
        if unknown:
            raise CategoryPolicyError("Card vectors contain unknown Card IDs")
        dimensions = {len(vector) for vector in self._card_vectors.values()}
        if len(dimensions) > 1:
            raise CategoryPolicyError("Card vectors have inconsistent dimensions")

    def classify(
        self,
        *,
        category: str | None,
        item_vector: Sequence[float] | None = None,
        vector_match: tuple[str, float, float] | None = None,
    ) -> CategoryAssignment:
        if category:
            card_id = self._source_categories.get(_normalize(category))
            if card_id is not None:
                return CategoryAssignment(card_id, "SOURCE_CATEGORY", 1.0)

        if vector_match is not None:
            card_id, vector_score, runner_up_score = vector_match
            self._policies.require(card_id)
            if not math.isfinite(vector_score) or not math.isfinite(runner_up_score):
                raise CategoryPolicyError("precomputed vector score is invalid")
            if _confident(vector_score, runner_up_score):
                return CategoryAssignment(card_id, "SEMANTIC_VECTOR", vector_score)

        if item_vector is not None and self._card_vectors:
            normalized_item = _normalized(tuple(float(value) for value in item_vector))
            candidates = sorted(
                (_dot(normalized_item, vector), card_id)
                for card_id, vector in self._card_vectors.items()
                if any(vector)
            )
            vector_score, card_id = candidates[-1] if candidates else (0.0, _FALLBACK_CARD_ID)
            runner_up_score = candidates[-2][0] if len(candidates) > 1 else 0.0
            if _confident(vector_score, runner_up_score):
                return CategoryAssignment(card_id, "SEMANTIC_VECTOR", vector_score)

        self._policies.require(_FALLBACK_CARD_ID)
        return CategoryAssignment(_FALLBACK_CARD_ID, "FALLBACK", 0.0)

    def vector_rows(self) -> tuple[tuple[str, ...], tuple[tuple[float, ...], ...]]:
        """Return immutable ordered rows for one server-side batch matrix."""

        return tuple(self._card_vectors), tuple(self._card_vectors.values())


def _normalize(value: str) -> str:
    return " ".join(value.casefold().replace("/", " / ").split())


def _normalized(vector: tuple[float, ...]) -> tuple[float, ...]:
    if not vector or any(not math.isfinite(value) for value in vector):
        raise CategoryPolicyError("Card or Item vector is invalid")
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return tuple(value / norm for value in vector)


def _dot(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if len(left) != len(right):
        raise CategoryPolicyError("Card and Item vector dimensions differ")
    return sum(a * b for a, b in zip(left, right, strict=True))


def _confident(best_score: float, runner_up_score: float) -> bool:
    """Reject weak or ambiguous nearest-Card matches without inspecting text."""

    return best_score >= _MIN_VECTOR_SCORE and best_score - runner_up_score >= _MIN_VECTOR_MARGIN


__all__ = ["CategoryAssignment", "CategoryClassifier"]
