# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Never

import pytest
from pydantic import ValidationError

from glodex.agent.catalog import (
    CandidateEligibility,
    CandidateManifest,
    CandidateStore,
    InMemoryCatalogGateway,
    ManifestRecord,
    ValidatedCandidatePool,
    build_fx_evaluation_view,
    evaluate_candidate_pool,
)
from glodex.agent.contracts import (
    BUSINESS_TOOL_SET,
    AgentCapabilities,
    AgentToolSummary,
    AttributeDistribution,
    Candidate,
    CandidateAttribute,
    CategoryInsightInput,
    CategoryInsightOutput,
    DataMode,
    EvidenceKind,
    InsightDepth,
    InsightStatus,
    ItemPickerOutput,
    ItemSearchInput,
    ItemSearchRuntimeResult,
    PlannerFallbackReason,
    PlannerInput,
    PlannerIntentKind,
    Platform,
    PriceComparisonScope,
    PriceTierInsight,
    SemanticAssertionOutput,
    ShoppingNarrationInput,
    ToolFailureCode,
    ToolName,
    WebEvidence,
    WebSearchInput,
    WebSearchOutput,
)
from glodex.agent.ports import ToolPortError
from glodex.agent.rule_intent import (
    AgentRuleIntentInterpreter,
    RuleIntentInterpreter,
)
from glodex.agent.state import TOOL_RESULT_BYTE_LIMIT
from glodex.agent.tool_session import (
    _retain_semantically_relevant_candidates,
    _unpriced_candidate_ids,
)
from glodex.application.search_service import SearchService
from glodex.config import GlodexConfig
from glodex.contracts import RunStatus, SearchRequest
from glodex.domain.catalog import (
    CatalogBatch,
    CostComponents,
    ExchangeRate,
    ExchangeRateTable,
    OfferIdentity,
    ProductAttribute,
)
from glodex.domain.evidence import EvidenceEntityType, FieldEvidence
from glodex.domain.intent import (
    BudgetMax,
    InterpretedRequest,
    PreferredCriterion,
    SourceSpan,
    TargetCategory,
    validate_interpreted_request,
)
from glodex.domain.pricing import KnownCost, UnknownCost
from glodex.retrieval.deterministic_ranker import DeterministicQueryRanker
from glodex.tools import engine as agent_tools
from glodex.tools.engine import (
    BUSINESS_TOOL_REGISTRY,
    ChatFallbackInput,
    ItemPickerInput,
    ItemSearchToolInput,
    PriceCompareInput,
    ShippingCalcInput,
    ShippingRule,
    ShoppingSummaryInput,
    TargetCandidateGroup,
    ToolDependencies,
    execute_business_tool,
    run_category_insight,
    run_item_picker,
    run_planner,
    run_price_compare,
    run_shipping_calc,
)
from tests.builders import (
    AcceptAllSemanticAssertion,
    VerifiedShoppingSummary,
    build_catalog_batch,
    build_evidence_ref,
    build_offer,
    build_product,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec(
        "GLO-M1D-P0-002",
        "GLO-M1D-P0-004",
        "GLO-M1D-NFR-002",
        "GLO-M1D-NFR-003",
    ),
]


@pytest.mark.parametrize(
    ("query", "category"),
    (
        ("推荐手机", "phone"),
        ("推荐智能手机", "phone"),
        ("推荐平板", "tablet"),
        ("推荐平板电脑", "tablet"),
    ),
)
def test_agent_rule_categories_are_narrowly_isolated(
    query: str,
    category: str,
) -> None:
    request = SearchRequest(query=query)

    default = asyncio.run(RuleIntentInterpreter().interpret(request))
    agent = asyncio.run(AgentRuleIntentInterpreter().interpret(request))

    assert not any(type(item) is TargetCategory for item in default.required)
    assert tuple(item.category for item in agent.required if type(item) is TargetCategory) == (
        category,
    )
    assert validate_interpreted_request(query, default).is_valid
    assert validate_interpreted_request(query, agent).is_valid


def test_default_rule_ignores_agent_tablet_alias_during_validation() -> None:
    query = "比较手机和平板"
    interpreted = asyncio.run(RuleIntentInterpreter().interpret(SearchRequest(query=query)))

    assert not any(type(item) is TargetCategory for item in interpreted.required)
    assert validate_interpreted_request(query, interpreted).is_valid


def _candidate() -> Candidate:
    return Candidate(
        candidate_id="amazon.product-1",
        item_id="item-1",
        platform=Platform.AMAZON,
        title="TravelBook 14",
        price=Decimal("699.00"),
        currency="USD",
        attributes=(CandidateAttribute(name="weight", value="1.2kg"),),
        source_ref="offer-1",
        record_ref="amazon-item-1",
    )


def _item_result(
    *,
    source_batch: CatalogBatch | None = None,
    fx_source_batch: CatalogBatch | None = None,
) -> ItemSearchRuntimeResult:
    return ItemSearchRuntimeResult(
        platform=Platform.AMAZON,
        target_query="test query",
        retrieval_query="test query",
        candidates=(_candidate(),),
        platform_sub_batch=build_fx_evaluation_view(
            (
                build_catalog_batch(delivery_days_min=2, delivery_days_max=10)
                if source_batch is None
                else source_batch
            ),
            _cny_fx_batch() if fx_source_batch is None else fx_source_batch,
        ),
        total_recall=1,
        returned_before_semantic_filter=1,
        truncated=False,
    )


def _pool(
    *,
    source_batch: CatalogBatch | None = None,
    fx_source_batch: CatalogBatch | None = None,
):
    manifest = CandidateManifest(
        data_mode=DataMode.SYNTHETIC_INTERVIEW,
        snapshot_version="m0-v1",
        records=(
            ManifestRecord(
                record_ref="amazon-item-1",
                source_ref="offer-1",
                item_id="item-1",
                platform=Platform.AMAZON,
                product_id="product-1",
                offer_identities=(OfferIdentity(provider_id="provider-a", offer_id="offer-1"),),
                provider_ids=("provider-a",),
            ),
        ),
    )
    return CandidateStore(manifest).merge(
        (
            _item_result(
                source_batch=source_batch,
                fx_source_batch=fx_source_batch,
            ),
        )
    )


def _same_product_pool(
    *,
    include_multi_platform_group: bool,
    include_second_multi_platform_group: bool = False,
) -> ValidatedCandidatePool:
    same_group_id = "sg-v1-aaaaaaaaaaaaaaaaaaaaaaaa"
    candidates = (
        Candidate(
            candidate_id="amazon.same-product",
            item_id="amazon-same-product",
            platform=Platform.AMAZON,
            title="Example Phone 128GB",
            price=Decimal("100.00"),
            currency="USD",
            source_ref="amazon-same-offer",
            record_ref="amazon-same-record",
            same_group_id=same_group_id,
        ),
        Candidate(
            candidate_id="shopee.same-product",
            item_id="shopee-same-product",
            platform=Platform.SHOPEE,
            title="Example Phone 128GB",
            price=Decimal("90.00"),
            currency="USD",
            source_ref="shopee-same-offer",
            record_ref="shopee-same-record",
            same_group_id=same_group_id if include_multi_platform_group else None,
        ),
        Candidate(
            candidate_id="ebay-other-product",
            item_id="ebay-other-product",
            platform=Platform.EBAY,
            title="Unrelated accessory",
            price=Decimal("1.00"),
            currency="USD",
            source_ref="ebay-other-offer",
            record_ref="ebay-other-record",
            same_group_id="sg-v1-bbbbbbbbbbbbbbbbbbbbbbbb",
        ),
        *(
            (
                Candidate(
                    candidate_id="amazon-other-product",
                    item_id="amazon-other-product",
                    platform=Platform.AMAZON,
                    title="Other Example Phone",
                    price=Decimal("80.00"),
                    currency="USD",
                    source_ref="amazon-other-offer",
                    record_ref="amazon-other-record",
                    same_group_id="sg-v1-bbbbbbbbbbbbbbbbbbbbbbbb",
                ),
            )
            if include_second_multi_platform_group
            else ()
        ),
    )
    records = tuple(
        ManifestRecord(
            record_ref=candidate.record_ref,
            source_ref=candidate.source_ref,
            item_id=candidate.item_id,
            platform=candidate.platform,
            product_id=candidate.candidate_id,
            offer_identities=(
                OfferIdentity(provider_id="fixture-provider", offer_id=candidate.source_ref),
            ),
            provider_ids=("fixture-provider",),
            same_group_id=candidate.same_group_id,
        )
        for candidate in candidates
    )
    return ValidatedCandidatePool(
        candidates=candidates,
        evaluation_batch=build_fx_evaluation_view(build_catalog_batch(), _cny_fx_batch()),
        records=records,
        data_mode=DataMode.SYNTHETIC_INTERVIEW,
    )


