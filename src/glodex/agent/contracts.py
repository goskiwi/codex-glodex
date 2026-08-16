"""Strict immutable contracts for the bounded M1d tool surface."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from glodex.contracts import (
    MAX_RESPONSE_EVIDENCE_IDS,
    ConfigFingerprint,
    CurrencyCode,
    FrozenDTO,
    Identifier,
    RunStatus,
    SearchRequest,
    SearchResponse,
)
from glodex.domain.catalog import CatalogBatch
from glodex.domain.intent import InterpretedRequest, RequiredConstraint, SourceSpan

ShortText = Annotated[str, StringConstraints(min_length=1, max_length=128)]
DateText = Annotated[str, StringConstraints(min_length=10, max_length=10)]
TitleText = Annotated[str, StringConstraints(min_length=1, max_length=256)]
SnippetText = Annotated[str, StringConstraints(min_length=1, max_length=280)]
QueryText = Annotated[str, StringConstraints(min_length=1, max_length=2_000)]
EvidenceQueryText = Annotated[str, StringConstraints(min_length=1, max_length=512)]
PositiveAmount = Annotated[Decimal, Field(gt=0)]
NonNegativeAmount = Annotated[Decimal, Field(ge=0)]


def _strict_tool_schema_requires_every_property(schema: dict[str, object]) -> None:
    """Make nullable/defaulted fields explicit in strict provider tool schemas."""

    properties = schema.get("properties")
    if isinstance(properties, dict):
        schema["required"] = list(properties)


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
    PARALLEL_DISPATCH_TOOL = "parallel_dispatch_tool"


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

# Dispatch is a control-plane action rather than another business capability.
# It is nevertheless native to every homogeneous AgentLoop so the model can
# choose serial work, one isolated fork, or bounded parallel platform fan-out.
FULL_TOOL_SET: tuple[ToolName, ...] = (
    *BUSINESS_TOOL_SET,
    ToolName.DISPATCH_TOOL,
    ToolName.PARALLEL_DISPATCH_TOOL,
)


class ToolFailureCode(StrEnum):
    WEB_SEARCH_NOT_ENABLED = "WEB_SEARCH_NOT_ENABLED"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_RESPONSE_INVALID = "PROVIDER_RESPONSE_INVALID"
    INDEX_INVALID = "INDEX_INVALID"
    PROVIDER_NOT_CONFIGURED = "PROVIDER_NOT_CONFIGURED"
    ITEM_SOURCE_INVALID = "ITEM_SOURCE_INVALID"
    TOOL_RESULT_TOO_LARGE = "TOOL_RESULT_TOO_LARGE"
    NO_INSIGHT = "NO_INSIGHT"
    INVALID_PRECONDITION = "INVALID_PRECONDITION"
    NO_ELIGIBLE_CANDIDATE = "NO_ELIGIBLE_CANDIDATE"
    COMPARISON_TARGET_UNCOVERED = "COMPARISON_TARGET_UNCOVERED"
    FINAL_GATE_FAILED = "FINAL_GATE_FAILED"
    RETRIEVAL_MODEL_UNAVAILABLE = "RETRIEVAL_MODEL_UNAVAILABLE"
    RETRIEVAL_MODEL_QUERY_EMBEDDING_FAILED = "RETRIEVAL_MODEL_QUERY_EMBEDDING_FAILED"
    RETRIEVAL_MODEL_RERANK_DEGRADED = "RETRIEVAL_MODEL_RERANK_DEGRADED"
    SEMANTIC_ASSERTION_UNAVAILABLE = "SEMANTIC_ASSERTION_UNAVAILABLE"
    SEMANTIC_ASSERTION_INVALID = "SEMANTIC_ASSERTION_INVALID"
    SHOPPING_SUMMARY_UNAVAILABLE = "SHOPPING_SUMMARY_UNAVAILABLE"
    SHOPPING_SUMMARY_INVALID = "SHOPPING_SUMMARY_INVALID"


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
    ALIBABA = "alibaba"
    WALMART = "walmart"
    SHEIN = "shein"


CURRENT_PRODUCT_PLATFORMS: tuple[Platform, ...] = (
    Platform.AMAZON,
    Platform.SHOPEE,
    Platform.ALIEXPRESS,
    Platform.EBAY,
)


class ForkReasonCode(StrEnum):
    """One sufficient reason for an isolated homogeneous AgentLoop."""

    PARALLEL = "PARALLEL"
    CONTEXT_ISOLATION = "CONTEXT_ISOLATION"
    DEEP_CHAIN = "DEEP_CHAIN"


class ForkObjectiveCode(StrEnum):
    """A bounded goal category, never a specialised child role."""

    INVESTIGATE = "INVESTIGATE"
    COMPARE = "COMPARE"
    VALIDATE = "VALIDATE"


class ForkDemand(FrozenDTO):
    """One model-requested fork, authorised from the parent's verified state.

    The model can select a reason and already-observed platform/reference IDs,
    but it cannot author a child prompt, a query, a tool allowlist, a deadline
    or an unbounded amount of work.  The graph runtime compiles the actual
    scope immediately before a child is created.
    """

    objective: ForkObjectiveCode
    reason: ForkReasonCode
    platforms: Annotated[tuple[Platform, ...], Field(min_length=1, max_length=8)]
    context_refs: Annotated[tuple[Identifier, ...], Field(max_length=16)] = ()
    estimated_tool_calls: Annotated[int, Field(ge=1, le=8)]

    @model_validator(mode="after")
    def scope_values_are_unique(self) -> Self:
        if len(self.platforms) != len(set(self.platforms)):
            raise ValueError("fork demand platforms must be unique")
        if len(self.context_refs) != len(set(self.context_refs)):
            raise ValueError("fork demand context refs must be unique")
        return self


class ForkDemandInput(BaseModel):
    """JSON-native tool arguments compiled into the strict fork contract.

    Tool calls arrive as JSON arrays and strings, while ``ForkDemand`` keeps
    the internal boundary exact (tuples and enum instances).  Separating the
    two prevents tool-schema coercion from weakening the runtime contract.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    objective: Literal["INVESTIGATE", "COMPARE", "VALIDATE"]
    reason: Literal["PARALLEL", "CONTEXT_ISOLATION", "DEEP_CHAIN"]
    platforms: Annotated[
        list[Literal["amazon", "shopee", "aliexpress", "ebay", "alibaba", "walmart", "shein"]],
        Field(min_length=1, max_length=8),
    ]
    context_refs: Annotated[list[Identifier], Field(max_length=16)] = Field(default_factory=list)
    estimated_tool_calls: Annotated[int, Field(ge=1, le=8)]

    @model_validator(mode="after")
    def input_values_are_unique(self) -> Self:
        if len(self.platforms) != len(set(self.platforms)):
            raise ValueError("fork input platforms must be unique")
        if len(self.context_refs) != len(set(self.context_refs)):
            raise ValueError("fork input context refs must be unique")
        return self

    def compile(self) -> ForkDemand:
        return ForkDemand(
            objective=ForkObjectiveCode(self.objective),
            reason=ForkReasonCode(self.reason),
            platforms=tuple(Platform(platform) for platform in self.platforms),
            context_refs=tuple(self.context_refs),
            estimated_tool_calls=self.estimated_tool_calls,
        )


