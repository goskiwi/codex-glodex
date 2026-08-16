from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pytest

from glodex.domain.intent import (
    BudgetCalculation,
    BudgetMax,
    Exclusion,
    IntentIssueCode,
    InterpretedRequest,
    PreferredCriterion,
    SourceSpan,
    StockRequired,
    TargetCategory,
    validate_interpreted_request,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec("GLO-P0-002", "AC-009"),
]


def _span_for(query: str, text: str) -> SourceSpan:
    start = query.index(text)
    return SourceSpan(start=start, end=start + len(text), text=text)


def test_intent_models_are_immutable_discriminated_variants() -> None:
    span = SourceSpan(start=0, end=2, text="轻薄")
    budget = BudgetMax(
        mode="maximum",
        target_amount=Decimal("999.90"),
        lower_bound=None,
        upper_bound=Decimal("999.90"),
        currency="CNY",
        source_span=span,
    )
    category = TargetCategory(category="laptop", source_span=span)
    stock = StockRequired(source_span=span)
    exclusion = Exclusion(value="翻新", source_span=span)
    preferred = PreferredCriterion(value="轻薄", source_span=span)
    interpreted = InterpretedRequest(
        required=(budget, category, stock, exclusion),
        preferred=(preferred,),
        parser_version="rule-v1",
    )

    assert tuple(item.kind for item in interpreted.required) == (
        "budget_max",
        "target_category",
        "stock_required",
        "exclusion",
    )
    assert interpreted.preferred[0].kind == "preferred"
    with pytest.raises(FrozenInstanceError):
        span.start = 1  # type: ignore[misc]


def test_validator_accepts_every_supported_required_and_preferred_variant() -> None:
    query = "预算1000元,只要手机现货,不要翻新,轻薄优先"
    interpreted = InterpretedRequest(
        required=(
            BudgetMax(
                mode="maximum",
                target_amount=Decimal("1000"),
                lower_bound=None,
                upper_bound=Decimal("1000"),
                currency="CNY",
                source_span=_span_for(query, "预算1000元"),
            ),
            TargetCategory(category="phone", source_span=_span_for(query, "手机")),
            StockRequired(source_span=_span_for(query, "现货")),
            Exclusion(value="refurbished", source_span=_span_for(query, "翻新")),
        ),
        preferred=(PreferredCriterion(value="lightweight", source_span=_span_for(query, "轻薄")),),
        parser_version="rule-v1",
    )

    result = validate_interpreted_request(query, interpreted)

    assert result.is_valid
    assert result.interpreted_request is interpreted


def test_budget_preserves_exact_decimal_and_optional_original_currency() -> None:
    span = SourceSpan(start=3, end=10, text="999.900")
    explicit = BudgetMax(
        mode="maximum",
        target_amount=Decimal("999.900"),
        lower_bound=None,
        upper_bound=Decimal("999.900"),
        currency="CNY",
        source_span=span,
    )
    inherited_later = BudgetMax(
        mode="maximum",
        target_amount=Decimal("999.900"),
        lower_bound=None,
        upper_bound=Decimal("999.900"),
        currency=None,
        source_span=span,
    )

    assert explicit.target_amount.as_tuple().exponent == -3
    assert explicit.currency == "CNY"
    assert inherited_later.currency is None


def test_validator_uses_trimmed_unnormalized_unicode_code_point_offsets() -> None:
    query = "  想要Ａ款📱,轻薄  "
    interpreted = InterpretedRequest(
        required=(
            TargetCategory(
                category="phone",
                source_span=SourceSpan(start=4, end=5, text="📱"),
            ),
        ),
        preferred=(
            PreferredCriterion(
                value="轻薄",
                source_span=SourceSpan(start=6, end=8, text="轻薄"),
            ),
        ),
        parser_version="rule-v1",
    )

    result = validate_interpreted_request(query, interpreted)

    assert result.is_valid
    assert result.interpreted_request is interpreted
    assert result.issues == ()


