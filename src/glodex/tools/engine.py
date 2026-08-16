"""Nine concrete M1d business tools and their static A01 registry."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, localcontext
from types import MappingProxyType
from typing import Literal

from glodex.agent.catalog import (
    CandidateEligibility,
    InMemoryCatalogGateway,
    RebindingMap,
    ValidatedCandidatePool,
    evaluate_candidate_pool,
    rebind_selected_candidates,
)
from glodex.agent.contracts import (
    Candidate,
    CategoryInsightInput,
    CategoryInsightOutput,
    ChatFallbackOutput,
    CostComponentStatus,
    DataMode,
    DutyTier,
    EmbeddingResult,
    InsightStatus,
    ItemPickerOutput,
    ItemSearchInput,
    ItemSearchRuntimeResult,
    LandedCostAdvisory,
    PickedAttribute,
    PickedItem,
    PlannerFallbackReason,
    PlannerInput,
    PlannerIntentKind,
    PlannerOutput,
    Platform,
    PreferenceAssessmentInput,
    PreferenceAssessmentOutput,
    PreferenceCandidate,
    PreferenceCandidateAssessment,
    PreferenceMatchStatus,
    PriceCompareOutput,
    PriceComparisonScope,
    PricePoint,
    PricePointStatus,
    ShippingCalcOutput,
    ShippingStatus,
    ShoppingNarrationInput,
    ShoppingSummaryOutput,
    ToolFailureCode,
    ToolName,
    WebSearchInput,
    WebSearchOutput,
)
from glodex.agent.ports import (
    CategoryInsightPort,
    ItemSourcePort,
    PreferenceAssessmentPort,
    SemanticAssertionPort,
    ShoppingSummaryPort,
    ToolPortError,
    WebSearchPort,
)
from glodex.agent.state import (
    TOOL_RESULT_BYTE_LIMIT,
    BudgetExceeded,
    require_byte_limit,
)
from glodex.application.search_service import SearchService
from glodex.contracts import (
    MAX_RESULT_EVIDENCE_IDS,
    RunStatus,
    SearchRequest,
    SearchResponse,
    SearchResult,
)
from glodex.domain.catalog import (
    CanonicalProduct,
    CatalogBatch,
    ExchangeRateTable,
    Offer,
    aggregate_catalog_batch,
)
from glodex.domain.evidence import EvidenceEntityType, EvidenceRef
from glodex.domain.intent import (
    BudgetMax,
    InterpretedRequest,
    PreferredCriterion,
    RequiredConstraint,
    SourceSpan,
    required_constraints_match,
)
from glodex.domain.pricing import KnownCost, UnknownCost, pricing_context
from glodex.domain.ranking import lexical_tokens


@dataclass(frozen=True, slots=True)
class ChatFallbackInput:
    reason_code: PlannerFallbackReason

    def __post_init__(self) -> None:
        if type(self.reason_code) is not PlannerFallbackReason:
            raise TypeError("fallback reason must be an exact PlannerFallbackReason")


@dataclass(frozen=True, slots=True)
class ItemSearchToolInput:
    request: ItemSearchInput
    data_mode: DataMode
    query_vector: tuple[float, ...] | None
    preference_vector: tuple[float, ...] | None = None

    def __post_init__(self) -> None:
        if type(self.request) is not ItemSearchInput:
            raise TypeError("item tool request must be an exact ItemSearchInput")
        if type(self.data_mode) is not DataMode:
            raise TypeError("item tool data_mode must be an exact DataMode")
        if self.data_mode is not DataMode.SYNTHETIC_INTERVIEW:
            raise ValueError("item search data mode is unsupported")
        if type(self.query_vector) is not tuple:
            raise ValueError("snapshot item search requires a query vector")
        EmbeddingResult(vectors=(self.query_vector,))
        if self.preference_vector is not None:
            if type(self.preference_vector) is not tuple:
                raise ValueError("item search preference vector is invalid")
            EmbeddingResult(vectors=(self.preference_vector,))
            if len(self.preference_vector) != len(self.query_vector):
                raise ValueError("item search vector dimensions differ")


@dataclass(frozen=True, slots=True)
class PriceCompareInput:
    pool: ValidatedCandidatePool
    top_n: int = 12

    def __post_init__(self) -> None:
        if type(self.pool) is not ValidatedCandidatePool:
            raise TypeError("price compare pool must be an exact ValidatedCandidatePool")
        if type(self.pool.evaluation_batch.exchange_rates) is not ExchangeRateTable:
            raise ValueError("price compare pool requires an evidence-closed FX table")
        if type(self.top_n) is not int or isinstance(self.top_n, bool) or not 1 <= self.top_n <= 30:
            raise ValueError("price compare top_n must be between one and 30")

    @property
    def exchange_rates(self) -> ExchangeRateTable:
        rates = self.pool.evaluation_batch.exchange_rates
        if type(rates) is not ExchangeRateTable:
            raise ValueError("price compare pool requires an evidence-closed FX table")
        return rates


@dataclass(frozen=True, slots=True)
class ShippingRule:
    platform: Platform
    flat_shipping: Decimal | None
    duty_rate: Decimal | None
    duty_threshold: Decimal | None
    effective_date: str
    eta_days_min: int | None
    eta_days_max: int | None

    def __post_init__(self) -> None:
        if type(self.platform) is not Platform:
            raise TypeError("shipping rule platform must be an exact Platform")
        for name, value in (
            ("flat_shipping", self.flat_shipping),
            ("duty_rate", self.duty_rate),
            ("duty_threshold", self.duty_threshold),
        ):
            if value is not None and (
                type(value) is not Decimal or not value.is_finite() or value < 0
            ):
                raise ValueError(f"{name} must be a finite non-negative Decimal or None")
        if self.duty_rate is not None and self.duty_rate > 1:
            raise ValueError("duty_rate cannot exceed one")
        _require_date(self.effective_date, name="effective_date")
        _require_optional_non_negative_integer(
            self.eta_days_min,
            name="eta_days_min",
        )
        _require_optional_non_negative_integer(
            self.eta_days_max,
            name="eta_days_max",
        )
        if (self.eta_days_min is None) is not (self.eta_days_max is None):
            raise ValueError("shipping rule ETA bounds must both be present or absent")
        if (
            self.eta_days_min is not None
            and self.eta_days_max is not None
            and self.eta_days_max < self.eta_days_min
        ):
            raise ValueError("shipping rule ETA maximum cannot precede minimum")


@dataclass(frozen=True, slots=True)
class ShippingCalcInput:
    pool: ValidatedCandidatePool
    price_points: PriceCompareOutput
    destination_country: Literal["CN"]
    rules: tuple[ShippingRule, ...]
    ruleset_version: str
    calculation_date: str

    def __post_init__(self) -> None:
        if type(self.pool) is not ValidatedCandidatePool:
            raise TypeError("shipping input requires an exact ValidatedCandidatePool")
        if type(self.pool.evaluation_batch.exchange_rates) is not ExchangeRateTable:
            raise ValueError("shipping input requires an evidence-closed FX table")
        if type(self.price_points) is not PriceCompareOutput:
            raise TypeError("shipping input requires an exact PriceCompareOutput")
        if self.destination_country != "CN":
            raise ValueError("shipping destination must be CN")
        if len(self.price_points.ranked) > 30:
            raise ValueError("shipping input cannot contain more than 30 price points")
        candidates_by_id = {candidate.candidate_id: candidate for candidate in self.pool.candidates}
        point_ids = tuple(point.candidate_id for point in self.price_points.ranked)
        if len(point_ids) != len(set(point_ids)):
            raise ValueError("shipping price points must have unique candidate IDs")
        for point in self.price_points.ranked:
            candidate = candidates_by_id.get(point.candidate_id)
            if candidate is None or (
                point.platform is not candidate.platform
                or point.source_ref != candidate.source_ref
                or point.source_amount != candidate.price
                or point.source_currency != candidate.currency
                or point.same_group_id != candidate.same_group_id
            ):
                raise ValueError("shipping price points must bind to the candidate pool")
        if type(self.rules) is not tuple or any(
            type(rule) is not ShippingRule for rule in self.rules
        ):
            raise TypeError("shipping rules must contain exact ShippingRule values")
        platforms = tuple(rule.platform for rule in self.rules)
        if len(platforms) != len(set(platforms)):
            raise ValueError("shipping rules cannot repeat a platform")
        effective_dates = tuple(rule.effective_date for rule in self.rules)
        if len(set(effective_dates)) > 1:
            raise ValueError("one ruleset cannot mix effective dates")
        _require_identifier(self.ruleset_version, "ruleset_version")
        _require_date(self.calculation_date, name="calculation_date")


@dataclass(frozen=True, slots=True)
class ItemPickerInput:
    eligibility: CandidateEligibility
    prices: PriceCompareOutput
    shipping: ShippingCalcOutput
    preferred: tuple[PreferredCriterion, ...]
    category_insight: CategoryInsightOutput | None
    target_candidate_groups: tuple[TargetCandidateGroup, ...]
    max_items: int
    budget: BudgetMax | None = None
    preference_assessment: PreferenceAssessmentOutput | None = None

    def __post_init__(self) -> None:
        if type(self.eligibility) is not CandidateEligibility:
            raise TypeError("picker requires exact canonical eligibility")
        if type(self.prices) is not PriceCompareOutput:
            raise TypeError("picker prices must be an exact PriceCompareOutput")
        if type(self.shipping) is not ShippingCalcOutput:
            raise TypeError("picker shipping must be an exact ShippingCalcOutput")
        priced_ids = {point.candidate_id for point in self.prices.ranked}
        shipping_ids = {advisory.candidate_id for advisory in self.shipping.advisories}
        if not set(self.publication_eligible_ids).issubset(priced_ids & shipping_ids):
            raise ValueError("picker eligibility must be covered by price and shipping results")
        if type(self.preferred) is not tuple or any(
            type(criterion) is not PreferredCriterion for criterion in self.preferred
        ):
            raise TypeError("picker preferred must contain exact PreferredCriterion values")
        for criterion in self.preferred:
            if type(criterion.value) is not str or not criterion.value.strip():
                raise ValueError("picker preferred values must be non-empty")
            if (
                type(criterion.source_span) is not SourceSpan
                or type(criterion.source_span.text) is not str
                or not criterion.source_span.text.strip()
            ):
                raise TypeError("picker preferred requires validated source spans")
        if self.category_insight is not None:
            if type(self.category_insight) is not CategoryInsightOutput:
                raise TypeError("picker category insight must be exact or None")
            if self.category_insight.status is not InsightStatus.FOUND:
                raise ValueError("picker requires FOUND category insight or explicit None")
        if type(self.target_candidate_groups) is not tuple or any(
            type(group) is not TargetCandidateGroup for group in self.target_candidate_groups
        ):
            raise TypeError("picker target groups must contain exact values")
        target_queries = tuple(group.target_query for group in self.target_candidate_groups)
        if len(target_queries) != len(set(target_queries)):
            raise ValueError("picker target groups must be unique")
        candidate_ids = {candidate.candidate_id for candidate in self.pool.candidates}
        if any(
            not set(group.candidate_ids).issubset(candidate_ids)
            for group in self.target_candidate_groups
        ):
            raise ValueError("picker target candidates must come from the candidate pool")
        if (
            type(self.max_items) is not int
            or isinstance(self.max_items, bool)
            or not 1 <= self.max_items <= 3
        ):
            raise ValueError("picker max_items must be between one and three")
        if len(self.target_candidate_groups) > self.max_items:
            raise ValueError("picker max_items cannot cover every comparison target")
        if self.budget is not None and type(self.budget) is not BudgetMax:
            raise TypeError("picker budget must be an exact BudgetMax or None")
        if self.preference_assessment is not None:
            if type(self.preference_assessment) is not PreferenceAssessmentOutput:
                raise TypeError("picker preference assessment must be exact or None")
            assessed_ids = tuple(
                candidate.candidate_id for candidate in self.preference_assessment.candidates
            )
            if len(assessed_ids) != len(set(assessed_ids)) or not set(assessed_ids).issubset(
                candidate_ids
            ):
                raise ValueError("picker preference assessments must bind to its pool")
            expected_preferences = tuple(criterion.source_span.text for criterion in self.preferred)
            if any(
                tuple(item.preference for item in candidate.assessments) != expected_preferences
                for candidate in self.preference_assessment.candidates
            ):
                raise ValueError("picker preference assessments changed user preferences")

    @property
    def pool(self) -> ValidatedCandidatePool:
        return self.eligibility.pool

    @property
    def publication_eligible_ids(self) -> tuple[str, ...]:
        return self.eligibility.eligible_candidate_ids


@dataclass(frozen=True, slots=True)
class TargetCandidateGroup:
    """Candidates verified for one grounded Planner comparison target."""

    target_query: str
    candidate_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.target_query) is not str or not self.target_query.strip():
            raise ValueError("target candidate query must be non-empty")
        if (
            type(self.candidate_ids) is not tuple
            or any(
                type(candidate_id) is not str or not candidate_id
                for candidate_id in self.candidate_ids
            )
            or len(self.candidate_ids) != len(set(self.candidate_ids))
        ):
            raise ValueError("target candidate IDs must be unique strings")


type SearchServiceFactory = Callable[
    [InMemoryCatalogGateway, InterpretedRequest],
    SearchService,
]


@dataclass(frozen=True, slots=True)
class ShoppingSummaryInput:
    agent_run_id: str
    request: SearchRequest
    interpreted_request: InterpretedRequest
    eligibility: CandidateEligibility
    picker: ItemPickerOutput
    fx_source_batch: CatalogBatch
    search_service_factory: SearchServiceFactory

    def __post_init__(self) -> None:
        _require_identifier(self.agent_run_id, "agent_run_id")
        if type(self.request) is not SearchRequest:
            raise TypeError("summary request must be an exact SearchRequest")
        if type(self.interpreted_request) is not InterpretedRequest:
            raise TypeError("summary intent must be an exact InterpretedRequest")
        if type(self.eligibility) is not CandidateEligibility:
            raise TypeError("summary requires exact canonical eligibility")
        if type(self.picker) is not ItemPickerOutput:
            raise TypeError("summary picker must be an exact ItemPickerOutput")
        if not set(self.picker.selected_candidate_ids).issubset(self.publication_eligible_ids):
            raise ValueError("picker IDs must come from publication eligibility")
        if not self.picker.selected_candidate_ids and self.publication_eligible_ids:
            raise ValueError("empty picker requires empty publication eligibility")
        if type(self.fx_source_batch) is not CatalogBatch:
            raise TypeError("summary FX source must be an exact CatalogBatch")
        if not callable(self.search_service_factory):
            raise TypeError("summary search service factory must be callable")

    @property
    def pool(self) -> ValidatedCandidatePool:
        return self.eligibility.pool

    @property
    def publication_eligible_ids(self) -> tuple[str, ...]:
        return self.eligibility.eligible_candidate_ids


@dataclass(frozen=True, slots=True)
class ToolDependencies:
    semantic_assertion: SemanticAssertionPort
    shopping_summary: ShoppingSummaryPort
    web_search: WebSearchPort | None = None
    category_insight: CategoryInsightPort | None = None
    item_source: ItemSourcePort | None = None
    preference_assessment: PreferenceAssessmentPort | None = None


def run_planner(tool_input: PlannerInput) -> PlannerOutput:
    """Validate the model's first AgentLoop action against runtime authority."""

    if type(tool_input) is not PlannerInput:
        raise TypeError("planner requires an exact PlannerInput")
    if not required_constraints_match(
        tool_input.required_baseline,
        tool_input.interpreted_request,
    ):
        raise ValueError("planner Required constraints differ from the baseline")
    if tool_input.declared_intent is PlannerIntentKind.UNSUPPORTED_OR_NON_SHOPPING:
        return _fallback_plan(
            tool_input.required_baseline.required,
            PlannerFallbackReason.NON_SHOPPING,
        )
    capabilities = tool_input.capabilities
    if tool_input.requested_platforms:
        unavailable = tuple(
            platform
            for platform in tool_input.requested_platforms
            if platform not in capabilities.available_platforms
        )
        if unavailable:
            return _fallback_plan(
                tool_input.required_baseline.required,
                PlannerFallbackReason.PLATFORM_NOT_CONFIGURED,
            )
        platforms = tool_input.requested_platforms
    else:
        platforms = capabilities.available_platforms
    return PlannerOutput(
        intent_kind=PlannerIntentKind.SHOPPING,
        constraints=tool_input.required_baseline.required,
        platforms=platforms,
        search_query=tool_input.search_query,
        comparison_targets=tool_input.comparison_targets,
    )