def test_interview_price_compare_keeps_all_products_and_marks_comparable_group() -> None:
    prices = run_price_compare(
        PriceCompareInput(pool=_same_product_pool(include_multi_platform_group=True))
    )

    assert prices.comparison_scope is PriceComparisonScope.ALL_RETRIEVED
    assert prices.compared_group_ids == ("sg-v1-aaaaaaaaaaaaaaaaaaaaaaaa",)
    assert {point.candidate_id for point in prices.ranked} == {
        "shopee.same-product",
        "amazon.same-product",
        "ebay-other-product",
    }


def test_interview_price_compare_does_not_drop_later_comparable_products() -> None:
    prices = run_price_compare(
        PriceCompareInput(
            pool=_same_product_pool(
                include_multi_platform_group=True,
                include_second_multi_platform_group=True,
            )
        )
    )

    assert prices.comparison_scope is PriceComparisonScope.ALL_RETRIEVED
    assert prices.compared_group_ids == (
        "sg-v1-aaaaaaaaaaaaaaaaaaaaaaaa",
        "sg-v1-bbbbbbbbbbbbbbbbbbbbbbbb",
    )
    assert {point.candidate_id for point in prices.ranked} == {
        "amazon.same-product",
        "shopee.same-product",
        "ebay-other-product",
        "amazon-other-product",
    }


def test_interview_price_compare_keeps_singletons_without_multi_platform_group() -> None:
    prices = run_price_compare(
        PriceCompareInput(pool=_same_product_pool(include_multi_platform_group=False))
    )

    assert prices.comparison_scope is PriceComparisonScope.ALL_RETRIEVED
    assert prices.compared_group_ids == ()
    assert {point.candidate_id for point in prices.ranked} == {
        "amazon.same-product",
        "shopee.same-product",
        "ebay-other-product",
    }
    assert prices.cheapest_per_platform == {
        "amazon": "amazon.same-product",
        "shopee": "shopee.same-product",
        "ebay": "ebay-other-product",
    }


def test_price_compare_uses_twelve_by_default_and_caps_selector_at_thirty() -> None:
    pool = _pool()

    assert PriceCompareInput(pool=pool).top_n == 12
    assert PriceCompareInput(pool=pool, top_n=30).top_n == 30
    with pytest.raises(ValueError, match="between one and 30"):
        PriceCompareInput(pool=pool, top_n=31)


def test_price_compare_pruning_excludes_unpriced_candidates_from_downstream_selection() -> None:
    pool = _signal_pool()
    prices = run_price_compare(PriceCompareInput(pool=pool, top_n=1))

    assert len(prices.ranked) == 1
    assert _unpriced_candidate_ids(pool, prices) == ("amazon.product-b",)
    shipping = run_shipping_calc(
        ShippingCalcInput(
            pool=pool,
            price_points=prices,
            destination_country="CN",
            rules=(),
            ruleset_version="test-v1",
            calculation_date="2026-08-12",
        )
    )
    eligibility = evaluate_candidate_pool(
        pool,
        InterpretedRequest(required=(), preferred=(), parser_version="test"),
        display_currency="USD",
    )
    with pytest.raises(ValueError, match="covered by price and shipping"):
        ItemPickerInput(
            eligibility=eligibility,
            prices=prices,
            shipping=shipping,
            preferred=(),
            category_insight=None,
            target_candidate_groups=(),
            max_items=1,
        )


def _signal_pool(
    *,
    second_signal: str = "long_battery",
    second_price: str = "699.00",
):
    products = tuple(
        build_product(
            product_id=f"product-{suffix}",
            source_uri=f"fixture://provider-a/products/product-{suffix}",
            title=f"TravelBook {suffix.upper()}",
            attributes=(
                ProductAttribute(
                    name="verified_signal",
                    value=signal,
                    evidence_id=f"ev-product-{suffix}-verified-signal",
                ),
            ),
            field_evidence=(
                FieldEvidence(
                    field_path="product.title",
                    evidence_id=f"ev-product-{suffix}-title",
                ),
                FieldEvidence(
                    field_path="product.category",
                    evidence_id=f"ev-product-{suffix}-category",
                ),
                FieldEvidence(
                    field_path="product.entity_kind",
                    evidence_id=f"ev-product-{suffix}-kind",
                ),
            ),
        )
        for suffix, signal in (("a", "portable"), ("b", second_signal))
    )
    offers = tuple(
        _signal_offer(
            suffix=suffix,
            product_id=f"product-{suffix}",
            amount=amount,
        )
        for suffix, amount in (("a", "699.00"), ("b", second_price))
    )
    records = tuple(
        ManifestRecord(
            record_ref=f"amazon-item-{suffix}",
            source_ref=f"offer-{suffix}",
            item_id=f"item-{suffix}",
            platform=Platform.AMAZON,
            product_id=f"product-{suffix}",
            offer_identities=(
                OfferIdentity(
                    provider_id="provider-a",
                    offer_id=f"offer-{suffix}",
                ),
            ),
            provider_ids=("provider-a",),
        )
        for suffix in ("a", "b")
    )
    candidates = tuple(
        Candidate(
            candidate_id=f"amazon.product-{suffix}",
            item_id=f"item-{suffix}",
            platform=Platform.AMAZON,
            title=product.title,
            price=offer.cost_components.item_price.amount,  # type: ignore[union-attr]
            currency="USD",
            attributes=tuple(
                CandidateAttribute(name=item.name, value=item.value) for item in product.attributes
            ),
            source_ref=f"offer-{suffix}",
            record_ref=f"amazon-item-{suffix}",
        )
        for suffix, product, offer in zip(
            ("a", "b"),
            products,
            offers,
            strict=True,
        )
    )
    batch = CatalogBatch(
        snapshot_version="m0-v1",
        products=products,
        offers=offers,
        evidence=(
            *_signal_product_evidence(products),
            *_signal_offer_evidence(offers),
        ),
    )
    result = ItemSearchRuntimeResult(
        platform=Platform.AMAZON,
        target_query="test query",
        retrieval_query="test query",
        candidates=candidates,
        platform_sub_batch=build_fx_evaluation_view(batch, _cny_fx_batch()),
        total_recall=2,
        returned_before_semantic_filter=2,
        truncated=False,
    )
    manifest = CandidateManifest(
        data_mode=DataMode.SYNTHETIC_INTERVIEW,
        snapshot_version="m0-v1",
        records=records,
    )
    return CandidateStore(manifest).merge((result,))


def _signal_offer(*, suffix: str, product_id: str, amount: str):
    prefix = f"ev-offer-{suffix}"
    return build_offer(
        offer_id=f"offer-{suffix}",
        product_id=product_id,
        source_uri=f"fixture://provider-a/offers/offer-{suffix}",
        cost_components=CostComponents(
            currency="USD",
            item_price=KnownCost(
                amount=Decimal(amount),
                evidence_id=f"{prefix}-item-price",
            ),
            shipping=KnownCost(
                amount=Decimal("0.00"),
                evidence_id=f"{prefix}-shipping",
            ),
            tax=KnownCost(
                amount=Decimal("0.00"),
                evidence_id=f"{prefix}-tax",
            ),
            duty=KnownCost(
                amount=Decimal("0.00"),
                evidence_id=f"{prefix}-duty",
            ),
        ),
        field_evidence=(
            FieldEvidence(
                field_path="offer.inventory",
                evidence_id=f"{prefix}-inventory",
            ),
            FieldEvidence(
                field_path="offer.market",
                evidence_id=f"{prefix}-market",
            ),
            FieldEvidence(
                field_path="offer.cost_components.currency",
                evidence_id=f"{prefix}-currency",
            ),
            FieldEvidence(
                field_path="offer.cost_components.item_price",
                evidence_id=f"{prefix}-item-price",
            ),
            FieldEvidence(
                field_path="offer.cost_components.shipping",
                evidence_id=f"{prefix}-shipping",
            ),
            FieldEvidence(
                field_path="offer.cost_components.tax",
                evidence_id=f"{prefix}-tax",
            ),
            FieldEvidence(
                field_path="offer.cost_components.duty",
                evidence_id=f"{prefix}-duty",
            ),
        ),
    )


