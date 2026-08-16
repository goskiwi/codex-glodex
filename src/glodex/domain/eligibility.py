"""Nominal, immutable boundary for hard gates before query scoring.

This slice owns the fixed product and offer business gates and their stage
orders. Generic callback executors remain module-private test seams, so public
callers cannot replace or relax the concrete eligibility rules.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum, StrEnum
from hashlib import sha256
from hmac import compare_digest
from typing import Protocol, Self, cast
from unicodedata import normalize
from weakref import ReferenceType, ref

from glodex.domain.catalog import (
    CanonicalAttribute,
    CanonicalProduct,
    EntityKind,
    Offer,
    ProductSource,
    StockStatus,
)
from glodex.domain.evidence import EvidenceEntityType, EvidenceRef, FieldEvidence
from glodex.domain.intent import (
    BudgetMax,
    Exclusion,
    InterpretedRequest,
    RequiredConstraint,
    StockRequired,
    TargetCategory,
)
from glodex.domain.pricing import (
    KnownCost,
    LandedCost,
    PricingFailure,
    PricingFailureCode,
    PricingResult,
    UnknownCost,
)


class ProductGateId(StrEnum):
    """The non-configurable product-level hard-gate order."""

    CATEGORY = "category"
    ENTITY_KIND = "entity_kind"
    EXCLUSION = "exclusion"
    REQUIRED_EVIDENCE = "required_evidence"


class OfferGateId(StrEnum):
    """The non-configurable offer-level hard-gate order."""

    SOURCE = "source"
    STOCK = "stock"
    COST_COMPLETENESS = "cost_completeness"
    EXCHANGE_RATE = "exchange_rate"
    BUDGET = "budget"


class AssemblyGateId(StrEnum):
    """The product-level assembly gate applied after all offer gates."""

    HAS_ELIGIBLE_OFFER = "has_eligible_offer"


class FilterScope(StrEnum):
    """The entity count represented by one filter stage."""

    PRODUCT = "product"
    OFFER = "offer"


class ProductRejectionCode(StrEnum):
    """Stable product-level hard-gate rejection codes."""

    CATEGORY_MISMATCH = "product.category-mismatch"
    NOT_PRIMARY_PRODUCT = "product.not-primary-product"
    EXPLICIT_EXCLUSION = "product.explicit-exclusion"
    REQUIRED_EVIDENCE_INVALID = "product.required-evidence-invalid"


class OfferRejectionCode(StrEnum):
    """Stable offer-level hard-gate rejection codes."""

    SOURCE_INVALID = "offer.source-invalid"
    OUT_OF_STOCK = "offer.out-of-stock"
    STOCK_UNKNOWN = "offer.stock-unknown"
    COST_UNKNOWN = "offer.cost-unknown"
    EXCHANGE_RATE_MISSING = "offer.exchange-rate-missing"
    PRICING_ARITHMETIC = "offer.pricing-arithmetic"
    OVER_BUDGET = "offer.over-budget"


_NO_ELIGIBLE_OFFER_REASON = "product.no-eligible-offer"


PRODUCT_GATE_ORDER: tuple[ProductGateId, ...] = (
    ProductGateId.CATEGORY,
    ProductGateId.ENTITY_KIND,
    ProductGateId.EXCLUSION,
    ProductGateId.REQUIRED_EVIDENCE,
)
OFFER_GATE_ORDER: tuple[OfferGateId, ...] = (
    OfferGateId.SOURCE,
    OfferGateId.STOCK,
    OfferGateId.COST_COMPLETENESS,
    OfferGateId.EXCHANGE_RATE,
    OfferGateId.BUDGET,
)

type _GateId = ProductGateId | OfferGateId

_REQUIRED_TYPES = (BudgetMax, TargetCategory, StockRequired, Exclusion)
_CORE_PRODUCT_EVIDENCE_PATHS = frozenset(
    {
        "product.title",
        "product.category",
        "product.entity_kind",
    }
)
_CORE_OFFER_EVIDENCE_PATHS = frozenset(
    {
        "offer.inventory",
        "offer.market",
        "offer.cost_components.currency",
    }
)
_COST_COMPONENT_NAMES = ("item_price", "shipping", "tax", "duty")
_EXCLUSION_ALIASES = {
    "翻新": "refurbished",
    "二手": "used",
    "配件": "accessory",
    "替换件": "replacement_part",
    "贴纸": "sticker",
    "支架": "stand",
    "轻薄": "lightweight",
    "light_weight": "lightweight",
}

type _Seal = Callable[[object, bytes], None]
type _SealVerifier = Callable[[object, bytes], bool]


def _make_identity_registry() -> tuple[_Seal, _SealVerifier]:
    """Keep construction authority in a closure, not on output instances."""

    records: dict[int, tuple[ReferenceType[object], bytes]] = {}

    def seal(value: object, signature: bytes) -> None:
        identity = id(value)

        def discard(dead_reference: ReferenceType[object]) -> None:
            current = records.get(identity)
            if current is not None and current[0] is dead_reference:
                records.pop(identity, None)

        reference = ref(value, discard)
        records[identity] = (reference, signature)

    def verify(value: object, signature: bytes) -> bool:
        record = records.get(id(value))
        return record is not None and record[0]() is value and compare_digest(record[1], signature)

    return seal, verify


_seal_product_output, _verify_product_seal = _make_identity_registry()
_seal_business_product_output, _verify_business_product_seal = _make_identity_registry()
_seal_offer_output, _verify_offer_seal = _make_identity_registry()
_seal_eligibility_output, _verify_eligibility_seal = _make_identity_registry()
_seal_assembled_output, _verify_assembled_seal = _make_identity_registry()
_seal_scorer_input, _verify_scorer_input_seal = _make_identity_registry()


@dataclass(frozen=True, slots=True)
class EligibilityContext:
    """Only hard requirements visible to eligibility predicates.

    Preferred criteria are intentionally absent, so changing them cannot alter
    candidate membership.
    """

    required: tuple[RequiredConstraint, ...]

    def __post_init__(self) -> None:
        if type(self.required) is not tuple:
            raise TypeError("eligibility required constraints must be a tuple")
        if any(type(constraint) not in _REQUIRED_TYPES for constraint in self.required):
            raise TypeError("eligibility context contains an invalid required constraint")

    @classmethod
    def from_interpreted_request(cls, interpreted: InterpretedRequest) -> Self:
        """Project a validated interpretation onto its hard requirements."""

        if type(interpreted) is not InterpretedRequest:
            raise TypeError("interpreted must be an InterpretedRequest")
        return cls(required=interpreted.required)


@dataclass(frozen=True, slots=True)
class RejectedCandidate[T]:
    """One candidate rejected at its first failing gate."""

    candidate: T
    gate: _GateId
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_gate(self.gate)
        _require_reasons(self.reasons, allow_empty=False)


@dataclass(frozen=True, slots=True)
class GateResult[T]:
    """Immutable result of one stage in the sequential eligibility funnel."""

    gate: _GateId
    before: tuple[T, ...]
    kept: tuple[T, ...]
    rejected: tuple[RejectedCandidate[T], ...]

    def __post_init__(self) -> None:
        _require_gate(self.gate)
        _require_tuple(self.before, "gate input candidates")
        _require_tuple(self.kept, "gate kept candidates")
        _require_tuple(self.rejected, "gate rejected candidates")
        if any(type(rejection) is not RejectedCandidate for rejection in self.rejected):
            raise TypeError("gate rejected candidates must contain RejectedCandidate values")
        if any(rejection.gate is not self.gate for rejection in self.rejected):
            raise ValueError("rejected candidate gate must match its gate result")
        rejected_candidates = tuple(rejection.candidate for rejection in self.rejected)
        partition = (*self.kept, *rejected_candidates)
        if (
            sorted(map(id, partition)) != sorted(map(id, self.before))
            or not _is_identity_subsequence(self.kept, self.before)
            or not _is_identity_subsequence(rejected_candidates, self.before)
        ):
            raise ValueError("gate result must identity-partition its input candidates")

    @property
    def before_count(self) -> int:
        return len(self.before)

    @property
    def after_count(self) -> int:
        return len(self.kept)


@dataclass(frozen=True, slots=True)
class FilterStage:
    """One stable before/after count in the two-level hard-gate funnel."""

    scope: FilterScope
    gate: ProductGateId | OfferGateId | AssemblyGateId
    before: int
    after: int

    def __post_init__(self) -> None:
        if type(self.scope) is not FilterScope:
            raise TypeError("filter stage scope must be a FilterScope")
        if self.scope is FilterScope.OFFER:
            if type(self.gate) is not OfferGateId:
                raise ValueError("offer filter stages require an OfferGateId")
        elif type(self.gate) not in (ProductGateId, AssemblyGateId):
            raise ValueError("product filter stages require a product or assembly gate")
        for name, value in (("before", self.before), ("after", self.after)):
            if type(value) is not int or value < 0:
                raise ValueError(f"filter stage {name} must be a non-negative integer")
        if self.after > self.before:
            raise ValueError("filter stage after cannot exceed before")


@dataclass(frozen=True, slots=True)
class FilterReasonCount:
    """Independent multi-label counts for one stable rejection reason."""

    reason: str
    product_count: int
    offer_count: int

    def __post_init__(self) -> None:
        if type(self.reason) is not str or not self.reason.strip():
            raise ValueError("filter reason must be a non-empty string")
        for name, value in (
            ("product_count", self.product_count),
            ("offer_count", self.offer_count),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.product_count == 0 and self.offer_count == 0:
            raise ValueError("a filter reason count must describe at least one entity")


@dataclass(frozen=True, slots=True)
class FilterSummary:
    """Stable sequential funnel stages and independent multi-label counts."""

    stages: tuple[FilterStage, ...]
    reason_counts: tuple[FilterReasonCount, ...]

    def __post_init__(self) -> None:
        if type(self.stages) is not tuple or any(
            type(stage) is not FilterStage for stage in self.stages
        ):
            raise TypeError("filter summary stages must contain exact FilterStage values")
        if type(self.reason_counts) is not tuple or any(
            type(count) is not FilterReasonCount for count in self.reason_counts
        ):
            raise TypeError(
                "filter summary reason_counts must contain exact FilterReasonCount values"
            )
        for stage in self.stages:
            FilterStage.__post_init__(stage)
        for count in self.reason_counts:
            FilterReasonCount.__post_init__(count)
        stage_keys = tuple((stage.scope, stage.gate) for stage in self.stages)
        if len(stage_keys) != len(set(stage_keys)):
            raise ValueError("filter summary stages must be unique")
        reasons = tuple(count.reason for count in self.reason_counts)
        if len(reasons) != len(set(reasons)):
            raise ValueError("filter summary reasons must be unique")


@dataclass(frozen=True, slots=True)
class OfferPricingCandidate:
    """One exact Offer identity paired with its immutable pricing outcome."""

    offer: Offer
    pricing: PricingResult

    def __post_init__(self) -> None:
        _validate_offer_pricing_candidate(self)


@dataclass(frozen=True, slots=True)
class EligibleOffer:
    """An offer that passed every active offer-level hard gate."""

    offer: Offer
    landed_cost: LandedCost
    passed_gates: tuple[OfferGateId, ...]

    def __post_init__(self) -> None:
        _require_offer_invariants(self.offer)
        _require_pricing_result_invariants(self.landed_cost)
        _validate_success_pricing_matches_offer(self.offer, self.landed_cost)
        if type(self.passed_gates) is not tuple or any(
            type(gate) is not OfferGateId for gate in self.passed_gates
        ):
            raise TypeError("eligible offer passed_gates must contain OfferGateId values")
        if self.passed_gates not in (OFFER_GATE_ORDER[:-1], OFFER_GATE_ORDER):
            raise ValueError("eligible offer passed_gates must equal the active offer gate order")


@dataclass(frozen=True, slots=True)
class EligibleProduct:
    """One unique product with every eligible offer and a deterministic selection."""

    product: CanonicalProduct
    eligible_offers: tuple[EligibleOffer, ...]
    selected_offer: EligibleOffer

    def __post_init__(self) -> None:
        _require_canonical_products((self.product,))
        if type(self.eligible_offers) is not tuple or any(
            type(offer) is not EligibleOffer for offer in self.eligible_offers
        ):
            raise TypeError("eligible_offers must be a tuple of exact EligibleOffer values")
        if not self.eligible_offers:
            raise ValueError("eligible product requires at least one eligible offer")
        for eligible in self.eligible_offers:
            EligibleOffer.__post_init__(eligible)
            if eligible.offer.product_id != self.product.product_id:
                raise ValueError("eligible offer must belong to its eligible product")
            if eligible.offer.snapshot_version != self.product.snapshot_version:
                raise ValueError("eligible offer snapshot must match its eligible product")
        identities = tuple(
            (eligible.offer.provider_id, eligible.offer.offer_id)
            for eligible in self.eligible_offers
        )
        if len(identities) != len(set(identities)):
            raise ValueError("eligible product offer identities must be unique")
        if len({eligible.landed_cost.display_currency for eligible in self.eligible_offers}) != 1:
            raise ValueError("eligible product offers must share one display currency")
        if type(self.selected_offer) is not EligibleOffer or not any(
            self.selected_offer is eligible for eligible in self.eligible_offers
        ):
            raise ValueError("selected offer must be an exact member of eligible offers")
        if self.selected_offer is not min(
            self.eligible_offers,
            key=_eligible_offer_selection_key,
        ):
            raise ValueError("selected offer must use the exact stable offer key")


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class ProductGateOutput[T]:
    """Nominal output minted only after all product gates have run."""

    context: EligibilityContext
    candidates: tuple[T, ...]
    gate_results: tuple[GateResult[T], ...]
    diagnostics: tuple[RejectedCandidate[T], ...]

    def __init__(self) -> None:
        raise TypeError("ProductGateOutput is constructed by internal product gates")


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class OfferGateOutput:
    """Sealed result of the concrete offer-level hard-gate pipeline."""

    product_output: ProductGateOutput[CanonicalProduct]
    input_candidates: tuple[OfferPricingCandidate, ...]
    funnel_candidates: tuple[OfferPricingCandidate, ...]
    gate_results: tuple[GateResult[OfferPricingCandidate], ...]
    diagnostics: tuple[RejectedCandidate[OfferPricingCandidate], ...]
    eligible_offers: tuple[EligibleOffer, ...]

    def __init__(self) -> None:
        raise TypeError("OfferGateOutput is constructed by concrete offer gates")


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class EligibilityOutput[T]:
    """Nominal output minted only after both fixed hard-gate pipelines."""

    context: EligibilityContext
    candidates: tuple[T, ...]
    product_gate_results: tuple[GateResult[T], ...]
    offer_gate_results: tuple[GateResult[T], ...]
    assembled_product_output: ProductGateOutput[CanonicalProduct] | None
    assembled_offer_output: OfferGateOutput | None
    filter_summary: FilterSummary | None

    def __init__(self) -> None:
        raise TypeError("EligibilityOutput is constructed by internal offer gates")


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class EligibleCandidates[T]:
    """The only nominal candidate collection intended for a scorer."""

    candidates: tuple[T, ...]

    def __init__(self) -> None:
        raise TypeError("EligibleCandidates is constructed by scorer_input")


@dataclass(frozen=True, slots=True)
class _PreparedOfferBoundary:
    product_output: ProductGateOutput[CanonicalProduct]
    context: EligibilityContext
    input_candidates: tuple[OfferPricingCandidate, ...]
    funnel_candidates: tuple[OfferPricingCandidate, ...]
    evidence_index: dict[str, EvidenceRef]
    evidence_signature: bytes
    active_gates: tuple[OfferGateId, ...]
    budget: BudgetMax | None


def run_product_gates(
    products: tuple[CanonicalProduct, ...],
    context: EligibilityContext,
    evidence: tuple[EvidenceRef, ...],
) -> ProductGateOutput[CanonicalProduct]:
    """Run the fixed product hard gates with no caller-supplied predicate."""

    checked_context = _require_business_product_context(context)
    checked_products = _require_canonical_products(products)
    evidence_index, evidence_signature = _prepare_product_evidence(evidence)

    def fixed_predicate(
        gate: ProductGateId,
        product: CanonicalProduct,
        gate_context: EligibilityContext,
    ) -> tuple[str, ...]:
        return _business_product_reasons(
            gate,
            product,
            gate_context,
            evidence_index,
        )

    kept, results = _run_trusted_fixed_gates(
        checked_products,
        checked_context,
        PRODUCT_GATE_ORDER,
        fixed_predicate,
    )
    diagnostics = _diagnose_product_candidates(
        checked_products,
        checked_context,
        evidence_index,
    )
    if not _has_structure_signature(evidence_signature, evidence):
        raise TypeError("product evidence changed while product gates were running")
    business_output = _new_product_gate_output(
        context=checked_context,
        candidates=kept,
        gate_results=results,
        diagnostics=diagnostics,
        business=True,
    )
    return business_output


def diagnose_product_rejections(
    products: tuple[CanonicalProduct, ...],
    context: EligibilityContext,
    evidence: tuple[EvidenceRef, ...],
) -> tuple[RejectedCandidate[CanonicalProduct], ...]:
    """Evaluate every product gate for diagnostics without changing membership."""

    checked_context = _require_business_product_context(context)
    checked_products = _require_canonical_products(products)
    evidence_index, evidence_signature = _prepare_product_evidence(evidence)
    baseline = _structure_signature(checked_context, checked_products, evidence)
    diagnostics = _diagnose_product_candidates(
        checked_products,
        checked_context,
        evidence_index,
    )
    if not _has_structure_signature(
        baseline, checked_context, checked_products, evidence
    ) or not _has_structure_signature(evidence_signature, evidence):
        raise TypeError("product diagnostic inputs changed during evaluation")
    return diagnostics


def _diagnose_product_candidates(
    products: tuple[CanonicalProduct, ...],
    context: EligibilityContext,
    evidence_index: dict[str, EvidenceRef],
) -> tuple[RejectedCandidate[CanonicalProduct], ...]:
    diagnostics: list[RejectedCandidate[CanonicalProduct]] = []
    for product in products:
        for gate in PRODUCT_GATE_ORDER:
            reasons = _business_product_reasons(
                gate,
                product,
                context,
                evidence_index,
            )
            _require_reasons(reasons, allow_empty=True)
            if reasons:
                diagnostics.append(
                    RejectedCandidate(
                        candidate=product,
                        gate=gate,
                        reasons=reasons,
                    )
                )
    return tuple(diagnostics)


def run_offer_gates(
    product_output: ProductGateOutput[CanonicalProduct],
    offers: tuple[Offer, ...],
    pricing: tuple[OfferPricingCandidate, ...],
    evidence: tuple[EvidenceRef, ...],
    *,
    display_currency: str,
) -> OfferGateOutput:
    """Run the fixed active offer gates with no caller-supplied predicate."""

    prepared = _prepare_offer_boundary(
        product_output,
        offers,
        pricing,
        evidence,
        display_currency=display_currency,
    )
    baseline = _structure_signature(offers, pricing, evidence)

    def fixed_predicate(
        gate: OfferGateId,
        candidate: OfferPricingCandidate,
        gate_context: EligibilityContext,
    ) -> tuple[str, ...]:
        del gate_context
        return _business_offer_reasons(
            gate,
            candidate,
            prepared.evidence_index,
            prepared.budget,
        )

    kept, results = _run_trusted_fixed_gates(
        prepared.funnel_candidates,
        prepared.context,
        prepared.active_gates,
        fixed_predicate,
    )
    diagnostics = _diagnose_offer_candidates(prepared)
    eligible_offers = tuple(
        EligibleOffer(
            offer=candidate.offer,
            landed_cost=cast(LandedCost, candidate.pricing),
            passed_gates=prepared.active_gates,
        )
        for candidate in kept
    )
    if not _has_structure_signature(
        baseline, offers, pricing, evidence
    ) or not _has_structure_signature(prepared.evidence_signature, evidence):
        raise TypeError("offer gate inputs changed while offer gates were running")
    return _new_offer_gate_output(
        product_output=prepared.product_output,
        input_candidates=prepared.input_candidates,
        funnel_candidates=prepared.funnel_candidates,
        gate_results=results,
        diagnostics=diagnostics,
        eligible_offers=eligible_offers,
        verified_product_output=prepared.product_output,
    )


def diagnose_offer_rejections(
    product_output: ProductGateOutput[CanonicalProduct],
    offers: tuple[Offer, ...],
    pricing: tuple[OfferPricingCandidate, ...],
    evidence: tuple[EvidenceRef, ...],
    *,
    display_currency: str,
) -> tuple[RejectedCandidate[OfferPricingCandidate], ...]:
    """Evaluate every active offer gate without restoring rejected candidates."""

    prepared = _prepare_offer_boundary(
        product_output,
        offers,
        pricing,
        evidence,
        display_currency=display_currency,
    )
    baseline = _structure_signature(offers, pricing, evidence)
    diagnostics = _diagnose_offer_candidates(prepared)
    if not _has_structure_signature(
        baseline, offers, pricing, evidence
    ) or not _has_structure_signature(prepared.evidence_signature, evidence):
        raise TypeError("offer diagnostic inputs changed during evaluation")
    return diagnostics


def _diagnose_offer_candidates(
    prepared: _PreparedOfferBoundary,
) -> tuple[RejectedCandidate[OfferPricingCandidate], ...]:
    diagnostics: list[RejectedCandidate[OfferPricingCandidate]] = []
    for candidate in prepared.funnel_candidates:
        for gate in prepared.active_gates:
            reasons = _business_offer_reasons(
                gate,
                candidate,
                prepared.evidence_index,
                prepared.budget,
            )
            _require_reasons(reasons, allow_empty=True)
            if reasons:
                diagnostics.append(
                    RejectedCandidate(
                        candidate=candidate,
                        gate=gate,
                        reasons=reasons,
                    )
                )
    return tuple(diagnostics)


def assemble_eligibility(
    product_output: ProductGateOutput[CanonicalProduct],
    offer_output: OfferGateOutput,
) -> EligibilityOutput[EligibleProduct]:
    """Group all eligible offers and mint the only product set intended for scoring."""

    if not _is_valid_business_product_output(product_output):
        raise TypeError("assemble_eligibility requires concrete product business gates")
    checked_product = product_output
    if type(offer_output) is not OfferGateOutput:
        raise TypeError("assemble_eligibility requires a sealed OfferGateOutput")
    if offer_output.product_output is not checked_product:
        raise ValueError("offer output must be bound to the exact product output")
    if not _is_valid_offer_output_with_product(offer_output, checked_product):
        raise TypeError("assemble_eligibility requires a sealed OfferGateOutput")
    checked_offer = offer_output

    _, survivor_products = _product_universe_from_output(checked_product)
    offers_by_product: dict[str, list[EligibleOffer]] = {
        product.product_id: [] for product in survivor_products
    }
    for eligible in checked_offer.eligible_offers:
        group = offers_by_product.get(eligible.offer.product_id)
        if group is None:
            raise ValueError("eligible offer belongs to a product outside the survivor universe")
        group.append(eligible)

    candidates = tuple(
        EligibleProduct(
            product=product,
            eligible_offers=tuple(offers_by_product[product.product_id]),
            selected_offer=min(
                offers_by_product[product.product_id],
                key=_eligible_offer_selection_key,
            ),
        )
        for product in survivor_products
        if offers_by_product[product.product_id]
    )
    filter_summary = _build_filter_summary(
        checked_product,
        checked_offer,
        candidates,
    )
    return _new_assembled_eligibility_output(
        product_output=checked_product,
        offer_output=checked_offer,
        candidates=candidates,
        filter_summary=filter_summary,
        verified_product_output=checked_product,
        verified_offer_output=checked_offer,
    )


def _validate_assembled_candidates(
    product_output: ProductGateOutput[CanonicalProduct],
    offer_output: OfferGateOutput,
    candidates: tuple[EligibleProduct, ...],
) -> None:
    _, survivor_products = _product_universe_from_output(product_output)
    offer_lists_by_product: dict[str, list[EligibleOffer]] = {
        product.product_id: [] for product in survivor_products
    }
    for eligible in offer_output.eligible_offers:
        product_offers = offer_lists_by_product.get(eligible.offer.product_id)
        if product_offers is not None:
            product_offers.append(eligible)
    offers_by_product = {
        product_id: tuple(eligible_offers)
        for product_id, eligible_offers in offer_lists_by_product.items()
    }
    expected_products = tuple(
        product for product in survivor_products if offers_by_product[product.product_id]
    )
    if len(candidates) != len(expected_products):
        raise ValueError("assembled candidates must contain every product with an eligible offer")
    for candidate, product in zip(candidates, expected_products, strict=True):
        EligibleProduct.__post_init__(candidate)
        if candidate.product is not product:
            raise ValueError("assembled candidates must preserve product identity and order")
        expected_offers = offers_by_product[product.product_id]
        if len(candidate.eligible_offers) != len(expected_offers) or any(
            actual is not expected
            for actual, expected in zip(
                candidate.eligible_offers,
                expected_offers,
                strict=True,
            )
        ):
            raise ValueError("assembled candidates must preserve every eligible offer identity")


def _build_filter_summary(
    product_output: ProductGateOutput[CanonicalProduct],
    offer_output: OfferGateOutput,
    candidates: tuple[EligibleProduct, ...],
) -> FilterSummary:
    stages = (
        *(
            FilterStage(
                scope=FilterScope.PRODUCT,
                gate=cast(ProductGateId, result.gate),
                before=result.before_count,
                after=result.after_count,
            )
            for result in product_output.gate_results
        ),
        *(
            FilterStage(
                scope=FilterScope.OFFER,
                gate=cast(OfferGateId, result.gate),
                before=result.before_count,
                after=result.after_count,
            )
            for result in offer_output.gate_results
        ),
        FilterStage(
            scope=FilterScope.PRODUCT,
            gate=AssemblyGateId.HAS_ELIGIBLE_OFFER,
            before=len(product_output.candidates),
            after=len(candidates),
        ),
    )

    products_by_reason: dict[str, set[str]] = {}
    offers_by_reason: dict[str, set[tuple[str, str]]] = {}
    product_reasons_by_gate: dict[ProductGateId, set[str]] = {
        gate: set() for gate in PRODUCT_GATE_ORDER
    }
    offer_reasons_by_gate: dict[OfferGateId, set[str]] = {gate: set() for gate in OFFER_GATE_ORDER}
    for product_diagnostic in product_output.diagnostics:
        product_gate = cast(ProductGateId, product_diagnostic.gate)
        for reason in product_diagnostic.reasons:
            products_by_reason.setdefault(reason, set()).add(
                product_diagnostic.candidate.product_id
            )
            product_reasons_by_gate[product_gate].add(reason)
    for offer_diagnostic in offer_output.diagnostics:
        offer_gate = cast(OfferGateId, offer_diagnostic.gate)
        offer = offer_diagnostic.candidate.offer
        for reason in offer_diagnostic.reasons:
            products_by_reason.setdefault(reason, set()).add(offer.product_id)
            offers_by_reason.setdefault(reason, set()).add((offer.provider_id, offer.offer_id))
            offer_reasons_by_gate[offer_gate].add(reason)

    assembled_product_ids = {candidate.product.product_id for candidate in candidates}
    missing_offer_product_ids = {
        product.product_id
        for product in product_output.candidates
        if product.product_id not in assembled_product_ids
    }
    if missing_offer_product_ids:
        products_by_reason[_NO_ELIGIBLE_OFFER_REASON] = missing_offer_product_ids

    reason_order: list[str] = []
    for product_gate in PRODUCT_GATE_ORDER:
        reason_order.extend(sorted(product_reasons_by_gate[product_gate]))
    for offer_gate in OFFER_GATE_ORDER:
        reason_order.extend(sorted(offer_reasons_by_gate[offer_gate]))
    if missing_offer_product_ids:
        reason_order.append(_NO_ELIGIBLE_OFFER_REASON)
    stable_reason_order = tuple(dict.fromkeys(reason_order))
    reason_counts = tuple(
        FilterReasonCount(
            reason=reason,
            product_count=len(products_by_reason.get(reason, set())),
            offer_count=len(offers_by_reason.get(reason, set())),
        )
        for reason in stable_reason_order
    )
    return FilterSummary(
        stages=stages,
        reason_counts=reason_counts,
    )


def _run_product_gates[T](
    candidates: tuple[T, ...],
    context: EligibilityContext,
    predicate: Callable[[ProductGateId, T, EligibilityContext], tuple[str, ...]],
) -> ProductGateOutput[T]:
    """Internal/test seam behind the concrete public product runner."""

    _require_tuple(candidates, "product gate candidates")
    if type(context) is not EligibilityContext:
        raise TypeError("context must be an EligibilityContext")
    kept, results = _run_fixed_gates(candidates, context, PRODUCT_GATE_ORDER, predicate)
    diagnostics = tuple(rejection for result in results for rejection in result.rejected)
    return _new_product_gate_output(
        context=context,
        candidates=kept,
        gate_results=results,
        diagnostics=diagnostics,
    )


def _run_offer_gates[T](
    product_output: ProductGateOutput[T],
    predicate: Callable[[OfferGateId, T, EligibilityContext], tuple[str, ...]],
) -> EligibilityOutput[T]:
    """Internal C01 seam; the concrete public offer runner does not expose it."""

    if not _is_valid_product_output(product_output):
        raise TypeError("run_offer_gates requires a ProductGateOutput")
    kept, results = _run_fixed_gates(
        product_output.candidates,
        product_output.context,
        OFFER_GATE_ORDER,
        predicate,
    )
    if not _is_valid_product_output(product_output):
        raise TypeError("run_offer_gates requires an untampered ProductGateOutput")
    return _new_eligibility_output(
        product_output=product_output,
        candidates=kept,
        offer_gate_results=results,
    )


def scorer_input[T](output: EligibilityOutput[T]) -> EligibleCandidates[T]:
    """Mint the scorer's nominal input from final eligible survivors only."""

    if not _is_valid_assembled_eligibility_output(output):
        raise TypeError("scorer_input requires an assembled EligibilityOutput")
    return _new_eligible_candidates(output.candidates)