def run_chat_fallback(tool_input: ChatFallbackInput) -> ChatFallbackOutput:
    """Render one local bounded shopping redirect for a planner reason."""

    if type(tool_input) is not ChatFallbackInput:
        raise TypeError("chat fallback requires an exact ChatFallbackInput")
    templates = {
        PlannerFallbackReason.NON_SHOPPING: "我可以帮助比较商品; 请告诉我想购买的品类。",
        PlannerFallbackReason.UNSUPPORTED_CATEGORY: "这个品类暂不支持, 请换一个购物品类。",
        PlannerFallbackReason.PLATFORM_NOT_CONFIGURED: "所选平台未配置, 请使用已启用的平台。",
    }
    return ChatFallbackOutput(
        reason_code=tool_input.reason_code,
        answer=templates[tool_input.reason_code],
    )


async def run_web_search(
    tool_input: WebSearchInput,
    port: WebSearchPort | None,
) -> WebSearchOutput:
    if type(tool_input) is not WebSearchInput:
        raise TypeError("web search requires an exact WebSearchInput")
    if port is None:
        raise ToolPortError(ToolFailureCode.WEB_SEARCH_NOT_ENABLED)
    result = await port.search(tool_input)
    if type(result) is not WebSearchOutput:
        raise ToolPortError(ToolFailureCode.PROVIDER_RESPONSE_INVALID)
    if len(result.evidence) > tool_input.max_results or any(
        evidence.source_type is not tool_input.evidence_kind for evidence in result.evidence
    ):
        raise ToolPortError(ToolFailureCode.PROVIDER_RESPONSE_INVALID)
    return result