class DataMode(StrEnum):
    SYNTHETIC_INTERVIEW = "SYNTHETIC_INTERVIEW"


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


class AgentCapabilities(FrozenDTO):
    data_mode: DataMode
    available_platforms: Annotated[tuple[Platform, ...], Field(min_length=1, max_length=8)]
    web_search_enabled: bool
    embedding_enabled: bool

    @model_validator(mode="after")
    def platform_capabilities_are_consistent(self) -> Self:
        if len(self.available_platforms) != len(set(self.available_platforms)):
            raise ValueError("available platforms must be unique")
        return self


@dataclass(frozen=True, slots=True)
class PlannerInput:
    request: SearchRequest
    interpreted_request: InterpretedRequest
    required_baseline: InterpretedRequest
    capabilities: AgentCapabilities
    declared_intent: PlannerIntentKind
    requested_platforms: tuple[Platform, ...]
    search_query: str | None
    comparison_targets: tuple[ComparisonTarget, ...]

    def __post_init__(self) -> None:
        if type(self.request) is not SearchRequest:
            raise TypeError("planner request must be an exact SearchRequest")
        if type(self.interpreted_request) is not InterpretedRequest:
            raise TypeError("planner intent must be an exact InterpretedRequest")
        if type(self.required_baseline) is not InterpretedRequest:
            raise TypeError("planner baseline must be an exact InterpretedRequest")
        if type(self.capabilities) is not AgentCapabilities:
            raise TypeError("planner capabilities must be exact AgentCapabilities")
        if type(self.declared_intent) is not PlannerIntentKind:
            raise TypeError("planner declared intent must be exact PlannerIntentKind")
        if type(self.requested_platforms) is not tuple or any(
            type(platform) is not Platform for platform in self.requested_platforms
        ):
            raise TypeError("planner requested platforms must contain exact Platform values")
        if len(self.requested_platforms) != len(set(self.requested_platforms)):
            raise ValueError("planner requested platforms must be unique")
        if type(self.comparison_targets) is not tuple or any(
            type(target) is not ComparisonTarget for target in self.comparison_targets
        ):
            raise TypeError("planner comparison targets must contain exact values")
        if len(self.comparison_targets) != len(
            {target.search_query for target in self.comparison_targets}
        ):
            raise ValueError("planner comparison targets must be unique")
        if self.declared_intent is PlannerIntentKind.SHOPPING:
            if type(self.search_query) is not str or not self.search_query.strip():
                raise ValueError("shopping planner input requires a concrete search query")
        elif self.search_query is not None or self.comparison_targets:
            raise ValueError("non-shopping planner input cannot carry shopping queries")


