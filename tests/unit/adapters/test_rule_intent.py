from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from glodex.adapters.rule_intent import (
    IntentInterpretationCode,
    IntentInterpretationError,
    RuleIntentInterpreter,
)
from glodex.contracts import SearchRequest
from glodex.domain.intent import (
    BudgetMax,
    Exclusion,
    PreferredCriterion,
    StockRequired,
    TargetCategory,
    validate_interpreted_request,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec("GLO-P0-002", "AC-009"),
]


def interpret(query: str):
    return asyncio.run(RuleIntentInterpreter().interpret(SearchRequest(query=query)))


def test_rule_interpreter_parses_the_phase_b_chinese_vertical_slice() -> None:
    query = "推荐 800 美元以内、有库存、适合出差的轻薄本，不要翻新"  # noqa: RUF001

    interpreted = interpret(query)

    budget = next(item for item in interpreted.required if isinstance(item, BudgetMax))
    category = next(item for item in interpreted.required if isinstance(item, TargetCategory))
    stock = next(item for item in interpreted.required if isinstance(item, StockRequired))
    exclusion = next(item for item in interpreted.required if isinstance(item, Exclusion))
    travel = next(
        item
        for item in interpreted.preferred
        if isinstance(item, PreferredCriterion) and item.value == "travel"
    )

    assert budget.amount == Decimal("800")
    assert budget.currency == "USD"
    assert category.category == "laptop"
    assert exclusion.value == "翻新"
    assert query[budget.source_span.start : budget.source_span.end] == "800 美元以内"
    assert query[stock.source_span.start : stock.source_span.end] == "有库存"
    assert query[category.source_span.start : category.source_span.end] == "轻薄本"
    assert query[travel.source_span.start : travel.source_span.end] == "适合出差"
    assert validate_interpreted_request(query, interpreted).is_valid


@pytest.mark.parametrize(
    ("query", "amount", "currency", "source_text"),
    [
        ("预算 799.90 EUR 的相机", Decimal("799.90"), "EUR", "预算 799.90 EUR"),
        ("800英镑以下的相机", Decimal("800"), "GBP", "800英镑以下"),
        ("预算不超过 900 元的耳机", Decimal("900"), "CNY", "预算不超过 900 元"),
        ("预算 500 以内的笔记本", Decimal("500"), None, "预算 500 以内"),
    ],
)
def test_budget_currency_and_exact_decimal_are_table_driven(
    query: str,
    amount: Decimal,
    currency: str | None,
    source_text: str,
) -> None:
    interpreted = interpret(query)
    budget = next(item for item in interpreted.required if isinstance(item, BudgetMax))

    assert budget.amount == amount
    assert budget.currency == currency
    assert budget.source_span.text == source_text


@pytest.mark.parametrize(
    ("query", "category"),
    [
        ("推荐笔记本电脑", "laptop"),
        ("推荐耳机", "headphones"),
        ("推荐照相机", "camera"),
    ],
)
def test_supported_categories_are_canonicalized(query: str, category: str) -> None:
    interpreted = interpret(query)

    assert any(
        isinstance(item, TargetCategory) and item.category == category
        for item in interpreted.required
    )


def test_negated_stock_and_preference_are_not_promoted() -> None:
    interpreted = interpret("推荐笔记本，但不要求有库存，也不要轻薄")  # noqa: RUF001

    assert not any(isinstance(item, StockRequired) for item in interpreted.required)
    assert not any(
        isinstance(item, PreferredCriterion) and item.value == "lightweight"
        for item in interpreted.preferred
    )


def test_bare_number_limit_is_not_fabricated_as_a_budget() -> None:
    interpreted = interpret("推荐13以下的笔记本")

    assert not any(isinstance(item, BudgetMax) for item in interpreted.required)


def test_unsupported_explicit_budget_currency_fails_closed() -> None:
    for query in (
        "预算 800 日元的相机",
        "预算800 CHF的相机",
        "预算800USDollars的相机",
        "800日元以下的相机",
        "预算800瑞郎的相机",
        "预算800俄罗斯卢布的相机",
        "预算800泰铢的相机",
        "预算800越南盾的相机",
        "预算800迪拉姆的相机",
        "预算800₹的相机",
        "预算800$的相机",
        "预算800€的相机",
        "预算800块钱的相机",
        "800瑞郎以下的相机",
    ):
        with pytest.raises(IntentInterpretationError):
            interpret(query)


