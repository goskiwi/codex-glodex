from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from random import Random

import pytest

from glodex.domain.assembly import (
    FinalInvariantViolation,
    assemble_result_drafts,
    guard_result_drafts,
    require_guarded_results,
)
from glodex.domain.catalog import (
    CostComponents,
    EntityKind,
    Product,
    ProductAttribute,
    aggregate_catalog,
)
from glodex.domain.eligibility import (
    EligibilityContext,
    OfferGateId,
    ProductGateId,
    _run_offer_gates,
    _run_product_gates,
)
from glodex.domain.evidence import FieldEvidence
from glodex.domain.intent import (
    BudgetMax,
    InterpretedRequest,
    PreferredCriterion,
    SourceSpan,
    TargetCategory,
    validate_interpreted_request,
)
from glodex.domain.pricing import (
    KnownCost,
    LandedCost,
    calculate_landed_cost,
    canonical_exact_amount,
)
from glodex.domain.ranking import LexicalScore, degraded_rank, validate_and_rank
from tests.builders import build_exchange_rates, build_offer, build_product
from tests.unit.domain.test_result_assembly import _pipeline

pytestmark = [
    pytest.mark.nfr,
    pytest.mark.spec("GLO-NFR-001", "GLO-NFR-002", "GLO-NFR-003", "GLO-NFR-004"),
]

_SEEDS = (104_729, 499_979, 982_451)
_COST_NAMES = ("item_price", "shipping", "tax", "duty")


def _label(seed: int, case: int) -> str:
    return f"seed={seed} case={case}"


def _decimal_cents(cents: int) -> Decimal:
    return Decimal(f"{cents // 100}.{cents % 100:02d}")


def _costs(cents: tuple[int, int, int, int], *, prefix: str) -> CostComponents:
    values = tuple(
        KnownCost(
            amount=_decimal_cents(amount),
            evidence_id=f"ev-{prefix}-{name}",
        )
        for name, amount in zip(_COST_NAMES, cents, strict=True)
    )
    return CostComponents(
        currency="USD",
        item_price=values[0],
        shipping=values[1],
        tax=values[2],
        duty=values[3],
    )


def test_generated_money_is_exact_deterministic_and_component_monotone() -> None:
    rates = build_exchange_rates()

    for seed in _SEEDS:
        random = Random(seed)
        for case in range(20):
            label = _label(seed, case)
            cents = (
                random.randint(0, 2_000_000),
                random.randint(0, 2_000_000),
                random.randint(0, 2_000_000),
                random.randint(0, 2_000_000),
            )
            component = random.randrange(len(_COST_NAMES))
            increment = random.randint(1, 100_000)
            increased_values = list(cents)
            increased_values[component] += increment
            increased = (
                increased_values[0],
                increased_values[1],
                increased_values[2],
                increased_values[3],
            )

            original = calculate_landed_cost(
                _costs(cents, prefix=f"{seed}-{case}-a"),
                rates,
                display_currency="USD",
            )
            repeated = calculate_landed_cost(
                _costs(cents, prefix=f"{seed}-{case}-a"),
                rates,
                display_currency="USD",
            )
            larger = calculate_landed_cost(
                _costs(increased, prefix=f"{seed}-{case}-b"),
                rates,
                display_currency="USD",
            )

            assert type(original) is LandedCost, label
            assert repeated == original, label
            assert type(larger) is LandedCost, label
            expected_total = sum((_decimal_cents(value) for value in cents), Decimal(0))
            assert original.display_exact == expected_total, label
            assert larger.display_exact - original.display_exact == _decimal_cents(increment), label
            assert larger.display_exact > original.display_exact, label
            rendered = canonical_exact_amount(original.display_exact)
            assert "E" not in rendered and "e" not in rendered, label
            assert Decimal(rendered) == expected_total, label