def _new_product_gate_output[T](
    *,
    context: EligibilityContext,
    candidates: tuple[T, ...],
    gate_results: tuple[GateResult[T], ...],
    diagnostics: tuple[RejectedCandidate[T], ...],
    business: bool = False,
) -> ProductGateOutput[T]:
    output = cast(ProductGateOutput[T], object.__new__(ProductGateOutput))
    object.__setattr__(output, "context", context)
    object.__setattr__(output, "candidates", candidates)
    object.__setattr__(output, "gate_results", gate_results)
    object.__setattr__(output, "diagnostics", diagnostics)
    signature = _product_output_signature(output)
    _seal_product_output(output, signature)
    if business:
        _seal_business_product_output(output, signature)
    return output


def _new_offer_gate_output(
    *,
    product_output: ProductGateOutput[CanonicalProduct],
    input_candidates: tuple[OfferPricingCandidate, ...],
    funnel_candidates: tuple[OfferPricingCandidate, ...],
    gate_results: tuple[GateResult[OfferPricingCandidate], ...],
    diagnostics: tuple[RejectedCandidate[OfferPricingCandidate], ...],
    eligible_offers: tuple[EligibleOffer, ...],
    verified_product_output: ProductGateOutput[CanonicalProduct],
) -> OfferGateOutput:
    output = object.__new__(OfferGateOutput)
    object.__setattr__(output, "product_output", product_output)
    object.__setattr__(output, "input_candidates", input_candidates)
    object.__setattr__(output, "funnel_candidates", funnel_candidates)
    object.__setattr__(output, "gate_results", gate_results)
    object.__setattr__(output, "diagnostics", diagnostics)
    object.__setattr__(output, "eligible_offers", eligible_offers)
    _seal_offer_output(
        output,
        _offer_output_signature(
            output,
            verified_product_output=verified_product_output,
        ),
    )
    return output