class PlannerBudgetInput(FrozenDTO):
    """One semantic budget decision plus a deterministic numeric proof."""

    model_config = ConfigDict(json_schema_extra=_strict_tool_schema_requires_every_property)

    source_text: ShortText
    mode: Literal["maximum", "around", "range"]
    base_amount: Annotated[str, StringConstraints(pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")]
    lower_bound: Annotated[
        str | None,
        StringConstraints(pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$"),
    ] = None
    allowance_amounts: Annotated[
        tuple[
            Annotated[
                str,
                StringConstraints(pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$"),
            ],
            ...,
        ],
        Field(max_length=2),
    ] = ()
    explicit_maximum: Annotated[
        str | None,
        StringConstraints(pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$"),
    ] = None
    maximum: Annotated[str, StringConstraints(pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")]
    currency: CurrencyCode | None = None

    @field_validator("allowance_amounts", mode="before")
    @classmethod
    def json_allowance_array_is_normalized(cls, value: object) -> object:
        return tuple(value) if type(value) is list else value

    @model_validator(mode="after")
    def calculation_is_exact(self) -> Self:
        base = Decimal(self.base_amount)
        lower_bound = None if self.lower_bound is None else Decimal(self.lower_bound)
        allowances = tuple(Decimal(value) for value in self.allowance_amounts)
        explicit_maximum = None if self.explicit_maximum is None else Decimal(self.explicit_maximum)
        maximum = Decimal(self.maximum)
        if any(
            not value.is_finite() or value <= 0
            for value in (
                base,
                maximum,
                *((lower_bound,) if lower_bound is not None else ()),
                *allowances,
                *((explicit_maximum,) if explicit_maximum is not None else ()),
            )
        ):
            raise ValueError("planner budget amounts must be finite and positive")
        if self.mode == "maximum":
            expected = (
                explicit_maximum
                if explicit_maximum is not None
                else base + (max(allowances) if allowances else 0)
            )
            if lower_bound is not None or maximum != expected:
                raise ValueError("planner maximum budget proof is inconsistent")
        elif self.mode == "around":
            if (
                allowances
                or explicit_maximum is not None
                or lower_bound != base * Decimal("0.90")
                or maximum != base * Decimal("1.10")
            ):
                raise ValueError("planner around budget requires exact ten-percent bounds")
        elif (
            allowances
            or explicit_maximum is not None
            or lower_bound is None
            or lower_bound >= maximum
            or base != maximum
        ):
            raise ValueError("planner range budget proof is inconsistent")
        return self


class PlannerCriterionInput(FrozenDTO):
    """One model-extracted constraint or preference with exact query evidence."""

    source_text: ShortText
    value: ShortText


class PlannerComparisonTargetInput(FrozenDTO):
    """One explicitly named comparison target quoted from the locked query."""

    source_text: ShortText


@dataclass(frozen=True, slots=True)
class ComparisonTarget:
    """One grounded product target that must be represented in a comparison."""

    source_span: SourceSpan
    search_query: str

    def __post_init__(self) -> None:
        if type(self.source_span) is not SourceSpan:
            raise TypeError("comparison target requires an exact source span")
        if (
            type(self.search_query) is not str
            or not self.search_query.strip()
            or len(self.search_query) > 128
            or self.search_query != self.source_span.text
        ):
            raise ValueError("comparison target query must equal its quoted source")


class PlannerDecisionInput(FrozenDTO):
    """JSON-native first action compiled into strict runtime planning facts."""

    # Native tool calls arrive as JSON arrays and strings. Pydantic's Python
    # strict mode would reject those before LangGraph can invoke the tool even
    # though the identical JSON validates through ``model_validate_json``.
    # Coercion is limited to this model-authored input; PlannerInput and the
    # trusted domain objects below retain exact enum/tuple boundaries.
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=False,
        json_schema_extra=_strict_tool_schema_requires_every_property,
    )

    intent_kind: PlannerIntentKind
    search_query: Annotated[
        str | None,
        StringConstraints(min_length=1, max_length=128),
    ] = None
    requested_platforms: Annotated[tuple[Platform, ...], Field(max_length=8)] = ()
    budget: PlannerBudgetInput | None = None
    stock_source_text: ShortText | None = None
    exclusions: Annotated[tuple[PlannerCriterionInput, ...], Field(max_length=8)] = ()
    preferences: Annotated[tuple[PlannerCriterionInput, ...], Field(max_length=8)] = ()
    comparison_targets: Annotated[
        tuple[PlannerComparisonTargetInput, ...], Field(max_length=3)
    ] = ()

    @model_validator(mode="after")
    def decision_is_consistent(self) -> Self:
        if len(self.requested_platforms) != len(set(self.requested_platforms)):
            raise ValueError("planner requested platforms must be unique")
        if self.intent_kind is PlannerIntentKind.UNSUPPORTED_OR_NON_SHOPPING and any(
            (
                self.requested_platforms,
                self.search_query is not None,
                self.budget is not None,
                self.stock_source_text is not None,
                self.exclusions,
                self.preferences,
                self.comparison_targets,
            )
        ):
            raise ValueError("non-shopping planner decisions cannot carry shopping facts")
        if self.intent_kind is PlannerIntentKind.SHOPPING and self.search_query is None:
            raise ValueError("shopping planner decisions require a retrieval query")
        target_texts = tuple(target.source_text for target in self.comparison_targets)
        if len(target_texts) != len(set(target_texts)):
            raise ValueError("planner comparison targets must be unique")
        return self


@dataclass(frozen=True, slots=True)
class PlannerOutput:
    """Trusted planning facts consumed by the native-tool session."""

    intent_kind: PlannerIntentKind
    constraints: tuple[RequiredConstraint, ...]
    platforms: tuple[Platform, ...]
    search_query: str | None
    comparison_targets: tuple[ComparisonTarget, ...]
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
        if type(self.comparison_targets) is not tuple or any(
            type(target) is not ComparisonTarget for target in self.comparison_targets
        ):
            raise TypeError("planner comparison targets must contain exact values")
        if len(self.comparison_targets) != len(
            {target.search_query for target in self.comparison_targets}
        ):
            raise ValueError("planner comparison targets must be unique")
        if self.intent_kind is PlannerIntentKind.SHOPPING:
            if (
                not self.platforms
                or type(self.search_query) is not str
                or not self.search_query.strip()
                or self.fallback_reason is not None
            ):
                raise ValueError("shopping plans require platforms, search query and no fallback")
        elif (
            self.platforms
            or self.search_query is not None
            or self.comparison_targets
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
        Field(min_length=1, max_length=8),
    ]

    @model_validator(mode="after")
    def texts_are_unique(self) -> Self:
        if len(self.texts) != len(set(self.texts)):
            raise ValueError("embedding texts must be unique")
        return self


class EmbeddingResult(FrozenDTO):
    vectors: Annotated[tuple[tuple[float, ...], ...], Field(min_length=1, max_length=8)]

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


class InsightStatus(StrEnum):
    FOUND = "FOUND"
    NO_INSIGHT = "NO_INSIGHT"


class BestsellerInsight(FrozenDTO):
    name: ShortText
    typical_price_cny: PositiveAmount
    why_popular: ShortText


class AttributeDistribution(FrozenDTO):
    name: ShortText
    distribution: Annotated[
        dict[ShortText, Annotated[Decimal, Field(ge=0, le=1)]],
        Field(min_length=1, max_length=12),
    ]

    @model_validator(mode="after")
    def distribution_is_normalized(self) -> Self:
        total = sum(self.distribution.values(), Decimal(0))
        if abs(total - Decimal(1)) > Decimal("0.01"):
            raise ValueError("attribute distribution must sum to one")
        return self


class PriceTierInsight(FrozenDTO):
    tier: Literal["budget", "mid", "premium"]
    range_cny: tuple[NonNegativeAmount, PositiveAmount]
    notes: ShortText

    @model_validator(mode="after")
    def range_is_ordered(self) -> Self:
        if self.range_cny[0] >= self.range_cny[1]:
            raise ValueError("price tier range must be increasing")
        return self


class CategoryInsightOutput(FrozenDTO):
    status: InsightStatus
    category: ShortText
    components: Annotated[tuple[ShortText, ...], Field(max_length=8)] = ()
    bestsellers: Annotated[tuple[BestsellerInsight, ...], Field(max_length=5)] = ()
    attributes: Annotated[tuple[AttributeDistribution, ...], Field(max_length=12)] = ()
    price_tiers: Annotated[tuple[PriceTierInsight, ...], Field(max_length=3)] = ()
    confidence: Annotated[Decimal, Field(ge=0, le=1)]

    @model_validator(mode="after")
    def no_insight_is_empty(self) -> Self:
        values = (
            self.components,
            self.bestsellers,
            self.attributes,
            self.price_tiers,
        )
        if self.status is InsightStatus.NO_INSIGHT:
            if any(values) or self.confidence != 0:
                raise ValueError("NO_INSIGHT cannot contain inferred facts")
        elif not any(values) or self.confidence <= 0:
            raise ValueError("FOUND insight requires structured knowledge")
        if len(self.components) != len(set(self.components)):
            raise ValueError("category components must be unique")
        tiers = tuple(item.tier for item in self.price_tiers)
        if len(tiers) != len(set(tiers)):
            raise ValueError("price insight tiers must be unique")
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
    attributes: tuple[CandidateAttribute, ...] = ()
    source_ref: ShortText
    record_ref: ShortText
    same_group_id: Identifier | None = None
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


class SemanticAssertionCandidate(FrozenDTO):
    """One already-observed candidate presented to the semantic assertion."""

    candidate_id: Identifier
    title: TitleText
    attributes: tuple[CandidateAttribute, ...] = ()


class SemanticAssertionInput(FrozenDTO):
    """Bounded evidence for checking target-product versus accessory drift."""

    query: QueryText
    category: ShortText
    components: Annotated[tuple[ShortText, ...], Field(max_length=8)] = ()
    candidates: Annotated[
        tuple[SemanticAssertionCandidate, ...],
        Field(min_length=1, max_length=50),
    ]

    @model_validator(mode="after")
    def candidates_are_unique(self) -> Self:
        candidate_ids = tuple(candidate.candidate_id for candidate in self.candidates)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("semantic assertion candidate IDs must be unique")
        return self


class SemanticAssertionOutput(FrozenDTO):
    """The exact observed IDs judged to be the requested product itself."""

    relevant_candidate_ids: Annotated[tuple[Identifier, ...], Field(max_length=50)] = ()

    @model_validator(mode="after")
    def relevant_ids_are_unique(self) -> Self:
        if len(self.relevant_candidate_ids) != len(set(self.relevant_candidate_ids)):
            raise ValueError("semantic assertion relevant IDs must be unique")
        return self


class ItemSearchInput(FrozenDTO):
    query: QueryText
    platform: Platform
    category: ShortText
    min_landed_cost_cny: Annotated[Decimal, Field(gt=0)] | None
    max_landed_cost_cny: Annotated[Decimal, Field(gt=0)] | None
    top_k: Annotated[int, Field(ge=1, le=50)] = 20

    @model_validator(mode="after")
    def structured_constraints_are_consistent(self) -> Self:
        if (
            self.min_landed_cost_cny is not None
            and self.max_landed_cost_cny is not None
            and self.min_landed_cost_cny > self.max_landed_cost_cny
        ):
            raise ValueError("item landed-cost bounds are reversed")
        return self


@dataclass(frozen=True, slots=True)
class ItemSearchRuntimeResult:
    platform: Platform
    target_query: str
    retrieval_query: str
    candidates: tuple[Candidate, ...]
    platform_sub_batch: CatalogBatch
    total_recall: int
    returned_before_semantic_filter: int
    truncated: bool

    def __post_init__(self) -> None:
        if type(self.platform) is not Platform:
            raise TypeError("item result platform must be an exact Platform")
        for name, value in (
            ("target_query", self.target_query),
            ("retrieval_query", self.retrieval_query),
        ):
            if type(value) is not str or not value.strip() or len(value) > 2_000:
                raise ValueError(f"item result {name} is invalid")
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
        if type(self.returned_before_semantic_filter) is not int or isinstance(
            self.returned_before_semantic_filter, bool
        ):
            raise TypeError("returned_before_semantic_filter must be an integer")
        if not len(self.candidates) <= self.returned_before_semantic_filter <= self.total_recall:
            raise ValueError("returned_before_semantic_filter is outside the recall bounds")
        if type(self.truncated) is not bool:
            raise TypeError("truncated must be a bool")
        if self.truncated is not (self.total_recall > self.returned_before_semantic_filter):
            raise ValueError("truncated must reflect the pre-semantic-filter return window")


class PricePointStatus(StrEnum):
    EXACT = "EXACT"
    UNKNOWN_FX = "UNKNOWN_FX"


class PriceComparisonScope(StrEnum):
    """The candidate universe retained by deterministic price comparison."""

    ALL_RETRIEVED = "ALL_RETRIEVED"
    COMPARABLE_PRODUCT_GROUPS = "COMPARABLE_PRODUCT_GROUPS"
    NO_COMPARABLE_GROUP = "NO_COMPARABLE_GROUP"


class PricePoint(FrozenDTO):
    candidate_id: Identifier
    platform: Platform
    source_ref: ShortText
    source_amount: PositiveAmount
    source_currency: CurrencyCode
    same_group_id: Identifier | None = None
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
    cheapest_per_platform: Annotated[dict[str, Identifier], Field(max_length=4)]
    comparison_scope: PriceComparisonScope = PriceComparisonScope.ALL_RETRIEVED
    compared_group_ids: Annotated[tuple[Identifier, ...], Field(max_length=50)] = ()

    @model_validator(mode="after")
    def cheapest_ids_are_known_and_unique(self) -> Self:
        point_by_id = {point.candidate_id: point for point in self.ranked}
        cheapest_ids = tuple(self.cheapest_per_platform.values())
        if len(cheapest_ids) != len(set(cheapest_ids)):
            raise ValueError("cheapest candidate IDs must be unique")
        if not set(cheapest_ids).issubset(point_by_id):
            raise ValueError("cheapest candidate IDs must exist in ranked points")
        try:
            platforms = tuple(Platform(platform) for platform in self.cheapest_per_platform)
        except ValueError:
            raise ValueError("cheapest platforms must be supported") from None
        if len(platforms) != len(set(platforms)) or any(
            point_by_id[candidate_id].platform is not platform
            for platform, candidate_id in zip(
                platforms,
                self.cheapest_per_platform.values(),
                strict=True,
            )
        ):
            raise ValueError("cheapest candidate IDs must match unique platforms")
        if len(self.compared_group_ids) != len(set(self.compared_group_ids)):
            raise ValueError("compared product group IDs must be unique")
        if self.comparison_scope is PriceComparisonScope.ALL_RETRIEVED:
            grouped: dict[str, set[Platform]] = {}
            for point in self.ranked:
                if point.same_group_id is not None:
                    grouped.setdefault(point.same_group_id, set()).add(point.platform)
            if any(
                group_id not in grouped or len(grouped[group_id]) < 2
                for group_id in self.compared_group_ids
            ):
                raise ValueError("claimed comparable product groups must span platforms")
        elif self.comparison_scope is PriceComparisonScope.COMPARABLE_PRODUCT_GROUPS:
            comparable_groups: dict[str, set[Platform]] = {}
            for point in self.ranked:
                if point.same_group_id is None:
                    raise ValueError("comparable price points require product group IDs")
                comparable_groups.setdefault(point.same_group_id, set()).add(point.platform)
            if set(comparable_groups) != set(self.compared_group_ids) or any(
                len(platforms) < 2 for platforms in comparable_groups.values()
            ):
                raise ValueError("comparable product groups must each span multiple platforms")
        elif self.ranked or self.cheapest_per_platform or self.compared_group_ids:
            raise ValueError("no-comparable-group output must be empty")
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
    destination_country: Literal["CN"]
    advisories: Annotated[tuple[LandedCostAdvisory, ...], Field(max_length=30)]
    ruleset_version: Identifier

    @model_validator(mode="after")
    def ruleset_is_consistent(self) -> Self:
        if any(item.ruleset_version != self.ruleset_version for item in self.advisories):
            raise ValueError("advisory ruleset versions must match")
        return self


class PickedAttribute(FrozenDTO):
    """One source-backed product fact handed from ItemPicker to ShoppingSummary."""

    name: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    value: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    evidence_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=8)]

    @model_validator(mode="after")
    def evidence_is_unique(self) -> Self:
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("picked attribute evidence IDs must be unique")
        return self


class PreferenceMatchStatus(StrEnum):
    """Model assessment of one soft preference against closed candidate facts."""

    MATCHED = "MATCHED"
    UNKNOWN = "UNKNOWN"


class PreferenceCandidate(FrozenDTO):
    """One publication-eligible candidate exposed to the preference evaluator."""

    candidate_id: Identifier
    title: TitleText
    title_evidence_id: Identifier
    attributes: tuple[PickedAttribute, ...] = ()


class PreferenceAssessmentInput(FrozenDTO):
    """Bounded user preferences and source-backed facts; never free-form inventory."""

    preferences: Annotated[tuple[QueryText, ...], Field(min_length=1, max_length=8)]
    candidates: Annotated[tuple[PreferenceCandidate, ...], Field(min_length=1, max_length=30)]

    @model_validator(mode="after")
    def identities_and_preferences_are_unique(self) -> Self:
        candidate_ids = tuple(candidate.candidate_id for candidate in self.candidates)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("preference candidates must be unique")
        if len(self.preferences) != len(set(self.preferences)):
            raise ValueError("preference labels must be unique")
        return self


class PreferenceCriterionAssessment(FrozenDTO):
    """Evidence-bound semantic judgment for one original user preference."""

    preference: QueryText
    status: PreferenceMatchStatus
    evidence_ids: Annotated[tuple[Identifier, ...], Field(max_length=8)] = ()
    reason: Annotated[str, StringConstraints(min_length=1, max_length=256)]

    @model_validator(mode="after")
    def decisive_status_requires_evidence(self) -> Self:
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("preference evidence IDs must be unique")
        if self.status is PreferenceMatchStatus.MATCHED and not self.evidence_ids:
            raise ValueError("decisive preference status requires source evidence")
        return self


class PreferenceCandidateAssessment(FrozenDTO):
    candidate_id: Identifier
    score: Annotated[int, Field(ge=0, le=100)]
    assessments: Annotated[
        tuple[PreferenceCriterionAssessment, ...], Field(min_length=1, max_length=8)
    ]


class PreferenceAssessmentOutput(FrozenDTO):
    candidates: Annotated[
        tuple[PreferenceCandidateAssessment, ...], Field(min_length=1, max_length=30)
    ]


class PickedItem(FrozenDTO):
    """One publication-eligible selection with the exact facts used to explain it."""

    candidate_id: Identifier
    title: TitleText
    platform: Platform
    landed_cost_cny: PositiveAmount | None
    shipping_status: ShippingStatus
    attributes: tuple[PickedAttribute, ...] = ()
    preference_assessments: Annotated[
        tuple[PreferenceCriterionAssessment, ...], Field(max_length=8)
    ] = ()
    score: Annotated[int, Field(ge=0, le=10_000)]
    reasons: Annotated[tuple[ShortText, ...], Field(min_length=1, max_length=3)]
    flags: Annotated[tuple[Identifier, ...], Field(max_length=8)] = ()

    @model_validator(mode="after")
    def picked_facts_are_unique(self) -> Self:
        names = tuple(attribute.name for attribute in self.attributes)
        if len(names) != len(set(names)):
            raise ValueError("picked attribute names must be unique")
        if len(self.flags) != len(set(self.flags)):
            raise ValueError("picked flags must be unique")
        preferences = tuple(item.preference for item in self.preference_assessments)
        if len(preferences) != len(set(preferences)):
            raise ValueError("picked preference assessments must be unique")
        return self


class ItemPickerOutput(FrozenDTO):
    picks: Annotated[tuple[PickedItem, ...], Field(max_length=3)] = ()
    rejected_brief: Annotated[tuple[Identifier, ...], Field(max_length=8)] = ()

    @model_validator(mode="after")
    def selected_ids_are_unique(self) -> Self:
        selected_candidate_ids = self.selected_candidate_ids
        if len(selected_candidate_ids) != len(set(selected_candidate_ids)):
            raise ValueError("picker candidate IDs must be unique")
        if not selected_candidate_ids and self.rejected_brief != (
            ToolFailureCode.NO_ELIGIBLE_CANDIDATE.value,
        ):
            raise ValueError("empty picker output requires a no-eligible rejection")
        if selected_candidate_ids and ToolFailureCode.NO_ELIGIBLE_CANDIDATE.value in (
            self.rejected_brief
        ):
            raise ValueError("non-empty picker cannot claim no eligible candidate")
        return self

    @property
    def selected_candidate_ids(self) -> tuple[str, ...]:
        return tuple(pick.candidate_id for pick in self.picks)


class ShoppingNarrationInput(FrozenDTO):
    """The only bounded facts DeepSeek may use to write the terminal comparison."""

    user_query: QueryText
    display_currency: Literal["CNY"]
    picks: Annotated[tuple[PickedItem, ...], Field(min_length=1, max_length=3)]


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


class AgentAnswerKind(StrEnum):
    SHOPPING_SUMMARY = "SHOPPING_SUMMARY"
    CHAT_FALLBACK = "CHAT_FALLBACK"


class AgentAnswer(FrozenDTO):
    kind: AgentAnswerKind
    text: Annotated[str, StringConstraints(min_length=1, max_length=2_000)]


class AgentToolSummary(FrozenDTO):
    tool_name: ToolName
    call_count: Annotated[int, Field(ge=1, le=10)]
    safe_outcome: Identifier


class AgentDemoResponse(FrozenDTO):
    """The independent public M1d terminal result."""

    schema_version: Literal["glodex.agent-result.v1"] = "glodex.agent-result.v1"
    run_id: Identifier
    status: RunStatus
    answer: AgentAnswer | None = None
    search_response: SearchResponse | None = None
    selected_product_ids: Annotated[tuple[Identifier, ...], Field(max_length=3)] = ()
    evidence_ids: Annotated[
        tuple[Identifier, ...],
        Field(max_length=MAX_RESPONSE_EVIDENCE_IDS),
    ] = ()
    web_evidence: Annotated[tuple[WebEvidence, ...], Field(max_length=8)] = ()
    landed_cost_advisories: Annotated[
        tuple[LandedCostAdvisory, ...],
        Field(max_length=30),
    ] = ()
    tool_summary: Annotated[tuple[AgentToolSummary, ...], Field(max_length=11)] = ()

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
    MODEL_STREAMING = "MODEL_STREAMING"
    MODEL_FINISHED = "MODEL_FINISHED"
    TOOL_STARTED = "TOOL_STARTED"
    TOOL_FINISHED = "TOOL_FINISHED"
    FORK_REQUESTED = "FORK_REQUESTED"
    CHILD_RUN_STARTED = "CHILD_RUN_STARTED"
    CHILD_CHECKPOINT_CONFIRMED = "CHILD_CHECKPOINT_CONFIRMED"
    CHILD_HANDOFF_READY = "CHILD_HANDOFF_READY"
    CHILD_FAILED = "CHILD_FAILED"
    FORK_JOINED = "FORK_JOINED"
    AGENT_RESULT = "AGENT_RESULT"
    AGENT_ERROR = "AGENT_ERROR"


class AgentEventScope(StrEnum):
    ROOT = "root"
    CHILD = "child"


class AgentTraceSource(StrEnum):
    """Origin of one browser-safe research trace fact."""

    USER_INPUT = "USER_INPUT"
    TRUSTED_TOOL = "TRUSTED_TOOL"
    VERIFIED_STATE = "VERIFIED_STATE"


class AgentTraceBullet(FrozenDTO):
    """One bounded fact suitable for the user-visible Agent timeline."""

    label: Annotated[str, StringConstraints(min_length=1, max_length=48)]
    value: Annotated[str, StringConstraints(min_length=1, max_length=192)]
    source: AgentTraceSource


class AgentRunEvent(FrozenDTO):
    """One safe internal fact projected by the independent Agent API."""

    kind: AgentEventKind
    run_id: Identifier
    scope: AgentEventScope = AgentEventScope.ROOT
    round: Annotated[int, Field(ge=1, le=14)] | None = None
    tool_name: ToolName | None = None
    child_id: Identifier | None = None
    depth: Annotated[int, Field(ge=1, le=10)] | None = None
    # Tree routing metadata is emitted only for the v2 fork lifecycle.  It
    # contains stable identifiers/digests, never a child journal or task body.
    parent_run_id: Identifier | None = None
    task_scope_digest: ConfigFingerprint | None = None
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
    platforms: Annotated[tuple[Platform, ...], Field(max_length=8)] = ()
    candidate_count: Annotated[int, Field(strict=True, ge=0, le=50)] | None = None
    trace_bullets: Annotated[tuple[AgentTraceBullet, ...], Field(max_length=6)] = ()

    @model_validator(mode="after")
    def event_fields_match_kind(self) -> Self:
        child_pair = (self.child_id is not None, self.depth is not None)
        tree_pair = (self.parent_run_id is not None, self.task_scope_digest is not None)
        if child_pair[0] is not child_pair[1]:
            raise ValueError("child ID and depth must appear together")
        if tree_pair[0] is not tree_pair[1]:
            raise ValueError("parent run ID and task scope digest must appear together")
        if (self.platforms or self.candidate_count is not None) and self.kind not in {
            AgentEventKind.FORK_REQUESTED,
            AgentEventKind.TOOL_FINISHED,
        }:
            raise ValueError(
                "only fork allocation and completed search events expose progress facts"
            )
        if self.trace_bullets and self.kind is not AgentEventKind.TOOL_FINISHED:
            raise ValueError("only completed tools may expose verified trace facts")
        if self.scope is AgentEventScope.CHILD and not child_pair[0]:
            raise ValueError("child-scoped events require child identity")
        if (
            self.scope is AgentEventScope.ROOT
            and child_pair[0]
            and self.kind
            not in {
                AgentEventKind.FORK_REQUESTED,
                AgentEventKind.FORK_JOINED,
            }
        ):
            raise ValueError("root events cannot carry child identity")

        tree_lifecycle = {
            AgentEventKind.FORK_REQUESTED,
            AgentEventKind.FORK_JOINED,
            AgentEventKind.CHILD_RUN_STARTED,
            AgentEventKind.CHILD_CHECKPOINT_CONFIRMED,
            AgentEventKind.CHILD_HANDOFF_READY,
            AgentEventKind.CHILD_FAILED,
        }
        if self.kind in tree_lifecycle:
            if not tree_pair[0]:
                raise ValueError("fork lifecycle events require tree routing metadata")
        elif tree_pair[0]:
            raise ValueError("only fork lifecycle events may carry tree routing metadata")

        if self.kind is AgentEventKind.AGENT_STARTED:
            expected = (None, None, None, None)
            actual = (self.round, self.tool_name, self.status, self.safe_code)
            if self.scope is not AgentEventScope.ROOT or child_pair[0] or actual != expected:
                raise ValueError("AGENT_STARTED has no step payload")
        elif self.kind in {
            AgentEventKind.MODEL_STARTED,
            AgentEventKind.MODEL_STREAMING,
        }:
            if (
                self.round is None
                or self.tool_name is not None
                or self.status is not None
                or self.safe_code is not None
            ):
                raise ValueError(f"{self.kind.value} requires only a round")
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
            search_succeeded = (
                self.tool_name is ToolName.ITEM_SEARCH and self.safe_code == "SUCCESS"
            )
            if search_succeeded is not (
                len(self.platforms) == 1 and self.candidate_count is not None
            ):
                raise ValueError("successful item search requires platform and candidate count")
            if not search_succeeded and (self.platforms or self.candidate_count is not None):
                raise ValueError("non-search tool outcomes cannot expose search progress")
        elif self.kind is AgentEventKind.FORK_REQUESTED:
            if (
                not child_pair[0]
                or self.parent_run_id != self.run_id
                or self.round is not None
                or self.tool_name is not None
                or self.status is not None
                or self.safe_code is not None
                or not self.platforms
                or self.candidate_count is not None
            ):
                raise ValueError(
                    "FORK_REQUESTED requires parent routing, child identity and platforms"
                )
        elif self.kind is AgentEventKind.FORK_JOINED:
            if (
                not child_pair[0]
                or self.parent_run_id != self.run_id
                or self.round is not None
                or self.tool_name is not None
                or self.status not in {"COMPLETED", "FAILED", "ABORTED"}
                or self.safe_code is not None
            ):
                raise ValueError("FORK_JOINED requires parent routing, child identity and status")
        elif self.kind in {
            AgentEventKind.CHILD_RUN_STARTED,
            AgentEventKind.CHILD_CHECKPOINT_CONFIRMED,
        }:
            if (
                self.scope is not AgentEventScope.CHILD
                or not child_pair[0]
                or self.round is not None
                or self.tool_name is not None
                or self.status is not None
                or self.safe_code is not None
            ):
                raise ValueError(f"{self.kind.value} requires child tree routing metadata")
        elif self.kind is AgentEventKind.CHILD_HANDOFF_READY:
            if (
                self.scope is not AgentEventScope.CHILD
                or not child_pair[0]
                or self.round is not None
                or self.tool_name is not None
                or self.status not in {"COMPLETED", "FAILED", "ABORTED"}
                or self.safe_code is not None
            ):
                raise ValueError("CHILD_HANDOFF_READY requires child identity and status")
        elif self.kind is AgentEventKind.CHILD_FAILED:
            if (
                self.scope is not AgentEventScope.CHILD
                or not child_pair[0]
                or self.round is not None
                or self.tool_name is not None
                or self.status != "FAILED"
                or self.safe_code is None
            ):
                raise ValueError("CHILD_FAILED requires child identity and a safe code")
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
    child_runs: Annotated[int, Field(ge=0, le=10)]
    terminal_code: Identifier | None = None
    tool_summary: Annotated[tuple[AgentToolSummary, ...], Field(max_length=11)] = ()
    events: tuple[AgentRunEvent, ...] = ()

    @model_validator(mode="after")
    def record_matches_terminal(self) -> Self:
        if any(
            event.scope is AgentEventScope.ROOT
            and event.run_id != self.run_id
            and event.kind not in {AgentEventKind.FORK_REQUESTED, AgentEventKind.FORK_JOINED}
            for event in self.events
        ):
            raise ValueError("only root fork lifecycle events may use another run ID")
        if any(
            event.scope is AgentEventScope.CHILD and event.run_id == self.run_id
            for event in self.events
        ):
            raise ValueError("child events must carry their own run ID")
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


__all__ = [
    "BUSINESS_TOOL_SET",
    "CURRENT_PRODUCT_PLATFORMS",
    "FULL_TOOL_SET",
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
    "AgentTraceBullet",
    "AgentTraceSource",
    "AttributeDistribution",
    "BestsellerInsight",
    "Candidate",
    "CandidateAttribute",
    "CategoryInsightInput",
    "CategoryInsightOutput",
    "ChatFallbackOutput",
    "ComparisonTarget",
    "CostComponentStatus",
    "DataMode",
    "DutyTier",
    "EmbeddingBatch",
    "EmbeddingResult",
    "EvidenceKind",
    "ForkDemand",
    "ForkDemandInput",
    "ForkObjectiveCode",
    "ForkReasonCode",
    "InsightDepth",
    "InsightStatus",
    "ItemPickerOutput",
    "ItemSearchInput",
    "ItemSearchRuntimeResult",
    "LandedCostAdvisory",
    "PickedAttribute",
    "PickedItem",
    "PlannerBudgetInput",
    "PlannerComparisonTargetInput",
    "PlannerCriterionInput",
    "PlannerDecisionInput",
    "PlannerFallbackReason",
    "PlannerInput",
    "PlannerIntentKind",
    "PlannerOutput",
    "Platform",
    "PreferenceAssessmentInput",
    "PreferenceAssessmentOutput",
    "PreferenceCandidate",
    "PreferenceCandidateAssessment",
    "PreferenceCriterionAssessment",
    "PreferenceMatchStatus",
    "PriceCompareOutput",
    "PriceComparisonScope",
    "PricePoint",
    "PricePointStatus",
    "PriceTierInsight",
    "SemanticAssertionCandidate",
    "SemanticAssertionInput",
    "SemanticAssertionOutput",
    "ShippingCalcOutput",
    "ShippingStatus",
    "ShoppingNarrationInput",
    "ShoppingSummaryOutput",
    "ToolFailureCode",
    "ToolName",
    "WebEvidence",
    "WebSearchInput",
    "WebSearchOutput",
]