@pytest.mark.parametrize(
    ("span", "expected_code"),
    [
        (
            SourceSpan(start=-1, end=1, text="预"),
            IntentIssueCode.SPAN_OUT_OF_BOUNDS,
        ),
        (
            SourceSpan(start=0, end=99, text="预算"),
            IntentIssueCode.SPAN_OUT_OF_BOUNDS,
        ),
        (
            SourceSpan(start=1, end=1, text="预"),
            IntentIssueCode.SPAN_EMPTY,
        ),
        (
            SourceSpan(start=2, end=1, text="预"),
            IntentIssueCode.SPAN_REVERSED,
        ),
        (
            SourceSpan(start=0, end=2, text="限额"),
            IntentIssueCode.SPAN_TEXT_MISMATCH,
        ),
    ],
)
def test_invalid_source_spans_fail_closed(
    span: SourceSpan,
    expected_code: IntentIssueCode,
) -> None:
    interpreted = InterpretedRequest(
        required=(
            BudgetMax(
                mode="maximum",
                target_amount=Decimal("1000"),
                lower_bound=None,
                upper_bound=Decimal("1000"),
                currency="CNY",
                source_span=span,
            ),
        ),
        parser_version="rule-v1",
    )

    result = validate_interpreted_request("预算1000元", interpreted)

    assert not result.is_valid
    assert result.interpreted_request is None
    assert expected_code in tuple(issue.code for issue in result.issues)


def test_validator_rejects_offsets_calculated_before_query_trim() -> None:
    interpreted = InterpretedRequest(
        required=(
            BudgetMax(
                mode="maximum",
                target_amount=Decimal("1000"),
                lower_bound=None,
                upper_bound=Decimal("1000"),
                currency="CNY",
                source_span=SourceSpan(start=2, end=4, text="预算"),
            ),
        ),
        parser_version="rule-v1",
    )

    result = validate_interpreted_request("  预算1000元  ", interpreted)

    assert tuple(issue.code for issue in result.issues) == (IntentIssueCode.SPAN_TEXT_MISMATCH,)
    assert result.interpreted_request is None


@pytest.mark.parametrize(
    ("amount", "currency", "expected_code"),
    [
        (
            Decimal("1"),
            "CNY",
            IntentIssueCode.BUDGET_AMOUNT_SPAN_MISMATCH,
        ),
        (
            Decimal("1000"),
            "USD",
            IntentIssueCode.BUDGET_CURRENCY_SPAN_MISMATCH,
        ),
        (
            Decimal("1000"),
            None,
            IntentIssueCode.BUDGET_CURRENCY_SPAN_MISMATCH,
        ),
    ],
)
def test_budget_values_must_be_bound_to_their_source_span(
    amount: Decimal,
    currency: str | None,
    expected_code: IntentIssueCode,
) -> None:
    query = "预算1000元的笔记本"
    interpreted = InterpretedRequest(
        required=(
            BudgetMax(
                mode="maximum",
                target_amount=amount,
                lower_bound=None,
                upper_bound=amount,
                currency=currency,
                source_span=_span_for(query, "预算1000元"),
            ),
        ),
        parser_version="malicious-v1",
    )

    result = validate_interpreted_request(query, interpreted)

    assert not result.is_valid
    assert expected_code in tuple(issue.code for issue in result.issues)


@pytest.mark.parametrize(
    "query",
    [
        "推荐型号1000的相机",
        "预算1000日元的相机",
    ],
)
def test_budget_span_without_structural_evidence_is_rejected(query: str) -> None:
    text = "1000" if "型号" in query else "预算1000日元"
    interpreted = InterpretedRequest(
        required=(
            BudgetMax(
                mode="maximum",
                target_amount=Decimal("1000"),
                lower_bound=None,
                upper_bound=Decimal("1000"),
                currency=None,
                source_span=_span_for(query, text),
            ),
        ),
        parser_version="malicious-v1",
    )

    result = validate_interpreted_request(query, interpreted)

    assert tuple(issue.code for issue in result.issues) == (
        IntentIssueCode.INVALID_BUDGET_SPAN_SEMANTICS,
    )


def test_bare_upper_limit_marker_is_a_valid_budget_contract() -> None:
    query = "推荐13以下的笔记本"
    interpreted = InterpretedRequest(
        required=(
            BudgetMax(
                mode="maximum",
                target_amount=Decimal("13"),
                lower_bound=None,
                upper_bound=Decimal("13"),
                currency=None,
                source_span=_span_for(query, "13以下"),
            ),
        ),
        parser_version="malicious-v1",
    )

    result = validate_interpreted_request(query, interpreted)

    assert result.is_valid


