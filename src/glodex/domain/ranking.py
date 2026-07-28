"""Pure, deterministic lexical scoring for eligible products."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Final, Literal
from unicodedata import category, name, normalize

from glodex.domain.catalog import CanonicalAttribute, CanonicalProduct
from glodex.domain.eligibility import EligibleProduct
from glodex.domain.intent import PreferredCriterion, SourceSpan

LEXICAL_ALGORITHM_VERSION: Final[Literal["lexical-v1"]] = "lexical-v1"
TITLE_TOKEN_WEIGHT: Final = 4
CATEGORY_TOKEN_WEIGHT: Final = 3
VERIFIED_ATTRIBUTE_TOKEN_WEIGHT: Final = 2
PREFERRED_TOKEN_WEIGHT: Final = 3


@dataclass(frozen=True, slots=True)
class LexicalScore:
    """One versioned, integer-only score for an eligible product."""

    product_id: str
    query_score: int
    verified_preference_coverage: int
    algorithm_version: Literal["lexical-v1"] = field(
        default=LEXICAL_ALGORITHM_VERSION,
        init=False,
    )

    def __post_init__(self) -> None:
        if type(self.product_id) is not str or not self.product_id.strip():
            raise ValueError("product_id must be a non-empty string")
        if type(self.query_score) is not int:
            raise TypeError("query_score must be an exact integer")
        if self.query_score < 0:
            raise ValueError("query_score must be non-negative")
        if type(self.verified_preference_coverage) is not int:
            raise TypeError("verified_preference_coverage must be an exact integer")
        if self.verified_preference_coverage < 0:
            raise ValueError("verified_preference_coverage must be non-negative")
        if (
            type(self.algorithm_version) is not str
            or self.algorithm_version != LEXICAL_ALGORITHM_VERSION
        ):
            raise ValueError("algorithm_version must be lexical-v1")


def lexical_tokens(text: str) -> frozenset[str]:
    """Return unique ``lexical-v1`` tokens for one text surface.

    NFKC and casefold are applied before scanning. Latin letters and decimal
    digits form contiguous tokens. Each contiguous CJK run emits its individual
    ideographs and adjacent bigrams; punctuation never bridges two runs.
    """

    if type(text) is not str:
        raise TypeError("lexical text must be an exact string")

    normalized = normalize("NFKC", text).casefold()
    tokens: set[str] = set()
    latin_digit_run: list[str] = []
    cjk_run: list[str] = []

    def flush_latin_digit_run() -> None:
        if latin_digit_run:
            tokens.add("".join(latin_digit_run))
            latin_digit_run.clear()

    def flush_cjk_run() -> None:
        if cjk_run:
            tokens.update(cjk_run)
            tokens.update(f"{left}{right}" for left, right in pairwise(cjk_run))
            cjk_run.clear()

    for character in normalized:
        if _is_latin_or_decimal_digit(character):
            flush_cjk_run()
            latin_digit_run.append(character)
            continue
        if _is_cjk_ideograph(character):
            flush_latin_digit_run()
            cjk_run.append(character)
            continue
        flush_latin_digit_run()
        flush_cjk_run()

    flush_latin_digit_run()
    flush_cjk_run()
    return frozenset(tokens)


def score_eligible_product(
    query: str,
    preferred: tuple[PreferredCriterion, ...],
    candidate: EligibleProduct,
) -> LexicalScore:
    """Score one candidate without reading offers, provider copy, or user state."""

    if type(query) is not str:
        raise TypeError("query must be an exact string")
    _require_preferred(preferred)
    if type(candidate) is not EligibleProduct:
        raise TypeError("candidate must be an exact EligibleProduct")
    product = candidate.product
    if type(product) is not CanonicalProduct:
        raise TypeError("candidate product must be an exact CanonicalProduct")

    query_tokens = lexical_tokens(query)
    title_tokens = lexical_tokens(product.title)
    category_tokens = lexical_tokens(product.category)
    attribute_tokens = _verified_attribute_tokens(product.attributes)
    verified_product_tokens = title_tokens | category_tokens | attribute_tokens
    matched_preferred_tokens = _preferred_tokens(preferred) & verified_product_tokens

    query_score = (
        TITLE_TOKEN_WEIGHT * len(query_tokens & title_tokens)
        + CATEGORY_TOKEN_WEIGHT * len(query_tokens & category_tokens)
        + VERIFIED_ATTRIBUTE_TOKEN_WEIGHT * len(query_tokens & attribute_tokens)
        + PREFERRED_TOKEN_WEIGHT * len(matched_preferred_tokens)
    )
    return LexicalScore(
        product_id=product.product_id,
        query_score=query_score,
        verified_preference_coverage=len(matched_preferred_tokens),
    )


def validate_and_rank(
    candidates: tuple[EligibleProduct, ...],
    untrusted_scores: object,
) -> tuple[EligibleProduct, ...]:
    """Atomically validate one complete score batch and sort original candidates."""

    checked_candidates = _require_rank_candidates(candidates)
    if type(untrusted_scores) is not tuple:
        raise TypeError("untrusted scores must be an exact tuple")
    if any(type(score) is not LexicalScore for score in untrusted_scores):
        raise TypeError("untrusted scores must contain exact LexicalScore values")

    checked_scores = untrusted_scores
    for score in checked_scores:
        try:
            LexicalScore.__post_init__(score)
        except AttributeError as error:
            raise TypeError("LexicalScore is missing required fields") from error

    candidate_ids = tuple(candidate.product.product_id for candidate in checked_candidates)
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("candidate product IDs must be unique")
    score_ids = tuple(score.product_id for score in checked_scores)
    if len(score_ids) != len(set(score_ids)):
        raise ValueError("score product IDs must be unique")
    if len(checked_scores) != len(checked_candidates) or set(score_ids) != set(candidate_ids):
        raise ValueError("score batch must contain exactly one score per candidate product ID")

    scores_by_id = {score.product_id: score for score in checked_scores}
    return tuple(
        sorted(
            checked_candidates,
            key=lambda candidate: _normal_rank_key(
                candidate,
                scores_by_id[candidate.product.product_id],
            ),
        )
    )


def degraded_rank(
    candidates: tuple[EligibleProduct, ...],
) -> tuple[EligibleProduct, ...]:
    """Return the deterministic fallback order without changing membership."""

    checked_candidates = _require_rank_candidates(candidates)
    product_ids = tuple(candidate.product.product_id for candidate in checked_candidates)
    if len(product_ids) != len(set(product_ids)):
        raise ValueError("candidate product IDs must be unique")
    return tuple(
        sorted(
            checked_candidates,
            key=lambda candidate: (
                candidate.product.snapshot_ordinal,
                candidate.product.product_id,
            ),
        )
    )


def isolate_rank_candidates(
    candidates: tuple[EligibleProduct, ...],
) -> tuple[EligibleProduct, ...]:
    """Deep-copy trusted candidates before an untrusted ranker can inspect them."""

    checked_candidates = _require_rank_candidates(candidates)
    isolated = deepcopy(checked_candidates)
    checked_isolated = _require_rank_candidates(isolated)
    if len(checked_isolated) != len(checked_candidates) or any(
        copied is original
        for copied, original in zip(checked_isolated, checked_candidates, strict=True)
    ):
        raise ValueError("rank candidate isolation must copy every candidate root")
    if checked_isolated != checked_candidates:
        raise ValueError("rank candidate isolation must preserve candidate values")
    return checked_isolated


def require_unchanged_rank_candidates(
    trusted: tuple[EligibleProduct, ...],
    isolated: tuple[EligibleProduct, ...],
) -> None:
    """Reject any content change made to the ranker's disposable candidate copy."""

    checked_trusted = _require_rank_candidates(trusted)
    checked_isolated = _require_rank_candidates(isolated)
    if len(checked_isolated) != len(checked_trusted) or any(
        copied is original
        for copied, original in zip(checked_isolated, checked_trusted, strict=True)
    ):
        raise ValueError("ranker candidates must remain isolated from trusted candidates")
    if checked_isolated != checked_trusted:
        raise ValueError("ranker changed candidate content")