def _source_product(
    *,
    product_id: str,
    provider_id: str,
    ordinal: int,
) -> Product:
    evidence_prefix = f"ev-{product_id}-{provider_id}"
    return build_product(
        product_id=product_id,
        provider_id=provider_id,
        source_uri=f"fixture://{provider_id}/products/{product_id}",
        title=f"Generated {product_id}",
        snapshot_ordinal=ordinal,
        attributes=(
            ProductAttribute(
                name="weight",
                value="1.2kg",
                evidence_id=f"{evidence_prefix}-weight",
            ),
        ),
        field_evidence=(
            FieldEvidence(
                field_path="product.title",
                evidence_id=f"{evidence_prefix}-title",
            ),
            FieldEvidence(
                field_path="product.category",
                evidence_id=f"{evidence_prefix}-category",
            ),
            FieldEvidence(
                field_path="product.entity_kind",
                evidence_id=f"{evidence_prefix}-kind",
            ),
        ),
    )


def test_generated_canonical_aggregation_conserves_every_legal_offer() -> None:
    for seed in _SEEDS:
        random = Random(seed)
        for case in range(10):
            label = _label(seed, case)
            product_count = random.randint(1, 5)
            products = []
            offers = []
            ordinal = 0
            for product_index in range(product_count):
                product_id = f"product-{case}-{product_index}"
                for source_index in range(random.randint(1, 3)):
                    provider_id = f"catalog-{product_index}-{source_index}"
                    products.append(
                        _source_product(
                            product_id=product_id,
                            provider_id=provider_id,
                            ordinal=ordinal,
                        )
                    )
                    ordinal += 1
                for offer_index in range(random.randint(1, 4)):
                    provider_id = f"seller-{product_index}-{offer_index}"
                    offer_id = f"offer-{case}-{product_index}-{offer_index}"
                    offers.append(
                        build_offer(
                            product_id=product_id,
                            provider_id=provider_id,
                            offer_id=offer_id,
                            source_uri=f"fixture://{provider_id}/offers/{offer_id}",
                            snapshot_ordinal=ordinal,
                        )
                    )
                    ordinal += 1

            expected = aggregate_catalog(tuple(products), tuple(offers))
            random.shuffle(products)
            random.shuffle(offers)
            shuffled = aggregate_catalog(tuple(products), tuple(offers))

            assert shuffled == expected, label
            assert shuffled.offers_conserved, label
            assert shuffled.quarantine_issues == (), label
            assert shuffled.source_offer_count == len(offers), label
            assert shuffled.quarantined_offer_count == 0, label
            assert len(shuffled.products) == product_count, label
            assert len({product.product_id for product in shuffled.products}) == product_count, (
                label
            )
            assert len(shuffled.offers) == len(offers), label


@dataclass(frozen=True, slots=True)
class _GateCandidate:
    product_id: str
    category: str
    entity_kind: EntityKind
    excluded: bool
    product_evidence: bool
    source_valid: bool
    in_stock: bool
    costs_complete: bool
    exchange_rate_present: bool
    exact_cost: Decimal


def _product_reasons(
    gate: ProductGateId,
    candidate: _GateCandidate,
    context: EligibilityContext,
) -> tuple[str, ...]:
    del context
    failed = {
        ProductGateId.CATEGORY: candidate.category != "laptop",
        ProductGateId.ENTITY_KIND: candidate.entity_kind is not EntityKind.PRIMARY_PRODUCT,
        ProductGateId.EXCLUSION: candidate.excluded,
        ProductGateId.REQUIRED_EVIDENCE: not candidate.product_evidence,
    }[gate]
    return (f"failed-{gate.value}",) if failed else ()


def _offer_reasons(
    gate: OfferGateId,
    candidate: _GateCandidate,
    context: EligibilityContext,
) -> tuple[str, ...]:
    budget = next(item for item in context.required if type(item) is BudgetMax)
    failed = {
        OfferGateId.SOURCE: not candidate.source_valid,
        OfferGateId.STOCK: not candidate.in_stock,
        OfferGateId.COST_COMPLETENESS: not candidate.costs_complete,
        OfferGateId.EXCHANGE_RATE: not candidate.exchange_rate_present,
        OfferGateId.BUDGET: candidate.exact_cost > budget.upper_bound,
    }[gate]
    return (f"failed-{gate.value}",) if failed else ()


def _passes_every_gate(candidate: _GateCandidate, budget: Decimal) -> bool:
    return (
        candidate.category == "laptop"
        and candidate.entity_kind is EntityKind.PRIMARY_PRODUCT
        and not candidate.excluded
        and candidate.product_evidence
        and candidate.source_valid
        and candidate.in_stock
        and candidate.costs_complete
        and candidate.exchange_rate_present
        and candidate.exact_cost <= budget
    )


