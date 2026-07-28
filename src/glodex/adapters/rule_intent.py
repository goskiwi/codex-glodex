"""Deterministic table-driven interpretation for the M0 Chinese fixture slice."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

from glodex.contracts import SearchRequest
from glodex.domain.intent import (
    BudgetMax,
    Exclusion,
    InterpretedRequest,
    PreferredCriterion,
    RequiredConstraint,
    SourceSpan,
    StockRequired,
    TargetCategory,
)


class IntentInterpretationCode(StrEnum):
    """Stable adapter failures for unsafe core-condition interpretation."""

    AMBIGUOUS_BUDGET = "intent.ambiguous-budget"
    AMBIGUOUS_CATEGORY = "intent.ambiguous-category"
    INVALID_BUDGET = "intent.invalid-budget"
    UNSAFE_BUDGET = "intent.unsafe-budget"


class IntentInterpretationError(ValueError):
    """A fail-closed rule interpretation failure."""

    def __init__(self, code: IntentInterpretationCode, message: str) -> None:
        self.code = code
        super().__init__(f"{code.value}: {message}")


@dataclass(frozen=True, slots=True)
class _BudgetMatch:
    start: int
    end: int
    text: str
    amount: Decimal
    currency: str | None


_AMOUNT = r"(?P<amount>[0-9]+(?:\.[0-9]+)?)"
_CURRENCY = (
    r"(?P<currency>人民币|美元|美金|欧元|英镑|元|"
    r"(?<![A-Za-z])(?:CNY|USD|EUR|GBP)(?![A-Za-z]))"
)
_BUDGET_BOUNDARY = (
    r"(?=\s*(?:$|[的,，。;；、!?！？]|"  # noqa: RUF001
    r"(?:且|并且|并|和|但|可是|有库存|现货|在售)))"
)
_EXPLICIT_BUDGET = re.compile(
    rf"(?P<full>预算\s*(?:(?:不超过|最多|为|是|[:：])\s*)?"  # noqa: RUF001
    rf"{_AMOUNT}(?:\s*{_CURRENCY})?(?:\s*(?:以内|以下|之内|封顶))?)"
    rf"{_BUDGET_BOUNDARY}",
    re.IGNORECASE,
)
_CURRENCY_LIMIT_BUDGET = re.compile(
    rf"(?P<full>{_AMOUNT}\s*{_CURRENCY}\s*(?:以内|以下|之内|封顶))"
    rf"{_BUDGET_BOUNDARY}",
    re.IGNORECASE,
)
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
_NEGATED_BUDGET = re.compile(
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
_BUDGET_REJECTION_MARKERS: Final[tuple[str, ...]] = (
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
_NUMBER = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_UNIT_LIMIT = re.compile(
    rf"{_AMOUNT}\s*"
    rf"(?P<unit>[^\s0-9,，。;；、!?！？的]{{1,16}}?)"  # noqa: RUF001
    rf"\s*(?:以内|以下|之内|封顶)",
    re.IGNORECASE,
)

_CURRENCY_CODES: Final[dict[str, str]] = {
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
_CATEGORIES: Final[tuple[tuple[str, str], ...]] = (
    ("笔记本电脑", "laptop"),
    ("轻薄本", "laptop"),
    ("笔记本", "laptop"),
    ("照相机", "camera"),
    ("相机", "camera"),
    ("耳机", "headphones"),
)
_STOCK_PHRASES: Final[tuple[str, ...]] = ("有库存", "现货", "在售")
_EXCLUSION_TERMS: Final[tuple[str, ...]] = (
    "替换件",
    "翻新",
    "二手",
    "配件",
    "支架",
    "贴纸",
    "轻薄",
)
_PREFERENCES: Final[tuple[tuple[str, str], ...]] = (
    ("适合出差", "travel"),
    ("续航长", "long_battery"),
    ("长续航", "long_battery"),
    ("轻薄", "lightweight"),
    ("便携", "portable"),
)
_NEGATION_MARKERS: Final[tuple[str, ...]] = (
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
_STOCK_NEGATION_MARKERS: Final[tuple[str, ...]] = (
    *_NEGATION_MARKERS,
    "没有",
    "没",
    "无",
    "非",
)
_POLARITY_BOUNDARIES: Final[tuple[str, ...]] = (
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
_SUFFIX_POLARITY_BOUNDARIES: Final[tuple[str, ...]] = tuple(
    separator for separator in _POLARITY_BOUNDARIES if separator != "的"
)
_COORDINATING_CONNECTORS: Final[tuple[str, ...]] = (
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
_EXCLUSION_MARKERS: Final[tuple[str, ...]] = (
    "不想要",
    "不考虑",
    "不要",
    "排除",
    "不含",
    "拒绝",
)


class RuleIntentInterpreter:
    """Interpret only the finite, reviewable semantics required by M0 fixtures."""

    parser_version = "rules-zh-cn-v1"

    async def interpret(self, request: SearchRequest) -> InterpretedRequest:
        if not isinstance(request, SearchRequest):
            raise TypeError("RuleIntentInterpreter requires a SearchRequest")

        query = request.query
        required: list[RequiredConstraint] = []
        budget = _parse_budget(query)
        if budget is not None:
            required.append(
                BudgetMax(
                    amount=budget.amount,
                    currency=budget.currency,
                    source_span=SourceSpan(
                        start=budget.start,
                        end=budget.end,
                        text=budget.text,
                    ),
                )
            )

        category = _parse_category(query)
        if category is not None:
            required.append(category)
        required.extend(_parse_stock(query))
        required.extend(_parse_exclusions(query))

        preferred = _parse_preferences(query)
        required.sort(key=lambda item: (item.source_span.start, item.kind))
        preferred.sort(key=lambda item: (item.source_span.start, item.value))
        return InterpretedRequest(
            required=tuple(required),
            preferred=tuple(preferred),
            parser_version=self.parser_version,
        )


def _parse_budget(query: str) -> _BudgetMatch | None:
    if _NEGATED_BUDGET.search(query):
        raise IntentInterpretationError(
            IntentInterpretationCode.UNSAFE_BUDGET,
            "negated budget language cannot be interpreted safely",
        )
    if "预算" in query and len(_NUMBER.findall(query)) > 1:
        raise IntentInterpretationError(
            IntentInterpretationCode.AMBIGUOUS_BUDGET,
            "multiple budget amounts are ambiguous",
        )
    _reject_unsupported_unit_limits(query)

    raw_matches: list[re.Match[str]] = list(_EXPLICIT_BUDGET.finditer(query))
    occupied = tuple((match.start("full"), match.end("full")) for match in raw_matches)
    for match in _CURRENCY_LIMIT_BUDGET.finditer(query):
        span = (match.start("full"), match.end("full"))
        if any(_overlaps(span, existing) for existing in occupied):
            continue
        raw_matches.append(match)

    matches = tuple(_to_budget_match(match) for match in raw_matches)
    if any(
        _budget_has_negative_context(query, match.start, match.end)
        or _has_marker(
            query,
            match.start,
            _BUDGET_REJECTION_MARKERS,
            end=match.end,
        )
        for match in matches
    ):
        raise IntentInterpretationError(
            IntentInterpretationCode.UNSAFE_BUDGET,
            "negated budget language cannot be interpreted safely",
        )
    if len(matches) > 1:
        raise IntentInterpretationError(
            IntentInterpretationCode.AMBIGUOUS_BUDGET,
            "multiple budget constraints are ambiguous",
        )
    if not matches:
        if "预算" in query and _NUMBER.search(query):
            raise IntentInterpretationError(
                IntentInterpretationCode.UNSAFE_BUDGET,
                "budget syntax is unsupported",
            )
        return None
    if not matches[0].amount.is_finite() or matches[0].amount <= 0:
        raise IntentInterpretationError(
            IntentInterpretationCode.INVALID_BUDGET,
            "budget amount must be positive",
        )
    return matches[0]


def _reject_unsupported_unit_limits(query: str) -> None:
    for match in _UNIT_LIMIT.finditer(query):
        token = match.group("unit")
        normalized = token.upper() if token.isascii() else token
        if normalized not in _CURRENCY_CODES:
            raise IntentInterpretationError(
                IntentInterpretationCode.UNSAFE_BUDGET,
                "quantified limit unit is unsupported",
            )


def _to_budget_match(match: re.Match[str]) -> _BudgetMatch:
    currency_text = match.group("currency")
    normalized_currency = (
        None
        if currency_text is None
        else currency_text.upper()
        if currency_text.isascii()
        else currency_text
    )
    currency = None if normalized_currency is None else _CURRENCY_CODES[normalized_currency]
    return _BudgetMatch(
        start=match.start("full"),
        end=match.end("full"),
        text=match.group("full"),
        amount=Decimal(match.group("amount")),
        currency=currency,
    )


def _parse_category(query: str) -> TargetCategory | None:
    matches: list[tuple[int, int, str, str]] = []
    occupied: list[tuple[int, int]] = []
    for phrase, category in _CATEGORIES:
        for found in re.finditer(re.escape(phrase), query):
            span = (found.start(), found.end())
            if any(_overlaps(span, existing) for existing in occupied):
                continue
            if _has_marker(
                query,
                found.start(),
                _NEGATION_MARKERS,
                end=found.end(),
            ):
                occupied.append(span)
                continue
            matches.append((found.start(), found.end(), phrase, category))
            occupied.append(span)

    categories = {match[3] for match in matches}
    if len(categories) > 1:
        raise IntentInterpretationError(
            IntentInterpretationCode.AMBIGUOUS_CATEGORY,
            "multiple target categories are ambiguous",
        )
    if not matches:
        return None
    start, end, text, category = min(matches, key=lambda item: item[0])
    return TargetCategory(
        category=category,
        source_span=SourceSpan(start=start, end=end, text=text),
    )


def _parse_stock(query: str) -> list[StockRequired]:
    requirements: list[StockRequired] = []
    for phrase in _STOCK_PHRASES:
        for found in re.finditer(re.escape(phrase), query):
            if _has_marker(
                query,
                found.start(),
                _STOCK_NEGATION_MARKERS,
                end=found.end(),
            ):
                continue
            requirements.append(
                StockRequired(
                    source_span=SourceSpan(
                        start=found.start(),
                        end=found.end(),
                        text=phrase,
                    )
                )
            )
    return requirements[:1]


def _parse_exclusions(query: str) -> list[Exclusion]:
    exclusions: list[Exclusion] = []
    seen: set[str] = set()
    for term in _EXCLUSION_TERMS:
        for found in re.finditer(re.escape(term), query):
            if term in seen or not _has_marker(
                query,
                found.start(),
                _EXCLUSION_MARKERS,
                end=found.end(),
            ):
                continue
            seen.add(term)
            exclusions.append(
                Exclusion(
                    value=term,
                    source_span=SourceSpan(
                        start=found.start(),
                        end=found.end(),
                        text=term,
                    ),
                )
            )
    return exclusions


def _parse_preferences(query: str) -> list[PreferredCriterion]:
    preferred: list[PreferredCriterion] = []
    seen: set[str] = set()
    for phrase, value in _PREFERENCES:
        for found in re.finditer(re.escape(phrase), query):
            if value in seen or _has_marker(
                query,
                found.start(),
                _NEGATION_MARKERS,
                end=found.end(),
            ):
                continue
            seen.add(value)
            preferred.append(
                PreferredCriterion(
                    value=value,
                    source_span=SourceSpan(
                        start=found.start(),
                        end=found.end(),
                        text=phrase,
                    ),
                )
            )
    return preferred


def _has_marker(
    query: str,
    start: int,
    markers: tuple[str, ...],
    *,
    end: int | None = None,
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
                    *(phrase for phrase, _ in _CATEGORIES),
                    *_STOCK_PHRASES,
                    *_EXCLUSION_TERMS,
                    *(phrase for phrase, _ in _PREFERENCES),
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
    return False


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


__all__ = [
    "IntentInterpretationCode",
    "IntentInterpretationError",
    "RuleIntentInterpreter",
]