def _signal_product_evidence(products):
    return tuple(
        build_evidence_ref(
            evidence_id=binding.evidence_id,
            product_id=product.product_id,
            field_path=binding.field_path,
            source_uri=product.source_uri,
        )
        for product in products
        for binding in (
            *product.field_evidence,
            *(
                FieldEvidence(
                    field_path=f"product.attributes.{attribute.name}",
                    evidence_id=attribute.evidence_id,
                )
                for attribute in product.attributes
            ),
        )
    )


def _signal_offer_evidence(offers):
    return tuple(
        build_evidence_ref(
            evidence_id=binding.evidence_id,
            entity_type=EvidenceEntityType.OFFER,
            product_id=offer.product_id,
            offer_id=offer.offer_id,
            field_path=binding.field_path,
            source_uri=offer.source_uri,
        )
        for offer in offers
        for binding in offer.field_evidence
    )


def _source_batch_with_unknown_costs(
    *unknown_components: str,
) -> CatalogBatch:
    batch = build_catalog_batch()
    offer = batch.offers[0]
    original = offer.cost_components
    costs = {
        "shipping": original.shipping,
        "tax": original.tax,
        "duty": original.duty,
    }
    for component in unknown_components:
        costs[component] = UnknownCost(reason=f"{component} unavailable")
    updated_costs = CostComponents(
        currency=original.currency,
        item_price=original.item_price,
        shipping=costs["shipping"],
        tax=costs["tax"],
        duty=costs["duty"],
    )
    removed_evidence_ids = {
        value.evidence_id
        for name, value in original.items()
        if name in unknown_components and type(value) is KnownCost
    }
    updated_offer = replace(
        offer,
        cost_components=updated_costs,
        field_evidence=tuple(
            binding
            for binding in offer.field_evidence
            if binding.evidence_id not in removed_evidence_ids
        ),
    )
    return replace(
        batch,
        offers=(updated_offer,),
        evidence=tuple(
            evidence
            for evidence in batch.evidence
            if evidence.evidence_id not in removed_evidence_ids
        ),
    )


def _cny_rates() -> ExchangeRateTable:
    return ExchangeRateTable(
        snapshot_version="m1d-fx-v1",
        base_currency="CNY",
        rates=(
            ExchangeRate(
                snapshot_version="m1d-fx-v1",
                currency="CNY",
                base_per_unit=Decimal("1"),
                minor_units=2,
                evidence_id="ev-rate-cny",
                snapshot_ordinal=0,
            ),
            ExchangeRate(
                snapshot_version="m1d-fx-v1",
                currency="USD",
                base_per_unit=Decimal("7.2"),
                minor_units=2,
                evidence_id="ev-rate-usd",
                snapshot_ordinal=1,
            ),
        ),
    )


def _cny_fx_batch() -> CatalogBatch:
    batch = build_catalog_batch(snapshot_version="m1d-fx-v1")
    cny_evidence = build_evidence_ref(
        evidence_id="ev-rate-cny",
        snapshot_version="m1d-fx-v1",
        entity_type=EvidenceEntityType.EXCHANGE_RATE,
        product_id=None,
        currency="CNY",
        field_path="exchange_rate.base_per_unit",
        provider_id="fixture-fx",
        source_uri="fixture://fx/CNY",
    )
    return CatalogBatch(
        snapshot_version=batch.snapshot_version,
        products=batch.products,
        offers=batch.offers,
        evidence=(*batch.evidence, cny_evidence),
        exchange_rates=_cny_rates(),
    )


def _cny_only_fx_batch() -> CatalogBatch:
    batch = build_catalog_batch(snapshot_version="m1d-fx-cny-only")
    cny_evidence = build_evidence_ref(
        evidence_id="ev-rate-cny",
        snapshot_version=batch.snapshot_version,
        entity_type=EvidenceEntityType.EXCHANGE_RATE,
        product_id=None,
        currency="CNY",
        field_path="exchange_rate.base_per_unit",
        provider_id="fixture-fx",
        source_uri="fixture://fx/CNY",
    )
    non_fx_evidence = tuple(
        evidence
        for evidence in batch.evidence
        if evidence.entity_type is not EvidenceEntityType.EXCHANGE_RATE
    )
    return CatalogBatch(
        snapshot_version=batch.snapshot_version,
        products=batch.products,
        offers=batch.offers,
        evidence=(*non_fx_evidence, cny_evidence),
        exchange_rates=ExchangeRateTable(
            snapshot_version=batch.snapshot_version,
            base_currency="CNY",
            rates=(
                ExchangeRate(
                    snapshot_version=batch.snapshot_version,
                    currency="CNY",
                    base_per_unit=Decimal("1"),
                    minor_units=2,
                    evidence_id="ev-rate-cny",
                    snapshot_ordinal=0,
                ),
            ),
        ),
    )


@dataclass
class _Web:
    async def search(self, request: WebSearchInput) -> WebSearchOutput:
        return WebSearchOutput(
            evidence=(
                WebEvidence(
                    source_id="web-1",
                    title="Laptop guide",
                    url_domain="example.test",
                    snippet="Untrusted guide snippet.",
                    source_type=request.evidence_kind,
                ),
            )
        )


@dataclass
class _Category:
    async def retrieve(self, request: CategoryInsightInput) -> CategoryInsightOutput:
        return CategoryInsightOutput(
            status=InsightStatus.FOUND,
            category=request.category,
            components=("CPU",),
            confidence=Decimal("0.900"),
        )


@dataclass
class _Items:
    async def search(
        self,
        request: ItemSearchInput,
        *,
        query_vector: tuple[float, ...] | None,
        preference_vector: tuple[float, ...] | None,
    ) -> ItemSearchRuntimeResult:
        assert request.platform is Platform.AMAZON
        assert query_vector is not None and len(query_vector) == 1_024
        assert preference_vector is None
        return replace(
            _item_result(),
            target_query=request.query,
            retrieval_query=request.query,
        )


@dataclass
class _Ids:
    def next_run_id(self) -> str:
        return "unused"


@dataclass
class _Clock:
    calls: int = 0

    def now_utc(self) -> datetime:
        return datetime(2026, 7, 29, tzinfo=UTC)

    def monotonic_ns(self) -> int:
        self.calls += 1
        return self.calls * 1_000_000


@dataclass(frozen=True)
class _BoundIntent:
    interpreted_request: InterpretedRequest

    async def interpret(self, request: SearchRequest) -> InterpretedRequest:
        assert type(request) is SearchRequest
        return self.interpreted_request


def _service_factory(
    gateway: InMemoryCatalogGateway,
    interpreted_request: InterpretedRequest,
) -> SearchService:
    return SearchService(
        config=GlodexConfig(
            data_dir=Path("data/snapshots"),
            default_snapshot="m0-v1",
            default_locale="zh-CN",
            default_currency="USD",
            default_top_k=3,
            fingerprint="a" * 64,
        ),
        run_id_provider=_Ids(),
        clock=_Clock(),
        intent_interpreter=_BoundIntent(interpreted_request),
        catalog_gateway=gateway,
        query_ranker=DeterministicQueryRanker(),
    )