def _normal_rank_key(
    candidate: EligibleProduct,
    score: LexicalScore,
) -> tuple[int, int, object, str]:
    return (
        -score.query_score,
        -score.verified_preference_coverage,
        candidate.selected_offer.landed_cost.display_exact,
        candidate.product.product_id,
    )


def _require_rank_candidates(
    candidates: tuple[EligibleProduct, ...],
) -> tuple[EligibleProduct, ...]:
    if type(candidates) is not tuple or any(
        type(candidate) is not EligibleProduct for candidate in candidates
    ):
        raise TypeError("candidates must be a tuple of exact EligibleProduct values")
    for candidate in candidates:
        try:
            EligibleProduct.__post_init__(candidate)
        except (AttributeError, TypeError, ValueError) as error:
            raise TypeError("eligible candidate invariants must hold") from error
    return candidates


def _verified_attribute_tokens(
    attributes: tuple[CanonicalAttribute, ...],
) -> frozenset[str]:
    if type(attributes) is not tuple or any(
        type(attribute) is not CanonicalAttribute for attribute in attributes
    ):
        raise TypeError("verified attributes must be exact CanonicalAttribute values")

    tokens: set[str] = set()
    for attribute in attributes:
        CanonicalAttribute.__post_init__(attribute)
        tokens.update(lexical_tokens(attribute.name))
        tokens.update(lexical_tokens(attribute.value))
    return frozenset(tokens)


