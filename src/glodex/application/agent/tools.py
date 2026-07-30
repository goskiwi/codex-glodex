"""Nine concrete M1d business tools and their static A01 registry."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext
from types import MappingProxyType
from typing import Literal

from glodex.application.agent.catalog import (
    CandidateEligibility,
    InMemoryCatalogGateway,
    ValidatedCandidatePool,
    evaluate_candidate_pool,
    rebind_selected_candidates,
)
from glodex.application.agent.contracts import (
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
    PlannerFallbackReason,
    PlannerInput,
    PlannerIntentKind,
    PlannerOutput,
    PlanStage,
    Platform,
    PriceCompareOutput,
    PricePoint,
    PricePointStatus,
    ShippingCalcOutput,
    ShippingStatus,
    ShoppingSummaryOutput,
    ToolFailureCode,
    ToolName,
    WebSearchInput,
    WebSearchOutput,
)
from glodex.application.agent.ports import (
    CategoryInsightPort,
    ItemSourcePort,
    ToolPortError,
    WebSearchPort,
)
from glodex.application.agent.state import (
    TOOL_RESULT_BYTE_LIMIT,
    BudgetExceeded,
    require_byte_limit,
)
from glodex.application.search_service import SearchService
from glodex.contracts import RunStatus, SearchRequest
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
    TargetCategory,
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

    def __post_init__(self) -> None:
        if type(self.request) is not ItemSearchInput:
            raise TypeError("item tool request must be an exact ItemSearchInput")
        if type(self.data_mode) is not DataMode:
            raise TypeError("item tool data_mode must be an exact DataMode")
        if self.data_mode is DataMode.DEMO_SNAPSHOT:
            if type(self.query_vector) is not tuple:
                raise ValueError("Demo item search requires a query vector")
            EmbeddingResult(vectors=(self.query_vector,))
        elif self.request.platform is not Platform.EBAY or self.query_vector is not None:
            raise ValueError("live item search requires ebay and no query vector")


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
    max_items: int

    def __post_init__(self) -> None:
        if type(self.eligibility) is not CandidateEligibility:
            raise TypeError("picker requires exact canonical eligibility")
        if type(self.prices) is not PriceCompareOutput:
            raise TypeError("picker prices must be an exact PriceCompareOutput")
        if type(self.shipping) is not ShippingCalcOutput:
            raise TypeError("picker shipping must be an exact ShippingCalcOutput")
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
        if (
            type(self.max_items) is not int
            or isinstance(self.max_items, bool)
            or not 1 <= self.max_items <= 3
        ):
            raise ValueError("picker max_items must be between one and three")

    @property
    def pool(self) -> ValidatedCandidatePool:
        return self.eligibility.pool

    @property
    def publication_eligible_ids(self) -> tuple[str, ...]:
        return self.eligibility.eligible_candidate_ids


type SearchServiceFactory = Callable[[InMemoryCatalogGateway], SearchService]


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
    web_search: WebSearchPort | None = None
    category_insight: CategoryInsightPort | None = None
    item_source: ItemSourcePort | None = None


def run_planner(tool_input: PlannerInput) -> PlannerOutput:
    """Form the deterministic trusted platform plan without network access."""

    if type(tool_input) is not PlannerInput:
        raise TypeError("planner requires an exact PlannerInput")
    if not required_constraints_match(
        tool_input.required_baseline,
        tool_input.interpreted_request,
    ):
        raise ValueError("planner Required constraints differ from the baseline")
    category = next(
        (
            criterion
            for criterion in tool_input.interpreted_request.required
            if type(criterion) is TargetCategory
        ),
        None,
    )
    if category is None:
        return _fallback_plan(
            tool_input.required_baseline.required,
            PlannerFallbackReason.NON_SHOPPING,
        )
    if category.category not in tool_input.capabilities.supported_categories:
        return _fallback_plan(
            tool_input.required_baseline.required,
            PlannerFallbackReason.UNSUPPORTED_CATEGORY,
        )

    explicit_platforms = _platforms_from_query(tool_input.request.query)
    capabilities = tool_input.capabilities
    if explicit_platforms:
        unavailable = tuple(
            platform
            for platform in explicit_platforms
            if platform not in capabilities.available_platforms
            or (
                capabilities.data_mode is DataMode.LIVE_MARKETPLACE
                and platform is Platform.EBAY
                and not capabilities.live_ebay_enabled
            )
        )
        if unavailable:
            return _fallback_plan(
                tool_input.required_baseline.required,
                PlannerFallbackReason.PLATFORM_NOT_CONFIGURED,
            )
        platforms = explicit_platforms
    elif capabilities.data_mode is DataMode.DEMO_SNAPSHOT:
        platforms = capabilities.available_platforms
    elif capabilities.live_ebay_enabled:
        platforms = (Platform.EBAY,)
    else:
        return _fallback_plan(
            tool_input.required_baseline.required,
            PlannerFallbackReason.PLATFORM_NOT_CONFIGURED,
        )
    return PlannerOutput(
        intent_kind=PlannerIntentKind.SHOPPING,
        constraints=tool_input.required_baseline.required,
        platforms=platforms,
        stages=(
            PlanStage.EVIDENCE,
            PlanStage.ITEM_SEARCH,
            PlanStage.PRICE_COMPARE,
            PlanStage.SHIPPING_CALC,
            PlanStage.ELIGIBILITY,
            PlanStage.ITEM_PICKER,
            PlanStage.SHOPPING_SUMMARY,
        ),
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
    limits = (3, 3, 5, 3, 8) if tool_input.depth.value == "quick" else (8, 5, 12, 5, 15)
    actual = (
        len(result.components),
        len(result.bestsellers),
        len(result.attributes),
        len(result.price_tiers),
        len(result.card_ids),
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
    )
    if type(result) is not ItemSearchRuntimeResult:
        raise ToolPortError(ToolFailureCode.ITEM_SOURCE_INVALID)
    if (
        result.platform is not tool_input.request.platform
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
    for index, candidate in enumerate(tool_input.pool.candidates):
        source_rate = rates.get(candidate.currency)
        if source_rate is None or base_rate is None:
            point = PricePoint(
                candidate_id=candidate.candidate_id,
                platform=candidate.platform,
                source_ref=candidate.source_ref,
                source_amount=candidate.price,
                source_currency=candidate.currency,
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
    cheapest: list[str] = []
    seen_platforms: set[Platform] = set()
    for point in ranked:
        if point.status is PricePointStatus.EXACT and point.platform not in seen_platforms:
            cheapest.append(point.candidate_id)
            seen_platforms.add(point.platform)
    return PriceCompareOutput(
        ranked=ranked,
        cheapest_per_platform=tuple(cheapest),
    )


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
        advisories=tuple(advisory for _, advisory in indexed),
        ruleset_version=tool_input.ruleset_version,
    )


def run_item_picker(tool_input: ItemPickerInput) -> ItemPickerOutput:
    """Select only deterministic publication-eligible candidate IDs."""

    if type(tool_input) is not ItemPickerInput:
        raise TypeError("item picker requires an exact ItemPickerInput")
    if not tool_input.publication_eligible_ids:
        return ItemPickerOutput(
            selected_candidate_ids=(),
            reason_codes=(ToolFailureCode.NO_ELIGIBLE_CANDIDATE.value,),
        )
    eligible = set(tool_input.publication_eligible_ids)
    advisory_by_id = {
        advisory.candidate_id: advisory for advisory in tool_input.shipping.advisories
    }
    price_order = {
        point.candidate_id: index for index, point in enumerate(tool_input.prices.ranked)
    }
    candidate_order = {
        candidate.candidate_id: index for index, candidate in enumerate(tool_input.pool.candidates)
    }
    soft_ranks = _picker_soft_ranks(tool_input)
    ranked = sorted(
        eligible,
        key=lambda candidate_id: (
            _advisory_rank(advisory_by_id.get(candidate_id))[0],
            -soft_ranks[candidate_id][0],
            -soft_ranks[candidate_id][1],
            _advisory_rank(advisory_by_id.get(candidate_id))[1],
            price_order.get(candidate_id, len(price_order)),
            candidate_order[candidate_id],
        ),
    )
    selected = tuple(ranked[: tool_input.max_items])
    return ItemPickerOutput(
        selected_candidate_ids=selected,
        reason_codes=tuple("eligible" for _ in selected),
    )


async def run_shopping_summary(
    tool_input: ShoppingSummaryInput,
) -> ShoppingSummaryOutput:
    """Rebind selected facts and invoke the existing full SearchService final gate."""

    if type(tool_input) is not ShoppingSummaryInput:
        raise TypeError("shopping summary requires an exact ShoppingSummaryInput")
    interpreted = tool_input.interpreted_request
    fx_source_batch = tool_input.fx_source_batch
    confirmed_eligibility = evaluate_candidate_pool(
        tool_input.pool,
        interpreted,
        display_currency=tool_input.request.display_currency,
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
        service = tool_input.search_service_factory(gateway)
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
        answer = f"已找到 {len(response.results)} 个符合条件的商品。"
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
    del dependencies
    if type(value) is not ItemPickerInput:
        raise TypeError("item_picker input type mismatch")
    return run_item_picker(value)


async def _summary_handler(
    value: object,
    dependencies: ToolDependencies,
) -> BusinessToolResult:
    del dependencies
    if type(value) is not ShoppingSummaryInput:
        raise TypeError("shopping_summary input type mismatch")
    return await run_shopping_summary(value)


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
    """Execute one of the exact nine business tools; dispatch is intentionally absent."""

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
        stages=(PlanStage.CHAT_FALLBACK,),
        fallback_reason=reason,
    )


def _platforms_from_query(query: str) -> tuple[Platform, ...]:
    normalized = query.casefold()
    aliases = {
        Platform.AMAZON: ("amazon", "亚马逊"),
        Platform.SHOPEE: ("shopee", "虾皮"),
        Platform.ALIEXPRESS: ("aliexpress", "速卖通"),
        Platform.EBAY: ("ebay",),
    }
    positions: list[tuple[int, Platform]] = []
    for platform, names in aliases.items():
        found = min(
            (position for name in names if (position := normalized.find(name)) >= 0),
            default=-1,
        )
        if found >= 0:
            positions.append((found, platform))
    positions.sort(key=lambda item: item[0])
    return tuple(platform for _, platform in positions)


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
    return LandedCostAdvisory(
        candidate_id=point.candidate_id,
        item_price=item_price,
        shipping=shipping,
        tax=tax,
        duty=duty,
        landed_total=landed_total,
        eta_days_min=None if active_rule is None else active_rule.eta_days_min,
        eta_days_max=None if active_rule is None else active_rule.eta_days_max,
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
        rule_effective_date=None if rule is None else rule.effective_date,
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


def _picker_soft_ranks(
    tool_input: ItemPickerInput,
) -> dict[str, tuple[int, int]]:
    """Score only Catalog-evidenced signals; Card content never creates a product fact."""

    batch = tool_input.pool.evaluation_batch
    aggregation = aggregate_catalog_batch(batch)
    product_by_id = {product.product_id: product for product in aggregation.products}
    evidence_by_id = {item.evidence_id: item for item in batch.evidence}
    preferred_tokens = _preferred_soft_tokens(tool_input.preferred)
    category_tokens = _category_soft_tokens(tool_input.category_insight)
    ranks: dict[str, tuple[int, int]] = {}
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
        ranks[candidate.candidate_id] = (
            len(preferred_tokens & verified_signal),
            len(category_tokens & verified_signal),
        )
    return ranks


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
    if (
        insight is None
        or insight.status is not InsightStatus.FOUND
        or not tuple(sorted(insight.card_ids))
    ):
        return frozenset()
    tokens: set[str] = set()
    for value in (
        *insight.components,
        *insight.bestsellers,
        *insight.attributes,
        *insight.price_tiers,
    ):
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
