"""Immutable intent models and fail-closed source-span validation.

The interpreter adapter is an untrusted producer at this boundary.  These
models deliberately remain simple values; :func:`validate_interpreted_request`
independently checks their runtime shape, discriminants, partition, and source
evidence before the application may consume an interpreted request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Literal, cast


@dataclass(frozen=True, slots=True)
class SourceSpan:
    """A half-open Unicode code-point range over the trimmed original query."""

    start: int
    end: int
    text: str


@dataclass(frozen=True, slots=True)
class BudgetCalculation:
    """Model-authored numeric proof for one semantically interpreted budget.

    Natural-language meaning remains the interpreter's responsibility.  This
    value lets the trust boundary verify quoted numeric evidence and arithmetic
    without maintaining a vocabulary of equivalent user phrases.
    """

    base_amount: Decimal
    allowance_amounts: tuple[Decimal, ...] = ()
    explicit_maximum: Decimal | None = None


@dataclass(frozen=True, slots=True)
class BudgetMax:
    """A typed price constraint whose target and inclusive bounds are explicit.

    The interpreter only supplies contract inputs; the system derives the
    bounds from the mode and rejects any interpreter-computed bound:
      - ``maximum``: price ≤ ``upper_bound == target_amount``
        (``lower_bound`` must be None)
      - ``around``: price ∈ [target·0.9, target·1.1]
      - ``range``:   price ∈ [``lower_bound``, ``upper_bound``]
        (``target_amount`` is the upper bound)
    """

    mode: Literal["maximum", "around", "range"]
    target_amount: Decimal
    lower_bound: Decimal | None
    upper_bound: Decimal
    source_span: SourceSpan
    currency: str | None = None
    calculation: BudgetCalculation | None = None
    kind: Literal["budget_max"] = field(default="budget_max", init=False)


@dataclass(frozen=True, slots=True)
class TargetCategory:
    """A product category that every result must satisfy."""

    category: str
    source_span: SourceSpan
    kind: Literal["target_category"] = field(default="target_category", init=False)


@dataclass(frozen=True, slots=True)
class StockRequired:
    """The user's explicit stock requirement.

    This is distinct from M0's unconditional in-stock offer eligibility rule.
    """

    source_span: SourceSpan
    kind: Literal["stock_required"] = field(default="stock_required", init=False)


@dataclass(frozen=True, slots=True)
class Exclusion:
    """A product term or attribute the user explicitly excluded."""

    value: str
    source_span: SourceSpan
    kind: Literal["exclusion"] = field(default="exclusion", init=False)


@dataclass(frozen=True, slots=True)
class PreferredCriterion:
    """A soft criterion that may affect ordering but never candidate membership."""

    value: str
    source_span: SourceSpan
    kind: Literal["preferred"] = field(default="preferred", init=False)


Preferred = PreferredCriterion
type RequiredConstraint = BudgetMax | TargetCategory | StockRequired | Exclusion
type IntentCriterion = RequiredConstraint | PreferredCriterion


@dataclass(frozen=True, slots=True)
class InterpretedRequest:
    """One adapter interpretation, not trusted until independently validated."""

    required: tuple[RequiredConstraint, ...] = ()
    preferred: tuple[PreferredCriterion, ...] = ()
    parser_version: str = "unavailable"


class IntentIssueCode(StrEnum):
    """Stable machine-readable failures emitted by the intent safety boundary."""

    PROVIDER_UNAVAILABLE = "intent.provider-unavailable"
    PROVIDER_RESPONSE_INVALID = "intent.provider-response-invalid"
    REQUIRED_BASELINE_FAILED = "intent.required-baseline-failed"
    REQUIRED_INCOMPLETE = "intent.required-incomplete"
    INVALID_INTERPRETED_REQUEST = "intent.invalid-interpreted-request"
    INVALID_COLLECTION = "intent.invalid-collection"
    INVALID_PARSER_VERSION = "intent.invalid-parser-version"
    INVALID_REQUIRED_VARIANT = "intent.invalid-required-variant"
    INVALID_PREFERRED_VARIANT = "intent.invalid-preferred-variant"
    DUPLICATE_CORE_CONSTRAINT = "intent.duplicate-core-constraint"
    AMBIGUOUS_CATEGORY = "intent.ambiguous-category"
    PREFERRED_UPGRADED_TO_REQUIRED = "intent.preferred-upgraded-to-required"
    REQUIRED_DEMOTED_TO_PREFERRED = "intent.required-demoted-to-preferred"
    DISCRIMINATOR_MISMATCH = "intent.discriminator-mismatch"
    INVALID_CRITERION_VALUE = "intent.invalid-criterion-value"
    INVALID_BUDGET_AMOUNT = "intent.invalid-budget-amount"
    INVALID_BUDGET_CURRENCY = "intent.invalid-budget-currency"
    INVALID_BUDGET_SPAN_SEMANTICS = "intent.invalid-budget-span-semantics"
    BUDGET_AMOUNT_SPAN_MISMATCH = "intent.budget-amount-span-mismatch"
    BUDGET_CURRENCY_SPAN_MISMATCH = "intent.budget-currency-span-mismatch"
    CRITERION_SPAN_SEMANTICS_MISMATCH = "intent.criterion-span-semantics-mismatch"
    INVALID_SOURCE_SPAN = "intent.invalid-source-span"
    SPAN_INDEX_INVALID = "intent.span-index-invalid"
    SPAN_EMPTY = "intent.span-empty"
    SPAN_REVERSED = "intent.span-reversed"
    SPAN_OUT_OF_BOUNDS = "intent.span-out-of-bounds"
    SPAN_TEXT_INVALID = "intent.span-text-invalid"
    SPAN_TEXT_MISMATCH = "intent.span-text-mismatch"


@dataclass(frozen=True, slots=True)
class IntentValidationIssue:
    """A deterministic diagnostic that is safe to expose outside the adapter."""

    code: IntentIssueCode
    location: str
    message: str


@dataclass(frozen=True, slots=True)
class IntentValidationResult:
    """Only a valid result carries an interpreted request forward."""

    interpreted_request: InterpretedRequest | None
    issues: tuple[IntentValidationIssue, ...]

    @property
    def is_valid(self) -> bool:
        return self.interpreted_request is not None and not self.issues


_REQUIRED_TYPES = (BudgetMax, TargetCategory, StockRequired, Exclusion)
_EXPECTED_KIND = {
    BudgetMax: "budget_max",
    TargetCategory: "target_category",
    StockRequired: "stock_required",
    Exclusion: "exclusion",
    PreferredCriterion: "preferred",
}
_PARSER_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
# Budget evidence is matched structurally, not by an expanding phrase
# allowlist: an amount token, optionally followed by a currency and/or an
# upper-limit or approximate marker, or two amounts joined by a range
# separator. A bare number without any of these is never budget evidence.
_BUDGET_CN_DIGITS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_BUDGET_AMOUNT_PATTERN = r"(?:[0-9]+[万千]|[一二两三四五六七八九][万千]|[0-9]+(?:\.[0-9]+)?)"
_BUDGET_CURRENCY_PATTERN = (
    r"(?:人民币|美元|美金|欧元|英镑|元|"
    r"(?<![A-Za-z])(?:CNY|USD|EUR|GBP)(?![A-Za-z]))"
)
_BUDGET_CURRENCY_TOKEN_RE = re.compile(
    r"(?:人民币|美元|美金|欧元|英镑|元|"
    r"(?<![A-Za-z])(?:CNY|USD|EUR|GBP)(?![A-Za-z]))",
    re.IGNORECASE,
)
_BUDGET_MARKER_PATTERN = (
    r"(?:以下|以内|之内|封顶|不超过|最多|不高于|"
    r"左右|上下|附近|大约|约|大概|差不多)"
)
_BUDGET_UP_LIMIT_MARKERS = ("以下", "以内", "之内", "封顶", "不超过", "最多", "不高于")
_BUDGET_AROUND_MARKERS = ("左右", "上下", "附近", "大约", "约", "大概", "差不多")
_BUDGET_RANGE_SEPARATORS = ("到", "至", "~", "—", "-", "－", "…")  # noqa: RUF001
_BUDGET_PREFIX_PATTERN = (
    r"预算\s*(?:(?:改成|改为|调整为|调到|不超过|最多|为|是|[:：])\s*)?"  # noqa: RUF001
)
# An amount must not be followed by unrecognized text (e.g. an unknown
# currency like 日元); only boundaries and connectors may follow it.
_BUDGET_TAIL_RE = re.compile(
    r"(?:$|[的,，。;；、!?！？]|\s*(?:且|并且|并|或|和|但|可是|有库存|现货|在售))",  # noqa: RUF001
    re.IGNORECASE,
)
_BUDGET_AMOUNT_TOKEN = re.compile(_BUDGET_AMOUNT_PATTERN)
_BUDGET_PREFIX_RE = re.compile(_BUDGET_PREFIX_PATTERN, re.IGNORECASE)
_BUDGET_CURRENCY_RE = re.compile(_BUDGET_CURRENCY_PATTERN, re.IGNORECASE)
_BUDGET_MARKER_RE = re.compile(_BUDGET_MARKER_PATTERN, re.IGNORECASE)
_BUDGET_NEGATION_PREFIX = (
    r"(?:不是|不要(?:这个|这项|该)?(?:使用|采用)?|无所谓|"
    r"我?不考虑(?:使用|采用)?|我?不采用|"
    r"不接受(?:使用|采用)?(?:这个|这项|该)?|"
    r"不采纳(?:这个|这项|该)?|不用(?:这个|这项|该)?|"
    r"别(?:用|使用|采用)(?:这个|这项|该)?|"
    r"我?没有(?:设置|设定|这个|这项|该)?|"
    r"没(?:有)?(?:设置|设定|这个|这项|该)?|"
    r"拒绝(?:使用|采用)?|取消(?:这个|这项|该)?|"
    r"放弃(?:这个|这项|该)?|忽略(?:这个|这项|该)?|"
    r"无需|不需要|不设|无|非)"
)
_NEGATED_BUDGET_QUERY = re.compile(
    r"(?:预算\s*(?:不是|不等于|不确定|不要求)|"
    rf"{_BUDGET_NEGATION_PREFIX}\s*预算)"
)
_BUDGET_NEGATIVE_CONTEXT = re.compile(
    r"(?:不(?:想|愿意?|打算|准备|接受|采纳|考虑|采用|使用|用|设|设置|设定|"
    r"要|需要|要求|是|等于|确定|作数)|"
    r"没(?:有|想|打算|准备|设置|设定)|"
    r"无(?:需|所谓)|别(?:用|使用|采用|设|设置|设定|定|要)|"
    r"拒绝|取消|放弃|忽略|可有可无|作废)"
)
_BUDGET_REJECTION_MARKERS = (
    "不要使用",
    "不要采用",
    "我不考虑",
    "不考虑",
    "我不采用",
    "不采用",
    "不接受",
    "不采纳",
    "不需要",
    "不要求",
    "不等于",
    "不确定",
    "别使用",
    "别采用",
    "别用",
    "无所谓",
    "我没有",
    "没有",
    "不是",
    "不要",
    "不用",
    "无需",
    "不设",
    "拒绝",
    "取消",
    "放弃",
    "忽略",
    "非",
)
_BUDGET_CURRENCY_CODES = {
    "CNY": "CNY",
    "EUR": "EUR",
    "GBP": "GBP",
    "USD": "USD",
    "人民币": "CNY",
    "元": "CNY",
    "欧元": "EUR",
    "美元": "USD",
    "美金": "USD",
    "英镑": "GBP",
}
_CATEGORY_BY_TEXT = {
    "笔记本电脑": "laptop",
    "轻薄本": "laptop",
    "笔记本": "laptop",
    "照相机": "camera",
    "相机": "camera",
    "耳机": "headphones",
    "手机": "phone",
    "📱": "phone",
}
_AGENT_CATEGORY_BY_TEXT = {
    **_CATEGORY_BY_TEXT,
    "智能手机": "phone",
    "平板电脑": "tablet",
    "平板": "tablet",
    "电脑": "laptop",
}
_STOCK_TEXTS = frozenset({"有库存", "现货", "在售"})
_EXCLUSION_TEXTS = {
    "lightweight": frozenset({"轻薄"}),
    "替换件": frozenset({"替换件"}),
    "翻新": frozenset({"翻新"}),
    "refurbished": frozenset({"翻新"}),
    "二手": frozenset({"二手"}),
    "配件": frozenset({"配件"}),
    "支架": frozenset({"支架"}),
    "贴纸": frozenset({"贴纸"}),
    "轻薄": frozenset({"轻薄"}),
}
_EXCLUSION_CANONICAL = {
    "lightweight": "lightweight",
    "替换件": "替换件",
    "翻新": "refurbished",
    "refurbished": "refurbished",
    "二手": "二手",
    "配件": "配件",
    "支架": "支架",
    "贴纸": "贴纸",
    "轻薄": "lightweight",
}
_PREFERRED_TEXTS = {
    "travel": frozenset({"适合出差"}),
    "long_battery": frozenset({"续航长", "长续航"}),
    "battery_endurance": frozenset({"续航要好", "续航好", "续航强", "续航优秀", "电池耐用"}),
    "gaming_performance": frozenset(
        {"游戏性能好", "游戏性能要好", "打游戏流畅", "游戏流畅", "适合游戏"}
    ),
    "lightweight": frozenset({"轻薄"}),
    "portable": frozenset({"便携"}),
    "轻薄": frozenset({"轻薄"}),
}
_PREFERRED_CANONICAL = {
    "travel": "travel",
    "long_battery": "long_battery",
    "battery_endurance": "battery_endurance",
    "gaming_performance": "gaming_performance",
    "lightweight": "lightweight",
    "portable": "portable",
    "轻薄": "lightweight",
}
_NEGATION_MARKERS = (
    "不是必须",
    "并非必须",
    "非必须",
    "不要求",
    "不需要",
    "不想要",
    "不考虑",
    "不用",
    "不要",
    "无需",
    "排除",
    "不含",
    "拒绝",
    "不是",
    "没有",
    "不",
    "没",
    "无",
    "非",
)
_EXCLUSION_MARKERS = (
    "不想要",
    "不考虑",
    "不要",
    "排除",
    "不含",
    "拒绝",
)
_POLARITY_BOUNDARIES = (
    ",",
    "，",  # noqa: RUF001
    "。",
    ";",
    "；",  # noqa: RUF001
    "!",
    "！",  # noqa: RUF001
    "?",
    "？",  # noqa: RUF001
    "的",
    "但是",
    "不过",
    "然而",
    "转而",
    "可是",
    "然后",
    "另外",
    "但",
    "却",
    "而",
)
_SUFFIX_POLARITY_BOUNDARIES = tuple(
    separator for separator in _POLARITY_BOUNDARIES if separator != "的"
)
_COORDINATING_CONNECTORS = (
    "并且",
    "以及",
    "和",
    "或",
    "及",
    "与",
    "、",
    "且",
    "并",
)


def required_constraints_match(
    baseline: InterpretedRequest,
    candidate: InterpretedRequest,
) -> bool:
    """Compare validated Required constraints without trusting collection order."""

    try:
        if type(baseline) is not InterpretedRequest or type(candidate) is not InterpretedRequest:
            return False
        if type(baseline.required) is not tuple or type(candidate.required) is not tuple:
            return False
        return _required_signatures(baseline.required) == _required_signatures(candidate.required)
    except Exception:
        return False


def _required_signatures(
    required: tuple[RequiredConstraint, ...],
) -> tuple[tuple[int, int, str, object], ...]:
    signatures: list[tuple[int, int, str, object]] = []
    for criterion in required:
        span = criterion.source_span
        if type(criterion) is BudgetMax:
            value: object = (
                criterion.mode,
                criterion.target_amount,
                criterion.lower_bound,
                criterion.upper_bound,
                criterion.currency,
            )
        elif type(criterion) is TargetCategory:
            value = criterion.category
        elif type(criterion) is StockRequired:
            value = True
        elif type(criterion) is Exclusion:
            value = _EXCLUSION_CANONICAL.get(criterion.value, criterion.value)
        else:
            raise TypeError("unsupported Required constraint")
        signatures.append((span.start, span.end, criterion.kind, value))
    signatures.sort(key=lambda item: (item[0], item[1], item[2], repr(item[3])))
    return tuple(signatures)


def validate_source_span(
    query: str,
    span: object,
    *,
    location: str = "source_span",
) -> tuple[IntentValidationIssue, ...]:
    """Validate one span, converting every malformed adapter object to an issue."""

    try:
        return _validate_source_span(query, span, location=location)
    except Exception:
        return (
            _issue(
                IntentIssueCode.INVALID_SOURCE_SPAN,
                location,
                "source_span could not be validated safely",
            ),
        )


def _validate_source_span(
    query: str,
    span: object,
    *,
    location: str,
) -> tuple[IntentValidationIssue, ...]:
    """Validate one span against the trimmed, otherwise untouched query.

    Python string indexes are Unicode code-point indexes.  No case folding or
    Unicode normalization is performed, because either operation could change
    the evidence text or its offsets.
    """

    if type(span) is not SourceSpan:
        return (
            _issue(
                IntentIssueCode.INVALID_SOURCE_SPAN,
                location,
                "source_span must be a SourceSpan value",
            ),
        )

    if type(span.start) is not int or type(span.end) is not int:
        return (
            _issue(
                IntentIssueCode.SPAN_INDEX_INVALID,
                location,
                "source span indexes must be integers",
            ),
        )
    if span.start == span.end:
        return (
            _issue(
                IntentIssueCode.SPAN_EMPTY,
                location,
                "source span must not be empty",
            ),
        )
    if span.start > span.end:
        return (
            _issue(
                IntentIssueCode.SPAN_REVERSED,
                location,
                "source span end must follow start",
            ),
        )

    trimmed_query = query.strip()
    if span.start < 0 or span.end > len(trimmed_query):
        return (
            _issue(
                IntentIssueCode.SPAN_OUT_OF_BOUNDS,
                location,
                "source span is outside the trimmed query",
            ),
        )
    if type(span.text) is not str or not span.text:
        return (
            _issue(
                IntentIssueCode.SPAN_TEXT_INVALID,
                location,
                "source span text must be a non-empty string",
            ),
        )
    if trimmed_query[span.start : span.end] != span.text:
        return (
            _issue(
                IntentIssueCode.SPAN_TEXT_MISMATCH,
                location,
                "source span text does not equal the trimmed query slice",
            ),
        )
    return ()


def validate_interpreted_request(
    query: str,
    interpreted_request: object,
) -> IntentValidationResult:
    """Validate an adapter result without allowing hostile values to escape."""

    try:
        return _validate_interpreted_request(query, interpreted_request)
    except Exception:
        return IntentValidationResult(
            interpreted_request=None,
            issues=(
                _issue(
                    IntentIssueCode.INVALID_INTERPRETED_REQUEST,
                    "interpreted_request",
                    "adapter result could not be validated safely",
                ),
            ),
        )


def _validate_interpreted_request(
    query: str,
    interpreted_request: object,
) -> IntentValidationResult:
    """Independently validate an adapter result and fail closed on any issue."""

    issues: list[IntentValidationIssue] = []
    if type(interpreted_request) is not InterpretedRequest:
        issues.append(
            _issue(
                IntentIssueCode.INVALID_INTERPRETED_REQUEST,
                "interpreted_request",
                "adapter result must be an InterpretedRequest",
            )
        )
        return IntentValidationResult(interpreted_request=None, issues=tuple(issues))

    if type(query) is not str:
        issues.append(
            _issue(
                IntentIssueCode.INVALID_SOURCE_SPAN,
                "query",
                "query must be a string",
            )
        )
        return IntentValidationResult(interpreted_request=None, issues=tuple(issues))

    if (
        type(interpreted_request.parser_version) is not str
        or _PARSER_VERSION.fullmatch(interpreted_request.parser_version) is None
    ):
        issues.append(
            _issue(
                IntentIssueCode.INVALID_PARSER_VERSION,
                "parser_version",
                "parser_version must be a safe identifier",
            )
        )

    required = interpreted_request.required
    if type(required) is not tuple:
        issues.append(
            _issue(
                IntentIssueCode.INVALID_COLLECTION,
                "required",
                "required constraints must be an immutable tuple",
            )
        )
    else:
        for index, required_criterion in enumerate(required):
            issues.extend(_validate_required(query, required_criterion, index))
        issues.extend(_validate_required_cardinality(required))
        issues.extend(
            _validate_query_category_ambiguity(
                query,
                include_agent_categories=any(
                    type(item) is TargetCategory
                    and type(item.source_span) is SourceSpan
                    and item.source_span.text in _AGENT_CATEGORY_BY_TEXT
                    and item.source_span.text not in _CATEGORY_BY_TEXT
                    for item in required
                ),
            )
        )

    preferred = interpreted_request.preferred
    if type(preferred) is not tuple:
        issues.append(
            _issue(
                IntentIssueCode.INVALID_COLLECTION,
                "preferred",
                "preferred criteria must be an immutable tuple",
            )
        )
    else:
        for index, preferred_criterion in enumerate(preferred):
            issues.extend(_validate_preferred(query, preferred_criterion, index))
        issues.extend(_validate_preferred_cardinality(preferred))

    if type(required) is tuple and type(preferred) is tuple:
        issues.extend(_validate_partition_conflicts(required, preferred))

    if issues:
        return IntentValidationResult(interpreted_request=None, issues=tuple(issues))
    return IntentValidationResult(interpreted_request=interpreted_request, issues=())


def _validate_required(
    query: str,
    criterion: object,
    index: int,
) -> tuple[IntentValidationIssue, ...]:
    location = f"required[{index}]"
    if type(criterion) is PreferredCriterion:
        return (
            _issue(
                IntentIssueCode.PREFERRED_UPGRADED_TO_REQUIRED,
                location,
                "a preferred criterion cannot be promoted to Required",
            ),
        )
    if type(criterion) not in _REQUIRED_TYPES:
        return (
            _issue(
                IntentIssueCode.INVALID_REQUIRED_VARIANT,
                location,
                "unsupported Required constraint variant",
            ),
        )
    return _validate_criterion(query, cast("RequiredConstraint", criterion), location)


def _validate_preferred(
    query: str,
    criterion: object,
    index: int,
) -> tuple[IntentValidationIssue, ...]:
    location = f"preferred[{index}]"
    if type(criterion) in _REQUIRED_TYPES:
        return (
            _issue(
                IntentIssueCode.REQUIRED_DEMOTED_TO_PREFERRED,
                location,
                "a Required constraint cannot be placed in Preferred",
            ),
        )
    if type(criterion) is not PreferredCriterion:
        return (
            _issue(
                IntentIssueCode.INVALID_PREFERRED_VARIANT,
                location,
                "unsupported Preferred criterion variant",
            ),
        )
    return _validate_criterion(query, criterion, location)


def _validate_criterion(
    query: str,
    criterion: IntentCriterion,
    location: str,
) -> tuple[IntentValidationIssue, ...]:
    issues: list[IntentValidationIssue] = []
    span_issues = validate_source_span(
        query,
        criterion.source_span,
        location=f"{location}.source_span",
    )
    expected_kind = _EXPECTED_KIND[type(criterion)]
    kind_value: object = criterion.kind
    if type(kind_value) is not str or kind_value != expected_kind:
        issues.append(
            _issue(
                IntentIssueCode.DISCRIMINATOR_MISMATCH,
                f"{location}.kind",
                f"criterion discriminator must be {expected_kind}",
            )
        )

    if type(criterion) is BudgetMax:
        amounts = (criterion.target_amount, criterion.upper_bound)
        amount_is_invalid = any(
            type(amount) is not Decimal or not amount.is_finite() or amount <= 0
            for amount in amounts
        )
        lower_is_invalid = criterion.lower_bound is not None and (
            type(criterion.lower_bound) is not Decimal
            or not criterion.lower_bound.is_finite()
            or criterion.lower_bound <= 0
        )
        mode_is_invalid = criterion.mode not in {"maximum", "around", "range"}
        bounds_are_invalid = (
            mode_is_invalid
            or lower_is_invalid
            or (
                criterion.mode == "maximum"
                and (
                    criterion.lower_bound is not None
                    or criterion.target_amount != criterion.upper_bound
                )
            )
            or (
                criterion.mode == "around"
                and (
                    criterion.lower_bound != criterion.target_amount * Decimal("0.90")
                    or criterion.upper_bound != criterion.target_amount * Decimal("1.10")
                )
            )
            or (
                criterion.mode == "range"
                and (
                    criterion.lower_bound is None
                    or criterion.upper_bound <= criterion.lower_bound
                    or criterion.target_amount != criterion.upper_bound
                )
            )
        )
        calculation = criterion.calculation
        calculation_is_invalid = calculation is not None and (
            type(calculation) is not BudgetCalculation
            or type(calculation.base_amount) is not Decimal
            or not calculation.base_amount.is_finite()
            or calculation.base_amount <= 0
            or type(calculation.allowance_amounts) is not tuple
            or len(calculation.allowance_amounts) > 2
            or any(
                type(amount) is not Decimal or not amount.is_finite() or amount <= 0
                for amount in calculation.allowance_amounts
            )
            or (
                calculation.explicit_maximum is not None
                and (
                    type(calculation.explicit_maximum) is not Decimal
                    or not calculation.explicit_maximum.is_finite()
                    or calculation.explicit_maximum <= 0
                )
            )
            or criterion.mode != "maximum"
            or criterion.target_amount
            != (
                calculation.explicit_maximum
                if calculation.explicit_maximum is not None
                else calculation.base_amount
                + (max(calculation.allowance_amounts) if calculation.allowance_amounts else 0)
            )
        )
        if amount_is_invalid:
            issues.append(
                _issue(
                    IntentIssueCode.INVALID_BUDGET_AMOUNT,
                    f"{location}.target_amount",
                    "budget target and upper bound must be finite positive Decimals",
                )
            )
        if bounds_are_invalid:
            issues.append(
                _issue(
                    IntentIssueCode.INVALID_BUDGET_AMOUNT,
                    f"{location}.lower_bound",
                    "budget mode, target, and inclusive bounds are inconsistent",
                )
            )
        if calculation_is_invalid:
            issues.append(
                _issue(
                    IntentIssueCode.INVALID_BUDGET_AMOUNT,
                    f"{location}.calculation",
                    "budget calculation must be finite, positive, bounded, "
                    "and arithmetically exact",
                )
            )
        currency_is_valid = criterion.currency is None or _is_currency_code(criterion.currency)
        if not currency_is_valid:
            issues.append(
                _issue(
                    IntentIssueCode.INVALID_BUDGET_CURRENCY,
                    f"{location}.currency",
                    "budget currency must be an uppercase three-letter code or None",
                )
            )
        if (
            not span_issues
            and not amount_is_invalid
            and not bounds_are_invalid
            and not calculation_is_invalid
            and currency_is_valid
        ):
            issues.extend(_validate_budget_span_binding(query, criterion, location))
    elif type(criterion) is TargetCategory:
        value_issues = _validate_non_empty_value(
            criterion.category,
            f"{location}.category",
        )
        issues.extend(value_issues)
        if not span_issues and not value_issues:
            issues.extend(_validate_category_span_binding(query, criterion, location))
    elif type(criterion) is StockRequired:
        if not span_issues:
            issues.extend(_validate_stock_span_binding(query, criterion, location))
    elif type(criterion) is Exclusion:
        value_issues = _validate_non_empty_value(criterion.value, f"{location}.value")
        issues.extend(value_issues)
        if not span_issues and not value_issues:
            issues.extend(_validate_exclusion_span_binding(query, criterion, location))
    elif type(criterion) is PreferredCriterion:
        value_issues = _validate_non_empty_value(criterion.value, f"{location}.value")
        issues.extend(value_issues)
        if not span_issues and not value_issues:
            issues.extend(_validate_preferred_span_binding(query, criterion, location))

    issues.extend(span_issues)
    return tuple(issues)


def _validate_required_cardinality(
    required: tuple[RequiredConstraint, ...],
) -> tuple[IntentValidationIssue, ...]:
    issues: list[IntentValidationIssue] = []
    if sum(type(item) is BudgetMax for item in required) > 1:
        issues.append(
            _issue(
                IntentIssueCode.DUPLICATE_CORE_CONSTRAINT,
                "required",
                "at most one BudgetMax constraint is allowed",
            )
        )
    if sum(type(item) is StockRequired for item in required) > 1:
        issues.append(
            _issue(
                IntentIssueCode.DUPLICATE_CORE_CONSTRAINT,
                "required",
                "at most one StockRequired constraint is allowed",
            )
        )
    if sum(type(item) is TargetCategory for item in required) > 1:
        issues.append(
            _issue(
                IntentIssueCode.AMBIGUOUS_CATEGORY,
                "required",
                "at most one target category may be selected",
            )
        )
    exclusion_keys = tuple(
        _EXCLUSION_CANONICAL.get(item.value, item.value)
        for item in required
        if type(item) is Exclusion and type(item.value) is str
    )
    if len(exclusion_keys) != len(set(exclusion_keys)):
        issues.append(
            _issue(
                IntentIssueCode.DUPLICATE_CORE_CONSTRAINT,
                "required",
                "semantically duplicate exclusions are not allowed",
            )
        )
    return tuple(issues)


def _validate_preferred_cardinality(
    preferred: tuple[PreferredCriterion, ...],
) -> tuple[IntentValidationIssue, ...]:
    keys = tuple(
        _PREFERRED_CANONICAL.get(item.value, item.value)
        for item in preferred
        if type(item) is PreferredCriterion and type(item.value) is str
    )
    if len(keys) == len(set(keys)):
        return ()
    return (
        _issue(
            IntentIssueCode.DUPLICATE_CORE_CONSTRAINT,
            "preferred",
            "semantically duplicate preferences are not allowed",
        ),
    )


def _validate_partition_conflicts(
    required: tuple[RequiredConstraint, ...],
    preferred: tuple[PreferredCriterion, ...],
) -> tuple[IntentValidationIssue, ...]:
    exclusions = {
        _EXCLUSION_CANONICAL.get(item.value, item.value)
        for item in required
        if type(item) is Exclusion and type(item.value) is str
    }
    preferences = {
        _PREFERRED_CANONICAL.get(item.value, item.value)
        for item in preferred
        if type(item) is PreferredCriterion and type(item.value) is str
    }
    if exclusions.isdisjoint(preferences):
        return ()
    return (
        _issue(
            IntentIssueCode.DUPLICATE_CORE_CONSTRAINT,
            "interpreted_request",
            "an excluded semantic cannot also be preferred",
        ),
    )


def _validate_query_category_ambiguity(
    query: str,
    *,
    include_agent_categories: bool,
) -> tuple[IntentValidationIssue, ...]:
    occupied: list[tuple[int, int]] = []
    categories: set[str] = set()
    categories_by_text = _AGENT_CATEGORY_BY_TEXT if include_agent_categories else _CATEGORY_BY_TEXT
    for phrase, category in categories_by_text.items():
        for found in re.finditer(re.escape(phrase), query.strip()):
            span = (found.start(), found.end())
            if any(_overlaps(span, existing) for existing in occupied):
                continue
            occupied.append(span)
            if not _has_negative_scope(
                query.strip(),
                found.start(),
                end=found.end(),
            ):
                categories.add(category)
    if len(categories) <= 1:
        return ()
    return (
        _issue(
            IntentIssueCode.AMBIGUOUS_CATEGORY,
            "query",
            "multiple positive target categories are ambiguous",
        ),
    )


def _validate_category_span_binding(
    query: str,
    criterion: TargetCategory,
    location: str,
) -> tuple[IntentValidationIssue, ...]:
    span = criterion.source_span
    trimmed_query = query.strip()
    categories_by_text = (
        _AGENT_CATEGORY_BY_TEXT
        if span.text in _AGENT_CATEGORY_BY_TEXT and span.text not in _CATEGORY_BY_TEXT
        else _CATEGORY_BY_TEXT
    )
    covering: list[tuple[int, int, str]] = []
    for phrase in categories_by_text:
        for found in re.finditer(re.escape(phrase), trimmed_query):
            if found.start() <= span.start and span.end <= found.end():
                covering.append((found.start(), found.end(), phrase))
    maximal = max(
        covering,
        key=lambda item: (item[1] - item[0], -item[0]),
        default=None,
    )
    if (
        categories_by_text.get(span.text) != criterion.category
        or maximal is None
        or (span.start, span.end, span.text) != maximal
        or _has_negative_scope(trimmed_query, maximal[0], end=maximal[1])
    ):
        return (
            _issue(
                IntentIssueCode.CRITERION_SPAN_SEMANTICS_MISMATCH,
                f"{location}.category",
                "target category is not a maximal, positive category reference",
            ),
        )
    return ()


def _validate_stock_span_binding(
    query: str,
    criterion: StockRequired,
    location: str,
) -> tuple[IntentValidationIssue, ...]:
    if criterion.source_span.text not in _STOCK_TEXTS or _has_negative_scope(
        query.strip(),
        criterion.source_span.start,
        end=criterion.source_span.end,
    ):
        return (
            _issue(
                IntentIssueCode.CRITERION_SPAN_SEMANTICS_MISMATCH,
                f"{location}.source_span",
                "stock requirement is not a positive approved stock phrase",
            ),
        )
    return ()


def _validate_exclusion_span_binding(
    query: str,
    criterion: Exclusion,
    location: str,
) -> tuple[IntentValidationIssue, ...]:
    if not _has_negative_scope(
        query.strip(),
        criterion.source_span.start,
        end=criterion.source_span.end,
        markers=_EXCLUSION_MARKERS,
    ):
        return (
            _issue(
                IntentIssueCode.CRITERION_SPAN_SEMANTICS_MISMATCH,
                f"{location}.value",
                "exclusion is not bound to explicit negative scope",
            ),
        )
    return ()


def _validate_preferred_span_binding(
    query: str,
    criterion: PreferredCriterion,
    location: str,
) -> tuple[IntentValidationIssue, ...]:
    if _has_negative_scope(
        query.strip(),
        criterion.source_span.start,
        end=criterion.source_span.end,
    ):
        return (
            _issue(
                IntentIssueCode.CRITERION_SPAN_SEMANTICS_MISMATCH,
                f"{location}.value",
                "preference is inside negative scope",
            ),
        )
    return ()


def _has_negative_scope(
    query: str,
    start: int,
    *,
    end: int | None = None,
    markers: tuple[str, ...] = _NEGATION_MARKERS,
) -> bool:
    prefix = query[:start]
    boundary_end = max(
        (
            index + len(separator)
            for separator in _POLARITY_BOUNDARIES
            if (index := prefix.rfind(separator)) >= 0
        ),
        default=0,
    )
    context = prefix[boundary_end:].strip()
    if any(context.endswith(marker) for marker in markers):
        return True
    connector = "(?:" + "|".join(re.escape(item) for item in _COORDINATING_CONNECTORS) + ")"
    marker_pattern = "|".join(
        re.escape(marker) for marker in sorted(markers, key=len, reverse=True)
    )
    if end is not None:
        tail = query[end:]
        boundary_start = min(
            (
                index
                for separator in _SUFFIX_POLARITY_BOUNDARIES
                if (index := tail.find(separator)) >= 0
            ),
            default=len(tail),
        )
        suffix = tail[:boundary_start].strip()
        if any(suffix.endswith(marker) for marker in markers) or re.match(
            rf"^(?:也|都|均|全|一律|同样)?(?:{marker_pattern})(?:$|{connector})",
            suffix,
        ):
            return True
    scoped_markers = tuple(marker for marker in markers if len(marker) > 1)
    if scoped_markers:
        scoped_marker_pattern = "|".join(
            re.escape(marker) for marker in sorted(scoped_markers, key=len, reverse=True)
        )
        if re.search(rf"(?:^|{connector})\s*(?:{scoped_marker_pattern})", context):
            return True

    if any(marker in markers for marker in ("不", "没", "无", "非")):
        phrases = tuple(
            sorted(
                {
                    *_CATEGORY_BY_TEXT,
                    *_STOCK_TEXTS,
                    *(text for values in _EXCLUSION_TEXTS.values() for text in values),
                    *(text for values in _PREFERRED_TEXTS.values() for text in values),
                },
                key=len,
                reverse=True,
            )
        )
        phrase_pattern = "|".join(re.escape(phrase) for phrase in phrases)
        if re.search(
            rf"(?:^|{connector})\s*(?:不|没|无|非)\s*(?:{phrase_pattern})",
            context,
        ):
            return True
    return (
        re.search(
            r"(?:不要|不想要|不考虑|拒绝|排除)(?:这个|这款|该)?\Z",
            context,
        )
        is not None
    )


@dataclass(frozen=True, slots=True)
class _BudgetContract:
    """One structural budget expression found verbatim in a query."""

    start: int
    end: int
    text: str
    amounts: tuple[Decimal, ...]
    currencies: tuple[str, ...]
    mode: Literal["maximum", "around", "range"]


def _amount_value(token: str) -> Decimal | None:
    """Parse one amount token into a positive Decimal.

    Supports Arabic decimals and the common 万/千 unit forms (1万, 一万,
    3000, 三千). Combined forms such as 一万二 are not supported.
    """

    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", token):
        value = Decimal(token)
        return value if value.is_finite() and value > 0 else None
    match = re.fullmatch(r"([0-9]+|[一二两三四五六七八九])([万千])", token)
    if match is None:
        return None
    head_text, unit = match.group(1), match.group(2)
    head = Decimal(head_text) if head_text.isdigit() else Decimal(_BUDGET_CN_DIGITS[head_text])
    return head * (Decimal("10000") if unit == "万" else Decimal("1000"))


def budget_amount_evidence(text: str) -> tuple[Decimal, ...]:
    """Return ordered numeric budget evidence without interpreting surrounding words."""

    if type(text) is not str:
        raise TypeError("budget evidence text must be a string")
    return tuple(
        value
        for token in _BUDGET_AMOUNT_TOKEN.findall(text)
        if (value := _amount_value(token)) is not None
    )


def budget_currency_evidence(text: str) -> tuple[str, ...]:
    """Return the distinct ISO currencies explicitly named in budget text."""

    if type(text) is not str:
        raise TypeError("budget evidence text must be a string")
    tokens = _BUDGET_CURRENCY_TOKEN_RE.findall(text)
    codes = {
        _BUDGET_CURRENCY_CODES[token.upper() if token.isascii() else token] for token in tokens
    }
    return tuple(sorted(codes))


def budget_source_spans(text: str) -> tuple[SourceSpan, ...]:
    """Return each independently validated structural budget expression."""

    if type(text) is not str:
        raise TypeError("budget source text must be a string")
    return tuple(
        SourceSpan(start=item.start, end=item.end, text=item.text)
        for item in _budget_contracts(text)
    )


def _span_currencies(text: str) -> tuple[str, ...]:
    return budget_currency_evidence(text)


def _budget_contracts(query: str) -> tuple[_BudgetContract, ...]:
    """Find every structural budget expression in a trimmed query.

    A budget expression is an amount token (with optional 万/千 unit),
    optionally followed by a currency and/or an upper-limit or approximate
    marker, or two amounts joined by a range separator. A bare number with
    none of these is never treated as budget evidence.
    """

    range_pattern = re.compile(
        rf"{_BUDGET_AMOUNT_PATTERN}\s*(?:{_BUDGET_CURRENCY_PATTERN})?\s*"
        rf"(?:到|至|~|—|-|－|…)\s*"  # noqa: RUF001
        rf"{_BUDGET_AMOUNT_PATTERN}\s*(?:{_BUDGET_CURRENCY_PATTERN})?",
        re.IGNORECASE,
    )
    single_pattern = re.compile(
        rf"(?:{_BUDGET_PREFIX_PATTERN})?"
        rf"{_BUDGET_AMOUNT_PATTERN}\s*(?:{_BUDGET_CURRENCY_PATTERN})?\s*"
        rf"(?:{_BUDGET_MARKER_PATTERN})?",
        re.IGNORECASE,
    )
    candidates: list[tuple[int, int]] = []
    for match in (
        *range_pattern.finditer(query),
        *single_pattern.finditer(query),
    ):
        text = match.group()
        if not text:
            continue
        has_evidence = (
            _BUDGET_PREFIX_RE.search(text) is not None
            or _BUDGET_CURRENCY_RE.search(text) is not None
            or _BUDGET_MARKER_RE.search(text) is not None
            or any(separator in text for separator in _BUDGET_RANGE_SEPARATORS)
        )
        if not has_evidence:
            continue  # a bare number is not budget evidence
        match_start, match_end = match.start(), match.end()
        tail = query[match_end:]
        if tail and _BUDGET_TAIL_RE.match(tail) is None:
            continue  # amount runs into unrecognized text, e.g. an unknown currency
        if any(match_start < end_ and start_ < match_end for start_, end_ in candidates):
            continue  # already covered by a longer candidate
        candidates.append((match_start, match_end))

    merged: list[tuple[int, int]] = []
    for start, end in sorted(candidates):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    contracts: list[_BudgetContract] = []
    for start, end in merged:
        text = query[start:end]
        amounts = tuple(
            value
            for token in _BUDGET_AMOUNT_TOKEN.findall(text)
            if (value := _amount_value(token)) is not None
        )
        currencies = _span_currencies(text)
        mode: Literal["maximum", "around", "range"] = (
            "range"
            if len(amounts) == 2
            and any(separator in text for separator in _BUDGET_RANGE_SEPARATORS)
            else "around"
            if any(marker in text for marker in _BUDGET_AROUND_MARKERS)
            else "maximum"
        )
        contracts.append(_BudgetContract(start, end, text, amounts, currencies, mode))
    return tuple(contracts)


def _validate_budget_span_binding(
    query: str,
    criterion: BudgetMax,
    location: str,
) -> tuple[IntentValidationIssue, ...]:
    """Prove that a budget contract's mode, amount, and currency are present in its evidence."""

    if criterion.calculation is not None:
        return _validate_model_budget_calculation_binding(criterion, location)

    issues: list[IntentValidationIssue] = []
    trimmed = query.strip()
    contracts = _budget_contracts(trimmed)
    if (
        _NEGATED_BUDGET_QUERY.search(query)
        or (
            len(contracts) == 1
            and _budget_has_negative_context(
                trimmed,
                contracts[0].start,
                contracts[0].end,
            )
        )
        or (
            len(contracts) == 1
            and _has_negative_scope(
                trimmed,
                contracts[0].start,
                end=contracts[0].end,
                markers=_BUDGET_REJECTION_MARKERS,
            )
        )
        or len(contracts) != 1
    ):
        return (
            _issue(
                IntentIssueCode.INVALID_BUDGET_SPAN_SEMANTICS,
                f"{location}.source_span",
                "source_span does not express a budget constraint",
            ),
        )
    contract = contracts[0]
    if (contract.start, contract.end, contract.text) != (
        criterion.source_span.start,
        criterion.source_span.end,
        criterion.source_span.text,
    ):
        return (
            _issue(
                IntentIssueCode.INVALID_BUDGET_SPAN_SEMANTICS,
                f"{location}.source_span",
                "source_span is not the maximal budget expression in the query",
            ),
        )

    if contract.mode == "range":
        if criterion.mode != "range" or len(contract.amounts) != 2:
            issues.append(
                _issue(
                    IntentIssueCode.INVALID_BUDGET_SPAN_SEMANTICS,
                    f"{location}.mode",
                    "budget mode does not match source_span semantics",
                )
            )
        elif (
            contract.amounts[0] != criterion.lower_bound
            or contract.amounts[1] != criterion.upper_bound
        ):
            issues.append(
                _issue(
                    IntentIssueCode.BUDGET_AMOUNT_SPAN_MISMATCH,
                    f"{location}.target_amount",
                    "budget amounts do not match the two numbers in source_span",
                )
            )
    else:
        if criterion.mode != contract.mode or len(contract.amounts) != 1:
            issues.append(
                _issue(
                    IntentIssueCode.INVALID_BUDGET_SPAN_SEMANTICS,
                    f"{location}.mode",
                    "budget mode does not match source_span semantics",
                )
            )
        elif contract.amounts[0] != criterion.target_amount:
            issues.append(
                _issue(
                    IntentIssueCode.BUDGET_AMOUNT_SPAN_MISMATCH,
                    f"{location}.target_amount",
                    "budget amount does not match exactly one number in source_span",
                )
            )

    if (
        len(contract.currencies) > 1
        or (contract.currencies and contract.currencies[0] != criterion.currency)
        or (not contract.currencies and criterion.currency is not None)
    ):
        issues.append(
            _issue(
                IntentIssueCode.BUDGET_CURRENCY_SPAN_MISMATCH,
                f"{location}.currency",
                "budget currency does not match the currency named in source_span",
            )
        )
    return tuple(issues)