async def run_category_insight(
    tool_input: CategoryInsightInput,
    port: CategoryInsightPort | None,
) -> CategoryInsightOutput:
    if type(tool_input) is not CategoryInsightInput:
        raise TypeError("category insight requires an exact CategoryInsightInput")
    if port is None:
        raise ToolPortError(ToolFailureCode.INDEX_INVALID)
    result = await port.retrieve(tool_input)
    if type(result) is not CategoryInsightOutput:
        raise ToolPortError(ToolFailureCode.INDEX_INVALID)
    limits = (3, 5, 0, 3) if tool_input.depth.value == "quick" else (8, 5, 12, 3)
    actual = (
        len(result.components),
        len(result.bestsellers),
        len(result.attributes),
        len(result.price_tiers),
    )
    if any(value > maximum for value, maximum in zip(actual, limits, strict=True)):
        raise ToolPortError(ToolFailureCode.INDEX_INVALID)
    return result


async def run_item_search(
    tool_input: ItemSearchToolInput,
    port: ItemSourcePort | None,
) -> ItemSearchRuntimeResult:
    if type(tool_input) is not ItemSearchToolInput:
        raise TypeError("item search requires an exact ItemSearchToolInput")
    if port is None:
        raise ToolPortError(ToolFailureCode.PROVIDER_NOT_CONFIGURED)
    result = await port.search(
        tool_input.request,
        query_vector=tool_input.query_vector,
        preference_vector=tool_input.preference_vector,
    )
    if type(result) is not ItemSearchRuntimeResult:
        raise ToolPortError(ToolFailureCode.ITEM_SOURCE_INVALID)
    if (
        result.platform is not tool_input.request.platform
        or result.retrieval_query != tool_input.request.query
        or len(result.candidates) > tool_input.request.top_k
    ):
        raise ToolPortError(ToolFailureCode.ITEM_SOURCE_INVALID)
    return result


def run_price_compare(tool_input: PriceCompareInput) -> PriceCompareOutput:
    """Convert source prices into versioned CNY facts with stable sorting."""

    if type(tool_input) is not PriceCompareInput:
        raise TypeError("price compare requires an exact PriceCompareInput")
    rates = {rate.currency: rate for rate in tool_input.exchange_rates.rates}
    base_rate = rates.get("CNY")
    indexed: list[tuple[int, PricePoint]] = []
    comparison_candidates, comparison_scope, compared_group_ids = _comparison_candidates(
        tool_input.pool
    )
    for index, candidate in comparison_candidates:
        source_rate = rates.get(candidate.currency)
        if source_rate is None or base_rate is None:
            point = PricePoint(
                candidate_id=candidate.candidate_id,
                platform=candidate.platform,
                source_ref=candidate.source_ref,
                source_amount=candidate.price,
                source_currency=candidate.currency,
                same_group_id=candidate.same_group_id,
                pack_size=candidate.pack_size,
                pack_note=candidate.pack_note,
                status=PricePointStatus.UNKNOWN_FX,
            )
        else:
            with localcontext(pricing_context()):
                base_amount = candidate.price * source_rate.base_per_unit / base_rate.base_per_unit
                per_unit = (
                    None if candidate.pack_size is None else base_amount / candidate.pack_size
                )
            point = PricePoint(
                candidate_id=candidate.candidate_id,
                platform=candidate.platform,
                source_ref=candidate.source_ref,
                source_amount=candidate.price,
                source_currency=candidate.currency,
                same_group_id=candidate.same_group_id,
                base_amount=base_amount,
                fx_evidence_id=source_rate.evidence_id,
                pack_size=candidate.pack_size,
                pack_note=candidate.pack_note,
                per_unit_base_amount=per_unit,
                status=PricePointStatus.EXACT,
            )
        indexed.append((index, point))
    indexed.sort(
        key=lambda pair: (
            1 if pair[1].status is PricePointStatus.UNKNOWN_FX else 0,
            (
                Decimal(0)
                if pair[1].status is PricePointStatus.UNKNOWN_FX
                else pair[1].per_unit_base_amount
                if pair[1].per_unit_base_amount is not None
                else pair[1].base_amount
            ),
            pair[0],
        )
    )
    ranked = tuple(point for _, point in indexed[: tool_input.top_n])
    ranked_group_platforms: dict[str, set[Platform]] = {}
    for point in ranked:
        if point.same_group_id is not None:
            ranked_group_platforms.setdefault(point.same_group_id, set()).add(point.platform)
    compared_group_ids = tuple(
        group_id
        for group_id in compared_group_ids
        if len(ranked_group_platforms.get(group_id, set())) >= 2
    )
    cheapest: dict[str, str] = {}
    seen_platforms: set[Platform] = set()
    for point in ranked:
        if point.status is PricePointStatus.EXACT and point.platform not in seen_platforms:
            cheapest[point.platform.value] = point.candidate_id
            seen_platforms.add(point.platform)
    return PriceCompareOutput(
        ranked=ranked,
        cheapest_per_platform=cheapest,
        comparison_scope=comparison_scope,
        compared_group_ids=compared_group_ids,
    )


def _comparison_candidates(
    pool: ValidatedCandidatePool,
) -> tuple[
    tuple[tuple[int, Candidate], ...],
    PriceComparisonScope,
    tuple[str, ...],
]:
    candidates = tuple(enumerate(pool.candidates))
    grouped: dict[str, list[tuple[int, Candidate]]] = {}
    for index, candidate in enumerate(pool.candidates):
        if candidate.same_group_id is not None:
            grouped.setdefault(candidate.same_group_id, []).append((index, candidate))
    viable_groups = [
        (group_id, members)
        for group_id, members in grouped.items()
        if len({candidate.platform for _index, candidate in members}) >= 2
    ]
    compared_group_ids = tuple(
        group_id for group_id, members in sorted(viable_groups, key=lambda group: group[1][0][0])
    )
    # Comparable same-product groups are useful metadata, never an eligibility
    # gate.  A valid singleton product must remain publishable even when no
    # second marketplace happens to retrieve the exact same document.
    return candidates, PriceComparisonScope.ALL_RETRIEVED, compared_group_ids