def _new_assembled_eligibility_output(
    *,
    product_output: ProductGateOutput[CanonicalProduct],
    offer_output: OfferGateOutput,
    candidates: tuple[EligibleProduct, ...],
    filter_summary: FilterSummary,
    verified_product_output: ProductGateOutput[CanonicalProduct],
    verified_offer_output: OfferGateOutput,
) -> EligibilityOutput[EligibleProduct]:
    output = cast(
        EligibilityOutput[EligibleProduct],
        object.__new__(EligibilityOutput),
    )
    object.__setattr__(output, "context", product_output.context)
    object.__setattr__(output, "candidates", candidates)
    object.__setattr__(output, "product_gate_results", product_output.gate_results)
    object.__setattr__(output, "offer_gate_results", offer_output.gate_results)
    object.__setattr__(output, "assembled_product_output", product_output)
    object.__setattr__(output, "assembled_offer_output", offer_output)
    object.__setattr__(output, "filter_summary", filter_summary)
    signature = _assembled_eligibility_output_signature(
        output,
        verified_product_output=verified_product_output,
        verified_offer_output=verified_offer_output,
    )
    _seal_assembled_output(output, signature)
    return output


def _new_eligibility_output[T](
    *,
    product_output: ProductGateOutput[T],
    candidates: tuple[T, ...],
    offer_gate_results: tuple[GateResult[T], ...],
) -> EligibilityOutput[T]:
    output = cast(EligibilityOutput[T], object.__new__(EligibilityOutput))
    object.__setattr__(output, "context", product_output.context)
    object.__setattr__(output, "candidates", candidates)
    object.__setattr__(
        output,
        "product_gate_results",
        product_output.gate_results,
    )
    object.__setattr__(output, "offer_gate_results", offer_gate_results)
    object.__setattr__(output, "assembled_product_output", None)
    object.__setattr__(output, "assembled_offer_output", None)
    object.__setattr__(output, "filter_summary", None)
    _seal_eligibility_output(output, _eligibility_output_signature(output))
    return output