def _validate_model_budget_calculation_binding(
    criterion: BudgetMax,
    location: str,
) -> tuple[IntentValidationIssue, ...]:
    """Validate numeric evidence without interpreting natural-language wording."""

    calculation = criterion.calculation
    if type(calculation) is not BudgetCalculation:
        return (
            _issue(
                IntentIssueCode.INVALID_BUDGET_AMOUNT,
                f"{location}.calculation",
                "budget calculation is invalid",
            ),
        )
    observed_amounts = budget_amount_evidence(criterion.source_span.text)
    expected_amounts = (
        calculation.base_amount,
        *calculation.allowance_amounts,
        *((calculation.explicit_maximum,) if calculation.explicit_maximum is not None else ()),
    )
    issues: list[IntentValidationIssue] = []
    if sorted(observed_amounts) != sorted(expected_amounts):
        issues.append(
            _issue(
                IntentIssueCode.BUDGET_AMOUNT_SPAN_MISMATCH,
                f"{location}.calculation",
                "budget calculation operands must exactly match quoted numeric evidence",
            )
        )
    currencies = _span_currencies(criterion.source_span.text)
    if (
        len(currencies) > 1
        or (currencies and currencies[0] != criterion.currency)
        or (not currencies and criterion.currency is not None)
    ):
        issues.append(
            _issue(
                IntentIssueCode.BUDGET_CURRENCY_SPAN_MISMATCH,
                f"{location}.currency",
                "budget currency does not match the currency named in source_span",
            )
        )
    return tuple(issues)