def run_shipping_calc(tool_input: ShippingCalcInput) -> ShippingCalcOutput:
    """Prefer source costs, then apply only effective versioned fallback rules."""

    if type(tool_input) is not ShippingCalcInput:
        raise TypeError("shipping calc requires an exact ShippingCalcInput")
    rule_by_platform = {rule.platform: rule for rule in tool_input.rules}
    indexed: list[tuple[int, LandedCostAdvisory]] = []
    for index, point in enumerate(tool_input.price_points.ranked):
        advisory = _build_shipping_advisory(
            tool_input,
            point=point,
            rule=rule_by_platform.get(point.platform),
        )
        indexed.append((index, advisory))
    indexed.sort(
        key=lambda pair: (
            {
                ShippingStatus.EXACT: 0,
                ShippingStatus.ESTIMATE: 1,
                ShippingStatus.UNKNOWN: 2,
            }[pair[1].item_status],
            Decimal(0) if pair[1].landed_total is None else pair[1].landed_total,
            pair[0],
        )
    )
    return ShippingCalcOutput(
        destination_country=tool_input.destination_country,
        advisories=tuple(advisory for _, advisory in indexed),
        ruleset_version=tool_input.ruleset_version,
    )


def run_item_picker(tool_input: ItemPickerInput) -> ItemPickerOutput:
    """Select only deterministic publication-eligible candidate IDs."""

    if type(tool_input) is not ItemPickerInput:
        raise TypeError("item picker requires an exact ItemPickerInput")
    if (
        tool_input.prices.comparison_scope is PriceComparisonScope.NO_COMPARABLE_GROUP
        or not tool_input.publication_eligible_ids
    ):
        return ItemPickerOutput(
            picks=(),
            rejected_brief=(ToolFailureCode.NO_ELIGIBLE_CANDIDATE.value,),
        )
    eligible = set(tool_input.publication_eligible_ids)
    if tool_input.prices.comparison_scope is PriceComparisonScope.COMPARABLE_PRODUCT_GROUPS:
        eligible.intersection_update(point.candidate_id for point in tool_input.prices.ranked)
    if not eligible:
        return ItemPickerOutput(
            picks=(),
            rejected_brief=(ToolFailureCode.NO_ELIGIBLE_CANDIDATE.value,),
        )
    advisory_by_id = {
        advisory.candidate_id: advisory for advisory in tool_input.shipping.advisories
    }
    price_order = {
        point.candidate_id: index for index, point in enumerate(tool_input.prices.ranked)
    }
    candidate_by_id = {
        candidate.candidate_id: candidate for candidate in tool_input.pool.candidates
    }
    candidate_order = {
        candidate.candidate_id: index for index, candidate in enumerate(tool_input.pool.candidates)
    }
    soft_ranks = _picker_soft_ranks(tool_input)
    grouped: dict[str, list[str]] = {}
    for candidate_id in eligible:
        candidate = candidate_by_id[candidate_id]
        group_key = candidate.same_group_id or candidate_id
        grouped.setdefault(group_key, []).append(candidate_id)
    representatives: list[tuple[str, int, tuple[int, int, int], Decimal]] = []
    for members in grouped.values():
        best_offer = min(
            members,
            key=lambda candidate_id: (
                _advisory_rank(advisory_by_id.get(candidate_id)),
                price_order.get(candidate_id, len(price_order)),
                candidate_order[candidate_id],
            ),
        )
        product_order = min(candidate_order[candidate_id] for candidate_id in members)
        product_soft_rank = max(soft_ranks[candidate_id] for candidate_id in members)
        representatives.append(
            (
                best_offer,
                product_order,
                product_soft_rank,
                _picker_budget_distance(
                    tool_input,
                    advisory=advisory_by_id.get(best_offer),
                ),
            )
        )
    representatives.sort(
        key=lambda item: (
            -item[2][0],
            -item[2][1],
            -item[2][2],
            item[3],
            _advisory_rank(advisory_by_id.get(item[0])),
            item[1],
            price_order.get(item[0], len(price_order)),
        )
    )
    representative_by_id = {item[0]: item for item in representatives}
    selected_list: list[str] = []
    for group in tool_input.target_candidate_groups:
        target_representatives = tuple(
            representative_by_id[candidate_id]
            for candidate_id in group.candidate_ids
            if candidate_id in representative_by_id and candidate_id not in selected_list
        )
        if not target_representatives:
            raise ToolPortError(ToolFailureCode.COMPARISON_TARGET_UNCOVERED)
        selected_list.append(
            min(
                target_representatives,
                key=lambda item: representatives.index(item),
            )[0]
        )
    selected_list.extend(item[0] for item in representatives if item[0] not in selected_list)
    selected = tuple(selected_list[: tool_input.max_items])
    picks = tuple(
        _picked_item(
            tool_input,
            candidate_id=candidate_id,
            soft_rank=soft_ranks[candidate_id],
            advisory=advisory_by_id.get(candidate_id),
        )
        for candidate_id in selected
    )
    return ItemPickerOutput(
        picks=picks,
        rejected_brief=tool_input.eligibility.excluded_candidate_ids[:8],
    )


def _picked_item(
    tool_input: ItemPickerInput,
    *,
    candidate_id: str,
    soft_rank: tuple[int, int, int],
    advisory: LandedCostAdvisory | None,
) -> PickedItem:
    candidate_by_id = {
        candidate.candidate_id: candidate for candidate in tool_input.pool.candidates
    }
    candidate = candidate_by_id[candidate_id]
    attributes = _closed_picker_attributes(tool_input, candidate_id=candidate_id)
    if advisory is None:
        raise ValueError("picked candidate shipping advisory is unavailable")
    reasons = [
        ("到手价未知" if advisory.landed_total is None else f"到手价 {advisory.landed_total} CNY")
    ]
    if attributes:
        reasons.append(f"{len(attributes)} 项商品属性具有来源证据")
    if tool_input.category_insight is not None:
        reasons.append(f"品类洞察置信度 {tool_input.category_insight.confidence}")
    flags: list[str] = []
    if advisory.item_status is ShippingStatus.UNKNOWN:
        flags.append("LANDED_COST_UNKNOWN")
    if not attributes:
        flags.append("PRODUCT_ATTRIBUTES_UNAVAILABLE")
    return PickedItem(
        candidate_id=candidate.candidate_id,
        title=candidate.title,
        platform=candidate.platform,
        landed_cost_cny=advisory.landed_total,
        shipping_status=advisory.item_status,
        attributes=tuple(attributes),
        preference_assessments=next(
            (
                assessment.assessments
                for assessment in (
                    ()
                    if tool_input.preference_assessment is None
                    else tool_input.preference_assessment.candidates
                )
                if assessment.candidate_id == candidate_id
            ),
            (),
        ),
        score=soft_rank[0] * 100 + soft_rank[1] * 10 + soft_rank[2],
        reasons=tuple(reasons[:3]),
        flags=tuple(flags),
    )


def _closed_picker_attributes(
    tool_input: ItemPickerInput,
    *,
    candidate_id: str,
) -> tuple[PickedAttribute, ...]:
    record_by_id = {
        candidate.candidate_id: record
        for candidate, record in zip(
            tool_input.pool.candidates,
            tool_input.pool.records,
            strict=True,
        )
    }
    record = record_by_id[candidate_id]
    aggregation = aggregate_catalog_batch(tool_input.pool.evaluation_batch)
    product = next(
        (item for item in aggregation.products if item.product_id == record.product_id),
        None,
    )
    if product is None:
        raise ValueError("picked candidate product is unavailable")
    evidence_by_id = {item.evidence_id: item for item in tool_input.pool.evaluation_batch.evidence}
    attributes: list[PickedAttribute] = []
    for attribute in product.attributes:
        field_path = f"product.attributes.{attribute.name}"
        if not all(
            _is_closed_product_evidence(
                evidence_by_id.get(evidence_id),
                evidence_id=evidence_id,
                product_id=product.product_id,
                snapshot_version=tool_input.pool.evaluation_batch.snapshot_version,
                field_path=field_path,
            )
            for evidence_id in attribute.evidence_ids
        ):
            raise ValueError("picked candidate attribute evidence is not closed")
        attributes.append(
            PickedAttribute(
                name=attribute.name,
                value=attribute.value,
                evidence_ids=attribute.evidence_ids,
            )
        )
    return tuple(attributes)


