from __future__ import annotations

from dataclasses import replace

import pytest

from glodex.domain.catalog import CanonicalAttribute
from glodex.domain.intent import PreferredCriterion, SourceSpan
from glodex.domain.ranking import (
    LEXICAL_ALGORITHM_VERSION,
    LexicalScore,
    lexical_tokens,
    score_eligible_product,
)
from glodex.retrieval.deterministic_ranker import DeterministicQueryRanker
from tests.unit.domain.test_eligibility_pipeline import _assemble, _offer, _product

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec("GLO-P0-007"),
]


def _preferred(*, value: str, text: str) -> PreferredCriterion:
    return PreferredCriterion(
        value=value,
        source_span=SourceSpan(start=0, end=len(text), text=text),
    )


def _candidate(
    *,
    product_id: str = "product-1",
    title: str = "unrelated title",
    category: str = "other",
    attributes: tuple[tuple[str, str], ...] = (),
    offer_source_uri: str | None = None,
    item_price: str = "20.00",
):
    base_product = _product(product_id, ordinal=0)
    product = replace(
        base_product,
        title=title,
        category=category,
        attributes=tuple(
            CanonicalAttribute(
                name=name,
                value=value,
                evidence_ids=(f"ev-{product_id}-attribute-{index}",),
            )
            for index, (name, value) in enumerate(attributes)
        ),
    )
    offer = _offer(
        product_id=product_id,
        provider_id="provider-a",
        offer_id=f"offer-{product_id}",
        item_price=item_price,
        ordinal=0,
    )
    if offer_source_uri is not None:
        offer = replace(offer, source_uri=offer_source_uri)
    eligible = _assemble((base_product,), (offer,)).candidates[0]
    return replace(eligible, product=product)


def test_lexical_tokens_apply_nfkc_casefold_and_latin_digit_runs() -> None:
    assert lexical_tokens("Ｔｒａｖｅｌ１２ travel12 ALPHA-42 beta_7") == frozenset(  # noqa: RUF001
        {"travel12", "alpha", "42", "beta", "7"}
    )


def test_lexical_tokens_emit_cjk_singletons_and_bigrams_per_contiguous_run() -> None:
    assert lexical_tokens("轻薄本，出差") == frozenset(  # noqa: RUF001
        {"轻", "薄", "本", "轻薄", "薄本", "出", "差", "出差"}
    )
    assert "本出" not in lexical_tokens("轻薄本，出差")  # noqa: RUF001


def test_lexical_tokens_count_each_normalized_token_only_once() -> None:
    assert lexical_tokens("Laptop laptop ＬＡＰＴＯＰ 轻薄轻薄") == frozenset(  # noqa: RUF001
        {"laptop", "轻", "薄", "轻薄", "薄轻"}
    )


@pytest.mark.parametrize(
    ("query", "candidate", "preferred", "expected_score", "expected_coverage"),
    [
        (
            "match",
            _candidate(product_id="title", title="MATCH"),
            (),
            4,
            0,
        ),
        (
            "match",
            _candidate(product_id="category", category="match"),
            (),
            3,
            0,
        ),
        (
            "match",
            _candidate(product_id="attribute", attributes=(("match", "unused"),)),
            (),
            2,
            0,
        ),
        (
            "unrelated",
            _candidate(product_id="preferred-value", title="travel"),
            (_preferred(value="travel", text="适合出差"),),
            0,
            0,
        ),
        (
            "unrelated",
            _candidate(product_id="preferred-span", title="轻薄"),
            (_preferred(value="lightweight", text="轻薄"),),
            9,
            3,
        ),
    ],
)
def test_fixed_field_and_preferred_weights(
    query: str,
    candidate: object,
    preferred: tuple[PreferredCriterion, ...],
    expected_score: int,
    expected_coverage: int,
) -> None:
    score = score_eligible_product(query, preferred, candidate)  # type: ignore[arg-type]

    assert score.query_score == expected_score
    assert score.verified_preference_coverage == expected_coverage


def test_score_uses_unique_attribute_and_preferred_tokens_with_fixed_weights() -> None:
    candidate = _candidate(
        title="ULTRA ultra",
        category="Laptop",
        attributes=(
            ("travel_ready", "true"),
            ("travel", "ready"),
            ("screen", "16"),
            ("segment", "portable"),
        ),
    )
    preferred = (
        _preferred(value="portable", text="travel ready"),
        _preferred(value="travel", text="portable"),
    )

    score = score_eligible_product(
        "ultra laptop travel ready 16 portable",
        preferred,
        candidate,
    )

    assert score.query_score == 24
    assert score.verified_preference_coverage == 3


def test_preferred_canonical_value_cannot_change_source_span_scoring() -> None:
    candidate = _candidate(title="travel 适合出差")
    first = (_preferred(value="travel", text="适合出差"),)
    second = (_preferred(value="unrelated-internal-value", text="适合出差"),)

    assert score_eligible_product("unrelated", first, candidate) == score_eligible_product(
        "unrelated",
        second,
        candidate,
    )


def test_offer_price_provider_marketing_and_input_order_do_not_affect_scores() -> None:
    plain = _candidate(
        product_id="plain",
        item_price="10.00",
        offer_source_uri="fixture://provider/plain",
    )
    marketed = _candidate(
        product_id="marketed",
        item_price="999.00",
        offer_source_uri="fixture://best-ultra-lightweight-travel/marketing",
    )
    ranker = DeterministicQueryRanker()

    forward = ranker.score_batch("ultra lightweight travel", (), (plain, marketed))
    reverse = ranker.score_batch("ultra lightweight travel", (), (marketed, plain))

    assert tuple(score.query_score for score in forward) == (0, 0)
    assert tuple(score.product_id for score in reverse) == ("marketed", "plain")
    assert tuple(score.query_score for score in reverse) == (0, 0)


def test_score_output_is_exact_integer_and_versioned() -> None:
    score = score_eligible_product("match", (), _candidate(title="match"))

    assert type(score.query_score) is int
    assert type(score.verified_preference_coverage) is int
    assert score.algorithm_version == "lexical-v1"
    assert score.algorithm_version == LEXICAL_ALGORITHM_VERSION

    with pytest.raises(TypeError, match="query_score"):
        LexicalScore(
            product_id="product-1",
            query_score=True,  # type: ignore[arg-type]
            verified_preference_coverage=0,
        )
    with pytest.raises(TypeError, match="verified_preference_coverage"):
        LexicalScore(
            product_id="product-1",
            query_score=0,
            verified_preference_coverage=False,  # type: ignore[arg-type]
        )


def test_ranker_score_and_score_batch_are_stateless_domain_delegates() -> None:
    first = _candidate(product_id="first", title="match")
    second = _candidate(product_id="second", category="match")
    ranker = DeterministicQueryRanker()

    assert ranker.score("match", (), first) == score_eligible_product("match", (), first)
    assert ranker.score_batch("match", (), (first, second)) == (
        score_eligible_product("match", (), first),
        score_eligible_product("match", (), second),
    )