def test_picker_soft_sort_uses_preferred_and_card_grounded_verified_signal() -> None:
    pool = _signal_pool()
    request = SearchRequest(
        query="推荐有库存的笔记本, 长续航",
        display_currency="USD",
    )
    interpreted = asyncio.run(RuleIntentInterpreter().interpret(request))
    eligibility = evaluate_candidate_pool(
        pool,
        interpreted,
        display_currency=request.display_currency,
    )
    prices = run_price_compare(PriceCompareInput(pool=pool))
    shipping = run_shipping_calc(
        ShippingCalcInput(
            pool=pool,
            price_points=prices,
            destination_country="CN",
            rules=(),
            ruleset_version="cn-v1",
            calculation_date="2026-07-29",
        )
    )

    def pick(
        *,
        preferred: tuple[PreferredCriterion, ...] = (),
        category_insight: CategoryInsightOutput | None = None,
    ) -> tuple[str, ...]:
        return run_item_picker(
            ItemPickerInput(
                eligibility=eligibility,
                prices=prices,
                shipping=shipping,
                preferred=preferred,
                category_insight=category_insight,
                target_candidate_groups=(),
                max_items=1,
            )
        ).selected_candidate_ids

    assert pick() == ("amazon.product-a",)
    assert pick(preferred=interpreted.preferred) == ("amazon.product-b",)
    assert pick(
        category_insight=CategoryInsightOutput(
            status=InsightStatus.FOUND,
            category="laptop",
            components=("long_battery",),
            confidence=Decimal("0.900"),
        )
    ) == ("amazon.product-b",)
    assert pick(
        category_insight=CategoryInsightOutput(
            status=InsightStatus.FOUND,
            category="display",
            components=("oled",),
            confidence=Decimal("0.900"),
        )
    ) == ("amazon.product-a",)

    with pytest.raises(ValueError, match="explicit None"):
        ItemPickerInput(
            eligibility=eligibility,
            prices=prices,
            shipping=shipping,
            preferred=(),
            category_insight=CategoryInsightOutput(
                status=InsightStatus.NO_INSIGHT,
                category="unknown",
                confidence=Decimal("0"),
            ),
            target_candidate_groups=(),
            max_items=1,
        )


def test_picker_uses_attribute_probability_and_price_range_as_soft_signals() -> None:
    pool = _signal_pool(second_price="799.00")
    interpreted = InterpretedRequest(required=(), preferred=(), parser_version="test")
    eligibility = evaluate_candidate_pool(pool, interpreted, display_currency="USD")
    prices = run_price_compare(PriceCompareInput(pool=pool))
    shipping = run_shipping_calc(
        ShippingCalcInput(
            pool=pool,
            price_points=prices,
            destination_country="CN",
            rules=(),
            ruleset_version="test-v1",
            calculation_date="2026-08-12",
        )
    )

    def pick(insight: CategoryInsightOutput) -> tuple[str, ...]:
        return run_item_picker(
            ItemPickerInput(
                eligibility=eligibility,
                prices=prices,
                shipping=shipping,
                preferred=(),
                category_insight=insight,
                target_candidate_groups=(),
                max_items=1,
            )
        ).selected_candidate_ids

    assert pick(
        CategoryInsightOutput(
            status=InsightStatus.FOUND,
            category="laptop",
            attributes=(
                AttributeDistribution(
                    name="verified_signal",
                    distribution={
                        "portable": Decimal("0.1"),
                        "long_battery": Decimal("0.9"),
                    },
                ),
            ),
            confidence=Decimal("0.9"),
        )
    ) == ("amazon.product-b",)
    assert pick(
        CategoryInsightOutput(
            status=InsightStatus.FOUND,
            category="laptop",
            price_tiers=(
                PriceTierInsight(
                    tier="mid",
                    range_cny=(Decimal("5700"), Decimal("5800")),
                    notes="合成演示价格档",
                ),
            ),
            confidence=Decimal("0.9"),
        )
    ) == ("amazon.product-b",)
    assert pick(
        CategoryInsightOutput(
            status=InsightStatus.FOUND,
            category="laptop",
            attributes=(
                AttributeDistribution(
                    name="verified_signal",
                    distribution={
                        "portable": Decimal("0.1"),
                        "long_battery": Decimal("0.9"),
                    },
                ),
            ),
            confidence=Decimal("0.4"),
        )
    ) == ("amazon.product-a",)


def test_picker_soft_signals_never_bypass_publication_eligibility() -> None:
    pool = _signal_pool(second_price="899.00")
    request = SearchRequest(
        query="推荐预算800美元、有库存的笔记本, 长续航",
        display_currency="USD",
    )
    interpreted = asyncio.run(RuleIntentInterpreter().interpret(request))
    eligibility = evaluate_candidate_pool(
        pool,
        interpreted,
        display_currency=request.display_currency,
    )
    prices = run_price_compare(PriceCompareInput(pool=pool))
    shipping = run_shipping_calc(
        ShippingCalcInput(
            pool=pool,
            price_points=prices,
            destination_country="CN",
            rules=(),
            ruleset_version="cn-v1",
            calculation_date="2026-07-29",
        )
    )

    picked = run_item_picker(
        ItemPickerInput(
            eligibility=eligibility,
            prices=prices,
            shipping=shipping,
            preferred=interpreted.preferred,
            category_insight=CategoryInsightOutput(
                status=InsightStatus.FOUND,
                category="laptop",
                components=("long_battery",),
                confidence=Decimal("1"),
            ),
            target_candidate_groups=(),
            max_items=3,
        )
    )

    assert eligibility.eligible_candidate_ids == ("amazon.product-a",)
    assert picked.selected_candidate_ids == eligibility.eligible_candidate_ids


def test_picker_treats_maximum_budget_as_a_ceiling_not_a_spend_target() -> None:
    pool = _signal_pool(second_signal="portable", second_price="799.00")
    request = SearchRequest(query="推荐预算800美元以内的笔记本", display_currency="USD")
    interpreted = asyncio.run(RuleIntentInterpreter().interpret(request))
    eligibility = evaluate_candidate_pool(pool, interpreted, display_currency="USD")
    prices = run_price_compare(PriceCompareInput(pool=pool))
    shipping = run_shipping_calc(
        ShippingCalcInput(
            pool=pool,
            price_points=prices,
            destination_country="CN",
            rules=(),
            ruleset_version="cn-v1",
            calculation_date="2026-07-29",
        )
    )
    budget = next(item for item in interpreted.required if type(item) is BudgetMax)

    picked = run_item_picker(
        ItemPickerInput(
            eligibility=eligibility,
            prices=prices,
            shipping=shipping,
            preferred=(),
            category_insight=None,
            target_candidate_groups=(),
            max_items=1,
            budget=budget,
        )
    )

    assert picked.selected_candidate_ids == ("amazon.product-a",)


def test_picker_uses_distance_only_for_an_around_budget_target() -> None:
    pool = _signal_pool(second_signal="portable", second_price="799.00")
    interpreted = InterpretedRequest(required=(), preferred=(), parser_version="test")
    eligibility = evaluate_candidate_pool(pool, interpreted, display_currency="USD")
    prices = run_price_compare(PriceCompareInput(pool=pool))
    shipping = run_shipping_calc(
        ShippingCalcInput(
            pool=pool,
            price_points=prices,
            destination_country="CN",
            rules=(),
            ruleset_version="cn-v1",
            calculation_date="2026-07-29",
        )
    )
    budget = BudgetMax(
        mode="around",
        target_amount=Decimal("800"),
        lower_bound=Decimal("720"),
        upper_bound=Decimal("880"),
        currency="USD",
        source_span=SourceSpan(start=2, end=9, text="预算800美元"),
    )

    picked = run_item_picker(
        ItemPickerInput(
            eligibility=eligibility,
            prices=prices,
            shipping=shipping,
            preferred=(),
            category_insight=None,
            target_candidate_groups=(),
            max_items=1,
            budget=budget,
        )
    )

    assert picked.selected_candidate_ids == ("amazon.product-b",)


