"""Strict immutable contracts for the bounded M1d tool surface."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, StringConstraints, field_validator, model_validator

from glodex.contracts import (
    CurrencyCode,
    FrozenDTO,
    Identifier,
    RunStatus,
    SearchRequest,
    SearchResponse,
)
from glodex.domain.catalog import CatalogBatch
from glodex.domain.intent import InterpretedRequest, RequiredConstraint

ShortText = Annotated[str, StringConstraints(min_length=1, max_length=128)]
DateText = Annotated[str, StringConstraints(min_length=10, max_length=10)]
TitleText = Annotated[str, StringConstraints(min_length=1, max_length=256)]
SnippetText = Annotated[str, StringConstraints(min_length=1, max_length=280)]
QueryText = Annotated[str, StringConstraints(min_length=1, max_length=2_000)]
EvidenceQueryText = Annotated[str, StringConstraints(min_length=1, max_length=512)]
DemandText = Annotated[str, StringConstraints(min_length=1, max_length=256)]
PositiveAmount = Annotated[Decimal, Field(gt=0)]
NonNegativeAmount = Annotated[Decimal, Field(ge=0)]


class ToolName(StrEnum):
    PLANNER = "planner"
    CHAT_FALLBACK = "chat_fallback"
    WEB_SEARCH = "web_search"
    CATEGORY_INSIGHT = "category_insight"
    ITEM_SEARCH = "item_search"
    ITEM_PICKER = "item_picker"
    PRICE_COMPARE = "price_compare"
    SHIPPING_CALC = "shipping_calc"
    SHOPPING_SUMMARY = "shopping_summary"
    DISPATCH_TOOL = "dispatch_tool"


BUSINESS_TOOL_SET: tuple[ToolName, ...] = (
    ToolName.PLANNER,
    ToolName.CHAT_FALLBACK,
    ToolName.WEB_SEARCH,
    ToolName.CATEGORY_INSIGHT,
    ToolName.ITEM_SEARCH,
    ToolName.ITEM_PICKER,
    ToolName.PRICE_COMPARE,
    ToolName.SHIPPING_CALC,
    ToolName.SHOPPING_SUMMARY,
)
FULL_TOOL_SET: tuple[ToolName, ...] = (*BUSINESS_TOOL_SET, ToolName.DISPATCH_TOOL)
TERMINAL_TOOLS: tuple[ToolName, ...] = (
    ToolName.SHOPPING_SUMMARY,
    ToolName.CHAT_FALLBACK,
)


class ToolFailureCode(StrEnum):
    WEB_SEARCH_NOT_ENABLED = "WEB_SEARCH_NOT_ENABLED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_RESPONSE_INVALID = "PROVIDER_RESPONSE_INVALID"
    INDEX_INVALID = "INDEX_INVALID"
    PROVIDER_NOT_CONFIGURED = "PROVIDER_NOT_CONFIGURED"
    ITEM_SOURCE_INVALID = "ITEM_SOURCE_INVALID"
    TOOL_RESULT_TOO_LARGE = "TOOL_RESULT_TOO_LARGE"
    NO_INSIGHT = "NO_INSIGHT"
    INVALID_PRECONDITION = "INVALID_PRECONDITION"
    NO_ELIGIBLE_CANDIDATE = "NO_ELIGIBLE_CANDIDATE"
    FINAL_GATE_FAILED = "FINAL_GATE_FAILED"
    M2A_RETRIEVAL_FAILED = "M2A_RETRIEVAL_FAILED"
    M2A_PROFILE_DEGRADED = "M2A_PROFILE_DEGRADED"
    M2A_RERANK_DEGRADED = "M2A_RERANK_DEGRADED"
    M2A_CATEGORY_RETRIEVAL_FAILED = "M2A_CATEGORY_RETRIEVAL_FAILED"
    M2C_MODEL_UNAVAILABLE = "M2C_MODEL_UNAVAILABLE"
    M2C_QUERY_EMBEDDING_FAILED = "M2C_QUERY_EMBEDDING_FAILED"
    M2C_USER_EMBEDDING_DEGRADED = "M2C_USER_EMBEDDING_DEGRADED"
    M2C_RERANK_DEGRADED = "M2C_RERANK_DEGRADED"
    M2C_PROFILE_MODEL_MISMATCH = "M2C_PROFILE_MODEL_MISMATCH"


class AgentFailureCode(StrEnum):
    """Stable runtime failures that never carry model or Provider text."""

    MODEL_INVALID = "MODEL_INVALID"
    INVALID_ACTION = "INVALID_ACTION"
    INVALID_PHASE = "INVALID_PHASE"
    LOOP_DETECTED = "LOOP_DETECTED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    TOOL_FAILED = "TOOL_FAILED"
    FORK_FAILED = "FORK_FAILED"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    RUN_ABORTED = "RUN_ABORTED"


class Platform(StrEnum):
    AMAZON = "amazon"
    SHOPEE = "shopee"
    ALIEXPRESS = "aliexpress"
    EBAY = "ebay"


PLATFORM_SET: tuple[Platform, ...] = (
    Platform.AMAZON,
    Platform.SHOPEE,
    Platform.ALIEXPRESS,
    Platform.EBAY,
)


class DataMode(StrEnum):
    DEMO_SNAPSHOT = "DEMO_SNAPSHOT"
    LIVE_MARKETPLACE = "LIVE_MARKETPLACE"


class EvidenceKind(StrEnum):
    REVIEW = "review"
    GUIDE = "guide"
    TREND = "trend"


class InsightDepth(StrEnum):
    QUICK = "quick"
    DEEP = "deep"


class PlannerIntentKind(StrEnum):
    SHOPPING = "SHOPPING"
    UNSUPPORTED_OR_NON_SHOPPING = "UNSUPPORTED_OR_NON_SHOPPING"


class PlannerFallbackReason(StrEnum):
    NON_SHOPPING = "NON_SHOPPING"
    UNSUPPORTED_CATEGORY = "UNSUPPORTED_CATEGORY"
    PLATFORM_NOT_CONFIGURED = "PLATFORM_NOT_CONFIGURED"


class PlanStage(StrEnum):
    EVIDENCE = "evidence"
    ITEM_SEARCH = "item_search"
    PRICE_COMPARE = "price_compare"
    SHIPPING_CALC = "shipping_calc"
    ELIGIBILITY = "eligibility"
    ITEM_PICKER = "item_picker"
    SHOPPING_SUMMARY = "shopping_summary"
    CHAT_FALLBACK = "chat_fallback"


class EmptySelector(FrozenDTO):
    """A strict empty object for tools with no model-selectable fields."""


class ChatFallbackSelector(FrozenDTO):
    reason_code: PlannerFallbackReason


class WebSearchSelector(FrozenDTO):
    evidence_kind: EvidenceKind


class CategoryInsightSelector(FrozenDTO):
    depth: InsightDepth


class ItemSearchSelector(FrozenDTO):
    platform: Platform
    top_k: Annotated[int, Field(ge=1, le=50)] = 20


class PriceCompareSelector(FrozenDTO):
    top_n: Annotated[int, Field(ge=1, le=30)] = 12


class ItemPickerSelector(FrozenDTO):
    max_items: Annotated[int, Field(ge=1, le=3)] = 3


class DispatchTaskSelector(FrozenDTO):
    task_id: Identifier
    demands: DemandText


class DispatchSelector(FrozenDTO):
    tasks: Annotated[tuple[DispatchTaskSelector, ...], Field(min_length=1, max_length=4)]

    @model_validator(mode="after")
    def task_ids_are_unique(self) -> Self:
        task_ids = tuple(task.task_id for task in self.tasks)
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("dispatch task IDs must be unique")
        return self


type ToolSelector = (
    EmptySelector
    | ChatFallbackSelector
    | WebSearchSelector
    | CategoryInsightSelector
    | ItemSearchSelector
    | PriceCompareSelector
    | ItemPickerSelector
    | DispatchSelector
)


class CallToolAction(FrozenDTO):
    """The only model-selected action shape accepted by the runtime."""

    tool_name: ToolName
    selector_args: ToolSelector

    @model_validator(mode="after")
    def selector_matches_tool(self) -> Self:
        expected: dict[ToolName, type[FrozenDTO]] = {
            ToolName.PLANNER: EmptySelector,
            ToolName.CHAT_FALLBACK: ChatFallbackSelector,
            ToolName.WEB_SEARCH: WebSearchSelector,
            ToolName.CATEGORY_INSIGHT: CategoryInsightSelector,
            ToolName.ITEM_SEARCH: ItemSearchSelector,
            ToolName.PRICE_COMPARE: PriceCompareSelector,
            ToolName.SHIPPING_CALC: EmptySelector,
            ToolName.ITEM_PICKER: ItemPickerSelector,
            ToolName.SHOPPING_SUMMARY: EmptySelector,
            ToolName.DISPATCH_TOOL: DispatchSelector,
        }
        if type(self.selector_args) is not expected[self.tool_name]:
            raise ValueError("selector_args do not match tool_name")
        return self


class SafeObservation(FrozenDTO):
    """A bounded fact-only projection allowed into the next model round."""

    round: Annotated[int, Field(ge=0, le=14)]
    phase: Identifier
    available_tools: Annotated[tuple[ToolName, ...], Field(max_length=10)]
    executed_tools: Annotated[tuple[ToolName, ...], Field(max_length=11)] = ()
    platforms: Annotated[tuple[Platform, ...], Field(max_length=4)] = ()
    candidate_count: Annotated[int, Field(ge=0, le=100)] = 0
    top_candidate_ids: Annotated[tuple[Identifier, ...], Field(max_length=3)] = ()
    publication_eligible_ids: Annotated[tuple[Identifier, ...], Field(max_length=3)] = ()
    safe_codes: Annotated[tuple[Identifier, ...], Field(max_length=16)] = ()

    @model_validator(mode="after")
    def projected_identifiers_are_unique(self) -> Self:
        for values in (
            self.available_tools,
            self.top_candidate_ids,
            self.publication_eligible_ids,
            self.safe_codes,
        ):
            if len(values) != len(set(values)):
                raise ValueError("safe observation collections must be unique")
        return self


class ActionSelectionInput(FrozenDTO):
    request: SearchRequest
    observation: SafeObservation


class AgentCapabilities(FrozenDTO):
    data_mode: DataMode
    available_platforms: Annotated[tuple[Platform, ...], Field(min_length=1, max_length=4)]
    supported_categories: Annotated[
        tuple[Identifier, ...],
        Field(min_length=1, max_length=32),
    ] = ("phone", "laptop", "tablet")
    web_search_enabled: bool = False
    embedding_enabled: bool = False
    live_ebay_enabled: bool = False

    @model_validator(mode="after")
    def platform_capabilities_are_consistent(self) -> Self:
        if len(self.available_platforms) != len(set(self.available_platforms)):
            raise ValueError("available platforms must be unique")
        if len(self.supported_categories) != len(set(self.supported_categories)) or any(
            category != category.strip() or "\0" in category
            for category in self.supported_categories
        ):
            raise ValueError("supported categories must be trimmed and unique")
        if self.data_mode is DataMode.LIVE_MARKETPLACE and self.available_platforms != (
            Platform.EBAY,
        ):
            raise ValueError("live marketplace mode only supports ebay")
        if self.live_ebay_enabled and Platform.EBAY not in self.available_platforms:
            raise ValueError("live ebay requires the ebay platform")
        return self


@dataclass(frozen=True, slots=True)
class PlannerInput:
    request: SearchRequest
    interpreted_request: InterpretedRequest
    required_baseline: InterpretedRequest
    capabilities: AgentCapabilities

    def __post_init__(self) -> None:
        if type(self.request) is not SearchRequest:
            raise TypeError("planner request must be an exact SearchRequest")
        if type(self.interpreted_request) is not InterpretedRequest:
            raise TypeError("planner intent must be an exact InterpretedRequest")
        if type(self.required_baseline) is not InterpretedRequest:
            raise TypeError("planner baseline must be an exact InterpretedRequest")
        if type(self.capabilities) is not AgentCapabilities:
            raise TypeError("planner capabilities must be exact AgentCapabilities")


@dataclass(frozen=True, slots=True)
class PlannerOutput:
    intent_kind: PlannerIntentKind
    constraints: tuple[RequiredConstraint, ...]
    platforms: tuple[Platform, ...]
    stages: tuple[PlanStage, ...]
    fallback_reason: PlannerFallbackReason | None = None

    def __post_init__(self) -> None:
        if type(self.intent_kind) is not PlannerIntentKind:
            raise TypeError("planner intent_kind must be PlannerIntentKind")
        if type(self.constraints) is not tuple:
            raise TypeError("planner constraints must be a tuple")
        if type(self.platforms) is not tuple or any(
            type(platform) is not Platform for platform in self.platforms
        ):
            raise TypeError("planner platforms must contain exact Platform values")
        if len(self.platforms) != len(set(self.platforms)):
            raise ValueError("planner platforms must be unique")
        if type(self.stages) is not tuple or any(
            type(stage) is not PlanStage for stage in self.stages
        ):
            raise TypeError("planner stages must contain exact PlanStage values")
        if self.intent_kind is PlannerIntentKind.SHOPPING:
            if not self.platforms or self.fallback_reason is not None:
                raise ValueError("shopping plans require platforms and no fallback")
            if self.stages[-1:] != (PlanStage.SHOPPING_SUMMARY,):
                raise ValueError("shopping plans must end in shopping_summary")
        elif (
            self.platforms
            or self.stages != (PlanStage.CHAT_FALLBACK,)
            or type(self.fallback_reason) is not PlannerFallbackReason
        ):
            raise ValueError("fallback plans require only a stable fallback reason")


class ChatFallbackOutput(FrozenDTO):
    reason_code: PlannerFallbackReason
    answer: Annotated[str, StringConstraints(min_length=1, max_length=280)]


class WebSearchInput(FrozenDTO):
    query: EvidenceQueryText
    evidence_kind: EvidenceKind
    max_results: Annotated[int, Field(ge=1, le=8)] = 8


class WebEvidence(FrozenDTO):
    source_id: Identifier
    title: TitleText
    url_domain: ShortText
    published_at: datetime | None = None
    snippet: SnippetText
    source_type: EvidenceKind

    @field_validator("published_at")
    @classmethod
    def published_at_must_be_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() != timedelta(0)):
            raise ValueError("published_at must be timezone-aware UTC")
        return value


class WebSearchOutput(FrozenDTO):
    evidence: Annotated[tuple[WebEvidence, ...], Field(max_length=8)] = ()

    @model_validator(mode="after")
    def source_ids_are_unique(self) -> Self:
        source_ids = tuple(item.source_id for item in self.evidence)
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("web evidence source IDs must be unique")
        return self


class EmbeddingBatch(FrozenDTO):
    texts: Annotated[
        tuple[Annotated[str, StringConstraints(min_length=1, max_length=2_000)], ...],
        Field(min_length=1, max_length=2),
    ]

    @model_validator(mode="after")
    def texts_are_unique(self) -> Self:
        if len(self.texts) != len(set(self.texts)):
            raise ValueError("embedding texts must be unique")
        return self


class EmbeddingResult(FrozenDTO):
    vectors: Annotated[tuple[tuple[float, ...], ...], Field(min_length=1, max_length=2)]

    @field_validator("vectors")
    @classmethod
    def vectors_are_normalized(cls, vectors: tuple[tuple[float, ...], ...]) -> object:
        for vector in vectors:
            if len(vector) != 1_024:
                raise ValueError("embedding vectors must have 1024 dimensions")
            if any(type(value) is not float or not math.isfinite(value) for value in vector):
                raise ValueError("embedding vectors must contain finite floats")
            norm = math.sqrt(sum(value * value for value in vector))
            if not math.isclose(norm, 1.0, rel_tol=0.0, abs_tol=1e-6):
                raise ValueError("embedding vectors must be L2-normalized")
        return vectors


class CategoryInsightInput(FrozenDTO):
    category: ShortText
    depth: InsightDepth
    query: QueryText
    query_vector: tuple[float, ...]
    index_version: Identifier

    @field_validator("query_vector")
    @classmethod
    def vector_is_normalized(cls, vector: tuple[float, ...]) -> object:
        EmbeddingResult(vectors=(vector,))
        return vector


class InsightStatus(StrEnum):
    FOUND = "FOUND"
    NO_INSIGHT = "NO_INSIGHT"


class CategoryInsightOutput(FrozenDTO):
    status: InsightStatus
    components: Annotated[tuple[ShortText, ...], Field(max_length=8)] = ()
    bestsellers: Annotated[tuple[ShortText, ...], Field(max_length=5)] = ()
    attributes: Annotated[tuple[ShortText, ...], Field(max_length=12)] = ()
    price_tiers: Annotated[tuple[ShortText, ...], Field(max_length=5)] = ()
    confidence: Annotated[Decimal, Field(ge=0, le=1)] | None = None
    card_ids: Annotated[tuple[Identifier, ...], Field(max_length=15)] = ()

    @model_validator(mode="after")
    def no_insight_is_empty(self) -> Self:
        values = (
            self.components,
            self.bestsellers,
            self.attributes,
            self.price_tiers,
            self.card_ids,
        )
        if self.status is InsightStatus.NO_INSIGHT:
            if any(values) or self.confidence is not None:
                raise ValueError("NO_INSIGHT cannot contain inferred facts")
        elif not self.card_ids or self.confidence is None:
            raise ValueError("FOUND insight requires cards and confidence")
        return self


class CandidateAttribute(FrozenDTO):
    name: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    value: Annotated[str, StringConstraints(min_length=1, max_length=256)]


class Candidate(FrozenDTO):
    candidate_id: Identifier
    item_id: Identifier
    platform: Platform
    title: TitleText
    price: PositiveAmount
    currency: CurrencyCode
    rating: Annotated[Decimal, Field(ge=0, le=5)] | None = None
    sales: Annotated[int, Field(ge=0)] | None = None
    image_url: Annotated[str, StringConstraints(min_length=1, max_length=2_048)] | None = None
    attributes: Annotated[tuple[CandidateAttribute, ...], Field(max_length=16)] = ()
    source_ref: ShortText
    record_ref: ShortText
    pack_size: PositiveAmount | None = None
    pack_note: ShortText | None = None

    @field_validator("image_url")
    @classmethod
    def image_url_is_safe_https(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("candidate image_url must be an HTTPS URL without userinfo")
        return value

    @model_validator(mode="after")
    def attributes_are_unique(self) -> Self:
        names = tuple(attribute.name for attribute in self.attributes)
        if len(names) != len(set(names)):
            raise ValueError("candidate attribute names must be unique")
        return self


class ItemSearchInput(FrozenDTO):
    query: QueryText
    platform: Platform
    top_k: Annotated[int, Field(ge=1, le=50)] = 20
    user_id: None = None


@dataclass(frozen=True, slots=True)
class ItemSearchRuntimeResult:
    platform: Platform
    candidates: tuple[Candidate, ...]
    platform_sub_batch: CatalogBatch
    total_recall: int
    truncated: bool

    def __post_init__(self) -> None:
        if type(self.platform) is not Platform:
            raise TypeError("item result platform must be an exact Platform")
        if type(self.candidates) is not tuple or any(
            type(candidate) is not Candidate for candidate in self.candidates
        ):
            raise TypeError("item result candidates must contain exact Candidate values")
        if len(self.candidates) > 50:
            raise ValueError("item result cannot contain more than 50 candidates")
        if any(candidate.platform is not self.platform for candidate in self.candidates):
            raise ValueError("candidate platform does not match item result")
        candidate_ids = tuple(candidate.candidate_id for candidate in self.candidates)
        record_refs = tuple(candidate.record_ref for candidate in self.candidates)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("item result candidate IDs must be unique")
        if len(record_refs) != len(set(record_refs)):
            raise ValueError("item result record refs must be unique")
        if type(self.platform_sub_batch) is not CatalogBatch:
            raise TypeError("item result batch must be an exact CatalogBatch")
        if type(self.total_recall) is not int or isinstance(self.total_recall, bool):
            raise TypeError("total_recall must be an integer")
        if self.total_recall < len(self.candidates):
            raise ValueError("total_recall cannot be smaller than returned candidates")
        if type(self.truncated) is not bool:
            raise TypeError("truncated must be a bool")
        if self.truncated is not (self.total_recall > len(self.candidates)):
            raise ValueError("truncated must exactly reflect total_recall")


class PricePointStatus(StrEnum):
    EXACT = "EXACT"
    UNKNOWN_FX = "UNKNOWN_FX"


class PricePoint(FrozenDTO):
    candidate_id: Identifier
    platform: Platform
    source_ref: ShortText
    source_amount: PositiveAmount
    source_currency: CurrencyCode
    base_currency: Literal["CNY"] = "CNY"
    base_amount: PositiveAmount | None = None
    fx_evidence_id: Identifier | None = None
    pack_size: PositiveAmount | None = None
    pack_note: ShortText | None = None
    per_unit_base_amount: PositiveAmount | None = None
    status: PricePointStatus

    @model_validator(mode="after")
    def status_shape_is_consistent(self) -> Self:
        if self.status is PricePointStatus.EXACT:
            if self.base_amount is None or self.fx_evidence_id is None:
                raise ValueError("EXACT price points require amount and FX evidence")
            if self.pack_size is not None and self.per_unit_base_amount is None:
                raise ValueError("pack size requires a per-unit amount")
        elif any(
            value is not None
            for value in (
                self.base_amount,
                self.fx_evidence_id,
                self.per_unit_base_amount,
            )
        ):
            raise ValueError("UNKNOWN_FX price points cannot contain converted amounts")
        return self


class PriceCompareOutput(FrozenDTO):
    ranked: Annotated[tuple[PricePoint, ...], Field(max_length=30)]
    cheapest_per_platform: tuple[Identifier, ...]

    @model_validator(mode="after")
    def cheapest_ids_are_known_and_unique(self) -> Self:
        known_ids = {point.candidate_id for point in self.ranked}
        if len(self.cheapest_per_platform) != len(set(self.cheapest_per_platform)):
            raise ValueError("cheapest candidate IDs must be unique")
        if not set(self.cheapest_per_platform).issubset(known_ids):
            raise ValueError("cheapest candidate IDs must exist in ranked points")
        return self


class CostComponentStatus(StrEnum):
    EXACT = "EXACT"
    ESTIMATE = "ESTIMATE"
    UNKNOWN = "UNKNOWN"


class ShippingStatus(StrEnum):
    EXACT = "EXACT"
    ESTIMATE = "ESTIMATE"
    UNKNOWN = "UNKNOWN"


class DutyTier(StrEnum):
    EXEMPT = "EXEMPT"
    LOW = "LOW"
    STANDARD = "STANDARD"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"


class LandedCostAdvisory(FrozenDTO):
    candidate_id: Identifier
    currency: Literal["CNY"] = "CNY"
    item_price: PositiveAmount | None
    shipping: NonNegativeAmount | None
    tax: NonNegativeAmount | None
    duty: NonNegativeAmount | None
    landed_total: PositiveAmount | None
    eta_days_min: Annotated[int, Field(ge=0)] | None
    eta_days_max: Annotated[int, Field(ge=0)] | None
    duty_tier: DutyTier
    item_status: ShippingStatus
    item_price_status: CostComponentStatus
    shipping_status: CostComponentStatus
    tax_status: CostComponentStatus
    duty_status: CostComponentStatus
    item_price_evidence_id: Identifier | None
    shipping_evidence_id: Identifier | None
    tax_evidence_id: Identifier | None
    duty_evidence_id: Identifier | None
    fx_evidence_id: Identifier | None
    rule_effective_date: DateText | None
    calculation_date: DateText
    ruleset_version: Identifier

    @model_validator(mode="after")
    def advisory_shape_is_consistent(self) -> Self:
        try:
            calculation_date = date.fromisoformat(self.calculation_date)
            rule_effective_date = (
                None
                if self.rule_effective_date is None
                else date.fromisoformat(self.rule_effective_date)
            )
        except ValueError:
            raise ValueError("advisory dates must be canonical ISO dates") from None
        if calculation_date.isoformat() != self.calculation_date or (
            rule_effective_date is not None
            and rule_effective_date.isoformat() != self.rule_effective_date
        ):
            raise ValueError("advisory dates must be canonical ISO dates")
        components = (
            (
                self.item_price,
                self.item_price_status,
                self.item_price_evidence_id,
            ),
            (
                self.shipping,
                self.shipping_status,
                self.shipping_evidence_id,
            ),
            (self.tax, self.tax_status, self.tax_evidence_id),
            (self.duty, self.duty_status, self.duty_evidence_id),
        )
        statuses = tuple(status for _amount, status, _evidence_id in components)
        for amount, status, evidence_id in components:
            if status is CostComponentStatus.EXACT:
                if amount is None or evidence_id is None:
                    raise ValueError("EXACT components require amount and evidence")
            elif status is CostComponentStatus.ESTIMATE:
                if amount is None or evidence_id is not None:
                    raise ValueError("ESTIMATE components require amount and rule provenance")
            elif amount is not None or evidence_id is not None:
                raise ValueError("UNKNOWN components cannot contain amount or evidence")
        if CostComponentStatus.EXACT in statuses:
            if self.fx_evidence_id is None:
                raise ValueError("EXACT converted components require FX evidence")
        elif self.fx_evidence_id is not None:
            raise ValueError("advisory without exact components cannot claim FX evidence")
        if CostComponentStatus.ESTIMATE in statuses and (
            rule_effective_date is None or rule_effective_date > calculation_date
        ):
            raise ValueError("ESTIMATE components require an effective versioned rule")
        if CostComponentStatus.UNKNOWN in statuses:
            if self.item_status is not ShippingStatus.UNKNOWN or self.landed_total is not None:
                raise ValueError("unknown components require UNKNOWN and null total")
        elif all(status is CostComponentStatus.EXACT for status in statuses):
            if self.item_status is not ShippingStatus.EXACT:
                raise ValueError("all exact components require EXACT")
        elif self.item_status is not ShippingStatus.ESTIMATE:
            raise ValueError("mixed known components require ESTIMATE")
        if self.landed_total is not None:
            if any(amount is None for amount, _status, _evidence_id in components):
                raise ValueError("landed total requires every cost component")
            expected_total = sum(
                (amount for amount, _status, _evidence_id in components if amount is not None),
                Decimal(0),
            )
            if self.landed_total != expected_total:
                raise ValueError("landed total must equal all cost components")
        if (self.duty_status is CostComponentStatus.UNKNOWN) is not (
            self.duty_tier is DutyTier.UNKNOWN
        ):
            raise ValueError("duty tier must match duty availability")
        if (self.eta_days_min is None) is not (self.eta_days_max is None):
            raise ValueError("ETA bounds must both be present or absent")
        if (
            self.eta_days_min is not None
            and self.eta_days_max is not None
            and self.eta_days_max < self.eta_days_min
        ):
            raise ValueError("ETA maximum cannot precede minimum")
        return self


class ShippingCalcOutput(FrozenDTO):
    advisories: Annotated[tuple[LandedCostAdvisory, ...], Field(max_length=30)]
    ruleset_version: Identifier

    @model_validator(mode="after")
    def ruleset_is_consistent(self) -> Self:
        if any(item.ruleset_version != self.ruleset_version for item in self.advisories):
            raise ValueError("advisory ruleset versions must match")
        return self


class ItemPickerOutput(FrozenDTO):
    selected_candidate_ids: Annotated[tuple[Identifier, ...], Field(max_length=3)] = ()
    reason_codes: tuple[Identifier, ...]

    @model_validator(mode="after")
    def selected_ids_are_unique(self) -> Self:
        if len(self.selected_candidate_ids) != len(set(self.selected_candidate_ids)):
            raise ValueError("picker candidate IDs must be unique")
        if not self.selected_candidate_ids and self.reason_codes != (
            ToolFailureCode.NO_ELIGIBLE_CANDIDATE.value,
        ):
            raise ValueError("empty picker output requires NO_ELIGIBLE_CANDIDATE")
        return self


class ShoppingSummaryOutput(FrozenDTO):
    status: Literal["COMPLETED", "NO_MATCH"]
    answer: Annotated[str, StringConstraints(min_length=1, max_length=2_000)]
    search_response: SearchResponse
    selected_product_ids: Annotated[tuple[Identifier, ...], Field(max_length=3)] = ()

    @model_validator(mode="after")
    def summary_matches_search(self) -> Self:
        if self.status != self.search_response.status.value:
            raise ValueError("summary status must match SearchResponse")
        expected = tuple(result.product_id for result in self.search_response.results)
        if self.selected_product_ids != expected:
            raise ValueError("selected product IDs must come from SearchResponse")
        return self


class ToolFailure(FrozenDTO):
    tool_name: ToolName
    code: ToolFailureCode


class ForkStatus(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


@dataclass(frozen=True, slots=True)
class ForkResult:
    child_id: str
    depth: int
    goal: str
    status: ForkStatus
    tool_name: ToolName
    task_id: str
    safe_code: ToolFailureCode | None
    typed_result: (
        WebSearchOutput
        | CategoryInsightOutput
        | ItemSearchRuntimeResult
        | PriceCompareOutput
        | ShippingCalcOutput
        | NestedForkReturn
        | None
    )
    model_calls: int
    tool_calls: int
    return_calls: int

    def __post_init__(self) -> None:
        _require_fork_text(self.child_id, name="child_id", maximum=128)
        _require_fork_text(self.goal, name="goal", maximum=256)
        _require_fork_text(self.task_id, name="task_id", maximum=128)
        if type(self.depth) is not int or isinstance(self.depth, bool) or not 1 <= self.depth <= 2:
            raise ValueError("fork result depth must be one or two")
        if type(self.status) is not ForkStatus:
            raise TypeError("fork result status must be an exact ForkStatus")
        if type(self.tool_name) is not ToolName:
            raise TypeError("fork result tool_name must be an exact ToolName")
        allowed_tools = {
            ToolName.WEB_SEARCH,
            ToolName.CATEGORY_INSIGHT,
            ToolName.ITEM_SEARCH,
            ToolName.PRICE_COMPARE,
            ToolName.SHIPPING_CALC,
            ToolName.DISPATCH_TOOL,
        }
        if self.tool_name not in allowed_tools:
            raise ValueError("fork result tool is not child-safe")
        _require_binary_counter(self.model_calls, name="model_calls")
        _require_binary_counter(self.tool_calls, name="tool_calls")
        _require_binary_counter(self.return_calls, name="return_calls")
        if self.return_calls != 1:
            raise ValueError("fork result requires exactly one runtime return")
        if self.status is ForkStatus.COMPLETED:
            if self.typed_result is None or self.safe_code is not None:
                raise ValueError("completed fork requires only a typed result")
            if self.model_calls != 1 or self.tool_calls != 1:
                raise ValueError("completed fork requires one model and one tool call")
            if self.tool_name is ToolName.DISPATCH_TOOL:
                if self.depth >= 2 or type(self.typed_result) is not NestedForkReturn:
                    raise ValueError("only depth-one forks may return a nested dispatch")
                if any(result.depth != self.depth + 1 for result in self.typed_result.results):
                    raise ValueError("nested fork result depth must advance exactly once")
            elif type(self.typed_result) is not _FORK_RESULT_TYPES[self.tool_name]:
                raise ValueError("fork typed result does not match its tool")
        elif self.typed_result is not None or type(self.safe_code) is not ToolFailureCode:
            raise ValueError("failed fork requires only a safe code")


@dataclass(frozen=True, slots=True)
class NestedForkReturn:
    """One typed return from an allowed depth-one nested dispatch."""

    results: tuple[ForkResult, ...]

    def __post_init__(self) -> None:
        if (
            type(self.results) is not tuple
            or not self.results
            or any(type(result) is not ForkResult for result in self.results)
        ):
            raise TypeError("nested dispatch requires exact non-empty ForkResult values")
        if len(self.results) > 4:
            raise ValueError("nested dispatch cannot return more than four child results")
        child_ids = tuple(result.child_id for result in self.results)
        if len(child_ids) != len(set(child_ids)):
            raise ValueError("nested dispatch child IDs must be unique")


class AgentAnswerKind(StrEnum):
    SHOPPING_SUMMARY = "SHOPPING_SUMMARY"
    CHAT_FALLBACK = "CHAT_FALLBACK"


class AgentAnswer(FrozenDTO):
    kind: AgentAnswerKind
    text: Annotated[str, StringConstraints(min_length=1, max_length=2_000)]


class AgentToolSummary(FrozenDTO):
    tool_name: ToolName
    call_count: Annotated[int, Field(ge=1, le=4)]
    safe_outcome: Identifier


class AgentDemoResponse(FrozenDTO):
    """The independent public M1d terminal result."""

    schema_version: Literal["glodex.agent-result.v1"] = "glodex.agent-result.v1"
    run_id: Identifier
    status: RunStatus
    answer: AgentAnswer | None = None
    search_response: SearchResponse | None = None
    selected_product_ids: Annotated[tuple[Identifier, ...], Field(max_length=3)] = ()
    evidence_ids: Annotated[tuple[Identifier, ...], Field(max_length=64)] = ()
    web_evidence: Annotated[tuple[WebEvidence, ...], Field(max_length=8)] = ()
    landed_cost_advisories: Annotated[
        tuple[LandedCostAdvisory, ...],
        Field(max_length=30),
    ] = ()
    tool_summary: Annotated[tuple[AgentToolSummary, ...], Field(max_length=10)] = ()

    @model_validator(mode="after")
    def terminal_shape_is_consistent(self) -> Self:
        tool_names = tuple(item.tool_name for item in self.tool_summary)
        if len(tool_names) != len(set(tool_names)):
            raise ValueError("tool summary cannot repeat a tool")
        if self.search_response is not None and self.search_response.run_id != self.run_id:
            raise ValueError("Agent and Search run IDs must match")

        if self.status is RunStatus.FAILED:
            if (
                self.answer is not None
                or self.selected_product_ids
                or self.evidence_ids
                or self.web_evidence
                or self.landed_cost_advisories
            ):
                raise ValueError("FAILED Agent results cannot expose partial projections")
            return self

        if self.answer is None:
            raise ValueError("successful Agent results require an answer")
        if self.answer.kind is AgentAnswerKind.CHAT_FALLBACK:
            if (
                self.status is not RunStatus.COMPLETED
                or self.search_response is not None
                or self.selected_product_ids
                or self.evidence_ids
                or self.web_evidence
                or self.landed_cost_advisories
            ):
                raise ValueError("chat fallback cannot claim shopping facts")
            return self

        if self.search_response is None or self.search_response.status is not self.status:
            raise ValueError("shopping answer requires a status-matched SearchResponse")
        expected_products = tuple(result.product_id for result in self.search_response.results)
        if self.selected_product_ids != expected_products:
            raise ValueError("selected product IDs must come from SearchResponse")
        expected_evidence = tuple(
            dict.fromkeys(
                evidence.evidence_id
                for result in self.search_response.results
                for evidence in result.evidence
            )
        )
        if self.evidence_ids != expected_evidence:
            raise ValueError("evidence IDs must come from SearchResponse")
        return self


class AgentEventKind(StrEnum):
    AGENT_STARTED = "AGENT_STARTED"
    MODEL_STARTED = "MODEL_STARTED"
    MODEL_FINISHED = "MODEL_FINISHED"
    TOOL_STARTED = "TOOL_STARTED"
    TOOL_FINISHED = "TOOL_FINISHED"
    FORK_STARTED = "FORK_STARTED"
    FORK_FINISHED = "FORK_FINISHED"
    AGENT_RESULT = "AGENT_RESULT"
    AGENT_ERROR = "AGENT_ERROR"


class AgentEventScope(StrEnum):
    ROOT = "root"
    CHILD = "child"


class AgentRunEvent(FrozenDTO):
    """One safe internal fact projected by the independent Agent API."""

    kind: AgentEventKind
    run_id: Identifier
    scope: AgentEventScope = AgentEventScope.ROOT
    round: Annotated[int, Field(ge=1, le=14)] | None = None
    tool_name: ToolName | None = None
    child_id: Identifier | None = None
    depth: Annotated[int, Field(ge=1, le=2)] | None = None
    status: (
        Literal[
            "COMPLETED",
            "NO_MATCH",
            "FAILED",
            "ABORTED",
        ]
        | None
    ) = None
    safe_code: Identifier | None = None

    @model_validator(mode="after")
    def event_fields_match_kind(self) -> Self:
        child_pair = (self.child_id is not None, self.depth is not None)
        if child_pair[0] is not child_pair[1]:
            raise ValueError("child ID and depth must appear together")
        if self.scope is AgentEventScope.CHILD and not child_pair[0]:
            raise ValueError("child-scoped events require child identity")
        if (
            self.scope is AgentEventScope.ROOT
            and child_pair[0]
            and self.kind not in {AgentEventKind.FORK_STARTED, AgentEventKind.FORK_FINISHED}
        ):
            raise ValueError("root events cannot carry child identity")

        if self.kind is AgentEventKind.AGENT_STARTED:
            expected = (None, None, None, None)
            actual = (self.round, self.tool_name, self.status, self.safe_code)
            if self.scope is not AgentEventScope.ROOT or child_pair[0] or actual != expected:
                raise ValueError("AGENT_STARTED has no step payload")
        elif self.kind is AgentEventKind.MODEL_STARTED:
            if (
                self.round is None
                or self.tool_name is not None
                or self.status is not None
                or self.safe_code is not None
            ):
                raise ValueError("MODEL_STARTED requires only a round")
        elif self.kind is AgentEventKind.MODEL_FINISHED:
            if (
                self.round is None
                or self.tool_name is None
                or self.status is not None
                or self.safe_code is not None
            ):
                raise ValueError("MODEL_FINISHED requires round and tool")
        elif self.kind is AgentEventKind.TOOL_STARTED:
            if (
                self.round is not None
                or self.tool_name is None
                or self.status is not None
                or self.safe_code is not None
            ):
                raise ValueError("TOOL_STARTED requires only a tool")
        elif self.kind is AgentEventKind.TOOL_FINISHED:
            if (
                self.round is not None
                or self.tool_name is None
                or self.status is not None
                or self.safe_code is None
            ):
                raise ValueError("TOOL_FINISHED requires tool and safe outcome")
        elif self.kind is AgentEventKind.FORK_STARTED:
            if (
                self.scope is not AgentEventScope.ROOT
                or not child_pair[0]
                or self.round is not None
                or self.tool_name is not None
                or self.status is not None
                or self.safe_code is not None
            ):
                raise ValueError("FORK_STARTED requires only child identity")
        elif self.kind is AgentEventKind.FORK_FINISHED:
            if (
                self.scope is not AgentEventScope.ROOT
                or not child_pair[0]
                or self.round is not None
                or self.tool_name is not None
                or self.status not in {"COMPLETED", "FAILED", "ABORTED"}
            ):
                raise ValueError("FORK_FINISHED requires child identity and status")
        elif self.kind is AgentEventKind.AGENT_RESULT:
            if (
                self.scope is not AgentEventScope.ROOT
                or child_pair[0]
                or self.round is not None
                or self.tool_name is not None
                or self.status not in {"COMPLETED", "NO_MATCH"}
                or self.safe_code is not None
            ):
                raise ValueError("AGENT_RESULT requires a successful terminal status")
        elif (
            self.scope is not AgentEventScope.ROOT
            or child_pair[0]
            or self.round is not None
            or self.tool_name is not None
            or self.status not in {"FAILED", "ABORTED"}
            or self.safe_code is None
        ):
            raise ValueError("AGENT_ERROR requires a safe error terminal")
        return self


class AgentRunRecord(FrozenDTO):
    run_id: Identifier
    status: RunStatus
    model_calls: Annotated[int, Field(ge=0, le=14)]
    tool_calls: Annotated[int, Field(ge=0, le=13)]
    child_runs: Annotated[int, Field(ge=0, le=4)]
    terminal_code: Identifier | None = None
    tool_summary: Annotated[tuple[AgentToolSummary, ...], Field(max_length=10)] = ()
    events: tuple[AgentRunEvent, ...] = ()

    @model_validator(mode="after")
    def record_matches_terminal(self) -> Self:
        if any(event.run_id != self.run_id for event in self.events):
            raise ValueError("run record events must share one run ID")
        if self.status is RunStatus.FAILED:
            if self.terminal_code is None:
                raise ValueError("FAILED run records require a terminal code")
        elif self.terminal_code is not None:
            raise ValueError("successful run records cannot have a terminal code")
        return self


@dataclass(frozen=True, slots=True)
class AgentExecution:
    response: AgentDemoResponse
    record: AgentRunRecord

    def __post_init__(self) -> None:
        if type(self.response) is not AgentDemoResponse:
            raise TypeError("Agent execution response must be exact AgentDemoResponse")
        if type(self.record) is not AgentRunRecord:
            raise TypeError("Agent execution record must be exact AgentRunRecord")
        if (
            self.response.run_id != self.record.run_id
            or self.response.status is not self.record.status
            or self.response.tool_summary != self.record.tool_summary
        ):
            raise ValueError("Agent execution response and record must match")


_FORK_RESULT_TYPES: dict[ToolName, type[object]] = {
    ToolName.WEB_SEARCH: WebSearchOutput,
    ToolName.CATEGORY_INSIGHT: CategoryInsightOutput,
    ToolName.ITEM_SEARCH: ItemSearchRuntimeResult,
    ToolName.PRICE_COMPARE: PriceCompareOutput,
    ToolName.SHIPPING_CALC: ShippingCalcOutput,
}


def _require_fork_text(value: object, *, name: str, maximum: int) -> None:
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be non-empty and at most {maximum} code points")


def _require_binary_counter(value: object, *, name: str) -> None:
    if type(value) is not int or value not in (0, 1):
        raise ValueError(f"{name} must be zero or one")


__all__ = [
    "BUSINESS_TOOL_SET",
    "FULL_TOOL_SET",
    "PLATFORM_SET",
    "TERMINAL_TOOLS",
    "ActionSelectionInput",
    "AgentAnswer",
    "AgentAnswerKind",
    "AgentCapabilities",
    "AgentDemoResponse",
    "AgentEventKind",
    "AgentEventScope",
    "AgentExecution",
    "AgentFailureCode",
    "AgentRunEvent",
    "AgentRunRecord",
    "AgentToolSummary",
    "CallToolAction",
    "Candidate",
    "CandidateAttribute",
    "CategoryInsightInput",
    "CategoryInsightOutput",
    "CategoryInsightSelector",
    "ChatFallbackOutput",
    "ChatFallbackSelector",
    "CostComponentStatus",
    "DataMode",
    "DispatchSelector",
    "DispatchTaskSelector",
    "DutyTier",
    "EmbeddingBatch",
    "EmbeddingResult",
    "EmptySelector",
    "EvidenceKind",
    "ForkResult",
    "ForkStatus",
    "InsightDepth",
    "InsightStatus",
    "ItemPickerOutput",
    "ItemPickerSelector",
    "ItemSearchInput",
    "ItemSearchRuntimeResult",
    "ItemSearchSelector",
    "LandedCostAdvisory",
    "NestedForkReturn",
    "PlanStage",
    "PlannerFallbackReason",
    "PlannerInput",
    "PlannerIntentKind",
    "PlannerOutput",
    "Platform",
    "PriceCompareOutput",
    "PriceCompareSelector",
    "PricePoint",
    "PricePointStatus",
    "SafeObservation",
    "ShippingCalcOutput",
    "ShippingStatus",
    "ShoppingSummaryOutput",
    "ToolFailure",
    "ToolFailureCode",
    "ToolName",
    "WebEvidence",
    "WebSearchInput",
    "WebSearchOutput",
    "WebSearchSelector",
]