def test_generated_hard_gates_never_leak_or_restore_a_rejected_candidate() -> None:
    for seed in _SEEDS:
        random = Random(seed)
        for case in range(15):
            label = _label(seed, case)
            budget = _decimal_cents(random.randint(10_000, 200_000))
            candidates = tuple(
                _GateCandidate(
                    product_id=f"candidate-{case}-{index}",
                    category=random.choice(("laptop", "camera")),
                    entity_kind=random.choice(tuple(EntityKind)),
                    excluded=random.choice((False, True)),
                    product_evidence=random.choice((False, True)),
                    source_valid=random.choice((False, True)),
                    in_stock=random.choice((False, True)),
                    costs_complete=random.choice((False, True)),
                    exchange_rate_present=random.choice((False, True)),
                    exact_cost=_decimal_cents(random.randint(0, 250_000)),
                )
                for index in range(random.randint(4, 20))
            )
            context = EligibilityContext(
                required=(
                    TargetCategory(
                        category="laptop",
                        source_span=SourceSpan(start=0, end=3, text="笔记本"),
                    ),
                    BudgetMax(
                        mode="maximum",
                        target_amount=budget,
                        lower_bound=None,
                        upper_bound=budget,
                        currency="USD",
                        source_span=SourceSpan(start=4, end=9, text="预算上限"),
                    ),
                )
            )

            product_output = _run_product_gates(candidates, context, _product_reasons)
            output = _run_offer_gates(product_output, _offer_reasons)
            expected = tuple(
                candidate for candidate in candidates if _passes_every_gate(candidate, budget)
            )

            assert output.candidates == expected, label
            all_results = (*product_output.gate_results, *output.offer_gate_results)
            assert all(result.after_count <= result.before_count for result in all_results), label
            assert all(
                result.kept == tuple(item for item in result.before if item in result.kept)
                for result in all_results
            ), label


def test_generated_ranker_faults_degrade_the_whole_batch_without_membership_change() -> None:
    for seed in _SEEDS:
        random = Random(seed)
        for case in range(8):
            label = _label(seed, case)
            pipeline = _pipeline(count=random.randint(2, 4))
            candidates = pipeline.ranked
            scores = tuple(
                LexicalScore(
                    product_id=candidate.product.product_id,
                    query_score=random.randint(0, 100),
                    verified_preference_coverage=random.randint(0, 10),
                )
                for candidate in candidates
            )
            fault = random.choice(("missing", "duplicate", "extra", "invalid"))
            if fault == "missing":
                hostile_scores = scores[:-1]
            elif fault == "duplicate":
                hostile_scores = (*scores[:-1], scores[0])
            elif fault == "extra":
                hostile_scores = (
                    *scores,
                    LexicalScore(
                        product_id=f"injected-{case}",
                        query_score=999,
                        verified_preference_coverage=999,
                    ),
                )
            else:
                object.__setattr__(scores[-1], "query_score", -1)
                hostile_scores = scores

            with pytest.raises((TypeError, ValueError)):
                validate_and_rank(candidates, hostile_scores)

            degraded = degraded_rank(candidates)
            assert len(degraded) == len(candidates), label
            assert {id(candidate) for candidate in degraded} == {
                id(candidate) for candidate in candidates
            }, label
            assert tuple(
                (candidate.product.snapshot_ordinal, candidate.product.product_id)
                for candidate in degraded
            ) == tuple(
                sorted(
                    (
                        candidate.product.snapshot_ordinal,
                        candidate.product.product_id,
                    )
                    for candidate in candidates
                )
            ), label