def _preference_assessment_input(tool_input: ItemPickerInput) -> PreferenceAssessmentInput:
    """Expose one closed product record per offer group to the semantic evaluator."""

    eligible = set(tool_input.publication_eligible_ids)
    prioritized_ids = [
        candidate_id
        for group in tool_input.target_candidate_groups
        for candidate_id in group.candidate_ids
        if candidate_id in eligible
    ]
    prioritized_ids.extend(
        candidate.candidate_id
        for candidate in tool_input.pool.candidates
        if candidate.candidate_id in eligible and candidate.candidate_id not in prioritized_ids
    )
    candidate_by_id = {
        candidate.candidate_id: candidate for candidate in tool_input.pool.candidates
    }
    representative_ids: list[str] = []
    seen_groups: set[str] = set()
    for candidate_id in prioritized_ids:
        candidate = candidate_by_id[candidate_id]
        group_id = candidate.same_group_id or candidate.candidate_id
        if group_id in seen_groups:
            continue
        seen_groups.add(group_id)
        representative_ids.append(candidate_id)
    return PreferenceAssessmentInput(
        preferences=tuple(criterion.source_span.text for criterion in tool_input.preferred),
        candidates=tuple(
            PreferenceCandidate(
                candidate_id=candidate_id,
                title=candidate_by_id[candidate_id].title,
                title_evidence_id=_picker_title_evidence_id(
                    tool_input,
                    candidate_id=candidate_id,
                ),
                attributes=_closed_picker_attributes(tool_input, candidate_id=candidate_id),
            )
            for candidate_id in representative_ids[:30]
        ),
    )


def _expand_group_preference_assessment(
    tool_input: ItemPickerInput,
    assessment: PreferenceAssessmentOutput,
) -> PreferenceAssessmentOutput:
    """Reuse product-level judgments for its equivalent platform offers."""

    assessed_by_id = {item.candidate_id: item for item in assessment.candidates}
    representative_by_group: dict[str, PreferenceCandidateAssessment] = {}
    candidate_by_id = {
        candidate.candidate_id: candidate for candidate in tool_input.pool.candidates
    }
    for candidate_id, item in assessed_by_id.items():
        candidate = candidate_by_id[candidate_id]
        representative_by_group[candidate.same_group_id or candidate_id] = item
    expanded: list[PreferenceCandidateAssessment] = []
    eligible = set(tool_input.publication_eligible_ids)
    for candidate in tool_input.pool.candidates:
        if candidate.candidate_id not in eligible:
            continue
        group_id = candidate.same_group_id or candidate.candidate_id
        representative = representative_by_group.get(group_id)
        if representative is None:
            raise ToolPortError(ToolFailureCode.PROVIDER_RESPONSE_INVALID)
        expanded.append(
            PreferenceCandidateAssessment(
                candidate_id=candidate.candidate_id,
                score=representative.score,
                assessments=representative.assessments,
            )
        )
    return PreferenceAssessmentOutput(candidates=tuple(expanded))


def _picker_title_evidence_id(
    tool_input: ItemPickerInput,
    *,
    candidate_id: str,
) -> str:
    record_by_id = {
        candidate.candidate_id: record
        for candidate, record in zip(
            tool_input.pool.candidates,
            tool_input.pool.records,
            strict=True,
        )
    }
    record = record_by_id[candidate_id]
    aggregation = aggregate_catalog_batch(tool_input.pool.evaluation_batch)
    product = next(
        (item for item in aggregation.products if item.product_id == record.product_id),
        None,
    )
    if product is None:
        raise ValueError("picker candidate product is unavailable")
    binding = next(
        (item for item in product.field_evidence if item.field_path == "product.title"),
        None,
    )
    if binding is None:
        raise ValueError("picker candidate title evidence is unavailable")
    evidence_by_id = {item.evidence_id: item for item in tool_input.pool.evaluation_batch.evidence}
    if not _is_closed_product_evidence(
        evidence_by_id.get(binding.evidence_id),
        evidence_id=binding.evidence_id,
        product_id=product.product_id,
        snapshot_version=tool_input.pool.evaluation_batch.snapshot_version,
        field_path="product.title",
    ):
        raise ValueError("picker candidate title evidence is not closed")
    return binding.evidence_id


async def run_shopping_summary(
    tool_input: ShoppingSummaryInput,
    port: ShoppingSummaryPort,
) -> ShoppingSummaryOutput:
    """Run the final gate, then let DeepSeek narrate only ItemPicker's verified picks."""

    if type(tool_input) is not ShoppingSummaryInput:
        raise TypeError("shopping summary requires an exact ShoppingSummaryInput")
    interpreted = tool_input.interpreted_request
    fx_source_batch = tool_input.fx_source_batch
    confirmed_eligibility = evaluate_candidate_pool(
        tool_input.pool,
        interpreted,
        display_currency=tool_input.request.display_currency,
        excluded_candidate_ids=tool_input.eligibility.excluded_candidate_ids,
    )
    if (
        confirmed_eligibility.eligible_candidate_ids
        != tool_input.eligibility.eligible_candidate_ids
    ):
        raise ToolPortError(ToolFailureCode.FINAL_GATE_FAILED)
    selected_ids = tool_input.picker.selected_candidate_ids
    rebound = rebind_selected_candidates(
        tool_input.pool,
        selected_ids,
        run_id=tool_input.agent_run_id,
        fx_source_batch=fx_source_batch,
    )
    budget = next(
        (criterion for criterion in interpreted.required if type(criterion) is BudgetMax),
        None,
    )
    budget_currency = (
        None
        if budget is None
        else budget.currency
        if budget.currency is not None
        else tool_input.request.display_currency
    )
    gateway = InMemoryCatalogGateway(
        rebound.batch,
        display_currency=tool_input.request.display_currency,
        budget_currency=budget_currency,
    )
    try:
        service = tool_input.search_service_factory(gateway, interpreted)
        if type(service) is not SearchService:
            raise TypeError("summary factory must return an exact SearchService")
        derived_request = SearchRequest(
            query=tool_input.request.query,
            locale=tool_input.request.locale,
            display_currency=tool_input.request.display_currency,
            top_k=tool_input.request.top_k,
            snapshot_version=rebound.batch.snapshot_version,
        )
        execution = await service.execute_run(
            derived_request,
            run_id=tool_input.agent_run_id,
            observer=None,
        )
    finally:
        gateway.clear()
    response = execution.response
    status: Literal["COMPLETED", "NO_MATCH"]
    if selected_ids:
        allowed_products = {
            rebound.mapping.for_candidate(candidate_id).product_id for candidate_id in selected_ids
        }
        actual_products = {result.product_id for result in response.results}
        if (
            response.status is not RunStatus.COMPLETED
            or not actual_products
            or not actual_products.issubset(allowed_products)
        ):
            raise ToolPortError(ToolFailureCode.FINAL_GATE_FAILED)
        result_by_product_id = {result.product_id: result for result in response.results}
        selected_product_order = tuple(
            rebound.mapping.for_candidate(candidate_id).product_id for candidate_id in selected_ids
        )
        response = SearchResponse.model_validate(
            {
                **response.model_dump(mode="python"),
                "results": tuple(
                    result_by_product_id[product_id]
                    for product_id in selected_product_order
                    if product_id in result_by_product_id
                ),
            }
        )
        pick_by_product_id = {
            rebound.mapping.for_candidate(pick.candidate_id).product_id: pick
            for pick in tool_input.picker.picks
        }
        try:
            narration_picks = tuple(
                pick_by_product_id[result.product_id] for result in response.results
            )
        except KeyError as error:
            raise ToolPortError(ToolFailureCode.FINAL_GATE_FAILED) from error
        response = _apply_preference_assessments(
            response,
            pick_by_product_id=pick_by_product_id,
            rebinding=rebound.mapping,
        )
        answer = await port.summarize(
            ShoppingNarrationInput(
                user_query=tool_input.request.query,
                display_currency="CNY",
                picks=narration_picks,
            )
        )
        status = "COMPLETED"
    else:
        if response.status is not RunStatus.NO_MATCH:
            raise ToolPortError(ToolFailureCode.FINAL_GATE_FAILED)
        answer = "没有通过全部硬性条件的商品。"
        status = "NO_MATCH"
    return ShoppingSummaryOutput(
        status=status,
        answer=answer,
        search_response=response,
        selected_product_ids=tuple(result.product_id for result in response.results),
    )