def test_picker_reserves_one_verified_candidate_per_comparison_target() -> None:
    pool = _signal_pool()
    request = SearchRequest(query="比较 A 和 B", display_currency="USD", top_k=2)
    interpreted = InterpretedRequest(required=(), preferred=(), parser_version="test")
    eligibility = evaluate_candidate_pool(pool, interpreted, display_currency="USD")
    prices = run_price_compare(PriceCompareInput(pool=pool))
    shipping = run_shipping_calc(
        ShippingCalcInput(
            pool=pool,
            price_points=prices,
            destination_country="CN",
            rules=(),
            ruleset_version="test-v1",
            calculation_date="2026-08-10",
        )
    )

    picked = run_item_picker(
        ItemPickerInput(
            eligibility=eligibility,
            prices=prices,
            shipping=shipping,
            preferred=(),
            category_insight=None,
            target_candidate_groups=(
                TargetCandidateGroup(
                    target_query="A",
                    candidate_ids=("amazon.product-a",),
                ),
                TargetCandidateGroup(
                    target_query="B",
                    candidate_ids=("amazon.product-b",),
                ),
            ),
            max_items=request.top_k,
        )
    )

    assert picked.selected_candidate_ids == (
        "amazon.product-a",
        "amazon.product-b",
    )


def test_static_business_registry_executes_all_nine_real_tools() -> None:
    async def scenario() -> None:
        request = SearchRequest(
            query="推荐预算800美元、有库存的笔记本, 在 amazon",
            display_currency="USD",
            top_k=1,
        )
        interpreted = await RuleIntentInterpreter().interpret(request)
        dependencies = ToolDependencies(
            semantic_assertion=AcceptAllSemanticAssertion(),
            shopping_summary=VerifiedShoppingSummary(),
            web_search=_Web(),
            category_insight=_Category(),
            item_source=_Items(),
        )
        planner = await execute_business_tool(
            ToolName.PLANNER,
            PlannerInput(
                request=request,
                interpreted_request=interpreted,
                required_baseline=interpreted,
                capabilities=AgentCapabilities(
                    data_mode=DataMode.SYNTHETIC_INTERVIEW,
                    available_platforms=(Platform.AMAZON,),
                    web_search_enabled=True,
                    embedding_enabled=True,
                ),
                declared_intent=PlannerIntentKind.SHOPPING,
                requested_platforms=(Platform.AMAZON,),
                search_query="笔记本 800美元 有库存",
                comparison_targets=(),
            ),
            dependencies,
        )
        assert planner.intent_kind is PlannerIntentKind.SHOPPING  # type: ignore[union-attr]

        fallback = await execute_business_tool(
            ToolName.CHAT_FALLBACK,
            ChatFallbackInput(reason_code=PlannerFallbackReason.NON_SHOPPING),
            dependencies,
        )
        assert "购买" in fallback.answer  # type: ignore[union-attr]

        web = await execute_business_tool(
            ToolName.WEB_SEARCH,
            WebSearchInput(query="laptop guide", evidence_kind=EvidenceKind.GUIDE),
            dependencies,
        )
        assert len(web.evidence) == 1  # type: ignore[union-attr]

        vector = (1.0, *(0.0 for _ in range(1_023)))
        category = await execute_business_tool(
            ToolName.CATEGORY_INSIGHT,
            CategoryInsightInput(
                category="laptop",
                depth=InsightDepth.QUICK,
            ),
            dependencies,
        )
        assert category.status is InsightStatus.FOUND  # type: ignore[union-attr]

        item = await execute_business_tool(
            ToolName.ITEM_SEARCH,
            ItemSearchToolInput(
                request=ItemSearchInput(
                    query=request.query,
                    platform=Platform.AMAZON,
                    category="laptop",
                    min_landed_cost_cny=None,
                    max_landed_cost_cny=None,
                    top_k=20,
                ),
                data_mode=DataMode.SYNTHETIC_INTERVIEW,
                query_vector=vector,
            ),
            dependencies,
        )
        assert item == replace(
            _item_result(),
            target_query=request.query,
            retrieval_query=request.query,
        )

        pool = _pool()
        eligibility = evaluate_candidate_pool(
            pool,
            interpreted,
            display_currency=request.display_currency,
        )
        prices = await execute_business_tool(
            ToolName.PRICE_COMPARE,
            PriceCompareInput(pool=pool, top_n=12),
            dependencies,
        )
        assert prices.ranked[0].base_amount == Decimal("5032.800")  # type: ignore[union-attr]

        shipping = await execute_business_tool(
            ToolName.SHIPPING_CALC,
            ShippingCalcInput(
                pool=pool,
                price_points=prices,  # type: ignore[arg-type]
                destination_country="CN",
                rules=(
                    ShippingRule(
                        platform=Platform.AMAZON,
                        flat_shipping=Decimal("30"),
                        duty_rate=Decimal("0.10"),
                        duty_threshold=Decimal("5000"),
                        effective_date="2026-07-01",
                        eta_days_min=3,
                        eta_days_max=7,
                    ),
                ),
                ruleset_version="cn-v1",
                calculation_date="2026-07-29",
            ),
            dependencies,
        )
        assert shipping.advisories[0].landed_total is not None  # type: ignore[union-attr]

        picked = await execute_business_tool(
            ToolName.ITEM_PICKER,
            ItemPickerInput(
                eligibility=eligibility,
                prices=prices,  # type: ignore[arg-type]
                shipping=shipping,  # type: ignore[arg-type]
                preferred=interpreted.preferred,
                category_insight=category,  # type: ignore[arg-type]
                target_candidate_groups=(),
                max_items=1,
            ),
            dependencies,
        )
        assert picked.selected_candidate_ids == ("amazon.product-1",)  # type: ignore[union-attr]

        summary = await execute_business_tool(
            ToolName.SHOPPING_SUMMARY,
            ShoppingSummaryInput(
                agent_run_id="agent-run-1",
                request=request,
                interpreted_request=interpreted,
                eligibility=eligibility,
                picker=picked,  # type: ignore[arg-type]
                fx_source_batch=build_catalog_batch(),
                search_service_factory=_service_factory,
            ),
            dependencies,
        )
        assert summary.status == RunStatus.COMPLETED.value  # type: ignore[union-attr]
        assert summary.search_response.status is RunStatus.COMPLETED  # type: ignore[union-attr]

    assert tuple(BUSINESS_TOOL_REGISTRY) == BUSINESS_TOOL_SET
    asyncio.run(scenario())


def test_tool_summary_accepts_the_root_loop_tool_budget() -> None:
    summary = AgentToolSummary(
        tool_name=ToolName.ITEM_SEARCH,
        call_count=10,
        safe_outcome="SUCCESS",
    )

    assert summary.call_count == 10
    with pytest.raises(ValueError):
        AgentToolSummary(
            tool_name=ToolName.ITEM_SEARCH,
            call_count=11,
            safe_outcome="SUCCESS",
        )


def test_semantic_assertion_removes_rejected_products_before_candidate_store() -> None:
    original = _item_result()

    filtered = _retain_semantically_relevant_candidates(
        original,
        SemanticAssertionOutput(relevant_candidate_ids=()),
    )

    assert filtered.candidates == ()
    assert filtered.platform_sub_batch.products == ()
    assert filtered.platform_sub_batch.offers == ()
    assert filtered.total_recall == original.total_recall
    assert filtered.returned_before_semantic_filter == 1
    assert filtered.truncated is False