@pytest.mark.parametrize(
    "source_text",
    [
        "预算1500左右，可以超出100-200",  # noqa: RUF001
        "预算基准1500，留出100-200的弹性",  # noqa: RUF001
        "价格目标1500，必要时上浮100至200",  # noqa: RUF001
    ],
)
def test_model_budget_calculation_does_not_depend_on_phrase_keywords(source_text: str) -> None:
    query = f"通勤双肩包，可装16寸电脑，{source_text}"  # noqa: RUF001
    interpreted = InterpretedRequest(
        required=(
            BudgetMax(
                mode="maximum",
                target_amount=Decimal("1700"),
                lower_bound=None,
                upper_bound=Decimal("1700"),
                currency=None,
                source_span=_span_for(query, source_text),
                calculation=BudgetCalculation(
                    base_amount=Decimal("1500"),
                    allowance_amounts=(Decimal("100"), Decimal("200")),
                ),
            ),
        ),
        parser_version="agent-loop-v1",
    )

    assert validate_interpreted_request(query, interpreted).is_valid
    invalid = replace(
        interpreted.required[0],
        target_amount=Decimal("1600"),
        upper_bound=Decimal("1600"),
    )
    result = validate_interpreted_request(
        query,
        replace(interpreted, required=(invalid,)),
    )
    assert tuple(issue.code for issue in result.issues) == (IntentIssueCode.INVALID_BUDGET_AMOUNT,)


@pytest.mark.parametrize(
    ("query", "text", "currency"),
    [
        ("预算1000元的相机", "预算1000", None),
        ("预算1000日元的相机", "预算1000", None),
        ("预算1000USDollars的相机", "预算1000", None),
        ("不是预算1000元的相机", "预算1000元", "CNY"),
        ("没有预算1000元限制的相机", "预算1000元", "CNY"),
        ("我不考虑预算1000元的相机", "预算1000元", "CNY"),
        ("不要使用预算1000元的相机", "预算1000元", "CNY"),
        ("拒绝预算1000元的相机", "预算1000元", "CNY"),
        ("取消预算1000元的相机", "预算1000元", "CNY"),
        ("不用预算1000元的相机", "预算1000元", "CNY"),
        ("不接受预算1000元的相机", "预算1000元", "CNY"),
        ("不采纳预算1000元的相机", "预算1000元", "CNY"),
        ("别用预算1000元的相机", "预算1000元", "CNY"),
        ("别设预算1000元的相机", "预算1000元", "CNY"),
        ("别定预算1000元的相机", "预算1000元", "CNY"),
        ("不想用预算1000元的相机", "预算1000元", "CNY"),
        ("不想采用预算1000元的相机", "预算1000元", "CNY"),
        ("不打算设置预算1000元的相机", "预算1000元", "CNY"),
        ("没打算设置预算1000元的相机", "预算1000元", "CNY"),
        ("预算1000元不需要", "预算1000元", "CNY"),
        ("预算1000元可有可无", "预算1000元", "CNY"),
        ("预算1000元作废", "预算1000元", "CNY"),
        ("预算1000元或2000元的相机", "预算1000元", "CNY"),
        ("800美元以内或900欧元以内的相机", "800美元以内", "USD"),
    ],
)
def test_budget_span_must_be_maximal_non_negated_and_unambiguous(
    query: str,
    text: str,
    currency: str | None,
) -> None:
    interpreted = InterpretedRequest(
        required=(
            BudgetMax(
                mode="maximum",
                target_amount=Decimal("1000") if "1000" in text else Decimal("800"),
                lower_bound=None,
                upper_bound=Decimal("1000") if "1000" in text else Decimal("800"),
                currency=currency,
                source_span=_span_for(query, text),
            ),
        ),
        parser_version="malicious-v1",
    )

    result = validate_interpreted_request(query, interpreted)

    assert tuple(issue.code for issue in result.issues) == (
        IntentIssueCode.INVALID_BUDGET_SPAN_SEMANTICS,
    )


def test_validator_does_not_nfkc_normalize_evidence_text() -> None:
    interpreted = InterpretedRequest(
        preferred=(
            PreferredCriterion(
                value="A款",
                source_span=SourceSpan(start=1, end=2, text="A"),
            ),
        ),
        parser_version="rule-v1",
    )

    result = validate_interpreted_request("要Ａ款", interpreted)

    assert tuple(issue.code for issue in result.issues) == (IntentIssueCode.SPAN_TEXT_MISMATCH,)