def _preferred_tokens(
    preferred: tuple[PreferredCriterion, ...],
) -> frozenset[str]:
    tokens: set[str] = set()
    for criterion in preferred:
        tokens.update(lexical_tokens(criterion.source_span.text))
    return frozenset(tokens)


def _require_preferred(
    preferred: tuple[PreferredCriterion, ...],
) -> tuple[PreferredCriterion, ...]:
    if type(preferred) is not tuple or any(
        type(criterion) is not PreferredCriterion for criterion in preferred
    ):
        raise TypeError("preferred must be a tuple of exact PreferredCriterion values")
    for criterion in preferred:
        if type(criterion.value) is not str or not criterion.value.strip():
            raise ValueError("preferred value must be a non-empty string")
        if type(criterion.source_span) is not SourceSpan:
            raise TypeError("preferred source_span must be an exact SourceSpan")
        if type(criterion.source_span.text) is not str or not criterion.source_span.text.strip():
            raise ValueError("preferred source span text must be a non-empty string")
    return preferred


def _is_latin_or_decimal_digit(character: str) -> bool:
    character_category = category(character)
    if character_category == "Nd":
        return True
    return character_category.startswith("L") and name(character, "").startswith("LATIN")


def _is_cjk_ideograph(character: str) -> bool:
    character_name = name(character, "")
    return character_name.startswith("CJK UNIFIED IDEOGRAPH-") or (
        character_name == "IDEOGRAPHIC NUMBER ZERO"
    )


__all__ = [
    "CATEGORY_TOKEN_WEIGHT",
    "LEXICAL_ALGORITHM_VERSION",
    "PREFERRED_TOKEN_WEIGHT",
    "TITLE_TOKEN_WEIGHT",
    "VERIFIED_ATTRIBUTE_TOKEN_WEIGHT",
    "LexicalScore",
    "degraded_rank",
    "isolate_rank_candidates",
    "lexical_tokens",
    "require_unchanged_rank_candidates",
    "score_eligible_product",
    "validate_and_rank",
]
