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
class BudgetMax:
    """An exact maximum amount and its optional currency as written by the user."""

    amount: Decimal
    source_span: SourceSpan
    currency: str | None = None
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
_BUDGET_AMOUNT_TEXT = r"(?P<amount>[0-9]+(?:\.[0-9]+)?)"
_BUDGET_CURRENCY_TEXT = (
    r"(?P<currency>人民币|美元|美金|欧元|英镑|元|"
    r"(?<![A-Za-z])(?:CNY|USD|EUR|GBP)(?![A-Za-z]))"
)
_BUDGET_BOUNDARY = (
    r"(?=\s*(?:$|[的,，。;；、!?！？]|"  # noqa: RUF001
    r"(?:且|并且|并|和|但|可是|有库存|现货|在售)))"
)
_EXPLICIT_BUDGET_SPAN = re.compile(
    rf"(?P<full>预算\s*(?:(?:不超过|最多|为|是|[:：])\s*)?"  # noqa: RUF001
    rf"{_BUDGET_AMOUNT_TEXT}(?:\s*{_BUDGET_CURRENCY_TEXT})?"
    rf"(?:\s*(?:以内|以下|之内|封顶))?){_BUDGET_BOUNDARY}",
    re.IGNORECASE,
)
_CURRENCY_LIMIT_BUDGET_SPAN = re.compile(
    rf"(?P<full>{_BUDGET_AMOUNT_TEXT}\s*{_BUDGET_CURRENCY_TEXT}"
    rf"\s*(?:以内|以下|之内|封顶)){_BUDGET_BOUNDARY}",
    re.IGNORECASE,
)
_QUERY_NUMBER = re.compile(r"[0-9]+(?:\.[0-9]+)?")
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
    "lightweight": frozenset({"轻薄"}),
    "portable": frozenset({"便携"}),
    "轻薄": frozenset({"轻薄"}),
}
_PREFERRED_CANONICAL = {
    "travel": "travel",
    "long_battery": "long_battery",
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
            value: object = (criterion.amount, criterion.currency)
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
        issues.extend(_validate_query_category_ambiguity(query))

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
        amount_is_invalid = (
            type(criterion.amount) is not Decimal
            or not criterion.amount.is_finite()
            or criterion.amount <= 0
        )
        if amount_is_invalid:
            issues.append(
                _issue(
                    IntentIssueCode.INVALID_BUDGET_AMOUNT,
                    f"{location}.amount",
                    "budget amount must be a finite positive Decimal",
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
        if not span_issues and not amount_is_invalid and currency_is_valid:
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
) -> tuple[IntentValidationIssue, ...]:
    occupied: list[tuple[int, int]] = []
    categories: set[str] = set()
    for phrase, category in _CATEGORY_BY_TEXT.items():
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
    covering: list[tuple[int, int, str]] = []
    for phrase in _CATEGORY_BY_TEXT:
        for found in re.finditer(re.escape(phrase), trimmed_query):
            if found.start() <= span.start and span.end <= found.end():
                covering.append((found.start(), found.end(), phrase))
    maximal = max(
        covering,
        key=lambda item: (item[1] - item[0], -item[0]),
        default=None,
    )
    if (
        _CATEGORY_BY_TEXT.get(span.text) != criterion.category
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
    allowed_texts = _EXCLUSION_TEXTS.get(criterion.value, frozenset())
    if criterion.source_span.text not in allowed_texts or not _has_negative_scope(
        query.strip(),
        criterion.source_span.start,
        end=criterion.source_span.end,
        markers=_EXCLUSION_MARKERS,
    ):
        return (
            _issue(
                IntentIssueCode.CRITERION_SPAN_SEMANTICS_MISMATCH,
                f"{location}.value",
                "exclusion is not bound to an explicitly excluded phrase",
            ),
        )
    return ()


def _validate_preferred_span_binding(
    query: str,
    criterion: PreferredCriterion,
    location: str,
) -> tuple[IntentValidationIssue, ...]:
    allowed_texts = _PREFERRED_TEXTS.get(criterion.value, frozenset())
    if criterion.source_span.text not in allowed_texts or _has_negative_scope(
        query.strip(),
        criterion.source_span.start,
        end=criterion.source_span.end,
    ):
        return (
            _issue(
                IntentIssueCode.CRITERION_SPAN_SEMANTICS_MISMATCH,
                f"{location}.value",
                "preference is not bound to a positive approved phrase",
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


def _validate_budget_span_binding(
    query: str,
    criterion: BudgetMax,
    location: str,
) -> tuple[IntentValidationIssue, ...]:
    """Prove that a budget's amount and currency are present in its evidence."""

    issues: list[IntentValidationIssue] = []
    matches = _approved_budget_matches(query.strip())
    if (
        _NEGATED_BUDGET_QUERY.search(query)
        or (
            len(matches) == 1
            and _budget_has_negative_context(
                query.strip(),
                matches[0].start("full"),
                matches[0].end("full"),
            )
        )
        or (
            len(matches) == 1
            and _has_negative_scope(
                query.strip(),
                matches[0].start("full"),
                end=matches[0].end("full"),
                markers=_BUDGET_REJECTION_MARKERS,
            )
        )
        or ("预算" in query and len(_QUERY_NUMBER.findall(query)) > 1)
        or len(matches) != 1
        or matches[0].start("full") != criterion.source_span.start
        or matches[0].end("full") != criterion.source_span.end
        or matches[0].group("full") != criterion.source_span.text
    ):
        return (
            _issue(
                IntentIssueCode.INVALID_BUDGET_SPAN_SEMANTICS,
                f"{location}.source_span",
                "source_span does not express an approved budget constraint",
            ),
        )
    match = matches[0]

    if Decimal(match.group("amount")) != criterion.amount:
        issues.append(
            _issue(
                IntentIssueCode.BUDGET_AMOUNT_SPAN_MISMATCH,
                f"{location}.amount",
                "budget amount does not match exactly one number in source_span",
            )
        )

    currency_token = match.group("currency")
    span_currency = (
        None
        if currency_token is None
        else _BUDGET_CURRENCY_CODES[
            currency_token.upper() if currency_token.isascii() else currency_token
        ]
    )
    if span_currency != criterion.currency:
        issues.append(
            _issue(
                IntentIssueCode.BUDGET_CURRENCY_SPAN_MISMATCH,
                f"{location}.currency",
                "budget currency does not match the currency named in source_span",
            )
        )
    return tuple(issues)


def _approved_budget_matches(query: str) -> tuple[re.Match[str], ...]:
    explicit = list(_EXPLICIT_BUDGET_SPAN.finditer(query))
    occupied = tuple((match.start("full"), match.end("full")) for match in explicit)
    bare = [
        match
        for match in _CURRENCY_LIMIT_BUDGET_SPAN.finditer(query)
        if not any(
            match.start("full") < end and start < match.end("full") for start, end in occupied
        )
    ]
    return tuple(sorted((*explicit, *bare), key=lambda match: match.start("full")))


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
    "required_constraints_match",
    "validate_interpreted_request",
    "validate_source_span",
]