def test_shopping_summary_compares_each_result_without_inventing_unknown_preferences() -> None:
    async def scenario() -> None:
        request = SearchRequest(
            query="推荐有库存的笔记本, 长续航",
            display_currency="USD",
            top_k=2,
        )
        interpreted = await RuleIntentInterpreter().interpret(request)
        pool = _signal_pool()
        eligibility = evaluate_candidate_pool(
            pool,
            interpreted,
            display_currency=request.display_currency,
        )
        prices = run_price_compare(PriceCompareInput(pool=pool))
        shipping = run_shipping_calc(
            ShippingCalcInput(
                pool=pool,
                price_points=prices,
                destination_country="CN",
                rules=(),
                ruleset_version="cn-v1",
                calculation_date="2026-07-29",
            )
        )
        picker = run_item_picker(
            ItemPickerInput(
                eligibility=eligibility,
                prices=prices,
                shipping=shipping,
                preferred=interpreted.preferred,
                category_insight=None,
                target_candidate_groups=(),
                max_items=2,
            )
        )
        final_gate_intents: list[InterpretedRequest] = []
        narration_inputs: list[ShoppingNarrationInput] = []

        class Narrator:
            async def summarize(self, narration: ShoppingNarrationInput) -> str:
                narration_inputs.append(narration)
                return (
                    "第1项：依据已验证的电池属性与到手价比较，价格为 CNY；"
                    "第2项：依据已验证的电池属性与到手价比较，价格为 CNY。"
                )

        def final_gate_factory(
            gateway: InMemoryCatalogGateway,
            final_intent: InterpretedRequest,
        ) -> SearchService:
            final_gate_intents.append(final_intent)
            return _service_factory(gateway, final_intent)

        summary = await execute_business_tool(
            ToolName.SHOPPING_SUMMARY,
            ShoppingSummaryInput(
                agent_run_id="agent-run-comparison",
                request=request,
                interpreted_request=interpreted,
                eligibility=eligibility,
                picker=picker,
                fx_source_batch=_cny_fx_batch(),
                search_service_factory=final_gate_factory,
            ),
            ToolDependencies(
                semantic_assertion=AcceptAllSemanticAssertion(),
                shopping_summary=Narrator(),
            ),
        )

        assert summary.status == RunStatus.COMPLETED.value  # type: ignore[union-attr]
        assert len(final_gate_intents) == 1
        assert final_gate_intents[0] is interpreted
        answer = summary.answer  # type: ignore[union-attr]
        assert "第1项" in answer and "第2项" in answer and "CNY" in answer
        assert len(narration_inputs) == 1
        narration = narration_inputs[0]
        assert narration.user_query == request.query
        narrated_picks = narration.picks
        assert len(narrated_picks) == 2
        assert tuple(pick.title for pick in narrated_picks) == tuple(
            pick.title for pick in picker.picks
        )
        assert all(pick.attributes for pick in narrated_picks)
        assert all(
            attribute.evidence_ids for pick in narrated_picks for attribute in pick.attributes
        )

    asyncio.run(scenario())


def test_dependency_failure_remains_a_safe_port_code() -> None:
    class BrokenWeb:
        async def search(self, request: WebSearchInput) -> WebSearchOutput:
            del request
            raise ToolPortError(ToolFailureCode.PROVIDER_UNAVAILABLE)

    with pytest.raises(ToolPortError) as captured:
        asyncio.run(
            execute_business_tool(
                ToolName.WEB_SEARCH,
                WebSearchInput(query="guide", evidence_kind=EvidenceKind.GUIDE),
                ToolDependencies(
                    semantic_assertion=AcceptAllSemanticAssertion(),
                    shopping_summary=VerifiedShoppingSummary(),
                    web_search=BrokenWeb(),
                ),
            )
        )
    assert captured.value.code is ToolFailureCode.PROVIDER_UNAVAILABLE
    assert str(captured.value) == "PROVIDER_UNAVAILABLE"


def test_business_executor_rejects_one_more_tool_result_byte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def oversized_result(
        value: object,
        dependencies: ToolDependencies,
    ) -> object:
        del value, dependencies
        return "x" * (TOOL_RESULT_BYTE_LIMIT - 1)

    monkeypatch.setattr(
        agent_tools,
        "BUSINESS_TOOL_REGISTRY",
        {ToolName.PLANNER: oversized_result},
    )
    with pytest.raises(ToolPortError) as captured:
        asyncio.run(
            execute_business_tool(
                ToolName.PLANNER,
                object(),
                ToolDependencies(
                    semantic_assertion=AcceptAllSemanticAssertion(),
                    shopping_summary=VerifiedShoppingSummary(),
                ),
            )
        )
    assert captured.value.code is ToolFailureCode.TOOL_RESULT_TOO_LARGE


def test_planner_rejects_required_drift_and_routes_unavailable_platform_to_fallback() -> None:
    async def scenario() -> None:
        request = SearchRequest(query="推荐预算800美元的笔记本, 在 ebay")
        interpreted = await RuleIntentInterpreter().interpret(request)
        changed = InterpretedRequest(
            required=tuple(
                criterion for criterion in interpreted.required if criterion.kind != "budget_max"
            ),
            preferred=interpreted.preferred,
            parser_version=interpreted.parser_version,
        )
        with pytest.raises(ValueError, match="Required"):
            run_planner(
                PlannerInput(
                    request=request,
                    interpreted_request=changed,
                    required_baseline=interpreted,
                    capabilities=AgentCapabilities(
                        data_mode=DataMode.SYNTHETIC_INTERVIEW,
                        available_platforms=(Platform.AMAZON,),
                        web_search_enabled=False,
                        embedding_enabled=True,
                    ),
                    declared_intent=PlannerIntentKind.SHOPPING,
                    requested_platforms=(Platform.EBAY,),
                    search_query="笔记本 800美元",
                    comparison_targets=(),
                )
            )

        fallback = run_planner(
            PlannerInput(
                request=request,
                interpreted_request=interpreted,
                required_baseline=interpreted,
                capabilities=AgentCapabilities(
                    data_mode=DataMode.SYNTHETIC_INTERVIEW,
                    available_platforms=(Platform.AMAZON,),
                    web_search_enabled=False,
                    embedding_enabled=True,
                ),
                declared_intent=PlannerIntentKind.SHOPPING,
                requested_platforms=(Platform.EBAY,),
                search_query="笔记本 800美元",
                comparison_targets=(),
            )
        )
        assert fallback.intent_kind is PlannerIntentKind.UNSUPPORTED_OR_NON_SHOPPING
        assert fallback.fallback_reason is PlannerFallbackReason.PLATFORM_NOT_CONFIGURED

    asyncio.run(scenario())


def test_planner_uses_model_declared_intent_and_defers_category_grounding() -> None:
    async def scenario() -> None:
        capabilities = AgentCapabilities(
            data_mode=DataMode.SYNTHETIC_INTERVIEW,
            available_platforms=(Platform.AMAZON,),
            web_search_enabled=False,
            embedding_enabled=True,
        )
        expectations = (
            (
                "今天天气如何",
                PlannerIntentKind.UNSUPPORTED_OR_NON_SHOPPING,
                PlannerIntentKind.UNSUPPORTED_OR_NON_SHOPPING,
                PlannerFallbackReason.NON_SHOPPING,
            ),
            (
                "推荐一台旅行相机",
                PlannerIntentKind.SHOPPING,
                PlannerIntentKind.SHOPPING,
                None,
            ),
            (
                "推荐一台旅行笔记本",
                PlannerIntentKind.SHOPPING,
                PlannerIntentKind.SHOPPING,
                None,
            ),
        )
        for query, declared_kind, expected_kind, expected_reason in expectations:
            request = SearchRequest(query=query)
            interpreted = await RuleIntentInterpreter().interpret(request)
            plan = run_planner(
                PlannerInput(
                    request=request,
                    interpreted_request=interpreted,
                    required_baseline=interpreted,
                    capabilities=capabilities,
                    declared_intent=declared_kind,
                    requested_platforms=(),
                    search_query=query if declared_kind is PlannerIntentKind.SHOPPING else None,
                    comparison_targets=(),
                )
            )
            assert plan.intent_kind is expected_kind
            assert plan.fallback_reason is expected_reason

    asyncio.run(scenario())


def test_provider_and_tool_boundaries_fail_closed_without_partial_facts() -> None:
    class OversizedCategory:
        async def retrieve(self, request: CategoryInsightInput) -> CategoryInsightOutput:
            return CategoryInsightOutput(
                status=InsightStatus.FOUND,
                category=request.category,
                components=("a", "b", "c", "d"),
                confidence=Decimal("0.5"),
            )

    class WrongPlatformItems:
        async def search(
            self,
            request: ItemSearchInput,
            *,
            query_vector: tuple[float, ...] | None,
            preference_vector: tuple[float, ...] | None,
        ) -> ItemSearchRuntimeResult:
            del request
            del query_vector
            del preference_vector
            return _item_result()

    with pytest.raises(ToolPortError) as category_error:
        asyncio.run(
            run_category_insight(
                CategoryInsightInput(
                    category="laptop",
                    depth=InsightDepth.QUICK,
                ),
                OversizedCategory(),
            )
        )
    assert category_error.value.code is ToolFailureCode.INDEX_INVALID

    with pytest.raises(ValueError, match="snapshot item search requires a query vector"):
        ItemSearchToolInput(
            request=ItemSearchInput(
                query="phone",
                platform=Platform.EBAY,
                category="phone",
                min_landed_cost_cny=None,
                max_landed_cost_cny=None,
            ),
            data_mode=DataMode.SYNTHETIC_INTERVIEW,
            query_vector=None,
        )