def _new_eligible_candidates[T](candidates: tuple[T, ...]) -> EligibleCandidates[T]:
    _candidate_graph_signature(candidates)
    output = cast(EligibleCandidates[T], object.__new__(EligibleCandidates))
    object.__setattr__(output, "candidates", candidates)
    _seal_scorer_input(output, _structure_signature(candidates))
    return output


def _is_valid_product_output(value: object) -> bool:
    return _product_output_seal_state(value)[0]


def _is_valid_business_product_output(value: object) -> bool:
    return _product_output_seal_state(value)[1]


def _product_output_seal_state(value: object) -> tuple[bool, bool]:
    """Compute one product signature and check both construction authorities."""

    if type(value) is not ProductGateOutput:
        return False, False
    try:
        signature = _product_output_signature(value)
    except (AttributeError, TypeError, ValueError):
        return False, False
    product_sealed = _verify_product_seal(value, signature)
    return product_sealed, product_sealed and _verify_business_product_seal(
        value,
        signature,
    )


def _is_valid_offer_output(value: object) -> bool:
    if type(value) is not OfferGateOutput:
        return False
    try:
        product_output = value.product_output
    except AttributeError:
        return False
    if not _is_valid_business_product_output(product_output):
        return False
    return _is_valid_offer_output_with_product(
        value,
        product_output,
    )


def _is_valid_offer_output_with_product(
    value: object,
    verified_product_output: ProductGateOutput[CanonicalProduct],
) -> bool:
    """Verify an offer seal after its exact product parent was fully verified."""

    if type(value) is not OfferGateOutput:
        return False
    if value.product_output is not verified_product_output:
        return False
    try:
        signature = _offer_output_signature(
            value,
            verified_product_output=verified_product_output,
        )
    except (AttributeError, ArithmeticError, TypeError, ValueError):
        return False
    return _verify_offer_seal(value, signature)


def _is_valid_assembled_eligibility_output(value: object) -> bool:
    if type(value) is not EligibilityOutput:
        return False
    try:
        product_output = value.assembled_product_output
        offer_output = value.assembled_offer_output
    except AttributeError:
        return False
    if type(product_output) is not ProductGateOutput or type(offer_output) is not OfferGateOutput:
        return False
    checked_product = product_output
    if not _is_valid_offer_output_with_product(offer_output, checked_product):
        return False
    checked_offer = offer_output
    try:
        signature = _assembled_eligibility_output_signature(
            value,
            verified_product_output=checked_product,
            verified_offer_output=checked_offer,
        )
    except (AttributeError, ArithmeticError, TypeError, ValueError):
        return False
    return _verify_assembled_seal(value, signature)


def _product_output_signature[T](output: ProductGateOutput[T]) -> bytes:
    if type(output.context) is not EligibilityContext:
        raise TypeError("product gate output context must be an EligibilityContext")
    _require_tuple(output.candidates, "product gate output candidates")
    gate_results = _require_tuple(output.gate_results, "product gate output results")
    diagnostics = _require_tuple(output.diagnostics, "product gate diagnostics")
    if any(type(result) is not GateResult for result in gate_results):
        raise TypeError("product gate results must contain exact GateResult values")
    checked_gate_results = cast(tuple[GateResult[T], ...], gate_results)
    original_candidates = (
        checked_gate_results[0].before if checked_gate_results else output.candidates
    )
    original_candidate_ids = {id(candidate) for candidate in original_candidates}
    diagnostic_keys: list[tuple[int, ProductGateId]] = []
    for diagnostic in diagnostics:
        if type(diagnostic) is not RejectedCandidate or type(diagnostic.gate) is not ProductGateId:
            raise TypeError("product diagnostics must contain product rejections")
        RejectedCandidate.__post_init__(diagnostic)
        if id(diagnostic.candidate) not in original_candidate_ids:
            raise ValueError("product diagnostic candidate must belong to the original universe")
        diagnostic_keys.append((id(diagnostic.candidate), diagnostic.gate))
    if len(diagnostic_keys) != len(set(diagnostic_keys)):
        raise ValueError("product diagnostics must be unique per candidate and gate")
    _candidate_graph_signature(output.candidates)
    return _structure_signature(
        output.context,
        output.candidates,
        checked_gate_results,
        diagnostics,
    )


def _offer_output_signature(
    output: OfferGateOutput,
    *,
    verified_product_output: ProductGateOutput[CanonicalProduct] | None = None,
) -> bytes:
    if verified_product_output is None:
        if not _is_valid_business_product_output(output.product_output):
            raise TypeError("offer output requires concrete product business gates")
        checked_product = output.product_output
    else:
        if output.product_output is not verified_product_output:
            raise ValueError("offer output must bind the exact verified product output")
        checked_product = verified_product_output
    input_candidates = _require_offer_pricing_candidates(output.input_candidates)
    funnel_candidates = cast(
        tuple[OfferPricingCandidate, ...],
        _require_tuple(output.funnel_candidates, "offer funnel candidates"),
    )
    if any(type(candidate) is not OfferPricingCandidate for candidate in funnel_candidates):
        raise TypeError("offer funnel candidates must contain OfferPricingCandidate values")
    for candidate in funnel_candidates:
        _validate_offer_pricing_candidate(candidate)
    if not _is_identity_subsequence(funnel_candidates, input_candidates):
        raise ValueError("offer funnel candidates must be an identity subsequence of inputs")
    gate_results = cast(
        tuple[GateResult[OfferPricingCandidate], ...],
        _require_tuple(output.gate_results, "offer gate results"),
    )
    active_gates = _active_offer_gates(_offer_budget(checked_product.context))
    if tuple(result.gate for result in gate_results) != active_gates:
        raise ValueError("offer gate results must follow the active offer gate order")
    expected_before = funnel_candidates
    for result in gate_results:
        if type(result) is not GateResult or result.before != expected_before:
            raise ValueError("offer gate results must form one sequential funnel")
        GateResult.__post_init__(result)
        expected_before = result.kept
    diagnostics = cast(
        tuple[RejectedCandidate[OfferPricingCandidate], ...],
        _require_tuple(output.diagnostics, "offer gate diagnostics"),
    )
    funnel_candidate_ids = {id(candidate) for candidate in funnel_candidates}
    diagnostic_keys: list[tuple[int, OfferGateId]] = []
    for diagnostic in diagnostics:
        if type(diagnostic) is not RejectedCandidate or type(diagnostic.gate) is not OfferGateId:
            raise TypeError("offer diagnostics must contain offer rejections")
        RejectedCandidate.__post_init__(diagnostic)
        if id(diagnostic.candidate) not in funnel_candidate_ids:
            raise ValueError("offer diagnostic candidate must belong to the offer funnel")
        if diagnostic.gate not in active_gates:
            raise ValueError("offer diagnostic gate must be active")
        diagnostic_keys.append((id(diagnostic.candidate), diagnostic.gate))
    if len(diagnostic_keys) != len(set(diagnostic_keys)):
        raise ValueError("offer diagnostics must be unique per candidate and gate")
    eligible_offers = cast(
        tuple[EligibleOffer, ...],
        _require_tuple(output.eligible_offers, "eligible offers"),
    )
    if any(type(item) is not EligibleOffer for item in eligible_offers):
        raise TypeError("eligible offers must contain exact EligibleOffer values")
    if len(eligible_offers) != len(expected_before):
        raise ValueError("eligible offers must correspond to final offer survivors")
    for eligible, candidate in zip(eligible_offers, expected_before, strict=True):
        EligibleOffer.__post_init__(eligible)
        if (
            eligible.offer is not candidate.offer
            or eligible.landed_cost is not candidate.pricing
            or eligible.passed_gates != active_gates
        ):
            raise ValueError("eligible offer must preserve its final survivor identity")
    return _structure_signature(
        b"glodex-offer-output-v2",
        id(checked_product),
        input_candidates,
        funnel_candidates,
        gate_results,
        diagnostics,
        eligible_offers,
    )


