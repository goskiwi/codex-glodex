from __future__ import annotations

from dataclasses import replace

import pytest

from glodex.domain.catalog import EntityKind, StockStatus
from glodex.domain.eligibility import (
    AssemblyGateId,
    FilterScope,
    OfferGateId,
    ProductGateId,
    assemble_eligibility,
    run_offer_gates,
    run_product_gates,
)
from tests.unit.domain.test_eligibility_pipeline import _assemble, _offer, _product
from tests.unit.domain.test_offer_gates import (
    _context as _offer_context,
)
from tests.unit.domain.test_offer_gates import (
    _offer as _gate_offer,
)
from tests.unit.domain.test_offer_gates import (
    _run as _run_offer_fixture,
)
from tests.unit.domain.test_product_gates import (
    _context as _product_context,
)
from tests.unit.domain.test_product_gates import (
    _evidence as _product_evidence,
)
from tests.unit.domain.test_product_gates import (
    _product as _gate_product,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec("GLO-P0-006", "GLO-P0-009", "GLO-P0-010", "GLO-NFR-008"),
]


def test_filter_summary_keeps_product_and_offer_funnels_in_fixed_scope_order() -> None:
    product = _product("product-1", ordinal=0)
    offer = _offer(
        product_id=product.product_id,
        provider_id="provider-a",
        offer_id="offer-a",
        item_price="20.00",
        ordinal=0,
    )

    summary = _assemble((product,), (offer,)).filter_summary

    assert summary is not None
    assert tuple((stage.scope, stage.gate) for stage in summary.stages) == (
        (FilterScope.PRODUCT, ProductGateId.CATEGORY),
        (FilterScope.PRODUCT, ProductGateId.ENTITY_KIND),
        (FilterScope.PRODUCT, ProductGateId.EXCLUSION),
        (FilterScope.PRODUCT, ProductGateId.REQUIRED_EVIDENCE),
        (FilterScope.OFFER, OfferGateId.SOURCE),
        (FilterScope.OFFER, OfferGateId.STOCK),
        (FilterScope.OFFER, OfferGateId.COST_COMPLETENESS),
        (FilterScope.OFFER, OfferGateId.EXCHANGE_RATE),
        (FilterScope.PRODUCT, AssemblyGateId.HAS_ELIGIBLE_OFFER),
    )
    assert tuple((stage.before, stage.after) for stage in summary.stages) == (
        (1, 1),
        (1, 1),
        (1, 1),
        (1, 1),
        (1, 1),
        (1, 1),
        (1, 1),
        (1, 1),
        (1, 1),
    )
    assert summary.reason_counts == ()


def test_multilabel_counts_same_reason_as_one_product_and_two_offers() -> None:
    product = _product("product-1", ordinal=0)
    offers = tuple(
        replace(
            _offer(
                product_id=product.product_id,
                provider_id=provider_id,
                offer_id=offer_id,
                item_price="20.00",
                ordinal=ordinal,
            ),
            stock_status=StockStatus.OUT_OF_STOCK,
        )
        for ordinal, (provider_id, offer_id) in enumerate(
            (("provider-a", "offer-a"), ("provider-b", "offer-b"))
        )
    )

    output = _assemble((product,), offers)
    counts = {count.reason: count for count in output.filter_summary.reason_counts}

    assert output.candidates == ()
    assert counts["offer.out-of-stock"].product_count == 1
    assert counts["offer.out-of-stock"].offer_count == 2
    assert counts["product.no-eligible-offer"].product_count == 1
    assert counts["product.no-eligible-offer"].offer_count == 0
    stock_stage = next(
        stage for stage in output.filter_summary.stages if stage.gate is OfferGateId.STOCK
    )
    assert (stock_stage.before, stock_stage.after) == (2, 0)


def test_multilabel_summary_keeps_later_offer_reason_after_first_gate_rejection() -> None:
    context = _offer_context(budget="800.00")
    offer = _gate_offer(
        item_price="900.00",
        stock_status=StockStatus.OUT_OF_STOCK,
    )

    offer_output = _run_offer_fixture((offer,), context)
    output = assemble_eligibility(offer_output.product_output, offer_output)
    counts = {count.reason: count for count in output.filter_summary.reason_counts}

    assert counts["offer.out-of-stock"].offer_count == 1
    assert counts["offer.over-budget"].offer_count == 1
    assert any(stage.gate is OfferGateId.BUDGET for stage in output.filter_summary.stages)
    stock_stage = next(
        stage for stage in output.filter_summary.stages if stage.gate is OfferGateId.STOCK
    )
    budget_stage = next(
        stage for stage in output.filter_summary.stages if stage.gate is OfferGateId.BUDGET
    )
    assert (stock_stage.before, stock_stage.after) == (1, 0)
    assert (budget_stage.before, budget_stage.after) == (0, 0)


def test_multilabel_summary_preserves_all_product_reasons_after_first_failure() -> None:
    product = _gate_product(
        category="camera",
        entity_kind=EntityKind.ACCESSORY,
    )
    context = _product_context(target_category="laptop")
    product_output = run_product_gates(
        (product,),
        context,
        _product_evidence(product),
    )
    offer_output = run_offer_gates(
        product_output,
        (),
        (),
        (),
        display_currency="USD",
    )

    summary = assemble_eligibility(product_output, offer_output).filter_summary
    counts = {count.reason: count for count in summary.reason_counts}

    assert counts["product.category-mismatch"].product_count == 1
    assert counts["product.not-primary-product"].product_count == 1
    entity_stage = next(
        stage for stage in summary.stages if stage.gate is ProductGateId.ENTITY_KIND
    )
    assert (entity_stage.before, entity_stage.after) == (0, 0)