def test_source_known_shipping_tax_and_duty_are_fx_exact_with_evidence() -> None:
    pool = _pool()
    prices = run_price_compare(PriceCompareInput(pool=pool))
    result = run_shipping_calc(
        ShippingCalcInput(
            pool=pool,
            price_points=prices,
            destination_country="CN",
            rules=(
                ShippingRule(
                    platform=Platform.AMAZON,
                    flat_shipping=Decimal("999"),
                    duty_rate=Decimal("1"),
                    duty_threshold=Decimal("0"),
                    effective_date="2026-07-01",
                    eta_days_min=3,
                    eta_days_max=7,
                ),
            ),
            ruleset_version="cn-v1",
            calculation_date="2026-07-29",
        )
    )

    advisory = result.advisories[0]
    assert advisory.item_status.value == "EXACT"
    assert advisory.item_price == Decimal("5032.800")
    assert advisory.shipping == Decimal("0.00")
    assert advisory.tax == Decimal("402.624")
    assert advisory.duty == Decimal("0.00")
    assert advisory.landed_total == Decimal("5435.424")
    assert advisory.item_price_status.value == "EXACT"
    assert advisory.shipping_status.value == "EXACT"
    assert advisory.tax_status.value == "EXACT"
    assert advisory.duty_status.value == "EXACT"
    assert advisory.item_price_evidence_id == "ev-offer-item-price"
    assert advisory.shipping_evidence_id == "ev-offer-shipping"
    assert advisory.tax_evidence_id == "ev-offer-tax"
    assert advisory.duty_evidence_id == "ev-offer-duty"
    assert advisory.fx_evidence_id == prices.ranked[0].fx_evidence_id
    assert advisory.eta_days_min == 2
    assert advisory.eta_days_max == 10
    assert advisory.rule_effective_date is None
    assert advisory.calculation_date == "2026-07-29"
    assert result.ruleset_version == "cn-v1"
    assert result.destination_country == "CN"


def test_shipping_calc_rejects_a_non_cn_destination() -> None:
    pool = _pool()
    prices = run_price_compare(PriceCompareInput(pool=pool))

    with pytest.raises(ValueError, match="destination must be CN"):
        ShippingCalcInput(
            pool=pool,
            price_points=prices,
            destination_country="US",  # type: ignore[arg-type]
            rules=(),
            ruleset_version="cn-v1",
            calculation_date="2026-07-29",
        )


def test_unknown_source_costs_use_active_rule_and_duty_threshold() -> None:
    pool = _pool(source_batch=_source_batch_with_unknown_costs("shipping", "duty"))
    prices = run_price_compare(PriceCompareInput(pool=pool))

    def calculate(threshold: Decimal):
        return run_shipping_calc(
            ShippingCalcInput(
                pool=pool,
                price_points=prices,
                destination_country="CN",
                rules=(
                    ShippingRule(
                        platform=Platform.AMAZON,
                        flat_shipping=Decimal("30"),
                        duty_rate=Decimal("0.10"),
                        duty_threshold=threshold,
                        effective_date="2026-07-01",
                        eta_days_min=3,
                        eta_days_max=7,
                    ),
                ),
                ruleset_version="cn-v1",
                calculation_date="2026-07-29",
            )
        ).advisories[0]

    taxable = calculate(Decimal("5000"))
    assert taxable.item_status.value == "ESTIMATE"
    assert taxable.shipping == Decimal("30")
    assert taxable.shipping_status.value == "ESTIMATE"
    assert taxable.tax == Decimal("402.624")
    assert taxable.tax_status.value == "EXACT"
    assert taxable.duty == Decimal("503.2800")
    assert taxable.duty_status.value == "ESTIMATE"
    assert taxable.landed_total == Decimal("5968.7040")
    assert taxable.eta_days_min == 3
    assert taxable.eta_days_max == 7
    assert taxable.rule_effective_date == "2026-07-01"

    exempt = calculate(Decimal("6000"))
    assert exempt.duty == Decimal("0")
    assert exempt.duty_tier.value == "EXEMPT"
    assert exempt.landed_total == Decimal("5465.424")


def test_unknown_tax_and_future_rules_remain_unknown_without_zero_fill() -> None:
    pool = _pool(
        source_batch=_source_batch_with_unknown_costs(
            "shipping",
            "tax",
            "duty",
        )
    )
    prices = run_price_compare(PriceCompareInput(pool=pool))
    future_rule = ShippingRule(
        platform=Platform.AMAZON,
        flat_shipping=Decimal("30"),
        duty_rate=Decimal("0.10"),
        duty_threshold=Decimal("5000"),
        effective_date="2026-08-01",
        eta_days_min=3,
        eta_days_max=7,
    )
    future = run_shipping_calc(
        ShippingCalcInput(
            pool=pool,
            price_points=prices,
            destination_country="CN",
            rules=(future_rule,),
            ruleset_version="cn-v1",
            calculation_date="2026-07-29",
        )
    ).advisories[0]
    assert future.item_price_status.value == "EXACT"
    assert future.shipping is None
    assert future.tax is None
    assert future.duty is None
    assert future.shipping_status.value == "UNKNOWN"
    assert future.tax_status.value == "UNKNOWN"
    assert future.duty_status.value == "UNKNOWN"
    assert future.item_status.value == "UNKNOWN"
    assert future.landed_total is None
    assert future.rule_effective_date is None

    active = run_shipping_calc(
        ShippingCalcInput(
            pool=pool,
            price_points=prices,
            destination_country="CN",
            rules=(replace(future_rule, effective_date="2026-07-01"),),
            ruleset_version="cn-v1",
            calculation_date="2026-07-29",
        )
    ).advisories[0]
    assert active.shipping == Decimal("30")
    assert active.duty == Decimal("503.2800")
    assert active.tax is None
    assert active.tax_status.value == "UNKNOWN"
    assert active.item_status.value == "UNKNOWN"
    assert active.landed_total is None
    assert active.rule_effective_date == "2026-07-01"


def test_unknown_fx_shipping_and_picker_forgery_are_separate() -> None:
    with pytest.raises(TypeError):
        PriceCompareInput(  # type: ignore[call-arg]
            pool=_pool(),
            exchange_rates=_cny_rates(),
        )

    unknown_pool = _pool(fx_source_batch=_cny_only_fx_batch())
    unknown_prices = run_price_compare(PriceCompareInput(pool=unknown_pool))
    assert unknown_prices.ranked[0].status.value == "UNKNOWN_FX"
    assert unknown_prices.cheapest_per_platform == {}
    unknown_shipping = run_shipping_calc(
        ShippingCalcInput(
            pool=unknown_pool,
            price_points=unknown_prices,
            destination_country="CN",
            rules=(),
            ruleset_version="cn-v1",
            calculation_date="2026-07-29",
        )
    )
    assert unknown_shipping.advisories[0].item_status.value == "UNKNOWN"
    assert unknown_shipping.advisories[0].landed_total is None

    pool = _pool()
    with pytest.raises(ValueError, match="canonical evaluation"):
        CandidateEligibility(
            pool=pool,
            evaluation=evaluate_candidate_pool(
                pool,
                asyncio.run(
                    RuleIntentInterpreter().interpret(
                        SearchRequest(query="推荐预算800美元的笔记本")
                    )
                ),
                display_currency="USD",
            ).evaluation,
            eligible_candidate_ids=("forged-id",),
        )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: WebSearchInput(
            query="guide",
            evidence_kind=EvidenceKind.GUIDE,
            max_results=9,
        ),
        lambda: ItemSearchInput(
            query="phone",
            platform=Platform.EBAY,
            category="phone",
            min_landed_cost_cny=None,
            max_landed_cost_cny=None,
            top_k=51,
        ),
        lambda: PriceCompareInput(
            pool=_pool(),
            top_n=31,
        ),
        lambda: ShippingRule(
            platform=Platform.AMAZON,
            flat_shipping=Decimal("-0.01"),
            duty_rate=Decimal("0.10"),
            duty_threshold=Decimal("5000"),
            effective_date="2026-07-01",
            eta_days_min=3,
            eta_days_max=7,
        ),
        lambda: ShippingRule(
            platform=Platform.AMAZON,
            flat_shipping=Decimal("30"),
            duty_rate=Decimal("1.01"),
            duty_threshold=Decimal("5000"),
            effective_date="2026-07-01",
            eta_days_min=3,
            eta_days_max=7,
        ),
        lambda: ShippingRule(
            platform=Platform.AMAZON,
            flat_shipping=Decimal("30"),
            duty_rate=Decimal("0.10"),
            duty_threshold=Decimal("5000"),
            effective_date="2026-02-30",
            eta_days_min=3,
            eta_days_max=7,
        ),
    ],
)
def test_selector_and_resource_one_more_values_are_rejected(
    factory: object,
) -> None:
    assert callable(factory)
    with pytest.raises((ValidationError, ValueError)):
        factory()