def test_preferred_cannot_be_upgraded_to_required() -> None:
    preferred = PreferredCriterion(
        value="轻薄",
        source_span=SourceSpan(start=0, end=2, text="轻薄"),
    )
    interpreted = InterpretedRequest(
        required=(preferred,),  # type: ignore[arg-type]
        parser_version="malicious-v1",
    )

    result = validate_interpreted_request("轻薄", interpreted)

    assert tuple(issue.code for issue in result.issues) == (
        IntentIssueCode.PREFERRED_UPGRADED_TO_REQUIRED,
    )
    assert result.interpreted_request is None


def test_required_variant_cannot_be_smuggled_into_preferred() -> None:
    required = TargetCategory(
        category="phone",
        source_span=SourceSpan(start=0, end=2, text="手机"),
    )
    interpreted = InterpretedRequest(
        preferred=(required,),  # type: ignore[arg-type]
        parser_version="malicious-v1",
    )

    result = validate_interpreted_request("手机", interpreted)

    assert tuple(issue.code for issue in result.issues) == (
        IntentIssueCode.REQUIRED_DEMOTED_TO_PREFERRED,
    )
    assert result.interpreted_request is None


@pytest.mark.parametrize("parser_version", ["", "bad version", "../unsafe", "x" * 129])
def test_parser_version_must_be_a_safe_public_identifier(
    parser_version: str,
) -> None:
    result = validate_interpreted_request(
        "推荐相机",
        InterpretedRequest(parser_version=parser_version),
    )

    assert tuple(issue.code for issue in result.issues) == (IntentIssueCode.INVALID_PARSER_VERSION,)


@pytest.mark.parametrize(
    ("query", "required", "expected_code"),
    [
        (
            "笔记本",
            (
                TargetCategory(
                    category="laptop",
                    source_span=SourceSpan(start=0, end=3, text="笔记本"),
                ),
                TargetCategory(
                    category="laptop",
                    source_span=SourceSpan(start=0, end=3, text="笔记本"),
                ),
            ),
            IntentIssueCode.AMBIGUOUS_CATEGORY,
        ),
        (
            "笔记本",
            (
                TargetCategory(
                    category="camera",
                    source_span=SourceSpan(start=0, end=3, text="笔记本"),
                ),
            ),
            IntentIssueCode.CRITERION_SPAN_SEMANTICS_MISMATCH,
        ),
        (
            "不要相机",
            (
                TargetCategory(
                    category="camera",
                    source_span=SourceSpan(start=2, end=4, text="相机"),
                ),
            ),
            IntentIssueCode.CRITERION_SPAN_SEMANTICS_MISMATCH,
        ),
    ],
)
def test_target_category_must_be_positive_bound_and_unambiguous(
    query: str,
    required: tuple[TargetCategory, ...],
    expected_code: IntentIssueCode,
) -> None:
    result = validate_interpreted_request(
        query,
        InterpretedRequest(required=required, parser_version="malicious-v1"),
    )

    assert not result.is_valid
    assert expected_code in tuple(issue.code for issue in result.issues)


def test_validator_rejects_an_omitted_category_from_an_ambiguous_query() -> None:
    query = "笔记本和相机"
    interpreted = InterpretedRequest(
        required=(
            TargetCategory(
                category="laptop",
                source_span=_span_for(query, "笔记本"),
            ),
        ),
        parser_version="malicious-v1",
    )

    result = validate_interpreted_request(query, interpreted)

    assert tuple(issue.code for issue in result.issues) == (IntentIssueCode.AMBIGUOUS_CATEGORY,)


@pytest.mark.parametrize(
    ("query", "required", "preferred"),
    [
        (
            "不要相机和笔记本",
            (
                TargetCategory(
                    category="laptop",
                    source_span=SourceSpan(start=5, end=8, text="笔记本"),
                ),
            ),
            (),
        ),
        (
            "不要轻薄和便携",
            (),
            (
                PreferredCriterion(
                    value="portable",
                    source_span=SourceSpan(start=5, end=7, text="便携"),
                ),
            ),
        ),
        (
            "不要相机，笔记本也不要",  # noqa: RUF001
            (
                TargetCategory(
                    category="laptop",
                    source_span=SourceSpan(start=5, end=8, text="笔记本"),
                ),
            ),
            (),
        ),
        (
            "轻薄和便携都不要",
            (),
            (
                PreferredCriterion(
                    value="portable",
                    source_span=SourceSpan(start=3, end=5, text="便携"),
                ),
            ),
        ),
    ],
)
def test_parallel_negative_polarity_cannot_be_spoofed(
    query: str,
    required: tuple[TargetCategory, ...],
    preferred: tuple[PreferredCriterion, ...],
) -> None:
    result = validate_interpreted_request(
        query,
        InterpretedRequest(
            required=required,
            preferred=preferred,
            parser_version="malicious-v1",
        ),
    )

    assert tuple(issue.code for issue in result.issues) == (
        IntentIssueCode.CRITERION_SPAN_SEMANTICS_MISMATCH,
    )