def _apply_preference_assessments(
    response: SearchResponse,
    *,
    pick_by_product_id: Mapping[str, PickedItem],
    rebinding: RebindingMap,
) -> SearchResponse:
    """Project validated semantic judgments without weakening any hard gate."""

    projected: list[SearchResult] = []
    for result in response.results:
        pick = pick_by_product_id[result.product_id]
        if not pick.preference_assessments:
            projected.append(result)
            continue
        labels = {assessment.preference for assessment in pick.preference_assessments}
        unknowns = [
            value
            for value in result.unknowns
            if not any(
                value
                in {
                    f"未证实偏好：{label}",  # noqa: RUF001
                    f"偏好证据不足：{label}",  # noqa: RUF001
                }
                for label in labels
            )
        ]
        reason_parts = [result.reason]
        evidence_by_id = {item.evidence_id: item for item in result.evidence}
        evidence_ids = list(evidence_by_id)
        for assessment in pick.preference_assessments:
            rebound_assessment_evidence = tuple(
                rebinding.for_evidence(evidence_id) for evidence_id in assessment.evidence_ids
            )
            if assessment.status is PreferenceMatchStatus.MATCHED:
                reason_parts.append(f"匹配偏好: {assessment.preference} ({assessment.reason})")
                evidence_ids.extend(rebound_assessment_evidence)
            else:
                unknowns.append(f"偏好证据不足：{assessment.preference}")  # noqa: RUF001
                reason_parts.append(f"偏好证据不足: {assessment.preference} ({assessment.reason})")
        unique_evidence_ids = tuple(dict.fromkeys(evidence_ids))[:MAX_RESULT_EVIDENCE_IDS]
        projected.append(
            SearchResult.model_validate(
                {
                    **result.model_dump(mode="python"),
                    "unknowns": tuple(unknowns),
                    "reason": "; ".join(reason_parts),
                    "evidence": tuple(
                        evidence_by_id[evidence_id] for evidence_id in unique_evidence_ids
                    ),
                }
            )
        )
    return SearchResponse.model_validate(
        {**response.model_dump(mode="python"), "results": tuple(projected)}
    )


type BusinessToolResult = (
    PlannerOutput
    | ChatFallbackOutput
    | WebSearchOutput
    | CategoryInsightOutput
    | ItemSearchRuntimeResult
    | PriceCompareOutput
    | ShippingCalcOutput
    | ItemPickerOutput
    | ShoppingSummaryOutput
)
type BusinessToolHandler = Callable[
    [object, ToolDependencies],
    Awaitable[BusinessToolResult],
]


async def _planner_handler(
    value: object,
    dependencies: ToolDependencies,
) -> BusinessToolResult:
    del dependencies
    if type(value) is not PlannerInput:
        raise TypeError("planner input type mismatch")
    return run_planner(value)


async def _fallback_handler(
    value: object,
    dependencies: ToolDependencies,
) -> BusinessToolResult:
    del dependencies
    if type(value) is not ChatFallbackInput:
        raise TypeError("chat_fallback input type mismatch")
    return run_chat_fallback(value)


async def _web_handler(
    value: object,
    dependencies: ToolDependencies,
) -> BusinessToolResult:
    if type(value) is not WebSearchInput:
        raise TypeError("web_search input type mismatch")
    return await run_web_search(value, dependencies.web_search)


async def _category_handler(
    value: object,
    dependencies: ToolDependencies,
) -> BusinessToolResult:
    if type(value) is not CategoryInsightInput:
        raise TypeError("category_insight input type mismatch")
    return await run_category_insight(value, dependencies.category_insight)


async def _item_handler(
    value: object,
    dependencies: ToolDependencies,
) -> BusinessToolResult:
    if type(value) is not ItemSearchToolInput:
        raise TypeError("item_search input type mismatch")
    return await run_item_search(value, dependencies.item_source)


async def _price_handler(
    value: object,
    dependencies: ToolDependencies,
) -> BusinessToolResult:
    del dependencies
    if type(value) is not PriceCompareInput:
        raise TypeError("price_compare input type mismatch")
    return run_price_compare(value)


async def _shipping_handler(
    value: object,
    dependencies: ToolDependencies,
) -> BusinessToolResult:
    del dependencies
    if type(value) is not ShippingCalcInput:
        raise TypeError("shipping_calc input type mismatch")
    return run_shipping_calc(value)


async def _picker_handler(
    value: object,
    dependencies: ToolDependencies,
) -> BusinessToolResult:
    if type(value) is not ItemPickerInput:
        raise TypeError("item_picker input type mismatch")
    if (
        value.preferred
        and value.publication_eligible_ids
        and dependencies.preference_assessment is not None
    ):
        assessment = await dependencies.preference_assessment.assess(
            _preference_assessment_input(value)
        )
        if type(assessment) is not PreferenceAssessmentOutput:
            raise ToolPortError(ToolFailureCode.PROVIDER_RESPONSE_INVALID)
        value = replace(
            value,
            preference_assessment=_expand_group_preference_assessment(value, assessment),
        )
    return run_item_picker(value)


async def _summary_handler(
    value: object,
    dependencies: ToolDependencies,
) -> BusinessToolResult:
    if type(value) is not ShoppingSummaryInput:
        raise TypeError("shopping_summary input type mismatch")
    return await run_shopping_summary(value, dependencies.shopping_summary)


BUSINESS_TOOL_REGISTRY: Mapping[ToolName, BusinessToolHandler] = MappingProxyType(
    {
        ToolName.PLANNER: _planner_handler,
        ToolName.CHAT_FALLBACK: _fallback_handler,
        ToolName.WEB_SEARCH: _web_handler,
        ToolName.CATEGORY_INSIGHT: _category_handler,
        ToolName.ITEM_SEARCH: _item_handler,
        ToolName.ITEM_PICKER: _picker_handler,
        ToolName.PRICE_COMPARE: _price_handler,
        ToolName.SHIPPING_CALC: _shipping_handler,
        ToolName.SHOPPING_SUMMARY: _summary_handler,
    }
)


async def execute_business_tool(
    tool_name: ToolName,
    tool_input: object,
    dependencies: ToolDependencies,
) -> BusinessToolResult:
    """Execute one of the exact nine business tools through the static registry."""

    if type(tool_name) is not ToolName or tool_name not in BUSINESS_TOOL_REGISTRY:
        raise ValueError("A01 registry requires an exact business tool")
    if type(dependencies) is not ToolDependencies:
        raise TypeError("tool dependencies must be exact ToolDependencies")
    result = await BUSINESS_TOOL_REGISTRY[tool_name](tool_input, dependencies)
    try:
        require_byte_limit(
            result,
            maximum=TOOL_RESULT_BYTE_LIMIT,
            code=ToolFailureCode.TOOL_RESULT_TOO_LARGE.value,
        )
    except BudgetExceeded:
        raise ToolPortError(ToolFailureCode.TOOL_RESULT_TOO_LARGE) from None
    return result


def _fallback_plan(
    constraints: tuple[RequiredConstraint, ...],
    reason: PlannerFallbackReason,
) -> PlannerOutput:
    return PlannerOutput(
        intent_kind=PlannerIntentKind.UNSUPPORTED_OR_NON_SHOPPING,
        constraints=constraints,
        platforms=(),
        search_query=None,
        comparison_targets=(),
        fallback_reason=reason,
    )


