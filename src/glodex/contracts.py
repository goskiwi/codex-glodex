"""Stable, transport-neutral public contracts for the M0 application boundary."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

NonEmptyString = Annotated[str, StringConstraints(min_length=1, max_length=2_000)]
CanonicalExactAmount = Annotated[
    str,
    StringConstraints(pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?$"),
]
FixedPointAmount = Annotated[
    str,
    StringConstraints(pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$"),
]
Identifier = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    ),
]
SnapshotVersion = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    ),
]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
ConfigFingerprint = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class FrozenDTO(BaseModel):
    """Base for DTOs that reject coercion, extension, and field reassignment."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class SearchRequest(FrozenDTO):
    """A syntactically valid request at the pre-run boundary.

    Snapshot existence and currency support deliberately do not belong here. They
    require snapshot I/O and are checked only after a run has been established.
    """

    query: Annotated[str, StringConstraints(min_length=1, max_length=2_000)]
    locale: Literal["zh-CN"] = "zh-CN"
    display_currency: CurrencyCode = "USD"
    top_k: Annotated[int, Field(ge=1, le=3)] = 3
    snapshot_version: SnapshotVersion | None = None

    @field_validator("query", mode="before")
    @classmethod
    def trim_query(cls, value: object) -> object:
        """Trim the request once, before length validation and source-span work."""

        if isinstance(value, str):
            return value.strip()
        return value


class RequestFieldError(FrozenDTO):
    """One deterministic field-level pre-run validation problem."""

    field: NonEmptyString
    code: Identifier
    message: NonEmptyString


class RequestRejected(FrozenDTO):
    """A rejected request; intentionally has no run identifier."""

    type: Literal["request_rejected"] = "request_rejected"
    errors: Annotated[tuple[RequestFieldError, ...], Field(min_length=1)]


class RunStatus(StrEnum):
    """Mutually exclusive terminal statuses for an established run."""

    COMPLETED = "COMPLETED"
    NO_MATCH = "NO_MATCH"
    FAILED = "FAILED"


class IssueSeverity(StrEnum):
    """Stable severity values used by warnings and diagnostics."""

    WARNING = "WARNING"
    ERROR = "ERROR"


class Detail(FrozenDTO):
    """An immutable key/value detail without exposing a mutable mapping."""

    key: Identifier
    value: str | int | bool | None


class Issue(FrozenDTO):
    """A safe public issue representation."""

    code: Identifier
    stage: Identifier
    message: NonEmptyString
    severity: IssueSeverity = IssueSeverity.WARNING
    entity_ref: str | None = None
    details: tuple[Detail, ...] = ()


class SourceSpanSummary(FrozenDTO):
    """A half-open code-point range over the trimmed request query."""

    start: Annotated[int, Field(ge=0)]
    end: Annotated[int, Field(gt=0)]
    text: NonEmptyString

    @model_validator(mode="after")
    def end_must_follow_start(self) -> Self:
        if self.end <= self.start:
            raise ValueError("source span end must be greater than start")
        return self


class InterpretedCriterionSummary(FrozenDTO):
    """Transport-neutral interpreted item with its supporting query span."""

    kind: Identifier
    value: NonEmptyString
    source_span: SourceSpanSummary


class InterpretedRequestSummary(FrozenDTO):
    """Phase-neutral summary; concrete intent models are introduced in Phase B."""

    required: tuple[InterpretedCriterionSummary, ...] = ()
    preferred: tuple[InterpretedCriterionSummary, ...] = ()
    parser_version: Identifier = "unavailable"


class FilterStageSummary(FrozenDTO):
    """Sequential funnel counts for one immutable hard-gate stage."""

    gate: Identifier
    before: Annotated[int, Field(ge=0)]
    after: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def after_cannot_exceed_before(self) -> Self:
        if self.after > self.before:
            raise ValueError("after cannot exceed before")
        return self


class FilterReasonCount(FrozenDTO):
    """Independent multi-label rejection counts."""

    reason: Identifier
    product_count: Annotated[int, Field(ge=0)] = 0
    offer_count: Annotated[int, Field(ge=0)] = 0


class FilterSummary(FrozenDTO):
    """Immutable sequential and multi-label filtering views."""

    stages: tuple[FilterStageSummary, ...] = ()
    reason_counts: tuple[FilterReasonCount, ...] = ()


class StageDiagnostic(FrozenDTO):
    """Observable facts for one application stage."""

    stage: Identifier
    duration_ms: Annotated[int, Field(ge=0)]


class Diagnostics(FrozenDTO):
    """Minimum diagnostic contract required before richer run journals exist."""

    ranker_degraded: bool = False
    stages: tuple[StageDiagnostic, ...] = ()
    issues: tuple[Issue, ...] = ()