def _eligibility_output_signature[T](output: EligibilityOutput[T]) -> bytes:
    if type(output.context) is not EligibilityContext:
        raise TypeError("eligibility output context must be an EligibilityContext")
    _require_tuple(output.candidates, "eligibility output candidates")
    _require_tuple(output.product_gate_results, "eligibility product gate results")
    _require_tuple(output.offer_gate_results, "eligibility offer gate results")
    if (
        output.assembled_product_output is not None
        or output.assembled_offer_output is not None
        or output.filter_summary is not None
    ):
        raise TypeError("private eligibility output cannot claim assembled provenance")
    _candidate_graph_signature(output.candidates)
    return _structure_signature(
        output.context,
        output.candidates,
        output.product_gate_results,
        output.offer_gate_results,
    )


def _assembled_eligibility_output_signature(
    output: EligibilityOutput[object],
    *,
    verified_product_output: ProductGateOutput[CanonicalProduct] | None = None,
    verified_offer_output: OfferGateOutput | None = None,
) -> bytes:
    product_output = output.assembled_product_output
    offer_output = output.assembled_offer_output
    summary = output.filter_summary
    if verified_product_output is None:
        if not _is_valid_business_product_output(product_output):
            raise TypeError("assembled output requires concrete product business gates")
        checked_product = cast(ProductGateOutput[CanonicalProduct], product_output)
    else:
        if product_output is not verified_product_output:
            raise ValueError("assembled output must bind the exact verified product output")
        checked_product = verified_product_output
    if verified_offer_output is None:
        if not _is_valid_offer_output_with_product(offer_output, checked_product):
            raise TypeError("assembled output requires a sealed OfferGateOutput")
        checked_offer = cast(OfferGateOutput, offer_output)
    else:
        if offer_output is not verified_offer_output:
            raise ValueError("assembled output must bind the exact verified offer output")
        if verified_offer_output.product_output is not checked_product:
            raise ValueError("verified offer output must bind the exact product output")
        checked_offer = verified_offer_output
    if checked_offer.product_output is not checked_product:
        raise ValueError("assembled offer output must bind the exact product output")
    if output.context is not checked_product.context:
        raise ValueError("assembled context must preserve product output identity")
    if output.product_gate_results is not checked_product.gate_results:
        raise ValueError("assembled product gate results must preserve tuple identity")
    if output.offer_gate_results is not checked_offer.gate_results:
        raise ValueError("assembled offer gate results must preserve tuple identity")
    candidates = cast(
        tuple[EligibleProduct, ...],
        _require_tuple(output.candidates, "assembled eligible products"),
    )
    if any(type(candidate) is not EligibleProduct for candidate in candidates):
        raise TypeError("assembled candidates must contain exact EligibleProduct values")
    _validate_assembled_candidates(checked_product, checked_offer, candidates)
    if type(summary) is not FilterSummary:
        raise TypeError("assembled output requires a FilterSummary")
    FilterSummary.__post_init__(summary)
    if summary != _build_filter_summary(checked_product, checked_offer, candidates):
        raise ValueError("assembled filter summary must be exactly reproducible")
    return _structure_signature(
        b"glodex-assembled-output-v2",
        _product_parent_identity_signature(checked_product),
        id(checked_offer),
        checked_product.context,
        candidates,
        summary,
    )


def _product_parent_identity_signature(
    output: ProductGateOutput[CanonicalProduct],
) -> bytes:
    """Bind an assembled seal to exact parent components without rehashing their graph."""

    component_ids = [
        id(output),
        id(output.context),
        id(output.candidates),
        id(output.gate_results),
        id(output.diagnostics),
    ]
    for result in output.gate_results:
        component_ids.extend(
            (
                id(result),
                id(result.gate),
                id(result.before),
                id(result.kept),
                id(result.rejected),
            )
        )
    return _structure_signature(
        b"glodex-product-parent-identity-v1",
        *component_ids,
    )


def _prepare_offer_boundary(
    product_output: object,
    offers: object,
    pricing: object,
    evidence: object,
    *,
    display_currency: object,
) -> _PreparedOfferBoundary:
    product_sealed, business_sealed = _product_output_seal_state(product_output)
    if not product_sealed:
        raise TypeError("run_offer_gates requires a sealed ProductGateOutput")
    if not business_sealed:
        raise TypeError("run_offer_gates requires concrete product business gates")
    checked_output = cast(ProductGateOutput[CanonicalProduct], product_output)
    context = checked_output.context
    budget = _require_business_offer_context(context, display_currency)
    checked_display_currency = cast(str, display_currency)
    original_products, survivor_products = _product_universe_from_output(checked_output)
    checked_offers = _require_offers(offers)
    original_by_id = {product.product_id: product for product in original_products}
    identities: list[tuple[str, str]] = []
    for offer in checked_offers:
        product = original_by_id.get(offer.product_id)
        if product is None:
            raise ValueError("offer references a product outside the product gate universe")
        if offer.snapshot_version != product.snapshot_version:
            raise ValueError("offer snapshot must match its canonical product")
        identities.append((offer.provider_id, offer.offer_id))
    if len(identities) != len(set(identities)):
        raise ValueError("provider-scoped offer identities must be unique")

    input_candidates = _require_offer_pricing_candidates(pricing)
    if len(input_candidates) != len(checked_offers):
        raise ValueError("offers and pricing must have one-to-one cardinality")
    for offer, candidate in zip(checked_offers, input_candidates, strict=True):
        if candidate.offer is not offer:
            raise ValueError("offers and pricing must preserve exact identity and order")
        _require_pricing_context_contract(
            candidate,
            budget,
            display_currency=checked_display_currency,
        )

    evidence_index, evidence_signature = _prepare_offer_evidence(evidence)
    survivor_ids = {product.product_id for product in survivor_products}
    funnel_candidates = tuple(
        candidate for candidate in input_candidates if candidate.offer.product_id in survivor_ids
    )
    return _PreparedOfferBoundary(
        product_output=checked_output,
        context=context,
        input_candidates=input_candidates,
        funnel_candidates=funnel_candidates,
        evidence_index=evidence_index,
        evidence_signature=evidence_signature,
        active_gates=_active_offer_gates(budget),
        budget=budget,
    )


def _product_universe_from_output(
    output: ProductGateOutput[CanonicalProduct],
) -> tuple[tuple[CanonicalProduct, ...], tuple[CanonicalProduct, ...]]:
    """Read a canonical universe from an already fully verified business seal."""

    if tuple(result.gate for result in output.gate_results) != PRODUCT_GATE_ORDER:
        raise TypeError("ProductGateOutput must contain the complete product gate order")
    original = output.gate_results[0].before
    survivors = output.candidates
    return original, survivors


def _require_business_offer_context(
    context: object,
    display_currency: object,
) -> BudgetMax | None:
    if type(context) is not EligibilityContext:
        raise TypeError("offer gate context must be an EligibilityContext")
    _require_currency_code(display_currency, "display currency")
    if type(context.required) is not tuple or any(
        type(constraint) not in _REQUIRED_TYPES for constraint in context.required
    ):
        raise TypeError("offer gate context contains invalid required constraints")
    budget = _offer_budget(context)
    for constraint in context.required:
        if type(constraint) is StockRequired and (
            type(constraint.kind) is not str or constraint.kind != "stock_required"
        ):
            raise ValueError("stock requirement discriminator must be stock_required")
    return budget


def _offer_budget(context: EligibilityContext) -> BudgetMax | None:
    budgets = tuple(constraint for constraint in context.required if type(constraint) is BudgetMax)
    if len(budgets) > 1:
        raise ValueError("offer gate context allows at most one budget maximum")
    if not budgets:
        return None
    budget = budgets[0]
    amounts = (budget.target_amount, budget.upper_bound)
    if any(
        type(amount) is not Decimal or not amount.is_finite() or amount <= 0 for amount in amounts
    ):
        raise ValueError("budget target and upper bound must be finite positive Decimals")
    if budget.lower_bound is not None and (
        type(budget.lower_bound) is not Decimal
        or not budget.lower_bound.is_finite()
        or budget.lower_bound <= 0
    ):
        raise ValueError("budget lower bound must be a finite positive Decimal or None")
    if budget.mode == "maximum":
        if budget.lower_bound is not None or budget.upper_bound != budget.target_amount:
            raise ValueError("maximum budget bounds are inconsistent")
    elif budget.mode == "around":
        if budget.lower_bound != budget.target_amount * Decimal(
            "0.90"
        ) or budget.upper_bound != budget.target_amount * Decimal("1.10"):
            raise ValueError("around budget requires exact ten-percent bounds")
    elif budget.mode == "range":
        if (
            budget.lower_bound is None
            or budget.lower_bound >= budget.upper_bound
            or budget.target_amount != budget.upper_bound
        ):
            raise ValueError("range budget requires explicit ordered bounds")
    else:
        raise ValueError("budget mode must be maximum, around, or range")
    if budget.currency is not None:
        _require_currency_code(budget.currency, "budget currency")
    if type(budget.kind) is not str or budget.kind != "budget_max":
        raise ValueError("budget discriminator must be budget_max")
    return budget


def _active_offer_gates(budget: BudgetMax | None) -> tuple[OfferGateId, ...]:
    return OFFER_GATE_ORDER if budget is not None else OFFER_GATE_ORDER[:-1]


def _require_currency_code(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 3
        or not value.isascii()
        or not value.isalpha()
        or not value.isupper()
    ):
        raise ValueError(f"{name} must be an uppercase three-letter code")
    return value


def _require_offers(offers: object) -> tuple[Offer, ...]:
    offer_tuple = _require_tuple(offers, "offers")
    if any(type(offer) is not Offer for offer in offer_tuple):
        raise TypeError("offers must contain exact Offer values")
    checked = cast(tuple[Offer, ...], offer_tuple)
    try:
        for offer in checked:
            _require_offer_invariants(offer)
        _candidate_graph_signature(checked)
    except (AttributeError, ArithmeticError, TypeError, ValueError) as error:
        raise TypeError("offer invariants must hold at the public boundary") from error
    return checked


def _require_offer_invariants(offer: object) -> Offer:
    if type(offer) is not Offer:
        raise TypeError("offer must be an exact Offer")
    Offer.__post_init__(offer)
    for binding in offer.field_evidence:
        FieldEvidence.__post_init__(binding)
    _structure_signature(offer)
    return offer