def _build_shipping_advisory(
    tool_input: ShippingCalcInput,
    *,
    point: PricePoint,
    rule: ShippingRule | None,
) -> LandedCostAdvisory:
    offer = _source_offer(tool_input.pool, point.candidate_id)
    rates = tool_input.pool.evaluation_batch.exchange_rates
    if type(rates) is not ExchangeRateTable:
        raise ValueError("shipping input lost its validated FX table")
    active_rule = (
        rule
        if rule is not None
        and _require_date(
            rule.effective_date,
            name="effective_date",
        )
        <= _require_date(
            tool_input.calculation_date,
            name="calculation_date",
        )
        else None
    )

    item_source = offer.cost_components.item_price
    item_conversion = _convert_known_cost(
        item_source,
        currency=offer.cost_components.currency,
        rates=rates,
    )
    if point.status is PricePointStatus.EXACT:
        if (
            point.base_amount is None
            or point.fx_evidence_id is None
            or type(item_source) is not KnownCost
            or item_conversion is None
            or item_conversion[0] != point.base_amount
            or item_conversion[1] != point.fx_evidence_id
        ):
            raise ValueError("price point differs from source item price and FX")
        item_price = point.base_amount
        item_price_status = CostComponentStatus.EXACT
        item_price_evidence_id = item_source.evidence_id
        fx_evidence_id = point.fx_evidence_id
    else:
        item_price = None
        item_price_status = CostComponentStatus.UNKNOWN
        item_price_evidence_id = None
        fx_evidence_id = None

    shipping_source = offer.cost_components.shipping
    shipping_conversion = _convert_known_cost(
        shipping_source,
        currency=offer.cost_components.currency,
        rates=rates,
    )
    if type(shipping_source) is KnownCost and shipping_conversion is not None:
        shipping = shipping_conversion[0]
        shipping_status = CostComponentStatus.EXACT
        shipping_evidence_id = shipping_source.evidence_id
    elif (
        type(shipping_source) is UnknownCost
        and active_rule is not None
        and active_rule.flat_shipping is not None
    ):
        shipping = active_rule.flat_shipping
        shipping_status = CostComponentStatus.ESTIMATE
        shipping_evidence_id = None
    else:
        shipping = None
        shipping_status = CostComponentStatus.UNKNOWN
        shipping_evidence_id = None

    tax_source = offer.cost_components.tax
    tax_conversion = _convert_known_cost(
        tax_source,
        currency=offer.cost_components.currency,
        rates=rates,
    )
    if type(tax_source) is KnownCost and tax_conversion is not None:
        tax = tax_conversion[0]
        tax_status = CostComponentStatus.EXACT
        tax_evidence_id = tax_source.evidence_id
    else:
        tax = None
        tax_status = CostComponentStatus.UNKNOWN
        tax_evidence_id = None

    duty_source = offer.cost_components.duty
    duty_conversion = _convert_known_cost(
        duty_source,
        currency=offer.cost_components.currency,
        rates=rates,
    )
    if type(duty_source) is KnownCost and duty_conversion is not None:
        duty = duty_conversion[0]
        duty_status = CostComponentStatus.EXACT
        duty_evidence_id = duty_source.evidence_id
    elif (
        type(duty_source) is UnknownCost
        and active_rule is not None
        and active_rule.duty_rate is not None
        and active_rule.duty_threshold is not None
        and item_price is not None
    ):
        with localcontext(pricing_context()):
            duty = (
                Decimal(0)
                if item_price <= active_rule.duty_threshold
                else item_price * active_rule.duty_rate
            )
        duty_status = CostComponentStatus.ESTIMATE
        duty_evidence_id = None
    else:
        duty = None
        duty_status = CostComponentStatus.UNKNOWN
        duty_evidence_id = None

    statuses = (
        item_price_status,
        shipping_status,
        tax_status,
        duty_status,
    )
    values = (item_price, shipping, tax, duty)
    if CostComponentStatus.UNKNOWN in statuses:
        item_status = ShippingStatus.UNKNOWN
        landed_total = None
    else:
        item_status = (
            ShippingStatus.EXACT
            if all(status is CostComponentStatus.EXACT for status in statuses)
            else ShippingStatus.ESTIMATE
        )
        with localcontext(pricing_context()):
            landed_total = sum(
                (value for value in values if value is not None),
                Decimal(0),
            )
    duty_tier = (
        DutyTier.UNKNOWN
        if duty is None
        else DutyTier.EXEMPT
        if duty == 0
        else _duty_tier(
            duty / item_price
            if item_price is not None
            else active_rule.duty_rate
            if active_rule is not None and active_rule.duty_rate is not None
            else Decimal(1)
        )
    )
    eta_days_min = (
        offer.delivery_days_min
        if offer.delivery_days_min is not None
        else None
        if active_rule is None
        else active_rule.eta_days_min
    )
    eta_days_max = (
        offer.delivery_days_max
        if offer.delivery_days_max is not None
        else None
        if active_rule is None
        else active_rule.eta_days_max
    )
    rule_used = active_rule is not None and (
        CostComponentStatus.ESTIMATE in statuses
        or (offer.delivery_days_min is None and active_rule.eta_days_min is not None)
    )
    return LandedCostAdvisory(
        candidate_id=point.candidate_id,
        item_price=item_price,
        shipping=shipping,
        tax=tax,
        duty=duty,
        landed_total=landed_total,
        eta_days_min=eta_days_min,
        eta_days_max=eta_days_max,
        duty_tier=duty_tier,
        item_status=item_status,
        item_price_status=item_price_status,
        shipping_status=shipping_status,
        tax_status=tax_status,
        duty_status=duty_status,
        item_price_evidence_id=item_price_evidence_id,
        shipping_evidence_id=shipping_evidence_id,
        tax_evidence_id=tax_evidence_id,
        duty_evidence_id=duty_evidence_id,
        fx_evidence_id=fx_evidence_id,
        rule_effective_date=(
            active_rule.effective_date if active_rule is not None and rule_used else None
        ),
        calculation_date=tool_input.calculation_date,
        ruleset_version=tool_input.ruleset_version,
    )


def _source_offer(pool: ValidatedCandidatePool, candidate_id: str) -> Offer:
    matching_records = tuple(
        record
        for candidate, record in zip(
            pool.candidates,
            pool.records,
            strict=True,
        )
        if candidate.candidate_id == candidate_id
    )
    if len(matching_records) != 1:
        raise ValueError("shipping candidate must bind exactly one manifest record")
    record = matching_records[0]
    source_identities = tuple(
        identity for identity in record.offer_identities if identity.offer_id == record.source_ref
    )
    if len(source_identities) != 1:
        raise ValueError("shipping record must bind exactly one source offer")
    identity = source_identities[0]
    offers = tuple(
        offer
        for offer in pool.evaluation_batch.offers
        if offer.provider_id == identity.provider_id and offer.offer_id == identity.offer_id
    )
    if len(offers) != 1:
        raise ValueError("shipping source offer is unavailable")
    return offers[0]


def _convert_known_cost(
    value: KnownCost | UnknownCost,
    *,
    currency: str,
    rates: ExchangeRateTable,
) -> tuple[Decimal, str] | None:
    if type(value) is not KnownCost:
        return None
    by_currency = {rate.currency: rate for rate in rates.rates}
    source_rate = by_currency.get(currency)
    base_rate = by_currency.get("CNY")
    if source_rate is None or base_rate is None:
        return None
    with localcontext(pricing_context()):
        amount = value.amount * source_rate.base_per_unit / base_rate.base_per_unit
    return amount, source_rate.evidence_id


def _duty_tier(rate: Decimal) -> DutyTier:
    if rate == 0:
        return DutyTier.EXEMPT
    if rate <= Decimal("0.05"):
        return DutyTier.LOW
    if rate <= Decimal("0.15"):
        return DutyTier.STANDARD
    return DutyTier.HIGH


def _advisory_rank(advisory: LandedCostAdvisory | None) -> tuple[int, Decimal]:
    if advisory is None or advisory.item_status is ShippingStatus.UNKNOWN:
        return (2, Decimal(0))
    rank = 0 if advisory.item_status is ShippingStatus.EXACT else 1
    return (rank, Decimal(0) if advisory.landed_total is None else advisory.landed_total)


def _picker_budget_distance(
    tool_input: ItemPickerInput,
    *,
    advisory: LandedCostAdvisory | None,
) -> Decimal:
    """Rank around-budget ties by distance from the user's actual spend target.

    A maximum or range budget is an eligibility boundary, not a request to
    spend up to its upper bound. Treating the ceiling as a target made a
    7,999 CNY product outrank a cheaper, equally suitable product for an
    ``8,000 CNY以内`` request. Only ``around`` expresses a preferred spend
    target; maximum/range candidates therefore receive the same distance and
    fall through to verified fit and landed-cost ordering.
    """

    budget = tool_input.budget
    if budget is None or advisory is None or advisory.landed_total is None:
        return Decimal("1E999")
    if budget.mode != "around":
        return Decimal(0)
    rates = tool_input.pool.evaluation_batch.exchange_rates
    if rates is None:
        return Decimal("1E999")
    by_currency = {rate.currency: rate for rate in rates.rates}
    budget_rate = by_currency.get(budget.currency or "CNY")
    cny_rate = by_currency.get("CNY")
    if budget_rate is None or cny_rate is None:
        return Decimal("1E999")
    with localcontext(pricing_context()):
        target_cny = budget.target_amount * budget_rate.base_per_unit / cny_rate.base_per_unit
        return abs(target_cny - advisory.landed_total)