class MoneySummary(FrozenDTO):
    """Exact and display representations of one landed cost."""

    currency: CurrencyCode
    exact: CanonicalExactAmount
    display: FixedPointAmount


class OfferSummary(FrozenDTO):
    """Minimum identity and price data for an eligible offer."""

    offer_id: Identifier
    provider_id: Identifier
    market: Identifier
    landed_cost: MoneySummary


class EvidenceSummary(FrozenDTO):
    """Reference to evidence validated by the domain evidence closure."""

    evidence_id: Identifier


class SearchResult(FrozenDTO):
    """Minimum result shape promised by the product specification."""

    product_id: Identifier
    title: NonEmptyString
    category: Identifier
    selected_offer: OfferSummary
    eligible_offers: Annotated[tuple[OfferSummary, ...], Field(min_length=1)]
    landed_cost: MoneySummary
    matched_requirements: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    reason: NonEmptyString
    evidence: Annotated[tuple[EvidenceSummary, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def selected_offer_must_be_eligible_and_match_cost(self) -> Self:
        eligible_ids = tuple((offer.provider_id, offer.offer_id) for offer in self.eligible_offers)
        if len(set(eligible_ids)) != len(eligible_ids):
            raise ValueError("eligible_offers cannot contain duplicate offer identities")
        if self.selected_offer not in self.eligible_offers:
            raise ValueError("selected_offer must be present in eligible_offers")
        if self.selected_offer.landed_cost != self.landed_cost:
            raise ValueError("landed_cost must match selected_offer")
        return self


class SearchResponse(FrozenDTO):
    """Terminal response for an established run."""

    run_id: Identifier
    status: RunStatus
    snapshot_version: SnapshotVersion
    config_fingerprint: ConfigFingerprint
    algorithm_version: Identifier
    interpreted_request: InterpretedRequestSummary = InterpretedRequestSummary()
    results: tuple[SearchResult, ...] = ()
    filter_summary: FilterSummary = FilterSummary()
    warnings: tuple[Issue, ...] = ()
    diagnostics: Diagnostics = Diagnostics()

    @model_validator(mode="after")
    def terminal_shape_is_consistent(self) -> Self:
        if self.status is RunStatus.COMPLETED and not self.results:
            raise ValueError("COMPLETED responses must contain results")
        if self.status in {RunStatus.NO_MATCH, RunStatus.FAILED} and self.results:
            raise ValueError(f"{self.status.value} responses cannot contain results")
        product_ids = tuple(result.product_id for result in self.results)
        if len(set(product_ids)) != len(product_ids):
            raise ValueError("results cannot contain duplicate product_id values")
        if len(self.results) > 3:
            raise ValueError("M0 responses cannot contain more than three results")
        return self


class SnapshotValidationResponse(FrozenDTO):
    """Structured outcome for the standalone snapshot validation command."""

    type: Literal["snapshot_validation"] = "snapshot_validation"
    snapshot_version: SnapshotVersion
    valid: bool
    product_count: Annotated[int, Field(ge=0)] = 0
    offer_count: Annotated[int, Field(ge=0)] = 0
    quarantine_count: Annotated[int, Field(ge=0)] = 0
    issues: tuple[Issue, ...] = ()

    @model_validator(mode="after")
    def invalid_snapshot_cannot_report_materialized_records(self) -> Self:
        if not self.valid and (self.product_count or self.offer_count):
            raise ValueError("invalid snapshot cannot report materialized records")
        return self


def validate_search_request(payload: object) -> SearchRequest | RequestRejected:
    """Validate request syntax without reading snapshots or creating a run."""

    try:
        return SearchRequest.model_validate(payload)
    except ValidationError as error:
        field_errors = tuple(
            RequestFieldError(
                field=_format_location(item["loc"]),
                code=str(item["type"]),
                message=str(item["msg"]),
            )
            for item in error.errors(include_url=False, include_context=False, include_input=False)
        )
        return RequestRejected(errors=field_errors)


def _format_location(location: tuple[int | str, ...]) -> str:
    if not location:
        return "$"
    return ".".join(str(part) for part in location)


__all__ = [
    "ConfigFingerprint",
    "CurrencyCode",
    "Detail",
    "Diagnostics",
    "EvidenceSummary",
    "FilterReasonCount",
    "FilterStageSummary",
    "FilterSummary",
    "FrozenDTO",
    "InterpretedCriterionSummary",
    "InterpretedRequestSummary",
    "Issue",
    "IssueSeverity",
    "MoneySummary",
    "OfferSummary",
    "RequestFieldError",
    "RequestRejected",
    "RunStatus",
    "SearchRequest",
    "SearchResponse",
    "SearchResult",
    "SnapshotValidationResponse",
    "SnapshotVersion",
    "SourceSpanSummary",
    "StageDiagnostic",
    "validate_search_request",
]