def test_stock_negation_does_not_negate_the_target_category() -> None:
    interpreted = interpret("推荐没有库存的相机")

    assert not any(isinstance(item, StockRequired) for item in interpreted.required)
    assert any(
        isinstance(item, TargetCategory) and item.category == "camera"
        for item in interpreted.required
    )


def test_negated_long_category_does_not_leak_through_a_nested_alias() -> None:
    interpreted = interpret("不要照相机")

    assert not any(isinstance(item, TargetCategory) for item in interpreted.required)


@pytest.mark.parametrize(
    "query",
    [
        "不要相机和笔记本",
        "不需要相机或笔记本",
    ],
)
def test_parallel_categories_inherit_the_same_negative_polarity(query: str) -> None:
    interpreted = interpret(query)

    assert not any(isinstance(item, TargetCategory) for item in interpreted.required)


def test_parallel_exclusions_are_all_preserved() -> None:
    interpreted = interpret("不要翻新和二手")

    assert {item.value for item in interpreted.required if isinstance(item, Exclusion)} == {
        "翻新",
        "二手",
    }


def test_parallel_preferences_inherit_the_same_negative_polarity() -> None:
    interpreted = interpret("不要轻薄和便携")

    assert interpreted.preferred == ()


@pytest.mark.parametrize(
    "query",
    [
        "不要相机，笔记本也不要",  # noqa: RUF001
        "相机不要且笔记本也不要",
    ],
)
def test_postpositive_category_negation_is_not_promoted(query: str) -> None:
    interpreted = interpret(query)

    assert not any(isinstance(item, TargetCategory) for item in interpreted.required)


@pytest.mark.parametrize(
    "query",
    [
        "轻薄和便携都不要",
        "轻薄和便携我都不要",
    ],
)
def test_postpositive_preference_negation_is_not_promoted(query: str) -> None:
    interpreted = interpret(query)

    assert interpreted.preferred == ()


@pytest.mark.parametrize(
    "query",
    [
        "有库存这个条件不需要",
        "有库存的条件不需要",
        "现货不要求",
        "在售不是必须",
    ],
)
def test_postpositive_stock_negation_is_not_promoted(query: str) -> None:
    interpreted = interpret(query)

    assert not any(isinstance(item, StockRequired) for item in interpreted.required)


@pytest.mark.parametrize(
    "query",
    [
        "翻新和二手都不要",
        "翻新、二手我都排除",
    ],
)
def test_postpositive_exclusions_are_preserved(query: str) -> None:
    interpreted = interpret(query)

    assert {item.value for item in interpreted.required if isinstance(item, Exclusion)} == {
        "翻新",
        "二手",
    }


def test_excluded_preference_is_not_also_promoted_to_preferred() -> None:
    interpreted = interpret("拒绝轻薄的笔记本")

    assert any(
        isinstance(item, Exclusion) and item.value == "轻薄" for item in interpreted.required
    )
    assert not any(
        isinstance(item, PreferredCriterion) and item.value == "lightweight"
        for item in interpreted.preferred
    )
    assert any(
        isinstance(item, TargetCategory) and item.category == "laptop"
        for item in interpreted.required
    )


def test_bare_negation_applies_only_until_an_explicit_polarity_reset() -> None:
    unavailable = interpret("推荐不在售的相机")
    not_lightweight = interpret("推荐不轻薄的笔记本")
    reset = interpret("不要翻新但要轻薄本")

    assert not any(isinstance(item, StockRequired) for item in unavailable.required)
    assert any(
        isinstance(item, TargetCategory) and item.category == "camera"
        for item in unavailable.required
    )
    assert not any(
        isinstance(item, PreferredCriterion) and item.value == "lightweight"
        for item in not_lightweight.preferred
    )
    assert any(isinstance(item, Exclusion) and item.value == "翻新" for item in reset.required)
    assert not any(isinstance(item, Exclusion) and item.value == "轻薄" for item in reset.required)
    assert any(
        isinstance(item, TargetCategory) and item.category == "laptop" for item in reset.required
    )
    assert any(
        isinstance(item, PreferredCriterion) and item.value == "lightweight"
        for item in reset.preferred
    )


@pytest.mark.parametrize(
    "query",
    [
        "不要翻新可是要轻薄本",
        "不要翻新然后要轻薄本",
        "不要翻新另外要轻薄本",
    ],
)
def test_positive_clause_after_connector_resets_negation(query: str) -> None:
    interpreted = interpret(query)

    assert any(
        isinstance(item, Exclusion) and item.value == "翻新" for item in interpreted.required
    )
    assert not any(
        isinstance(item, Exclusion) and item.value == "轻薄" for item in interpreted.required
    )
    assert any(
        isinstance(item, TargetCategory) and item.category == "laptop"
        for item in interpreted.required
    )
    assert any(
        isinstance(item, PreferredCriterion) and item.value == "lightweight"
        for item in interpreted.preferred
    )