def _picker_soft_ranks(
    tool_input: ItemPickerInput,
) -> dict[str, tuple[int, int, int]]:
    """Use the generic evidence-bound assessment; never encode product-category rules."""

    batch = tool_input.pool.evaluation_batch
    aggregation = aggregate_catalog_batch(batch)
    product_by_id = {product.product_id: product for product in aggregation.products}
    evidence_by_id = {item.evidence_id: item for item in batch.evidence}
    assessment_by_id = {
        candidate.candidate_id: candidate
        for candidate in (
            ()
            if tool_input.preference_assessment is None
            else tool_input.preference_assessment.candidates
        )
    }
    preferred_tokens = _preferred_soft_tokens(tool_input.preferred)
    category_tokens = _category_soft_tokens(tool_input.category_insight)
    advisory_by_id = {
        advisory.candidate_id: advisory for advisory in tool_input.shipping.advisories
    }
    ranks: dict[str, tuple[int, int, int]] = {}
    for candidate, record in zip(
        tool_input.pool.candidates,
        tool_input.pool.records,
        strict=True,
    ):
        product = product_by_id.get(record.product_id)
        verified_signal = (
            frozenset()
            if product is None
            else _verified_signal_tokens(
                product,
                evidence_by_id=evidence_by_id,
                snapshot_version=batch.snapshot_version,
            )
        )
        assessment = assessment_by_id.get(candidate.candidate_id)
        preference_coverage = (
            0
            if assessment is None
            else sum(
                item.status is PreferenceMatchStatus.MATCHED for item in assessment.assessments
            )
        )
        preference_score = 0 if assessment is None else assessment.score
        ranks[candidate.candidate_id] = (
            preference_coverage,
            preference_score + len(preferred_tokens & verified_signal),
            _category_fit_score(
                tool_input.category_insight,
                candidate=candidate,
                landed_total=(
                    None
                    if advisory_by_id.get(candidate.candidate_id) is None
                    else advisory_by_id[candidate.candidate_id].landed_total
                ),
                verified_signal=verified_signal,
                category_tokens=category_tokens,
            ),
        )
    return ranks


def _category_fit_score(
    insight: CategoryInsightOutput | None,
    *,
    candidate: Candidate,
    landed_total: Decimal | None,
    verified_signal: frozenset[str],
    category_tokens: frozenset[str],
) -> int:
    """Apply only confidence-weighted soft category signals; never filter."""

    if (
        insight is None
        or insight.status is not InsightStatus.FOUND
        or insight.confidence < Decimal("0.5")
    ):
        return 0
    candidate_attributes = {
        _normalized_category_value(attribute.name): _normalized_category_value(attribute.value)
        for attribute in candidate.attributes
    }
    matched_probabilities: list[Decimal] = []
    for attribute in insight.attributes:
        candidate_value = candidate_attributes.get(_normalized_category_value(attribute.name))
        if candidate_value is None:
            continue
        probability = _distribution_probability(attribute.distribution, candidate_value)
        if probability is not None:
            matched_probabilities.append(probability)
    attribute_score = (
        Decimal(0)
        if not matched_probabilities
        else sum(matched_probabilities, Decimal(0))
        / Decimal(len(matched_probabilities))
        * Decimal(100)
    )
    price_score = Decimal(0)
    if landed_total is not None and any(
        tier.range_cny[0] <= landed_total <= tier.range_cny[1] for tier in insight.price_tiers
    ):
        price_score = Decimal(20)
    lexical_score = Decimal(min(10, len(category_tokens & verified_signal)))
    weighted = (attribute_score + price_score + lexical_score) * insight.confidence
    return int(weighted.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _distribution_probability(
    distribution: Mapping[str, Decimal],
    candidate_value: str,
) -> Decimal | None:
    exact = {
        _normalized_category_value(value): probability
        for value, probability in distribution.items()
    }
    if candidate_value in exact:
        return exact[candidate_value]
    matches = [
        probability
        for value, probability in exact.items()
        if value != "其他" and (value in candidate_value or candidate_value in value)
    ]
    if matches:
        return max(matches)
    return exact.get("其他")


def _normalized_category_value(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _preferred_soft_tokens(
    preferred: tuple[PreferredCriterion, ...],
) -> frozenset[str]:
    tokens: set[str] = set()
    for criterion in preferred:
        tokens.update(lexical_tokens(criterion.value))
        tokens.update(lexical_tokens(criterion.source_span.text))
    return frozenset(tokens)


def _category_soft_tokens(
    insight: CategoryInsightOutput | None,
) -> frozenset[str]:
    if insight is None or insight.status is not InsightStatus.FOUND:
        return frozenset()
    tokens: set[str] = set()
    values = [*insight.components]
    values.extend(
        value
        for bestseller in insight.bestsellers
        for value in (bestseller.name, bestseller.why_popular)
    )
    values.extend(
        value
        for attribute in insight.attributes
        for value in (attribute.name, *attribute.distribution)
    )
    values.extend(value for tier in insight.price_tiers for value in (tier.tier, tier.notes))
    for value in values:
        tokens.update(lexical_tokens(value))
    return frozenset(tokens)


def _verified_signal_tokens(
    product: CanonicalProduct,
    *,
    evidence_by_id: Mapping[str, EvidenceRef],
    snapshot_version: str,
) -> frozenset[str]:
    signal = next(
        (attribute for attribute in product.attributes if attribute.name == "verified_signal"),
        None,
    )
    if signal is None:
        return frozenset()
    expected_path = "product.attributes.verified_signal"
    if not all(
        _is_closed_product_evidence(
            evidence_by_id.get(evidence_id),
            evidence_id=evidence_id,
            product_id=product.product_id,
            snapshot_version=snapshot_version,
            field_path=expected_path,
        )
        for evidence_id in signal.evidence_ids
    ):
        return frozenset()
    return lexical_tokens(signal.value)


def _is_closed_product_evidence(
    evidence: EvidenceRef | None,
    *,
    evidence_id: str,
    product_id: str,
    snapshot_version: str,
    field_path: str,
) -> bool:
    return (
        type(evidence) is EvidenceRef
        and evidence.evidence_id == evidence_id
        and evidence.snapshot_version == snapshot_version
        and evidence.entity_type is EvidenceEntityType.PRODUCT
        and evidence.product_id == product_id
        and evidence.field_path == field_path
    )


def _require_identifier(value: object, name: str) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > 128
        or not value[0].isalnum()
        or any(not (character.isalnum() or character in "._:/-") for character in value)
    ):
        raise ValueError(f"{name} must be a safe identifier")
    return value


def _require_optional_non_negative_integer(value: object, *, name: str) -> None:
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError(f"{name} must be a non-negative integer or None")


def _require_date(value: object, *, name: str) -> date:
    if type(value) is not str or len(value) != 10:
        raise TypeError(f"{name} must be a canonical ISO date string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name} must be a canonical ISO date string") from None
    if parsed.isoformat() != value:
        raise ValueError(f"{name} must be a canonical ISO date string")
    return parsed


__all__ = [
    "BUSINESS_TOOL_REGISTRY",
    "ChatFallbackInput",
    "ItemPickerInput",
    "ItemSearchToolInput",
    "PriceCompareInput",
    "SearchServiceFactory",
    "ShippingCalcInput",
    "ShippingRule",
    "ShoppingSummaryInput",
    "TargetCandidateGroup",
    "ToolDependencies",
    "execute_business_tool",
    "run_category_insight",
    "run_chat_fallback",
    "run_item_picker",
    "run_item_search",
    "run_planner",
    "run_price_compare",
    "run_shipping_calc",
    "run_shopping_summary",
    "run_web_search",
]