def _require_offer_pricing_candidates(
    pricing: object,
) -> tuple[OfferPricingCandidate, ...]:
    pricing_tuple = _require_tuple(pricing, "offer pricing")
    if any(type(candidate) is not OfferPricingCandidate for candidate in pricing_tuple):
        raise TypeError("offer pricing must contain exact OfferPricingCandidate values")
    checked = cast(tuple[OfferPricingCandidate, ...], pricing_tuple)
    for candidate in checked:
        _validate_offer_pricing_candidate(candidate)
    _candidate_graph_signature(checked)
    return checked


def _validate_offer_pricing_candidate(candidate: object) -> None:
    if type(candidate) is not OfferPricingCandidate:
        raise TypeError("pricing candidate must be an exact OfferPricingCandidate")
    offer = _require_offer_invariants(candidate.offer)
    result = _require_pricing_result_invariants(candidate.pricing)
    if isinstance(result, LandedCost):
        _validate_success_pricing_matches_offer(offer, result)
    else:
        _validate_failure_pricing_matches_offer(offer, result)


def _eligible_offer_selection_key(
    eligible: EligibleOffer,
) -> tuple[Decimal, str, str]:
    return (
        eligible.landed_cost.display_exact,
        eligible.offer.provider_id,
        eligible.offer.offer_id,
    )


def _require_pricing_result_invariants(result: object) -> PricingResult:
    try:
        if type(result) is LandedCost:
            LandedCost.__post_init__(result)
        elif type(result) is PricingFailure:
            PricingFailure.__post_init__(result)
        else:
            raise TypeError("pricing must be an exact LandedCost or PricingFailure")
        _structure_signature(result)
    except (AttributeError, ArithmeticError, TypeError, ValueError) as error:
        raise TypeError("pricing result invariants must hold at the public boundary") from error
    return result


def _validate_success_pricing_matches_offer(
    offer: Offer,
    result: LandedCost,
) -> None:
    if (
        result.trace.snapshot_version != offer.snapshot_version
        or result.trace.source_currency != offer.cost_components.currency
    ):
        raise ValueError("successful pricing trace must match the offer snapshot and currency")
    offer_costs = tuple(
        (name, getattr(offer.cost_components, name)) for name in _COST_COMPONENT_NAMES
    )
    if any(type(cost) is not KnownCost for _, cost in offer_costs):
        raise ValueError("successful pricing requires four known offer costs")
    for trace, (name, cost) in zip(result.trace.components, offer_costs, strict=True):
        known = cast(KnownCost, cost)
        if (
            trace.component != name
            or trace.source_amount != known.amount
            or trace.evidence_id != known.evidence_id
        ):
            raise ValueError("successful pricing components must exactly match the offer")


def _validate_failure_pricing_matches_offer(
    offer: Offer,
    result: PricingFailure,
) -> None:
    unknown_costs = tuple(
        (name, cost.reason)
        for name in _COST_COMPONENT_NAMES
        if type(cost := getattr(offer.cost_components, name)) is UnknownCost
    )
    if result.code is PricingFailureCode.UNKNOWN_COST:
        details = tuple((detail.component, detail.reason) for detail in result.unknown_costs)
        if details != unknown_costs:
            raise ValueError("unknown-cost pricing details must exactly match the offer")
    elif unknown_costs:
        raise ValueError("unknown offer costs require an UNKNOWN_COST pricing failure")


def _require_pricing_context_contract(
    candidate: OfferPricingCandidate,
    budget: BudgetMax | None,
    *,
    display_currency: str,
) -> None:
    result = candidate.pricing
    effective_budget_currency = (
        display_currency if budget is None or budget.currency is None else budget.currency
    )
    if isinstance(result, LandedCost):
        if result.display_currency != display_currency:
            raise ValueError("pricing display currency does not match the request")
        if result.budget_currency != effective_budget_currency:
            raise ValueError("pricing budget currency does not match the effective budget")
        expected_constraint = (
            (None, None, None, None)
            if budget is None
            else (
                budget.mode,
                budget.target_amount,
                budget.lower_bound,
                budget.upper_bound,
            )
        )
        actual_constraint = (
            result.budget_mode,
            result.budget_target_amount,
            result.budget_lower_bound,
            result.budget_upper_bound,
        )
        if actual_constraint != expected_constraint:
            raise ValueError("pricing budget constraint does not match the request")
    elif isinstance(result, PricingFailure) and (
        result.code is PricingFailureCode.MISSING_EXCHANGE_RATE
    ):
        required_currencies = {
            candidate.offer.cost_components.currency,
            display_currency,
            effective_budget_currency,
        }
        if not set(result.missing_currencies).issubset(required_currencies):
            raise ValueError("missing-rate pricing names an unrelated currency")


def _prepare_offer_evidence(
    evidence: object,
) -> tuple[dict[str, EvidenceRef], bytes]:
    evidence_tuple = _require_tuple(evidence, "offer evidence")
    if any(type(item) is not EvidenceRef for item in evidence_tuple):
        raise TypeError("offer evidence must contain exact EvidenceRef values")
    checked = cast(tuple[EvidenceRef, ...], evidence_tuple)
    index: dict[str, EvidenceRef] = {}
    try:
        for item in checked:
            EvidenceRef.__post_init__(item)
            if item.evidence_id in index:
                raise ValueError(f"duplicate evidence ID: {item.evidence_id}")
            index[item.evidence_id] = item
        signature = _structure_signature(checked)
    except (AttributeError, ArithmeticError, TypeError) as error:
        raise TypeError("offer evidence invariants must hold at the public boundary") from error
    return index, signature


def _business_offer_reasons(
    gate: OfferGateId,
    candidate: OfferPricingCandidate,
    evidence_index: dict[str, EvidenceRef],
    budget: BudgetMax | None,
) -> tuple[str, ...]:
    if gate is OfferGateId.SOURCE:
        if not _has_required_offer_evidence(candidate.offer, evidence_index):
            return (OfferRejectionCode.SOURCE_INVALID.value,)
        return ()
    if gate is OfferGateId.STOCK:
        if candidate.offer.stock_status is StockStatus.OUT_OF_STOCK:
            return (OfferRejectionCode.OUT_OF_STOCK.value,)
        if candidate.offer.stock_status is StockStatus.UNKNOWN:
            return (OfferRejectionCode.STOCK_UNKNOWN.value,)
        return ()
    if gate is OfferGateId.COST_COMPLETENESS:
        if (
            type(candidate.pricing) is PricingFailure
            and candidate.pricing.code is PricingFailureCode.UNKNOWN_COST
        ):
            return (OfferRejectionCode.COST_UNKNOWN.value,)
        return ()
    if gate is OfferGateId.EXCHANGE_RATE:
        if type(candidate.pricing) is PricingFailure:
            if candidate.pricing.code is PricingFailureCode.MISSING_EXCHANGE_RATE:
                return (OfferRejectionCode.EXCHANGE_RATE_MISSING.value,)
            if candidate.pricing.code is PricingFailureCode.DECIMAL_ARITHMETIC:
                return (OfferRejectionCode.PRICING_ARITHMETIC.value,)
        return ()
    if gate is OfferGateId.BUDGET:
        if (
            budget is not None
            and type(candidate.pricing) is LandedCost
            and candidate.pricing.within_budget is not True
        ):
            return (OfferRejectionCode.OVER_BUDGET.value,)
        return ()
    raise TypeError("unsupported offer gate")


def _has_required_offer_evidence(
    offer: Offer,
    evidence_index: dict[str, EvidenceRef],
) -> bool:
    paths = {binding.field_path for binding in offer.field_evidence}
    if not _CORE_OFFER_EVIDENCE_PATHS.issubset(paths):
        return False
    return all(
        _offer_evidence_id_closes(
            offer,
            binding.evidence_id,
            binding.field_path,
            evidence_index,
        )
        for binding in offer.field_evidence
    )


def _offer_evidence_id_closes(
    offer: Offer,
    evidence_id: str,
    expected_path: str,
    evidence_index: dict[str, EvidenceRef],
) -> bool:
    evidence = evidence_index.get(evidence_id)
    if type(evidence) is not EvidenceRef:
        return False
    return (
        evidence.snapshot_version == offer.snapshot_version
        and evidence.entity_type is EvidenceEntityType.OFFER
        and evidence.product_id == offer.product_id
        and evidence.offer_id == offer.offer_id
        and evidence.currency is None
        and evidence.field_path == expected_path
        and evidence.provider_id == offer.provider_id
        and evidence.source_uri == offer.source_uri
        and type(evidence.captured_at) is datetime
        and evidence.captured_at.tzinfo is not None
        and evidence.captured_at.utcoffset() == timedelta(0)
    )


def _require_canonical_products(products: object) -> tuple[CanonicalProduct, ...]:
    product_tuple = _require_tuple(products, "canonical products")
    if any(type(product) is not CanonicalProduct for product in product_tuple):
        raise TypeError("canonical products must contain exact CanonicalProduct values")
    checked = cast(tuple[CanonicalProduct, ...], product_tuple)
    try:
        for product in checked:
            CanonicalProduct.__post_init__(product)
            for source in product.sources:
                ProductSource.__post_init__(source)
            for attribute in product.attributes:
                CanonicalAttribute.__post_init__(attribute)
            for binding in product.field_evidence:
                FieldEvidence.__post_init__(binding)
        _candidate_graph_signature(checked)
    except (AttributeError, TypeError, ValueError) as error:
        raise TypeError("canonical product invariants must hold at the public boundary") from error
    product_ids = tuple(product.product_id for product in checked)
    if len(product_ids) != len(set(product_ids)):
        raise ValueError("canonical product IDs must be unique")
    if len({product.snapshot_version for product in checked}) > 1:
        raise ValueError("canonical products must belong to one snapshot")
    return checked