def test_generated_final_guard_rejects_every_output_mutation_atomically() -> None:
    for seed in _SEEDS:
        random = Random(seed)
        for case in range(6):
            label = _label(seed, case)
            pipeline = _pipeline(count=random.randint(2, 3))
            top_k = random.randint(1, len(pipeline.ranked))
            drafts = assemble_result_drafts(
                pipeline.ranked,
                pipeline.query,
                pipeline.interpreted,
                pipeline.evidence,
                pipeline.rates,
                top_k=top_k,
            )
            guarded = guard_result_drafts(
                drafts,
                ranked=pipeline.ranked,
                eligibility_output=pipeline.eligibility,
                query=pipeline.query,
                interpreted=pipeline.interpreted,
                evidence=pipeline.evidence,
                exchange_rates=pipeline.rates,
                display_currency=pipeline.display_currency,
                snapshot_version=pipeline.snapshot_version,
                top_k=top_k,
            )
            assert require_guarded_results(guarded).items == drafts, label
            assert len({draft.candidate.product.product_id for draft in drafts}) == len(drafts), (
                label
            )
            assert all(draft.evidence_ids for draft in drafts), label

            mutation = random.choice(("reason", "requirements", "evidence", "prefix"))
            first = drafts[0]
            if mutation == "reason":
                tampered = (
                    replace(
                        first,
                        projection=replace(
                            first.projection,
                            reason=f"{first.projection.reason} forged",
                        ),
                    ),
                    *drafts[1:],
                )
            elif mutation == "requirements":
                tampered = (
                    replace(
                        first,
                        matched_requirements=(*first.matched_requirements, "forged"),
                    ),
                    *drafts[1:],
                )
            elif mutation == "evidence":
                tampered = (
                    replace(first, evidence_ids=(*first.evidence_ids, "ev-forged")),
                    *drafts[1:],
                )
            else:
                tampered = tuple(reversed(drafts)) if len(drafts) > 1 else (*drafts, first)

            with pytest.raises(FinalInvariantViolation):
                guard_result_drafts(
                    tampered,
                    ranked=pipeline.ranked,
                    eligibility_output=pipeline.eligibility,
                    query=pipeline.query,
                    interpreted=pipeline.interpreted,
                    evidence=pipeline.evidence,
                    exchange_rates=pipeline.rates,
                    display_currency=pipeline.display_currency,
                    snapshot_version=pipeline.snapshot_version,
                    top_k=top_k,
                )


def test_generated_source_spans_are_exact_and_bad_offsets_fail_closed() -> None:
    for seed in _SEEDS:
        random = Random(seed)
        for case in range(20):
            label = _label(seed, case)
            amount = random.randint(1, 100_000)
            prefix = random.choice(("想要", "请推荐", "给我找"))
            budget_text = f"预算{amount}美元"
            query = f"{prefix}{budget_text}的笔记本，轻薄"  # noqa: RUF001
            budget_start = query.index(budget_text)
            category_start = query.index("笔记本")
            preferred_start = query.index("轻薄")
            valid_budget = BudgetMax(
                mode="maximum",
                target_amount=Decimal(amount),
                lower_bound=None,
                upper_bound=Decimal(amount),
                currency="USD",
                source_span=SourceSpan(
                    start=budget_start,
                    end=budget_start + len(budget_text),
                    text=budget_text,
                ),
            )
            interpreted = InterpretedRequest(
                required=(
                    valid_budget,
                    TargetCategory(
                        category="laptop",
                        source_span=SourceSpan(
                            start=category_start,
                            end=category_start + len("笔记本"),
                            text="笔记本",
                        ),
                    ),
                ),
                preferred=(
                    PreferredCriterion(
                        value="lightweight",
                        source_span=SourceSpan(
                            start=preferred_start,
                            end=preferred_start + len("轻薄"),
                            text="轻薄",
                        ),
                    ),
                ),
                parser_version="generated-v1",
            )
            assert validate_interpreted_request(query, interpreted).is_valid, label

            bad_kind = random.choice(("start", "end", "text", "bounds"))
            span = valid_budget.source_span
            if bad_kind == "start":
                bad_span = replace(span, start=span.start + 1)
            elif bad_kind == "end":
                bad_span = replace(span, end=span.end - 1)
            elif bad_kind == "text":
                bad_span = replace(span, text=f"{span.text}伪造")
            else:
                bad_span = replace(span, end=len(query) + 1)
            hostile = replace(
                interpreted,
                required=(replace(valid_budget, source_span=bad_span), interpreted.required[1]),
            )
            result = validate_interpreted_request(query, hostile)

            assert not result.is_valid, label
            assert result.interpreted_request is None, label
            assert result.issues, label