@pytest.mark.parametrize(
    ("query", "text"),
    [
        ("有库存这个条件不需要", "有库存"),
        ("有库存的条件不需要", "有库存"),
        ("现货不要求", "现货"),
        ("在售不是必须", "在售"),
    ],
)
def test_postpositive_stock_negation_cannot_be_spoofed(query: str, text: str) -> None:
    result = validate_interpreted_request(
        query,
        InterpretedRequest(
            required=(StockRequired(source_span=_span_for(query, text)),),
            parser_version="malicious-v1",
        ),
    )

    assert tuple(issue.code for issue in result.issues) == (
        IntentIssueCode.CRITERION_SPAN_SEMANTICS_MISMATCH,
    )


def test_postpositive_exclusions_remain_explicit_required_constraints() -> None:
    query = "翻新和二手都不要"
    result = validate_interpreted_request(
        query,
        InterpretedRequest(
            required=(
                Exclusion(value="翻新", source_span=_span_for(query, "翻新")),
                Exclusion(value="二手", source_span=_span_for(query, "二手")),
            ),
            parser_version="rules-zh-cn-v1",
        ),
    )

    assert result.is_valid


def test_duplicate_budget_constraint_is_rejected() -> None:
    query = "预算1000元的笔记本"
    budget = BudgetMax(
        mode="maximum",
        target_amount=Decimal("1000"),
        lower_bound=None,
        upper_bound=Decimal("1000"),
        currency="CNY",
        source_span=_span_for(query, "预算1000元"),
    )

    result = validate_interpreted_request(
        query,
        InterpretedRequest(
            required=(budget, budget),
            parser_version="malicious-v1",
        ),
    )

    assert IntentIssueCode.DUPLICATE_CORE_CONSTRAINT in tuple(issue.code for issue in result.issues)


@pytest.mark.parametrize(
    ("query", "required", "preferred"),
    [
        (
            "不要翻新",
            (
                Exclusion(value="翻新", source_span=SourceSpan(start=2, end=4, text="翻新")),
                Exclusion(
                    value="refurbished",
                    source_span=SourceSpan(start=2, end=4, text="翻新"),
                ),
            ),
            (),
        ),
        (
            "轻薄",
            (),
            (
                PreferredCriterion(
                    value="lightweight",
                    source_span=SourceSpan(start=0, end=2, text="轻薄"),
                ),
                PreferredCriterion(
                    value="轻薄",
                    source_span=SourceSpan(start=0, end=2, text="轻薄"),
                ),
            ),
        ),
    ],
)
def test_duplicate_or_synonymous_soft_criteria_are_rejected(
    query: str,
    required: tuple[Exclusion, ...],
    preferred: tuple[PreferredCriterion, ...],
) -> None:
    result = validate_interpreted_request(
        query,
        InterpretedRequest(
            required=required,
            preferred=preferred,
            parser_version="malicious-v1",
        ),
    )

    assert IntentIssueCode.DUPLICATE_CORE_CONSTRAINT in tuple(issue.code for issue in result.issues)


def test_excluded_semantic_cannot_also_be_preferred() -> None:
    query = "不要轻薄，但要轻薄"  # noqa: RUF001
    interpreted = InterpretedRequest(
        required=(
            Exclusion(
                value="轻薄",
                source_span=SourceSpan(start=2, end=4, text="轻薄"),
            ),
        ),
        preferred=(
            PreferredCriterion(
                value="lightweight",
                source_span=SourceSpan(start=7, end=9, text="轻薄"),
            ),
        ),
        parser_version="malicious-v1",
    )

    result = validate_interpreted_request(query, interpreted)

    assert IntentIssueCode.DUPLICATE_CORE_CONSTRAINT in tuple(issue.code for issue in result.issues)