def _require_business_product_context(context: object) -> EligibilityContext:
    """Revalidate product-relevant constraints at the concrete public boundary."""

    if type(context) is not EligibilityContext:
        raise TypeError("context must be an EligibilityContext")
    if type(context.required) is not tuple:
        raise TypeError("eligibility required constraints must be a tuple")
    if any(type(constraint) not in _REQUIRED_TYPES for constraint in context.required):
        raise TypeError("eligibility context contains an invalid required constraint")

    target_count = 0
    exclusion_tokens: list[str] = []
    try:
        for constraint in context.required:
            if type(constraint) is TargetCategory:
                target_count += 1
                if type(constraint.category) is not str:
                    raise TypeError("target category must be an exact string")
                if not constraint.category.strip():
                    raise ValueError("target category must be non-empty")
                if type(constraint.kind) is not str or constraint.kind != "target_category":
                    raise ValueError("target category discriminator must be target_category")
            elif type(constraint) is Exclusion:
                if type(constraint.value) is not str:
                    raise TypeError("exclusion value must be an exact string")
                if not constraint.value.strip():
                    raise ValueError("exclusion value must be non-empty")
                if type(constraint.kind) is not str or constraint.kind != "exclusion":
                    raise ValueError("exclusion discriminator must be exclusion")
                token = _canonical_exclusion_token(constraint.value)
                if token is None:
                    raise ValueError("exclusion value must be non-empty")
                exclusion_tokens.append(token)
    except AttributeError as error:
        raise TypeError("product constraint must contain initialized exact fields") from error

    if target_count > 1:
        raise ValueError("product gate context allows at most one target category")
    if len(exclusion_tokens) != len(set(exclusion_tokens)):
        raise ValueError("product gate context contains a duplicate exclusion")
    return context


def _prepare_product_evidence(
    evidence: object,
) -> tuple[dict[str, EvidenceRef], bytes]:
    evidence_tuple = _require_tuple(evidence, "product evidence")
    if any(type(item) is not EvidenceRef for item in evidence_tuple):
        raise TypeError("product evidence must contain exact EvidenceRef values")
    checked = cast(tuple[EvidenceRef, ...], evidence_tuple)
    index: dict[str, EvidenceRef] = {}
    try:
        for item in checked:
            if type(item.evidence_id) is not str or not item.evidence_id.strip():
                raise TypeError("product evidence IDs must be non-empty strings")
            if item.evidence_id in index:
                raise ValueError(f"duplicate evidence ID: {item.evidence_id}")
            index[item.evidence_id] = item
        signature = _structure_signature(checked)
    except (AttributeError, TypeError) as error:
        raise TypeError(
            "product evidence must contain initialized deeply immutable EvidenceRef values"
        ) from error
    return index, signature


def _business_product_reasons(
    gate: ProductGateId,
    product: CanonicalProduct,
    context: EligibilityContext,
    evidence_index: dict[str, EvidenceRef],
) -> tuple[str, ...]:
    if gate is ProductGateId.CATEGORY:
        targets = tuple(
            constraint for constraint in context.required if type(constraint) is TargetCategory
        )
        product_category = _canonical_exclusion_token(product.category)
        if any(
            product_category is None
            or product_category != _canonical_exclusion_token(target.category)
            for target in targets
        ):
            return (ProductRejectionCode.CATEGORY_MISMATCH.value,)
        return ()
    if gate is ProductGateId.ENTITY_KIND:
        if type(product.entity_kind) is not EntityKind or (
            product.entity_kind is not EntityKind.PRIMARY_PRODUCT
        ):
            return (ProductRejectionCode.NOT_PRIMARY_PRODUCT.value,)
        return ()
    if gate is ProductGateId.EXCLUSION:
        return _explicit_exclusion_reasons(product, context, evidence_index)
    if gate is ProductGateId.REQUIRED_EVIDENCE:
        if not _has_required_product_evidence(product, evidence_index):
            return (ProductRejectionCode.REQUIRED_EVIDENCE_INVALID.value,)
        return ()
    raise TypeError("unsupported product gate")


def _explicit_exclusion_reasons(
    product: CanonicalProduct,
    context: EligibilityContext,
    evidence_index: dict[str, EvidenceRef],
) -> tuple[str, ...]:
    surfaces: set[str] = set()
    if _core_product_field_is_closed(product, "product.category", evidence_index):
        category = _canonical_exclusion_token(product.category)
        if category is not None:
            surfaces.add(category)
    if type(product.entity_kind) is EntityKind and _core_product_field_is_closed(
        product,
        "product.entity_kind",
        evidence_index,
    ):
        entity_kind = _canonical_exclusion_token(product.entity_kind.value)
        if entity_kind is not None:
            surfaces.add(entity_kind)
    if type(product.attributes) is tuple:
        for attribute in product.attributes:
            if type(attribute) is not CanonicalAttribute or not _attribute_evidence_is_closed(
                product,
                attribute,
                evidence_index,
            ):
                continue
            attribute_value = _canonical_exclusion_token(attribute.value)
            if attribute_value is not None:
                surfaces.add(attribute_value)
            if attribute_value in {"1", "present", "true", "yes", "有", "是"}:
                attribute_name = _canonical_exclusion_token(attribute.name)
                if attribute_name is not None:
                    surfaces.add(attribute_name)

    matched = {
        token
        for constraint in context.required
        if type(constraint) is Exclusion
        if (token := _canonical_exclusion_token(constraint.value)) is not None
        if token in surfaces
    }
    prefix = ProductRejectionCode.EXPLICIT_EXCLUSION.value
    return tuple(f"{prefix}:{token}" for token in sorted(matched))


def _canonical_exclusion_token(value: object) -> str | None:
    if type(value) is not str:
        return None
    folded = normalize("NFKC", value).casefold().strip()
    for separator in ("-", "_", "/"):
        folded = folded.replace(separator, " ")
    token = "_".join(folded.split())
    if not token:
        return None
    return _EXCLUSION_ALIASES.get(token, token)


def _has_required_product_evidence(
    product: CanonicalProduct,
    evidence_index: dict[str, EvidenceRef],
) -> bool:
    sources = _product_source_pairs(product)
    if sources is None or not _valid_product_evidence_identity(product):
        return False
    if type(product.field_evidence) is not tuple:
        return False
    bindings = product.field_evidence
    if any(type(binding) is not FieldEvidence for binding in bindings):
        return False
    binding_pairs: list[tuple[str, str]] = []
    for binding in bindings:
        if (
            type(binding.field_path) is not str
            or not binding.field_path.strip()
            or type(binding.evidence_id) is not str
            or not binding.evidence_id.strip()
        ):
            return False
        binding_pairs.append((binding.field_path, binding.evidence_id))
        if not _evidence_id_closes(
            product,
            binding.evidence_id,
            binding.field_path,
            sources,
            evidence_index,
        ):
            return False
    if len(binding_pairs) != len(set(binding_pairs)):
        return False
    if not _CORE_PRODUCT_EVIDENCE_PATHS.issubset({binding.field_path for binding in bindings}):
        return False
    if type(product.attributes) is not tuple:
        return False
    if any(type(attribute) is not CanonicalAttribute for attribute in product.attributes):
        return False
    attribute_names = tuple(attribute.name for attribute in product.attributes)
    if any(type(name) is not str or not name.strip() for name in attribute_names) or len(
        attribute_names
    ) != len(set(attribute_names)):
        return False
    return all(
        _attribute_evidence_is_closed(product, attribute, evidence_index)
        for attribute in product.attributes
    )


def _core_product_field_is_closed(
    product: CanonicalProduct,
    expected_path: str,
    evidence_index: dict[str, EvidenceRef],
) -> bool:
    sources = _product_source_pairs(product)
    if sources is None or type(product.field_evidence) is not tuple:
        return False
    bindings = tuple(
        binding
        for binding in product.field_evidence
        if type(binding) is FieldEvidence and binding.field_path == expected_path
    )
    return bool(bindings) and all(
        _evidence_id_closes(
            product,
            binding.evidence_id,
            expected_path,
            sources,
            evidence_index,
        )
        for binding in bindings
    )


def _valid_product_evidence_identity(product: CanonicalProduct) -> bool:
    return (
        type(product.snapshot_version) is str
        and bool(product.snapshot_version.strip())
        and type(product.product_id) is str
        and bool(product.product_id.strip())
        and type(product.title) is str
        and bool(product.title.strip())
        and type(product.category) is str
        and bool(product.category.strip())
        and type(product.entity_kind) is EntityKind
        and type(product.snapshot_ordinal) is int
        and not isinstance(product.snapshot_ordinal, bool)
        and product.snapshot_ordinal >= 0
    )


def _product_source_pairs(
    product: CanonicalProduct,
) -> frozenset[tuple[str, str]] | None:
    if type(product.sources) is not tuple or not product.sources:
        return None
    pairs: set[tuple[str, str]] = set()
    for source in product.sources:
        if (
            type(source) is not ProductSource
            or type(source.provider_id) is not str
            or not source.provider_id.strip()
            or type(source.source_uri) is not str
            or not source.source_uri.strip()
            or type(source.snapshot_ordinal) is not int
            or isinstance(source.snapshot_ordinal, bool)
            or source.snapshot_ordinal < 0
        ):
            return None
        pairs.add((source.provider_id, source.source_uri))
    return frozenset(pairs)


def _attribute_evidence_is_closed(
    product: CanonicalProduct,
    attribute: CanonicalAttribute,
    evidence_index: dict[str, EvidenceRef],
) -> bool:
    sources = _product_source_pairs(product)
    if (
        sources is None
        or type(attribute.name) is not str
        or not attribute.name.strip()
        or type(attribute.value) is not str
        or not attribute.value.strip()
        or type(attribute.evidence_ids) is not tuple
        or not attribute.evidence_ids
        or any(
            type(evidence_id) is not str or not evidence_id.strip()
            for evidence_id in attribute.evidence_ids
        )
        or len(attribute.evidence_ids) != len(set(attribute.evidence_ids))
    ):
        return False
    expected_path = f"product.attributes.{attribute.name}"
    return all(
        _evidence_id_closes(
            product,
            evidence_id,
            expected_path,
            sources,
            evidence_index,
        )
        for evidence_id in attribute.evidence_ids
    )


def _evidence_id_closes(
    product: CanonicalProduct,
    evidence_id: str,
    expected_path: str,
    sources: frozenset[tuple[str, str]],
    evidence_index: dict[str, EvidenceRef],
) -> bool:
    evidence = evidence_index.get(evidence_id)
    if type(evidence) is not EvidenceRef:
        return False
    try:
        return (
            type(evidence.evidence_id) is str
            and evidence.evidence_id == evidence_id
            and type(evidence.snapshot_version) is str
            and evidence.snapshot_version == product.snapshot_version
            and evidence.entity_type is EvidenceEntityType.PRODUCT
            and type(evidence.product_id) is str
            and evidence.product_id == product.product_id
            and evidence.offer_id is None
            and evidence.currency is None
            and type(evidence.field_path) is str
            and evidence.field_path == expected_path
            and type(evidence.provider_id) is str
            and type(evidence.source_uri) is str
            and (evidence.provider_id, evidence.source_uri) in sources
            and type(evidence.captured_at) is datetime
            and evidence.captured_at.tzinfo is not None
            and evidence.captured_at.utcoffset() == timedelta(0)
        )
    except (AttributeError, TypeError, ValueError):
        return False