def test_summary_rejects_real_search_service_final_gate_drift() -> None:
    gateways: list[InMemoryCatalogGateway] = []

    @dataclass
    class FailingIntent:
        async def interpret(self, request: SearchRequest) -> InterpretedRequest:
            del request
            raise RuntimeError("unavailable")

    def drifting_factory(
        gateway: InMemoryCatalogGateway,
        interpreted_request: InterpretedRequest,
    ) -> SearchService:
        del interpreted_request
        gateways.append(gateway)
        return SearchService(
            config=GlodexConfig(
                data_dir=Path("data/snapshots"),
                default_snapshot="m0-v1",
                default_locale="zh-CN",
                default_currency="USD",
                default_top_k=3,
                fingerprint="b" * 64,
            ),
            run_id_provider=_Ids(),
            clock=_Clock(),
            intent_interpreter=FailingIntent(),
            catalog_gateway=gateway,
            query_ranker=DeterministicQueryRanker(),
        )

    async def scenario() -> None:
        request = SearchRequest(
            query="推荐预算800美元、有库存的笔记本, 在 amazon",
            display_currency="USD",
            top_k=1,
        )
        interpreted = await RuleIntentInterpreter().interpret(request)
        pool = _pool()
        eligibility = evaluate_candidate_pool(
            pool,
            interpreted,
            display_currency=request.display_currency,
        )
        prices = run_price_compare(PriceCompareInput(pool=pool))
        shipping = run_shipping_calc(
            ShippingCalcInput(
                pool=pool,
                price_points=prices,
                destination_country="CN",
                rules=(),
                ruleset_version="cn-v1",
                calculation_date="2026-07-29",
            )
        )
        picker = run_item_picker(
            ItemPickerInput(
                eligibility=eligibility,
                prices=prices,
                shipping=shipping,
                preferred=interpreted.preferred,
                category_insight=None,
                target_candidate_groups=(),
                max_items=1,
            )
        )
        with pytest.raises(ToolPortError) as captured:
            await execute_business_tool(
                ToolName.SHOPPING_SUMMARY,
                ShoppingSummaryInput(
                    agent_run_id="agent-run-drift",
                    request=request,
                    interpreted_request=interpreted,
                    eligibility=eligibility,
                    picker=picker,
                    fx_source_batch=build_catalog_batch(),
                    search_service_factory=drifting_factory,
                ),
                ToolDependencies(
                    semantic_assertion=AcceptAllSemanticAssertion(),
                    shopping_summary=VerifiedShoppingSummary(),
                ),
            )
        assert captured.value.code is ToolFailureCode.FINAL_GATE_FAILED
        assert len(gateways) == 1
        with pytest.raises(RuntimeError, match="cleared"):
            await gateways[0].load(
                "agent-does-not-matter",
                display_currency="USD",
                budget_currency="USD",
            )

    asyncio.run(scenario())


def test_summary_emits_no_match_only_from_typed_empty_eligibility() -> None:
    async def scenario() -> None:
        request = SearchRequest(
            query="推荐预算1美元以内的笔记本",
            display_currency="USD",
            top_k=1,
        )
        interpreted = await RuleIntentInterpreter().interpret(request)
        pool = _pool()
        eligibility = evaluate_candidate_pool(
            pool,
            interpreted,
            display_currency=request.display_currency,
        )
        assert eligibility.eligible_candidate_ids == ()
        empty_picker = ItemPickerOutput(
            picks=(),
            rejected_brief=(ToolFailureCode.NO_ELIGIBLE_CANDIDATE.value,),
        )
        summary = await execute_business_tool(
            ToolName.SHOPPING_SUMMARY,
            ShoppingSummaryInput(
                agent_run_id="agent-run-no-match",
                request=request,
                interpreted_request=interpreted,
                eligibility=eligibility,
                picker=empty_picker,
                fx_source_batch=build_catalog_batch(),
                search_service_factory=_service_factory,
            ),
            ToolDependencies(
                semantic_assertion=AcceptAllSemanticAssertion(),
                shopping_summary=VerifiedShoppingSummary(),
            ),
        )
        assert summary.status == RunStatus.NO_MATCH.value  # type: ignore[union-attr]
        assert summary.search_response.results == ()  # type: ignore[union-attr]

        eligible_request = SearchRequest(
            query="推荐预算800美元、有库存的笔记本",
            display_currency="USD",
            top_k=1,
        )
        eligible_intent = await RuleIntentInterpreter().interpret(eligible_request)
        eligible = evaluate_candidate_pool(
            pool,
            eligible_intent,
            display_currency=eligible_request.display_currency,
        )
        with pytest.raises(ValueError, match="empty picker"):
            ShoppingSummaryInput(
                agent_run_id="agent-run-forged-no-match",
                request=eligible_request,
                interpreted_request=eligible_intent,
                eligibility=eligible,
                picker=empty_picker,
                fx_source_batch=build_catalog_batch(),
                search_service_factory=_service_factory,
            )

    asyncio.run(scenario())


def test_picker_skips_preference_model_when_no_candidate_is_eligible() -> None:
    class UnexpectedPreferenceAssessment:
        async def assess(self, _request: object) -> Never:
            raise AssertionError("preference model must not run for an empty eligible set")

    async def scenario() -> None:
        pool = _pool()
        preference = PreferredCriterion(
            value="游戏性能要好",
            source_span=SourceSpan(start=0, end=6, text="游戏性能要好"),
        )
        eligibility = evaluate_candidate_pool(
            pool,
            InterpretedRequest(required=(), preferred=(preference,), parser_version="test"),
            display_currency="USD",
            excluded_candidate_ids=tuple(candidate.candidate_id for candidate in pool.candidates),
        )
        prices = run_price_compare(PriceCompareInput(pool=pool))
        shipping = run_shipping_calc(
            ShippingCalcInput(
                pool=pool,
                price_points=prices,
                destination_country="CN",
                rules=(),
                ruleset_version="test-v1",
                calculation_date="2026-08-11",
            )
        )

        result = await execute_business_tool(
            ToolName.ITEM_PICKER,
            ItemPickerInput(
                eligibility=eligibility,
                prices=prices,
                shipping=shipping,
                preferred=(preference,),
                category_insight=None,
                target_candidate_groups=(),
                max_items=3,
            ),
            ToolDependencies(
                semantic_assertion=AcceptAllSemanticAssertion(),
                shopping_summary=VerifiedShoppingSummary(),
                preference_assessment=UnexpectedPreferenceAssessment(),
            ),
        )

        assert isinstance(result, ItemPickerOutput)
        assert result.picks == ()
        assert result.rejected_brief == (ToolFailureCode.NO_ELIGIBLE_CANDIDATE.value,)

    asyncio.run(scenario())