def _budget_has_negative_context(query: str, start: int, end: int) -> bool:
    prefix = query[:start]
    boundary_end = max(
        (
            index + len(separator)
            for separator in _POLARITY_BOUNDARIES
            if (index := prefix.rfind(separator)) >= 0
        ),
        default=0,
    )
    tail = query[end:]
    boundary_start = min(
        (
            index
            for separator in _SUFFIX_POLARITY_BOUNDARIES
            if (index := tail.find(separator)) >= 0
        ),
        default=len(tail),
    )
    context = f"{prefix[boundary_end:]} {tail[:boundary_start]}"
    return _BUDGET_NEGATIVE_CONTEXT.search(context) is not None


def _overlaps(first: tuple[int, int], second: tuple[int, int]) -> bool:
    return first[0] < second[1] and second[0] < first[1]


def _validate_non_empty_value(
    value: object,
    location: str,
) -> tuple[IntentValidationIssue, ...]:
    if type(value) is str and value.strip() and len(value) <= 2_000:
        return ()
    return (
        _issue(
            IntentIssueCode.INVALID_CRITERION_VALUE,
            location,
            "criterion value must be a non-empty string of at most 2000 code points",
        ),
    )


def _is_currency_code(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 3
        and value.isascii()
        and value.isalpha()
        and value.isupper()
    )


def _issue(
    code: IntentIssueCode,
    location: str,
    message: str,
) -> IntentValidationIssue:
    return IntentValidationIssue(code=code, location=location, message=message)


__all__ = [
    "BudgetCalculation",
    "BudgetMax",
    "Exclusion",
    "IntentCriterion",
    "IntentIssueCode",
    "IntentValidationIssue",
    "IntentValidationResult",
    "InterpretedRequest",
    "Preferred",
    "PreferredCriterion",
    "RequiredConstraint",
    "SourceSpan",
    "StockRequired",
    "TargetCategory",
    "budget_amount_evidence",
    "budget_currency_evidence",
    "budget_source_spans",
    "required_constraints_match",
    "validate_interpreted_request",
    "validate_source_span",
]