def _run_trusted_fixed_gates[T, GateT: (ProductGateId, OfferGateId)](
    candidates: tuple[T, ...],
    context: EligibilityContext,
    gate_order: tuple[GateT, ...],
    predicate: Callable[[GateT, T, EligibilityContext], tuple[str, ...]],
) -> tuple[tuple[T, ...], tuple[GateResult[T], ...]]:
    """Run module-owned predicates after their public inputs were fully validated."""

    kept = candidates
    results: list[GateResult[T]] = []
    for gate in gate_order:
        before = kept
        next_kept: list[T] = []
        rejected: list[RejectedCandidate[T]] = []
        for candidate in before:
            reasons = predicate(gate, candidate, context)
            _require_reasons(reasons, allow_empty=True)
            if reasons:
                rejected.append(
                    RejectedCandidate(
                        candidate=candidate,
                        gate=gate,
                        reasons=reasons,
                    )
                )
            else:
                next_kept.append(candidate)
        kept = tuple(next_kept)
        results.append(
            GateResult(
                gate=gate,
                before=before,
                kept=kept,
                rejected=tuple(rejected),
            )
        )
    return kept, tuple(results)


def _run_fixed_gates[T, GateT: (ProductGateId, OfferGateId)](
    candidates: tuple[T, ...],
    context: EligibilityContext,
    gate_order: tuple[GateT, ...],
    predicate: Callable[[GateT, T, EligibilityContext], tuple[str, ...]],
) -> tuple[tuple[T, ...], tuple[GateResult[T], ...]]:
    _candidate_graph_signature(candidates)
    baseline = _structure_signature(context, candidates)
    kept = candidates
    results: list[GateResult[T]] = []
    for gate in gate_order:
        before = kept
        next_kept: list[T] = []
        rejected: list[RejectedCandidate[T]] = []
        for candidate in before:
            reasons = predicate(gate, candidate, context)
            _require_reasons(reasons, allow_empty=True)
            if reasons:
                rejected.append(
                    RejectedCandidate(
                        candidate=candidate,
                        gate=gate,
                        reasons=reasons,
                    )
                )
            else:
                next_kept.append(candidate)
        if not _has_structure_signature(baseline, context, candidates):
            raise TypeError("gate predicate must not mutate candidate or context state")
        kept = tuple(next_kept)
        results.append(
            GateResult(
                gate=gate,
                before=before,
                kept=kept,
                rejected=tuple(rejected),
            )
        )
    if not _has_structure_signature(baseline, context, candidates):
        raise TypeError("gate predicate must not mutate candidate or context state")
    return kept, tuple(results)


class _HashSink(Protocol):
    def update(self, value: bytes, /) -> object: ...


def _candidate_graph_signature(candidates: object) -> bytes:
    candidate_tuple = _require_tuple(candidates, "gate candidates")
    for candidate in candidate_tuple:
        _require_frozen_slots_dataclass(candidate, candidate_root=True)
    return _structure_signature(candidate_tuple)


def _structure_signature(*values: object) -> bytes:
    hasher = sha256()
    active: set[int] = set()
    visited: set[int] = set()
    _write_signature_bytes(hasher, b"glodex-eligibility-structure-v1")
    for value in values:
        _update_structure_signature(hasher, value, active, visited)
    return hasher.digest()


def _has_structure_signature(expected: bytes, *values: object) -> bool:
    try:
        actual = _structure_signature(*values)
    except (AttributeError, TypeError, ValueError):
        return False
    return compare_digest(expected, actual)


def _update_structure_signature(
    hasher: _HashSink,
    value: object,
    active: set[int],
    visited: set[int],
) -> None:
    if value is None:
        _write_signature_bytes(hasher, b"none")
        return
    if isinstance(value, Enum):
        _write_signature_bytes(hasher, b"enum")
        _write_type_identity(hasher, type(value))
        _write_signature_bytes(hasher, value.name.encode("utf-8"))
        _update_structure_signature(hasher, value.value, active, visited)
        return
    if type(value) is bool:
        _write_signature_bytes(hasher, b"bool:1" if value else b"bool:0")
        return
    if type(value) is int:
        _write_signature_bytes(hasher, b"int")
        _write_signature_bytes(hasher, str(value).encode("ascii"))
        return
    if type(value) is str:
        _write_signature_bytes(hasher, b"str")
        _write_signature_bytes(hasher, value.encode("utf-8"))
        return
    if type(value) is bytes:
        _write_signature_bytes(hasher, b"bytes")
        _write_signature_bytes(hasher, value)
        return
    if type(value) is Decimal:
        _write_signature_bytes(hasher, b"decimal")
        _write_signature_bytes(hasher, str(value).encode("ascii"))
        return
    if type(value) is datetime:
        _write_signature_bytes(hasher, b"datetime")
        _write_signature_bytes(hasher, value.isoformat().encode("ascii"))
        return
    if type(value) is date:
        _write_signature_bytes(hasher, b"date")
        _write_signature_bytes(hasher, value.isoformat().encode("ascii"))
        return
    if type(value) is time:
        _write_signature_bytes(hasher, b"time")
        _write_signature_bytes(hasher, value.isoformat().encode("ascii"))
        return
    if type(value) is timedelta:
        _write_signature_bytes(hasher, b"timedelta")
        _write_signature_bytes(
            hasher,
            f"{value.days}:{value.seconds}:{value.microseconds}".encode("ascii"),
        )
        return
    if type(value) is tuple:
        if _write_repeated_node(hasher, value, active, visited):
            return
        _enter_immutable_node(value, active)
        try:
            _write_signature_bytes(hasher, b"tuple")
            _write_signature_bytes(hasher, str(id(value)).encode("ascii"))
            _write_signature_bytes(hasher, str(len(value)).encode("ascii"))
            for item in value:
                _update_structure_signature(hasher, item, active, visited)
        finally:
            active.remove(id(value))
        return
    if is_dataclass(value) and not isinstance(value, type):
        _require_frozen_slots_dataclass(value, candidate_root=False)
        if _write_repeated_node(hasher, value, active, visited):
            return
        _enter_immutable_node(value, active)
        try:
            _write_signature_bytes(hasher, b"dataclass")
            _write_type_identity(hasher, type(value))
            _write_signature_bytes(hasher, str(id(value)).encode("ascii"))
            dataclass_fields = fields(value)
            _write_signature_bytes(hasher, str(len(dataclass_fields)).encode("ascii"))
            for dataclass_field in dataclass_fields:
                _write_signature_bytes(hasher, dataclass_field.name.encode("utf-8"))
                _update_structure_signature(
                    hasher,
                    getattr(value, dataclass_field.name),
                    active,
                    visited,
                )
        finally:
            active.remove(id(value))
        return
    raise TypeError("candidate graph must be deeply immutable")


def _require_frozen_slots_dataclass(value: object, *, candidate_root: bool) -> None:
    value_type = type(value)
    parameters = getattr(value_type, "__dataclass_params__", None)
    if (
        not is_dataclass(value)
        or isinstance(value, type)
        or parameters is None
        or not parameters.frozen
        or "__slots__" not in value_type.__dict__
    ):
        description = "candidate" if candidate_root else "candidate graph"
        raise TypeError(f"{description} must be a frozen slots dataclass and deeply immutable")


def _enter_immutable_node(value: object, active: set[int]) -> None:
    identity = id(value)
    if identity in active:
        raise TypeError("candidate graph must be acyclic and deeply immutable")
    active.add(identity)


def _write_repeated_node(
    hasher: _HashSink,
    value: object,
    active: set[int],
    visited: set[int],
) -> bool:
    """Write one identity reference for repeated immutable nodes in this call."""

    identity = id(value)
    if identity in active:
        raise TypeError("candidate graph must be acyclic and deeply immutable")
    if identity in visited:
        _write_signature_bytes(hasher, b"immutable-reference")
        _write_signature_bytes(hasher, str(identity).encode("ascii"))
        return True
    visited.add(identity)
    return False


def _write_type_identity(hasher: _HashSink, value_type: type[object]) -> None:
    _write_signature_bytes(hasher, value_type.__module__.encode("utf-8"))
    _write_signature_bytes(hasher, value_type.__qualname__.encode("utf-8"))


def _write_signature_bytes(hasher: _HashSink, value: bytes) -> None:
    hasher.update(len(value).to_bytes(8, "big"))
    hasher.update(value)


def _require_gate(gate: object) -> None:
    if type(gate) not in (ProductGateId, OfferGateId):
        raise TypeError("gate must be a ProductGateId or OfferGateId")


def _require_reasons(reasons: object, *, allow_empty: bool) -> None:
    if type(reasons) is not tuple:
        raise TypeError("gate predicate reasons must be a tuple")
    if not allow_empty and not reasons:
        raise ValueError("a rejected candidate requires at least one reason")
    if any(type(reason) is not str or not reason.strip() for reason in reasons):
        raise ValueError("gate predicate reasons must be non-empty strings")
    if len(reasons) != len(set(reasons)):
        raise ValueError("gate predicate reasons must be unique")


def _require_tuple(value: object, name: str) -> tuple[object, ...]:
    if type(value) is not tuple:
        raise TypeError(f"{name} must be a tuple")
    return value


def _is_identity_subsequence[T](values: tuple[T, ...], source: tuple[T, ...]) -> bool:
    source_iterator = iter(source)
    return all(any(value is candidate for candidate in source_iterator) for value in values)


__all__ = [
    "OFFER_GATE_ORDER",
    "PRODUCT_GATE_ORDER",
    "AssemblyGateId",
    "EligibilityContext",
    "EligibilityOutput",
    "EligibleCandidates",
    "EligibleOffer",
    "EligibleProduct",
    "FilterReasonCount",
    "FilterScope",
    "FilterStage",
    "FilterSummary",
    "GateResult",
    "OfferGateId",
    "OfferGateOutput",
    "OfferPricingCandidate",
    "OfferRejectionCode",
    "ProductGateId",
    "ProductGateOutput",
    "ProductRejectionCode",
    "RejectedCandidate",
    "assemble_eligibility",
    "diagnose_offer_rejections",
    "diagnose_product_rejections",
    "run_offer_gates",
    "run_product_gates",
    "scorer_input",
]