@pytest.mark.parametrize(
    ("query", "required", "preferred"),
    [
        (
            "推荐翻新笔记本",
            (
                Exclusion(
                    value="翻新",
                    source_span=SourceSpan(start=2, end=4, text="翻新"),
                ),
            ),
            (),
        ),
        (
            "不要轻薄的笔记本",
            (),
            (
                PreferredCriterion(
                    value="lightweight",
                    source_span=SourceSpan(start=2, end=4, text="轻薄"),
                ),
            ),
        ),
    ],
)
def test_exclusion_and_preference_polarity_cannot_be_spoofed(
    query: str,
    required: tuple[Exclusion, ...],
    preferred: tuple[PreferredCriterion, ...],
) -> None:
    result = validate_interpreted_request(
        query,
        InterpretedRequest(
            required=required,
            preferred=preferred,
            parser_version="malicious-v1",
        ),
    )

    assert tuple(issue.code for issue in result.issues) == (
        IntentIssueCode.CRITERION_SPAN_SEMANTICS_MISMATCH,
    )
    assert result.interpreted_request is None


def test_spoofed_discriminator_fails_closed_even_with_a_valid_span() -> None:
    budget = BudgetMax(
        mode="maximum",
        target_amount=Decimal("1000"),
        lower_bound=None,
        upper_bound=Decimal("1000"),
        currency="CNY",
        source_span=SourceSpan(start=0, end=7, text="预算1000元"),
    )
    object.__setattr__(budget, "kind", "preferred")
    interpreted = InterpretedRequest(required=(budget,), parser_version="malicious-v1")

    result = validate_interpreted_request("预算1000元", interpreted)

    assert tuple(issue.code for issue in result.issues) == (IntentIssueCode.DISCRIMINATOR_MISMATCH,)
    assert result.interpreted_request is None


class _HostileDiscriminator:
    def __ne__(self, other: object) -> bool:
        raise RuntimeError("hostile discriminator")


class _AlwaysEqualDiscriminator:
    def __ne__(self, other: object) -> bool:
        return False


def test_uninitialized_interpreted_request_returns_a_stable_issue() -> None:
    interpreted = object.__new__(InterpretedRequest)

    result = validate_interpreted_request("相机", interpreted)

    assert tuple(issue.code for issue in result.issues) == (
        IntentIssueCode.INVALID_INTERPRETED_REQUEST,
    )


def test_uninitialized_source_span_returns_a_stable_issue() -> None:
    span = object.__new__(SourceSpan)
    interpreted = InterpretedRequest(
        preferred=(PreferredCriterion(value="lightweight", source_span=span),),
        parser_version="malicious-v1",
    )

    result = validate_interpreted_request("轻薄", interpreted)

    assert tuple(issue.code for issue in result.issues) == (IntentIssueCode.INVALID_SOURCE_SPAN,)


def test_uninitialized_criterion_returns_a_stable_issue() -> None:
    budget = object.__new__(BudgetMax)
    interpreted = InterpretedRequest(
        required=(budget,),
        parser_version="malicious-v1",
    )

    result = validate_interpreted_request("预算1000元", interpreted)

    assert tuple(issue.code for issue in result.issues) == (
        IntentIssueCode.INVALID_INTERPRETED_REQUEST,
    )


def test_hostile_discriminator_returns_a_stable_issue() -> None:
    preferred = PreferredCriterion(
        value="lightweight",
        source_span=SourceSpan(start=0, end=2, text="轻薄"),
    )
    object.__setattr__(preferred, "kind", _HostileDiscriminator())
    interpreted = InterpretedRequest(
        preferred=(preferred,),
        parser_version="malicious-v1",
    )

    result = validate_interpreted_request("轻薄", interpreted)

    assert tuple(issue.code for issue in result.issues) == (IntentIssueCode.DISCRIMINATOR_MISMATCH,)


def test_non_string_discriminator_cannot_spoof_equality() -> None:
    preferred = PreferredCriterion(
        value="lightweight",
        source_span=SourceSpan(start=0, end=2, text="轻薄"),
    )
    object.__setattr__(preferred, "kind", _AlwaysEqualDiscriminator())
    interpreted = InterpretedRequest(
        preferred=(preferred,),
        parser_version="malicious-v1",
    )

    result = validate_interpreted_request("轻薄", interpreted)

    assert tuple(issue.code for issue in result.issues) == (IntentIssueCode.DISCRIMINATOR_MISMATCH,)
