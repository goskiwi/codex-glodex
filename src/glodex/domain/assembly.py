# ruff: noqa: RUF001
"""Evidence-closed facts and deterministic copy for result assembly.

This module is the only authority that may mint :class:`VerifiedClaims`.
Every rendered sentence remains bound to the exact product, offer, provider,
source, snapshot, field, and FX inputs from which it was derived.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from hmac import compare_digest
from typing import cast
from unicodedata import normalize
from weakref import ReferenceType, ref

from glodex.domain.catalog import (
    CanonicalAttribute,
    CanonicalProduct,
    ExchangeRate,
    ExchangeRateTable,
    Offer,
    ProductSource,
    StockStatus,
)
from glodex.domain.eligibility import (
    EligibilityContext,
    EligibilityOutput,
    EligibleOffer,
    EligibleProduct,
    OfferPricingCandidate,
    assemble_eligibility,
    run_offer_gates,
    run_product_gates,
    scorer_input,
)
from glodex.domain.evidence import (
    EvidenceEntityType,
    EvidenceRef,
    VerifiedClaim,
    VerifiedClaimType,
)
from glodex.domain.intent import (
    BudgetMax,
    Exclusion,
    InterpretedRequest,
    PreferredCriterion,
    SourceSpan,
    StockRequired,
    TargetCategory,
    validate_interpreted_request,
)
from glodex.domain.pricing import (
    PRICING_ALGORITHM_VERSION,
    CostComponentTrace,
    ExchangeRateTrace,
    KnownCost,
    LandedCost,
    PricingTrace,
    canonical_exact_amount,
)
from glodex.domain.ranking import lexical_tokens

_COST_COMPONENT_NAMES = ("item_price", "shipping", "tax", "duty")
_REQUIRED_TYPES = (BudgetMax, TargetCategory, StockRequired, Exclusion)
_NON_AFFIRMATIVE_ATTRIBUTE_VALUES = frozenset(
    {
        "false",
        "n/a",
        "na",
        "no",
        "none",
        "not available",
        "not supported",
        "null",
        "unavailable",
        "unknown",
        "unset",
        "不可用",
        "不支持",
        "否",
        "未知",
        "未提供",
    }
)

type _Seal = Callable[[object, bytes], None]
type _SealVerifier = Callable[[object, bytes], bool]


def _make_identity_registry() -> tuple[_Seal, _SealVerifier]:
    """Create a weak identity registry whose minting authority stays in this module."""

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


_seal_verified_claims, _verify_verified_claims_seal = _make_identity_registry()
_seal_guarded_results, _verify_guarded_results_seal = _make_identity_registry()


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class VerifiedClaims:
    """A candidate-bound claim bundle minted only after complete closure checks."""

    candidate: EligibleProduct
    claims: tuple[VerifiedClaim, ...]
    evidence: tuple[EvidenceRef, ...]
    exchange_rates: ExchangeRateTable

    def __init__(self) -> None:
        raise TypeError("VerifiedClaims is constructed by internal evidence assembly")


@dataclass(frozen=True, slots=True)
class ReasonProjection:
    """Deterministic copy and provenance derived from the claims actually used."""

    reason: str
    unknowns: tuple[str, ...]
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_text(self.reason, "reason", maximum=32_768)
        if type(self.unknowns) is not tuple or any(
            type(unknown) is not str for unknown in self.unknowns
        ):
            raise TypeError("reason unknowns must be a tuple of strings")
        for unknown in self.unknowns:
            _require_text(unknown, "reason unknown", maximum=16_384)
        _require_unique(self.unknowns, "reason unknowns")
        if type(self.evidence_ids) is not tuple or any(
            type(evidence_id) is not str for evidence_id in self.evidence_ids
        ):
            raise TypeError("reason evidence_ids must be a tuple of strings")
        if not self.evidence_ids:
            raise ValueError("reason projection requires used evidence")
        for evidence_id in self.evidence_ids:
            _require_text(evidence_id, "reason evidence ID", maximum=128)
        _require_unique(self.evidence_ids, "reason evidence IDs")


class FinalInvariantViolation(ValueError):
    """Raised atomically when a final result batch cannot be proven safe."""

    code = "result.final-invariant-violation"


@dataclass(frozen=True, slots=True)
class ResultDraft:
    """One transport-independent result projection awaiting the final guard."""

    candidate: EligibleProduct
    verified_claims: VerifiedClaims
    projection: ReasonProjection
    matched_requirements: tuple[str, ...]
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.candidate) is not EligibleProduct:
            raise TypeError("result candidate must be an exact EligibleProduct")
        EligibleProduct.__post_init__(self.candidate)
        if type(self.verified_claims) is not VerifiedClaims:
            raise TypeError("result claims must be an exact VerifiedClaims bundle")
        checked_claims = require_verified_claims(self.verified_claims)
        if checked_claims.candidate is not self.candidate:
            raise ValueError("result claims must bind the exact result candidate")
        if type(self.projection) is not ReasonProjection:
            raise TypeError("result projection must be an exact ReasonProjection")
        ReasonProjection.__post_init__(self.projection)
        _require_string_tuple(
            self.matched_requirements,
            "matched requirements",
            allow_empty=True,
            maximum=128,
        )
        _require_string_tuple(
            self.evidence_ids,
            "result evidence IDs",
            allow_empty=False,
            maximum=128,
        )
        _require_unique(self.evidence_ids, "result evidence IDs")
        if not set(self.projection.evidence_ids).issubset(self.evidence_ids):
            raise ValueError("reason evidence must be a subset of result evidence")


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class GuardedResults:
    """A complete result batch minted only by the final invariant guard."""

    items: tuple[ResultDraft, ...]

    def __init__(self) -> None:
        raise TypeError("GuardedResults is constructed by the final invariant guard")


def build_verified_claims(
    candidate: EligibleProduct,
    interpreted: InterpretedRequest,
    evidence: tuple[EvidenceRef, ...],
    exchange_rates: ExchangeRateTable,
) -> VerifiedClaims:
    """Build all raw and derived facts for one eligible product or fail closed."""

    checked_candidate = _require_candidate(candidate)
    budget = _require_interpreted_request(interpreted, checked_candidate)
    evidence_index = _prepare_evidence(
        evidence,
        snapshot_version=checked_candidate.product.snapshot_version,
    )
    checked_rates = _require_exchange_rates(
        exchange_rates,
        snapshot_version=checked_candidate.product.snapshot_version,
    )

    claims: list[VerifiedClaim] = []
    claims.append(
        _build_category_claim(
            checked_candidate.product,
            evidence_index,
        )
    )
    claims.extend(
        _build_attribute_claim(
            checked_candidate.product,
            attribute,
            evidence_index,
        )
        for attribute in checked_candidate.product.attributes
    )
    for eligible in checked_candidate.eligible_offers:
        claims.extend(
            _build_offer_claims(
                eligible,
                checked_candidate.product,
                budget,
                evidence_index,
                checked_rates,
            )
        )
    return _new_verified_claims(
        checked_candidate,
        tuple(claims),
        evidence,
        checked_rates,
    )


def render_reason(
    verified_claims: VerifiedClaims,
    interpreted: InterpretedRequest,
) -> ReasonProjection:
    """Render fixed copy from a sealed bundle and validated preference source spans."""

    bundle = require_verified_claims(verified_claims)
    budget = _require_interpreted_request(interpreted, bundle.candidate)
    selected = bundle.candidate.selected_offer.offer
    selected_identity = (selected.provider_id, selected.offer_id)

    inventory_claims = tuple(
        claim
        for claim in bundle.claims
        if claim.claim_type is VerifiedClaimType.INVENTORY
        and (claim.provider_id, claim.offer_id) == selected_identity
    )
    if len(inventory_claims) != 1:
        raise ValueError("selected offer requires exactly one inventory claim")
    inventory_claim = inventory_claims[0]

    budget_claims = tuple(
        claim
        for claim in bundle.claims
        if claim.claim_type is VerifiedClaimType.WITHIN_BUDGET
        and (claim.provider_id, claim.offer_id) == selected_identity
    )
    budget_claim: VerifiedClaim | None = None
    if budget is not None:
        if len(budget_claims) != 1:
            raise ValueError("budget request requires exactly one selected budget claim")
        budget_claim = budget_claims[0]
    elif budget_claims:
        raise ValueError("selected budget claim is not bound to the interpreted request")

    evidence_index = _prepare_evidence(
        bundle.evidence,
        snapshot_version=bundle.candidate.product.snapshot_version,
    )
    expected_offer_claims = _build_offer_claims(
        bundle.candidate.selected_offer,
        bundle.candidate.product,
        budget,
        evidence_index,
        bundle.exchange_rates,
    )
    expected_inventory = next(
        claim for claim in expected_offer_claims if claim.claim_type is VerifiedClaimType.INVENTORY
    )
    if inventory_claim != expected_inventory:
        raise ValueError("selected inventory claim does not match its closed offer facts")
    if budget_claim is not None:
        expected_budget = next(
            claim
            for claim in expected_offer_claims
            if claim.claim_type is VerifiedClaimType.WITHIN_BUDGET
        )
        if budget_claim != expected_budget:
            raise ValueError("selected budget claim does not match its closed pricing facts")

    attribute_claims = _closed_attribute_claims(bundle, evidence_index)
    matched_attributes, unknowns = _match_preferred_claims(
        interpreted.preferred,
        attribute_claims,
    )
    used_claims = (
        *((budget_claim,) if budget_claim is not None else ()),
        inventory_claim,
        *matched_attributes,
    )
    reason_parts = (
        *((f"满足预算：到手价 {budget_claim.value}",) if budget_claim is not None else ()),
        f"有库存：{inventory_claim.value}",
        *(f"匹配偏好：{claim.value}" for claim in matched_attributes),
    )
    evidence_ids = tuple(
        dict.fromkeys(evidence_id for claim in used_claims for evidence_id in claim.evidence_ids)
    )
    return ReasonProjection(
        reason="；".join(reason_parts),
        unknowns=unknowns,
        evidence_ids=evidence_ids,
    )


def require_verified_claims(value: object) -> VerifiedClaims:
    """Return a sealed, untampered bundle for a later deterministic consumer."""

    if not _is_valid_verified_claims(value):
        raise TypeError("value must be a sealed, untampered VerifiedClaims bundle")
    return cast(VerifiedClaims, value)


def result_evidence_closure(
    ranked: tuple[EligibleProduct, ...],
    evidence: tuple[EvidenceRef, ...],
    exchange_rates: ExchangeRateTable,
) -> tuple[EvidenceRef, ...]:
    """Validate the full batch once and retain only final-result dependencies."""

    checked_ranked = _require_final_candidates(ranked, "ranked candidates")
    if not checked_ranked:
        raise ValueError("result evidence closure requires ranked candidates")
    snapshots = {candidate.product.snapshot_version for candidate in checked_ranked}
    if len(snapshots) != 1:
        raise ValueError("ranked candidates must belong to one snapshot")
    snapshot_version = next(iter(snapshots))
    checked_rates = _require_exchange_rates(
        exchange_rates,
        snapshot_version=snapshot_version,
    )

    required_ids = {
        *(
            binding.evidence_id
            for candidate in checked_ranked
            for binding in candidate.product.field_evidence
        ),
        *(
            evidence_id
            for candidate in checked_ranked
            for attribute in candidate.product.attributes
            for evidence_id in attribute.evidence_ids
        ),
        *(
            binding.evidence_id
            for candidate in checked_ranked
            for eligible in candidate.eligible_offers
            for binding in eligible.offer.field_evidence
        ),
        *(rate.evidence_id for rate in checked_rates.rates),
    }
    if not required_ids:
        raise ValueError("result evidence closure cannot be empty")

    if type(evidence) is not tuple:
        raise TypeError("evidence must be a tuple of exact EvidenceRef values")
    seen_ids: set[str] = set()
    closure: list[EvidenceRef] = []
    for item in evidence:
        if type(item) is not EvidenceRef:
            raise TypeError("evidence must contain exact EvidenceRef values")
        try:
            EvidenceRef.__post_init__(item)
        except AttributeError as error:
            raise TypeError("evidence record is missing required fields") from error
        if item.evidence_id in seen_ids:
            raise ValueError("duplicate evidence IDs are ambiguous")
        seen_ids.add(item.evidence_id)
        if item.snapshot_version != snapshot_version:
            raise ValueError("evidence snapshot does not match the final candidates")
        if item.evidence_id in required_ids:
            closure.append(item)

    missing_ids = required_ids - seen_ids
    if missing_ids:
        raise ValueError("result evidence closure is incomplete")
    return tuple(closure)


def assemble_result_drafts(
    ranked: tuple[EligibleProduct, ...],
    query: str,
    interpreted: InterpretedRequest,
    evidence: tuple[EvidenceRef, ...],
    exchange_rates: ExchangeRateTable,
    *,
    top_k: int,
) -> tuple[ResultDraft, ...]:
    """Build the requested ranked prefix without granting it final authority."""

    checked_ranked = _require_final_candidates(ranked, "ranked candidates")
    checked_interpreted = _require_validated_interpreted(query, interpreted)
    checked_top_k = _require_top_k(top_k)
    if not checked_ranked:
        raise ValueError("result assembly requires at least one ranked candidate")
    return tuple(
        _assemble_result_draft(
            candidate,
            checked_interpreted,
            evidence,
            exchange_rates,
        )
        for candidate in checked_ranked[:checked_top_k]
    )


def guard_result_drafts(
    drafts: object,
    *,
    ranked: tuple[EligibleProduct, ...],
    eligibility_output: EligibilityOutput[EligibleProduct],
    query: str,
    interpreted: InterpretedRequest,
    evidence: tuple[EvidenceRef, ...],
    exchange_rates: ExchangeRateTable,
    display_currency: str,
    snapshot_version: str,
    top_k: int,
) -> GuardedResults:
    """Re-run every final invariant and either seal the whole batch or fail."""

    try:
        return _guard_result_drafts(
            drafts,
            ranked=ranked,
            eligibility_output=eligibility_output,
            query=query,
            interpreted=interpreted,
            evidence=evidence,
            exchange_rates=exchange_rates,
            display_currency=display_currency,
            snapshot_version=snapshot_version,
            top_k=top_k,
        )
    except FinalInvariantViolation:
        raise
    except Exception as error:
        raise FinalInvariantViolation("final result invariants could not be proven") from error


def require_guarded_results(value: object) -> GuardedResults:
    """Return only an untampered result batch minted by the final guard."""

    if not _is_valid_guarded_results(value):
        raise TypeError("value must be sealed, untampered GuardedResults")
    return cast(GuardedResults, value)


def _assemble_result_draft(
    candidate: EligibleProduct,
    interpreted: InterpretedRequest,
    evidence: tuple[EvidenceRef, ...],
    exchange_rates: ExchangeRateTable,
) -> ResultDraft:
    verified_claims = build_verified_claims(
        candidate,
        interpreted,
        evidence,
        exchange_rates,
    )
    projection = render_reason(verified_claims, interpreted)
    evidence_ids = _result_evidence_ids(verified_claims)
    matched_requirements = tuple(
        dict.fromkeys(constraint.kind for constraint in interpreted.required)
    )
    return ResultDraft(
        candidate=candidate,
        verified_claims=verified_claims,
        projection=projection,
        matched_requirements=matched_requirements,
        evidence_ids=evidence_ids,
    )


def _guard_result_drafts(
    drafts: object,
    *,
    ranked: tuple[EligibleProduct, ...],
    eligibility_output: EligibilityOutput[EligibleProduct],
    query: str,
    interpreted: InterpretedRequest,
    evidence: tuple[EvidenceRef, ...],
    exchange_rates: ExchangeRateTable,
    display_currency: str,
    snapshot_version: str,
    top_k: int,
) -> GuardedResults:
    checked_top_k = _require_top_k(top_k)
    checked_interpreted = _require_validated_interpreted(query, interpreted)
    checked_snapshot = _require_text(snapshot_version, "snapshot version", maximum=128)
    checked_currency = _require_currency(display_currency)
    checked_rates = _require_exchange_rates(
        exchange_rates,
        snapshot_version=checked_snapshot,
    )

    if type(eligibility_output) is not EligibilityOutput:
        raise TypeError("final guard requires an exact EligibilityOutput")
    trusted = _require_final_candidates(
        scorer_input(eligibility_output).candidates,
        "eligible candidates",
    )
    if not trusted:
        raise ValueError("final guard requires at least one eligible candidate")
    expected_context = EligibilityContext.from_interpreted_request(checked_interpreted)
    if eligibility_output.context != expected_context:
        raise ValueError("final intent no longer matches the eligibility context")

    checked_ranked = _require_final_candidates(ranked, "ranked candidates")
    if len(checked_ranked) != len(trusted) or {id(item) for item in checked_ranked} != {
        id(item) for item in trusted
    }:
        raise ValueError("ranked candidates must be an identity permutation of eligibility")

    for candidate in trusted:
        if candidate.product.snapshot_version != checked_snapshot:
            raise ValueError("result candidate snapshot does not match the run")
        if any(
            eligible.offer.snapshot_version != checked_snapshot
            or eligible.landed_cost.display_currency != checked_currency
            for eligible in candidate.eligible_offers
        ):
            raise ValueError("result offer snapshot or display currency is inconsistent")

    _revalidate_final_eligibility(
        trusted,
        expected_context,
        evidence,
        display_currency=checked_currency,
    )

    if type(drafts) is not tuple or any(type(item) is not ResultDraft for item in drafts):
        raise TypeError("result drafts must be a tuple of exact ResultDraft values")
    checked_drafts = cast(tuple[ResultDraft, ...], drafts)
    expected_count = min(checked_top_k, len(checked_ranked))
    if len(checked_drafts) != expected_count:
        raise ValueError("result drafts must equal the exact requested ranked prefix")

    verified: list[ResultDraft] = []
    for index, draft in enumerate(checked_drafts):
        ResultDraft.__post_init__(draft)
        if draft.candidate is not checked_ranked[index]:
            raise ValueError("result draft candidate is outside the ranked prefix")
        expected = _assemble_result_draft(
            checked_ranked[index],
            checked_interpreted,
            evidence,
            checked_rates,
        )
        if draft != expected:
            raise ValueError("result draft does not equal its recomputed trusted projection")
        verified.append(draft)
    return _new_guarded_results(tuple(verified))


def _revalidate_final_eligibility(
    trusted: tuple[EligibleProduct, ...],
    context: EligibilityContext,
    evidence: tuple[EvidenceRef, ...],
    *,
    display_currency: str,
) -> None:
    products = tuple(candidate.product for candidate in trusted)
    product_output = run_product_gates(products, context, evidence)
    pricing = tuple(
        OfferPricingCandidate(
            offer=eligible.offer,
            pricing=eligible.landed_cost,
        )
        for candidate in trusted
        for eligible in candidate.eligible_offers
    )
    offer_output = run_offer_gates(
        product_output,
        tuple(item.offer for item in pricing),
        pricing,
        evidence,
        display_currency=display_currency,
    )
    reassembled = assemble_eligibility(product_output, offer_output)
    if scorer_input(reassembled).candidates != trusted:
        raise ValueError("current hard gates no longer reproduce the eligible candidates")


def _result_evidence_ids(bundle: VerifiedClaims) -> tuple[str, ...]:
    checked = require_verified_claims(bundle)
    available = {item.evidence_id for item in checked.evidence}
    evidence_ids = tuple(
        dict.fromkeys(
            (
                *(binding.evidence_id for binding in checked.candidate.product.field_evidence),
                *(
                    evidence_id
                    for attribute in checked.candidate.product.attributes
                    for evidence_id in attribute.evidence_ids
                ),
                *(evidence_id for claim in checked.claims for evidence_id in claim.evidence_ids),
            )
        )
    )
    if not evidence_ids or not set(evidence_ids).issubset(available):
        raise ValueError("result evidence IDs must resolve in the verified evidence bundle")
    return evidence_ids


def _require_validated_interpreted(
    query: object,
    interpreted: object,
) -> InterpretedRequest:
    validation = validate_interpreted_request(cast(str, query), interpreted)
    if validation.interpreted_request is None or validation.issues:
        raise ValueError("final interpreted request must pass source-span validation")
    return validation.interpreted_request


def _require_top_k(top_k: object) -> int:
    if type(top_k) is not int or not 1 <= top_k <= 3:
        raise ValueError("top_k must be an exact integer from one through three")
    return top_k


def _require_final_candidates(
    candidates: object,
    name: str,
) -> tuple[EligibleProduct, ...]:
    if type(candidates) is not tuple or any(
        type(candidate) is not EligibleProduct for candidate in candidates
    ):
        raise TypeError(f"{name} must be a tuple of exact EligibleProduct values")
    checked = cast(tuple[EligibleProduct, ...], candidates)
    for candidate in checked:
        EligibleProduct.__post_init__(candidate)
    product_ids = tuple(candidate.product.product_id for candidate in checked)
    if len(product_ids) != len(set(product_ids)):
        raise ValueError(f"{name} must contain unique product IDs")
    return checked


def _closed_attribute_claims(
    bundle: VerifiedClaims,
    evidence_index: dict[str, EvidenceRef],
) -> tuple[tuple[VerifiedClaim, CanonicalAttribute], ...]:
    expected: dict[VerifiedClaim, CanonicalAttribute] = {}
    for attribute in bundle.candidate.product.attributes:
        claim = _build_attribute_claim(
            bundle.candidate.product,
            attribute,
            evidence_index,
        )
        if claim in expected:
            raise ValueError("candidate attributes produce ambiguous verified claims")
        expected[claim] = attribute

    closed: list[tuple[VerifiedClaim, CanonicalAttribute]] = []
    seen: set[VerifiedClaim] = set()
    for claim in bundle.claims:
        if claim.claim_type is not VerifiedClaimType.ATTRIBUTE:
            continue
        matched_attribute = expected.get(claim)
        if matched_attribute is None:
            raise ValueError("attribute claim does not match a closed candidate attribute")
        if claim in seen:
            raise ValueError("attribute claims must be unique")
        seen.add(claim)
        closed.append((claim, matched_attribute))
    return tuple(sorted(closed, key=lambda item: _reason_claim_key(item[0])))


def _match_preferred_claims(
    preferred: tuple[PreferredCriterion, ...],
    attribute_claims: tuple[tuple[VerifiedClaim, CanonicalAttribute], ...],
) -> tuple[tuple[VerifiedClaim, ...], tuple[str, ...]]:
    matched: dict[tuple[object, ...], VerifiedClaim] = {}
    unknowns: list[str] = []
    for criterion in preferred:
        text = _require_preferred_source_text(criterion)
        preference_tokens = lexical_tokens(text)
        criterion_matches = tuple(
            claim
            for claim, attribute in attribute_claims
            if _is_affirmative_attribute(attribute)
            if preference_tokens and preference_tokens.issubset(lexical_tokens(claim.value))
        )
        if not criterion_matches:
            unknown = f"未证实偏好：{text}"
            if unknown not in unknowns:
                unknowns.append(unknown)
            continue
        for claim in criterion_matches:
            matched.setdefault(_reason_claim_key(claim), claim)
    return (
        tuple(matched[key] for key in sorted(matched)),
        tuple(unknowns),
    )


def _is_affirmative_attribute(attribute: CanonicalAttribute) -> bool:
    """Reject explicit negative/unknown sentinels before claiming a preference match."""

    CanonicalAttribute.__post_init__(attribute)
    normalized_value = normalize("NFKC", attribute.value)
    folded = normalized_value.strip().casefold().replace("_", " ").replace("-", " ")
    folded = " ".join(folded.split())
    return folded not in _NON_AFFIRMATIVE_ATTRIBUTE_VALUES


def _require_preferred_source_text(criterion: PreferredCriterion) -> str:
    if type(criterion) is not PreferredCriterion:
        raise TypeError("preferred criteria must be exact PreferredCriterion values")
    if criterion.kind != "preferred":
        raise ValueError("preferred criterion discriminator is invalid")
    _require_text(criterion.value, "preferred value", maximum=16_384)
    if type(criterion.source_span) is not SourceSpan:
        raise TypeError("preferred source_span must be an exact SourceSpan")
    span = criterion.source_span
    text = _require_text(span.text, "preferred source text", maximum=2_000)
    if (
        type(span.start) is not int
        or isinstance(span.start, bool)
        or type(span.end) is not int
        or isinstance(span.end, bool)
        or span.start < 0
        or span.end <= span.start
        or span.end - span.start != len(text)
    ):
        raise ValueError("preferred source span is invalid")
    return text


def _reason_claim_key(claim: VerifiedClaim) -> tuple[object, ...]:
    return (
        claim.value,
        claim.evidence_ids,
        claim.product_id,
        claim.provider_id or "",
        claim.offer_id or "",
    )


def _require_candidate(candidate: object) -> EligibleProduct:
    if type(candidate) is not EligibleProduct:
        raise TypeError("candidate must be an exact EligibleProduct")
    checked = candidate
    try:
        EligibleProduct.__post_init__(checked)
    except AttributeError as error:
        raise TypeError("eligible product is missing required fields") from error
    return checked


def _require_interpreted_request(
    interpreted: object,
    candidate: EligibleProduct,
) -> BudgetMax | None:
    if type(interpreted) is not InterpretedRequest:
        raise TypeError("interpreted must be an exact InterpretedRequest")
    checked = interpreted
    if type(checked.required) is not tuple or any(
        type(constraint) not in _REQUIRED_TYPES for constraint in checked.required
    ):
        raise TypeError("interpreted required constraints must be an exact tuple")
    if type(checked.preferred) is not tuple or any(
        type(criterion) is not PreferredCriterion for criterion in checked.preferred
    ):
        raise TypeError("interpreted preferred criteria must be an exact tuple")
    _require_text(checked.parser_version, "parser version", maximum=128)

    targets = tuple(
        constraint for constraint in checked.required if type(constraint) is TargetCategory
    )
    if len(targets) > 1:
        raise ValueError("interpreted request cannot contain multiple target categories")
    for target in targets:
        _require_text(target.category, "target category", maximum=128)
        if target.kind != "target_category":
            raise ValueError("target category discriminator is invalid")
        if target.category != candidate.product.category:
            raise ValueError("candidate category does not match the interpreted request")

    budgets = tuple(constraint for constraint in checked.required if type(constraint) is BudgetMax)
    if len(budgets) > 1:
        raise ValueError("interpreted request cannot contain multiple budgets")
    if not budgets:
        return None
    budget = budgets[0]
    if type(budget.amount) is not Decimal or not budget.amount.is_finite() or budget.amount < 0:
        raise ValueError("budget amount must be a finite non-negative Decimal")
    if budget.currency is not None:
        _require_currency(budget.currency)
    if budget.kind != "budget_max":
        raise ValueError("budget discriminator is invalid")
    return budget


def _prepare_evidence(
    evidence: object,
    *,
    snapshot_version: str,
) -> dict[str, EvidenceRef]:
    if type(evidence) is not tuple or any(type(item) is not EvidenceRef for item in evidence):
        raise TypeError("evidence must be a tuple of exact EvidenceRef values")
    checked = cast(tuple[EvidenceRef, ...], evidence)
    evidence_ids = tuple(item.evidence_id for item in checked)
    if len(evidence_ids) != len(set(evidence_ids)):
        raise ValueError("duplicate evidence IDs are ambiguous")
    index: dict[str, EvidenceRef] = {}
    for item in checked:
        try:
            EvidenceRef.__post_init__(item)
        except AttributeError as error:
            raise TypeError("evidence record is missing required fields") from error
        if item.snapshot_version != snapshot_version:
            kind = (
                "exchange-rate evidence"
                if item.entity_type is EvidenceEntityType.EXCHANGE_RATE
                else "evidence"
            )
            raise ValueError(f"{kind} snapshot does not match the candidate")
        index[item.evidence_id] = item
    return index


def _require_exchange_rates(
    exchange_rates: object,
    *,
    snapshot_version: str,
) -> ExchangeRateTable:
    if type(exchange_rates) is not ExchangeRateTable:
        raise TypeError("exchange_rates must be an exact ExchangeRateTable")
    checked = exchange_rates
    try:
        ExchangeRateTable.__post_init__(checked)
        for rate in checked.rates:
            if type(rate) is not ExchangeRate:
                raise TypeError("exchange-rate table must contain exact ExchangeRate values")
            ExchangeRate.__post_init__(rate)
    except AttributeError as error:
        raise TypeError("exchange-rate table is missing required fields") from error
    if checked.snapshot_version != snapshot_version:
        raise ValueError("exchange-rate table snapshot does not match the candidate")
    return checked


def _build_category_claim(
    product: CanonicalProduct,
    evidence_index: dict[str, EvidenceRef],
) -> VerifiedClaim:
    evidence_ids = tuple(
        binding.evidence_id
        for binding in product.field_evidence
        if binding.field_path == "product.category"
    )
    if not evidence_ids:
        raise ValueError("category claim requires product evidence")
    _require_unique(evidence_ids, "category evidence IDs")
    for evidence_id in evidence_ids:
        _require_product_evidence(
            product,
            evidence_id,
            "product.category",
            evidence_index,
        )
    return VerifiedClaim(
        claim_type=VerifiedClaimType.CATEGORY,
        product_id=product.product_id,
        value=product.category,
        evidence_ids=evidence_ids,
    )


def _build_attribute_claim(
    product: CanonicalProduct,
    attribute: CanonicalAttribute,
    evidence_index: dict[str, EvidenceRef],
) -> VerifiedClaim:
    expected_path = f"product.attributes.{attribute.name}"
    for evidence_id in attribute.evidence_ids:
        _require_product_evidence(
            product,
            evidence_id,
            expected_path,
            evidence_index,
        )
    return VerifiedClaim(
        claim_type=VerifiedClaimType.ATTRIBUTE,
        product_id=product.product_id,
        value=f"{attribute.name}={attribute.value}",
        evidence_ids=attribute.evidence_ids,
    )


def _require_product_evidence(
    product: CanonicalProduct,
    evidence_id: str,
    expected_path: str,
    evidence_index: dict[str, EvidenceRef],
) -> EvidenceRef:
    item = evidence_index.get(evidence_id)
    sources = tuple(source for source in product.sources if type(source) is ProductSource)
    owners = tuple(source for source in sources if evidence_id in source.evidence_ids)
    if len(owners) == 1:
        expected_source = owners[0]
    elif len(sources) == 1 and not sources[0].evidence_ids:
        expected_source = sources[0]
    else:
        raise ValueError(f"product evidence has no unique source binding for {expected_path}")
    if (
        type(item) is not EvidenceRef
        or item.evidence_id != evidence_id
        or item.snapshot_version != product.snapshot_version
        or item.entity_type is not EvidenceEntityType.PRODUCT
        or item.product_id != product.product_id
        or item.offer_id is not None
        or item.currency is not None
        or item.field_path != expected_path
        or item.provider_id != expected_source.provider_id
        or item.source_uri != expected_source.source_uri
        or not _is_utc(item.captured_at)
    ):
        raise ValueError(f"product evidence does not close {expected_path}")
    return item


def _build_offer_claims(
    eligible: EligibleOffer,
    product: CanonicalProduct,
    budget: BudgetMax | None,
    evidence_index: dict[str, EvidenceRef],
    exchange_rates: ExchangeRateTable,
) -> tuple[VerifiedClaim, ...]:
    offer = eligible.offer
    if offer.product_id != product.product_id:
        raise ValueError("eligible offer belongs to the wrong product")
    if offer.snapshot_version != product.snapshot_version:
        raise ValueError("eligible offer snapshot does not match its product")
    if offer.stock_status is not StockStatus.IN_STOCK:
        raise ValueError("unknown or unavailable stock cannot produce an inventory claim")

    inventory_id = _require_offer_field_evidence(
        offer,
        "offer.inventory",
        evidence_index,
    )
    market_id = _require_offer_field_evidence(
        offer,
        "offer.market",
        evidence_index,
    )
    inventory_claim = VerifiedClaim(
        claim_type=VerifiedClaimType.INVENTORY,
        product_id=product.product_id,
        provider_id=offer.provider_id,
        offer_id=offer.offer_id,
        value=f"{offer.provider_id}/{offer.market}",
        evidence_ids=(inventory_id, market_id),
    )

    derived_evidence = _require_landed_cost_closure(
        eligible,
        product,
        evidence_index,
        exchange_rates,
    )
    landed = eligible.landed_cost
    landed_claim = VerifiedClaim(
        claim_type=VerifiedClaimType.LANDED_COST,
        product_id=product.product_id,
        provider_id=offer.provider_id,
        offer_id=offer.offer_id,
        value=f"{landed.display_exact_json} {landed.display_currency}",
        evidence_ids=derived_evidence,
        algorithm_version=PRICING_ALGORITHM_VERSION,
    )

    if budget is None:
        if landed.budget_max is not None or landed.within_budget is not None:
            raise ValueError("landed cost carries a budget absent from the request")
        return (inventory_claim, landed_claim)

    effective_currency = budget.currency or landed.display_currency
    if (
        landed.budget_max != budget.amount
        or landed.budget_currency != effective_currency
        or landed.trace.budget_currency != effective_currency
        or landed.within_budget is not True
        or landed.budget_exact > budget.amount
    ):
        raise ValueError("landed cost does not satisfy the exact request budget")
    budget_claim = VerifiedClaim(
        claim_type=VerifiedClaimType.WITHIN_BUDGET,
        product_id=product.product_id,
        provider_id=offer.provider_id,
        offer_id=offer.offer_id,
        value=(
            f"{landed.budget_exact_json} {landed.budget_currency} ≤ "
            f"{canonical_exact_amount(budget.amount)} {effective_currency}"
        ),
        evidence_ids=derived_evidence,
        algorithm_version=PRICING_ALGORITHM_VERSION,
    )
    return (inventory_claim, landed_claim, budget_claim)


def _require_offer_field_evidence(
    offer: Offer,
    expected_path: str,
    evidence_index: dict[str, EvidenceRef],
) -> str:
    bindings = tuple(
        binding for binding in offer.field_evidence if binding.field_path == expected_path
    )
    if len(bindings) != 1:
        raise ValueError(f"offer evidence binding is missing or ambiguous for {expected_path}")
    evidence_id = bindings[0].evidence_id
    item = evidence_index.get(evidence_id)
    if (
        type(item) is not EvidenceRef
        or item.evidence_id != evidence_id
        or item.snapshot_version != offer.snapshot_version
        or item.entity_type is not EvidenceEntityType.OFFER
        or item.product_id != offer.product_id
        or item.offer_id != offer.offer_id
        or item.currency is not None
        or item.field_path != expected_path
        or item.provider_id != offer.provider_id
        or item.source_uri != offer.source_uri
        or not _is_utc(item.captured_at)
    ):
        raise ValueError(f"offer evidence does not close {expected_path}")
    return evidence_id


def _require_landed_cost_closure(
    eligible: EligibleOffer,
    product: CanonicalProduct,
    evidence_index: dict[str, EvidenceRef],
    exchange_rates: ExchangeRateTable,
) -> tuple[str, ...]:
    offer = eligible.offer
    landed = eligible.landed_cost
    if type(landed) is not LandedCost:
        raise TypeError("eligible offer must carry an exact LandedCost")
    try:
        LandedCost.__post_init__(landed)
    except AttributeError as error:
        raise TypeError("landed cost is missing required fields") from error
    trace = landed.trace
    _require_trace_context(trace, offer, product, exchange_rates)

    evidence_ids: list[str] = []
    offer_costs = offer.cost_components.items()
    if len(offer_costs) != len(trace.components):
        raise ValueError("pricing trace must close all four cost components")
    for (component_name, cost), component_trace in zip(
        offer_costs,
        trace.components,
        strict=True,
    ):
        if type(cost) is not KnownCost:
            raise ValueError("unknown cost cannot produce a landed-cost claim")
        if type(component_trace) is not CostComponentTrace:
            raise TypeError("pricing trace contains an invalid cost component")
        if (
            component_trace.component != component_name
            or component_trace.source_amount != cost.amount
            or component_trace.evidence_id != cost.evidence_id
        ):
            raise ValueError("pricing trace cost component does not match its offer")
        evidence_ids.append(
            _require_offer_field_evidence(
                offer,
                f"offer.cost_components.{component_name}",
                evidence_index,
            )
        )

    evidence_ids.append(
        _require_offer_field_evidence(
            offer,
            "offer.cost_components.currency",
            evidence_index,
        )
    )
    for rate_trace in (trace.source_rate, trace.display_rate, trace.budget_rate):
        rate_evidence_id = _require_rate_trace(
            rate_trace,
            trace,
            exchange_rates,
            evidence_index,
        )
        if rate_evidence_id not in evidence_ids:
            evidence_ids.append(rate_evidence_id)
    return tuple(evidence_ids)


def _require_trace_context(
    trace: PricingTrace,
    offer: Offer,
    product: CanonicalProduct,
    exchange_rates: ExchangeRateTable,
) -> None:
    if type(trace) is not PricingTrace:
        raise TypeError("landed cost trace must be an exact PricingTrace")
    try:
        PricingTrace.__post_init__(trace)
    except AttributeError as error:
        raise TypeError("pricing trace is missing required fields") from error
    if trace.algorithm_version != PRICING_ALGORITHM_VERSION:
        raise ValueError("pricing trace must identify pricing-v1")
    if (
        trace.snapshot_version != product.snapshot_version
        or trace.snapshot_version != offer.snapshot_version
        or trace.snapshot_version != exchange_rates.snapshot_version
    ):
        raise ValueError("pricing trace snapshot does not close the candidate")
    if trace.base_currency != exchange_rates.base_currency:
        raise ValueError("pricing trace base currency does not match the rate table")
    if trace.source_currency != offer.cost_components.currency:
        raise ValueError("pricing trace source currency does not match the offer")


def _require_rate_trace(
    trace_rate: ExchangeRateTrace,
    pricing_trace: PricingTrace,
    exchange_rates: ExchangeRateTable,
    evidence_index: dict[str, EvidenceRef],
) -> str:
    if type(trace_rate) is not ExchangeRateTrace:
        raise TypeError("pricing trace contains an invalid exchange-rate trace")
    matching_rates = tuple(
        rate for rate in exchange_rates.rates if rate.currency == trace_rate.currency
    )
    if len(matching_rates) != 1:
        raise ValueError("exchange-rate trace currency is absent or ambiguous")
    actual = matching_rates[0]
    if (
        actual.snapshot_version != pricing_trace.snapshot_version
        or actual.base_per_unit != trace_rate.base_per_unit
        or actual.minor_units != trace_rate.minor_units
        or actual.evidence_id != trace_rate.evidence_id
        or actual.snapshot_ordinal != trace_rate.snapshot_ordinal
    ):
        raise ValueError("exchange-rate trace does not match the actual rate table")

    item = evidence_index.get(trace_rate.evidence_id)
    if (
        type(item) is not EvidenceRef
        or item.evidence_id != trace_rate.evidence_id
        or item.snapshot_version != pricing_trace.snapshot_version
        or item.entity_type is not EvidenceEntityType.EXCHANGE_RATE
        or item.product_id is not None
        or item.offer_id is not None
        or item.currency != trace_rate.currency
        or item.field_path != "exchange_rate.base_per_unit"
        or not _is_utc(item.captured_at)
    ):
        raise ValueError(f"exchange-rate evidence does not close currency {trace_rate.currency}")
    return trace_rate.evidence_id


def _new_verified_claims(
    candidate: EligibleProduct,
    claims: tuple[VerifiedClaim, ...],
    evidence: tuple[EvidenceRef, ...],
    exchange_rates: ExchangeRateTable,
) -> VerifiedClaims:
    if not claims:
        raise ValueError("verified claim bundle cannot be empty")
    for claim in claims:
        if type(claim) is not VerifiedClaim:
            raise TypeError("claim bundle must contain exact VerifiedClaim values")
        VerifiedClaim.__post_init__(claim)
        if claim.product_id != candidate.product.product_id:
            raise ValueError("claim belongs to the wrong product")
    bundle = object.__new__(VerifiedClaims)
    object.__setattr__(bundle, "candidate", candidate)
    object.__setattr__(bundle, "claims", claims)
    object.__setattr__(bundle, "evidence", evidence)
    object.__setattr__(bundle, "exchange_rates", exchange_rates)
    _seal_verified_claims(bundle, _verified_claims_signature(bundle))
    return bundle


def _is_valid_verified_claims(value: object) -> bool:
    if type(value) is not VerifiedClaims:
        return False
    checked = value
    try:
        if type(checked.candidate) is not EligibleProduct:
            return False
        EligibleProduct.__post_init__(checked.candidate)
        if type(checked.claims) is not tuple or not checked.claims:
            return False
        for claim in checked.claims:
            if type(claim) is not VerifiedClaim:
                return False
            VerifiedClaim.__post_init__(claim)
            if claim.product_id != checked.candidate.product.product_id:
                return False
        _prepare_evidence(
            checked.evidence,
            snapshot_version=checked.candidate.product.snapshot_version,
        )
        _require_exchange_rates(
            checked.exchange_rates,
            snapshot_version=checked.candidate.product.snapshot_version,
        )
        return _verify_verified_claims_seal(
            checked,
            _verified_claims_signature(checked),
        )
    except (AttributeError, TypeError, ValueError):
        return False


def _verified_claims_signature(bundle: VerifiedClaims) -> bytes:
    candidate = bundle.candidate
    hasher = sha256()
    identity_parts = (
        id(candidate),
        id(candidate.product),
        id(candidate.eligible_offers),
        id(candidate.selected_offer),
        id(bundle.claims),
        id(bundle.evidence),
        id(bundle.exchange_rates),
        id(bundle.exchange_rates.rates),
        *(id(eligible) for eligible in candidate.eligible_offers),
        *(id(eligible.offer) for eligible in candidate.eligible_offers),
        *(id(eligible.landed_cost) for eligible in candidate.eligible_offers),
        *(id(claim) for claim in bundle.claims),
        *(id(claim.evidence_ids) for claim in bundle.claims),
        *(id(item) for item in bundle.evidence),
        *(id(rate) for rate in bundle.exchange_rates.rates),
    )
    hasher.update(":".join(str(identity) for identity in identity_parts).encode("ascii"))
    hasher.update(repr(candidate).encode("utf-8"))
    hasher.update(repr(bundle.claims).encode("utf-8"))
    hasher.update(repr(bundle.evidence).encode("utf-8"))
    hasher.update(repr(bundle.exchange_rates).encode("utf-8"))
    return hasher.digest()


def _new_guarded_results(items: tuple[ResultDraft, ...]) -> GuardedResults:
    if not items:
        raise ValueError("guarded results cannot be empty")
    for item in items:
        if type(item) is not ResultDraft:
            raise TypeError("guarded results must contain exact ResultDraft values")
        ResultDraft.__post_init__(item)
    product_ids = tuple(item.candidate.product.product_id for item in items)
    if len(product_ids) != len(set(product_ids)):
        raise ValueError("guarded results must contain unique products")
    guarded = object.__new__(GuardedResults)
    object.__setattr__(guarded, "items", items)
    _seal_guarded_results(guarded, _guarded_results_signature(guarded))
    return guarded


def _is_valid_guarded_results(value: object) -> bool:
    if type(value) is not GuardedResults:
        return False
    checked = value
    try:
        if type(checked.items) is not tuple or not checked.items:
            return False
        product_ids: list[str] = []
        for item in checked.items:
            if type(item) is not ResultDraft:
                return False
            ResultDraft.__post_init__(item)
            product_ids.append(item.candidate.product.product_id)
        if len(product_ids) != len(set(product_ids)):
            return False
        return _verify_guarded_results_seal(
            checked,
            _guarded_results_signature(checked),
        )
    except (AttributeError, TypeError, ValueError):
        return False


def _guarded_results_signature(guarded: GuardedResults) -> bytes:
    hasher = sha256()
    identity_parts = (
        id(guarded.items),
        *(id(item) for item in guarded.items),
        *(id(item.candidate) for item in guarded.items),
        *(id(item.verified_claims) for item in guarded.items),
        *(id(item.projection) for item in guarded.items),
        *(id(item.matched_requirements) for item in guarded.items),
        *(id(item.evidence_ids) for item in guarded.items),
    )
    hasher.update(":".join(str(identity) for identity in identity_parts).encode("ascii"))
    hasher.update(repr(guarded.items).encode("utf-8"))
    return hasher.digest()


def _require_text(value: object, name: str, *, maximum: int) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if len(value) > maximum:
        raise ValueError(f"{name} exceeds {maximum} code points")
    return value


def _require_string_tuple(
    values: object,
    name: str,
    *,
    allow_empty: bool,
    maximum: int,
) -> tuple[str, ...]:
    if type(values) is not tuple or any(type(value) is not str for value in values):
        raise TypeError(f"{name} must be a tuple of strings")
    checked = cast(tuple[str, ...], values)
    if not allow_empty and not checked:
        raise ValueError(f"{name} cannot be empty")
    for value in checked:
        _require_text(value, name, maximum=maximum)
    return checked


def _require_currency(value: object) -> str:
    if (
        type(value) is not str
        or len(value) != 3
        or not value.isascii()
        or not value.isalpha()
        or not value.isupper()
    ):
        raise ValueError("currency must be an uppercase three-letter code")
    return value


def _require_unique(values: tuple[str, ...], name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{name} must be unique")


def _is_utc(value: object) -> bool:
    return (
        type(value) is datetime and value.tzinfo is not None and value.utcoffset() == timedelta(0)
    )


__all__ = [
    "FinalInvariantViolation",
    "GuardedResults",
    "ReasonProjection",
    "ResultDraft",
    "VerifiedClaims",
    "assemble_result_drafts",
    "build_verified_claims",
    "guard_result_drafts",
    "render_reason",
    "require_guarded_results",
    "require_verified_claims",
    "result_evidence_closure",
]
