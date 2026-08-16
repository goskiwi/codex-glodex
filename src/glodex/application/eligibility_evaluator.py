"""Shared, deterministic pricing and hard-gate composition."""

from __future__ import annotations

from dataclasses import dataclass

from glodex.domain.catalog import CatalogAggregationResult, CatalogBatch
from glodex.domain.eligibility import (
    EligibilityContext,
    EligibilityOutput,
    EligibleProduct,
    FilterSummary,
    OfferPricingCandidate,
    assemble_eligibility,
    run_offer_gates,
    run_product_gates,
)
from glodex.domain.intent import BudgetMax, InterpretedRequest
from glodex.domain.pricing import calculate_landed_cost


@dataclass(frozen=True, slots=True)
class EligibilityEvaluation:
    """The complete immutable output of the canonical eligibility pipeline."""

    pricing: tuple[OfferPricingCandidate, ...]
    eligibility_output: EligibilityOutput[EligibleProduct]
    filter_summary: FilterSummary

    def __post_init__(self) -> None:
        if type(self.pricing) is not tuple or any(
            type(candidate) is not OfferPricingCandidate for candidate in self.pricing
        ):
            raise TypeError("pricing must contain exact OfferPricingCandidate values")
        if type(self.eligibility_output) is not EligibilityOutput:
            raise TypeError("eligibility_output must be an exact EligibilityOutput")
        if type(self.filter_summary) is not FilterSummary:
            raise TypeError("filter_summary must be an exact FilterSummary")
        if self.eligibility_output.filter_summary is not self.filter_summary:
            raise ValueError("filter_summary must be the eligibility output summary")


def evaluate_eligibility(
    aggregation: CatalogAggregationResult,
    catalog_batch: CatalogBatch,
    interpreted_request: InterpretedRequest,
    *,
    display_currency: str,
) -> EligibilityEvaluation:
    """Run the one canonical pricing and hard-gate pipeline without I/O."""

    budget = next(
        (criterion for criterion in interpreted_request.required if type(criterion) is BudgetMax),
        None,
    )
    budget_currency = (
        None
        if budget is None
        else budget.currency
        if budget.currency is not None
        else display_currency
    )
    exchange_rates = catalog_batch.exchange_rates
    if exchange_rates is None:
        raise ValueError("catalog batch has no exchange-rate table")
    required_currencies = {
        display_currency,
        *(() if budget_currency is None else (budget_currency,)),
    }
    if not required_currencies.issubset(exchange_rates.supported_currencies):
        raise ValueError("requested display or budget currency is unsupported")

    context = EligibilityContext.from_interpreted_request(interpreted_request)
    pricing = tuple(
        OfferPricingCandidate(
            offer=offer,
            pricing=calculate_landed_cost(
                offer.cost_components,
                exchange_rates,
                display_currency=display_currency,
                budget_mode=None if budget is None else budget.mode,
                budget_target_amount=None if budget is None else budget.target_amount,
                budget_lower_bound=None if budget is None else budget.lower_bound,
                budget_upper_bound=None if budget is None else budget.upper_bound,
                budget_currency=None if budget is None else budget.currency,
            ),
        )
        for offer in aggregation.offers
    )
    product_output = run_product_gates(
        aggregation.products,
        context,
        catalog_batch.evidence,
    )
    offer_output = run_offer_gates(
        product_output,
        aggregation.offers,
        pricing,
        catalog_batch.evidence,
        display_currency=display_currency,
    )
    eligibility_output = assemble_eligibility(product_output, offer_output)
    filter_summary = eligibility_output.filter_summary
    if filter_summary is None:
        raise TypeError("eligibility assembly returned no filter summary")
    return EligibilityEvaluation(
        pricing=pricing,
        eligibility_output=eligibility_output,
        filter_summary=filter_summary,
    )


__all__ = ["EligibilityEvaluation", "evaluate_eligibility"]