def test_punctuation_and_explicit_transition_reset_parallel_negation() -> None:
    interpreted = interpret("不要相机，但要笔记本")  # noqa: RUF001

    assert any(
        isinstance(item, TargetCategory) and item.category == "laptop"
        for item in interpreted.required
    )


@pytest.mark.parametrize(
    "query",
    [
        "推荐无线耳机",
        "无线耳机有库存",
        "推荐不锈钢轻薄本",
    ],
)
def test_negation_characters_inside_normal_words_do_not_negate(query: str) -> None:
    interpreted = interpret(query)

    assert any(isinstance(item, TargetCategory) for item in interpreted.required)
    if "有库存" in query:
        assert any(isinstance(item, StockRequired) for item in interpreted.required)
    if "轻薄本" in query:
        assert any(
            isinstance(item, PreferredCriterion) and item.value == "lightweight"
            for item in interpreted.preferred
        )


@pytest.mark.parametrize(
    "query",
    [
        "不是预算1000元的相机",
        "没有预算1000元限制的相机",
    ],
)
def test_negated_budget_context_fails_closed(query: str) -> None:
    with pytest.raises(IntentInterpretationError):
        interpret(query)


@pytest.mark.parametrize(
    "query",
    [
        "不要这个预算1000元的相机",
        "无所谓预算1000元的相机",
        "我没有设置预算1000元的相机",
    ],
)
def test_budget_negation_with_safe_gap_words_fails_closed(query: str) -> None:
    with pytest.raises(IntentInterpretationError):
        interpret(query)


@pytest.mark.parametrize(
    "query",
    [
        "我不考虑预算1000元的相机",
        "不要使用预算1000元的相机",
        "拒绝预算1000元的相机",
        "取消预算1000元的相机",
    ],
)
def test_explicit_budget_rejection_verbs_fail_closed(query: str) -> None:
    with pytest.raises(IntentInterpretationError) as error:
        interpret(query)

    assert error.value.code is IntentInterpretationCode.UNSAFE_BUDGET


@pytest.mark.parametrize(
    "query",
    [
        "不用预算1000元的相机",
        "不接受预算1000元的相机",
        "不采纳预算1000元的相机",
        "别用预算1000元的相机",
        "别设预算1000元的相机",
        "别定预算1000元的相机",
        "不想用预算1000元的相机",
        "不想采用预算1000元的相机",
        "不打算设置预算1000元的相机",
        "没打算设置预算1000元的相机",
        "预算1000元可有可无",
        "预算1000元作废",
    ],
)
def test_additional_budget_rejection_verbs_fail_closed(query: str) -> None:
    with pytest.raises(IntentInterpretationError) as error:
        interpret(query)

    assert error.value.code is IntentInterpretationCode.UNSAFE_BUDGET


@pytest.mark.parametrize(
    "query",
    [
        "推荐无线耳机预算1000元",
        "推荐不锈钢轻薄本预算1000元",
        "非洲相机预算1000元",
    ],
)
def test_normal_words_before_budget_do_not_create_false_negation(query: str) -> None:
    interpreted = interpret(query)

    assert any(isinstance(item, BudgetMax) for item in interpreted.required)


def test_multiple_positive_target_categories_fail_closed() -> None:
    with pytest.raises(IntentInterpretationError) as error:
        interpret("笔记本和相机")

    assert error.value.code is IntentInterpretationCode.AMBIGUOUS_CATEGORY


@pytest.mark.parametrize(
    "query",
    [
        "预算不是 800 美元，推荐笔记本",  # noqa: RUF001
        "预算 800 美元或 900 欧元，推荐笔记本",  # noqa: RUF001
        "预算 0 元以内的耳机",
    ],
)
def test_unsafe_or_ambiguous_core_budget_fails_closed(query: str) -> None:
    with pytest.raises(IntentInterpretationError):
        interpret(query)


def test_interpreter_is_reproducible_and_returns_immutable_tuples() -> None:
    request = SearchRequest(query="推荐 800 美元以内有库存的轻薄本")
    interpreter = RuleIntentInterpreter()

    outcomes = tuple(asyncio.run(interpreter.interpret(request)) for _ in range(20))

    assert all(outcome == outcomes[0] for outcome in outcomes)
    assert isinstance(outcomes[0].required, tuple)
    assert isinstance(outcomes[0].preferred, tuple)
