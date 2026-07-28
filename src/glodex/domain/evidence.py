"""Evidence identities and entity ownership rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


class EvidenceEntityType(StrEnum):
    PRODUCT = "PRODUCT"
    OFFER = "OFFER"
    EXCHANGE_RATE = "EXCHANGE_RATE"


class VerifiedClaimType(StrEnum):
    """The closed set of evidence-backed facts that deterministic assembly may render."""

    CATEGORY = "category"
    ATTRIBUTE = "attribute"
    INVENTORY = "inventory"
    LANDED_COST = "landed_cost"
    WITHIN_BUDGET = "within_budget"


@dataclass(frozen=True, slots=True)
class FieldEvidence:
    field_path: str
    evidence_id: str

    def __post_init__(self) -> None:
        _require_text(self.field_path, "field path", maximum=512)
        _require_text(self.evidence_id, "evidence ID", maximum=128)


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    evidence_id: str
    snapshot_version: str
    entity_type: EvidenceEntityType
    product_id: str | None
    offer_id: str | None
    currency: str | None
    field_path: str
    provider_id: str
    source_uri: str
    captured_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.evidence_id, "evidence ID", maximum=128)
        _require_text(self.snapshot_version, "snapshot version", maximum=128)
        _require_text(self.provider_id, "provider ID", maximum=128)
        _require_text(self.source_uri, "source URI", maximum=4_096)
        _require_text(self.field_path, "field path", maximum=512)
        if type(self.entity_type) is not EvidenceEntityType:
            raise TypeError("evidence entity_type must be an EvidenceEntityType")
        _require_utc(self.captured_at, "evidence captured_at")

        if self.entity_type is EvidenceEntityType.PRODUCT:
            _require_text(self.product_id, "product ID", maximum=128)
            if self.offer_id is not None or self.currency is not None:
                raise ValueError("product evidence cannot identify an offer or currency")
            if not self.field_path.startswith("product."):
                raise ValueError("product evidence must use a product field path")
        elif self.entity_type is EvidenceEntityType.OFFER:
            _require_text(self.product_id, "product ID", maximum=128)
            _require_text(self.offer_id, "offer ID", maximum=128)
            if self.currency is not None:
                raise ValueError("offer evidence cannot identify a currency")
            if not self.field_path.startswith("offer."):
                raise ValueError("offer evidence must use an offer field path")
        else:
            if self.product_id is not None or self.offer_id is not None:
                raise ValueError("exchange-rate evidence cannot identify a product or offer")
            _require_currency(self.currency)
            if not self.field_path.startswith("exchange_rate."):
                raise ValueError("exchange-rate evidence must use an exchange_rate field path")

    @property
    def source(self) -> str:
        return self.source_uri


@dataclass(frozen=True, slots=True)
class VerifiedClaim:
    """One deterministic fact whose complete evidence closure has been checked."""

    claim_type: VerifiedClaimType
    product_id: str
    value: str
    evidence_ids: tuple[str, ...]
    provider_id: str | None = None
    offer_id: str | None = None
    algorithm_version: str | None = None

    def __post_init__(self) -> None:
        if type(self.claim_type) is not VerifiedClaimType:
            raise TypeError("claim_type must be a VerifiedClaimType")
        _require_text(self.product_id, "claim product ID", maximum=128)
        _require_text(self.value, "claim value", maximum=32_768)
        if type(self.evidence_ids) is not tuple or any(
            type(evidence_id) is not str for evidence_id in self.evidence_ids
        ):
            raise TypeError("claim evidence_ids must be a tuple of strings")
        if not self.evidence_ids:
            raise ValueError("verified claim requires evidence")
        for evidence_id in self.evidence_ids:
            _require_text(evidence_id, "claim evidence ID", maximum=128)
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("claim evidence IDs must be unique")

        offer_claims = {
            VerifiedClaimType.INVENTORY,
            VerifiedClaimType.LANDED_COST,
            VerifiedClaimType.WITHIN_BUDGET,
        }
        if self.claim_type in offer_claims:
            _require_text(self.provider_id, "claim provider ID", maximum=128)
            _require_text(self.offer_id, "claim offer ID", maximum=128)
        elif self.provider_id is not None or self.offer_id is not None:
            raise ValueError("product claims cannot identify an offer")

        derived_claims = {
            VerifiedClaimType.LANDED_COST,
            VerifiedClaimType.WITHIN_BUDGET,
        }
        if self.claim_type in derived_claims:
            if self.algorithm_version != "pricing-v1":
                raise ValueError("derived monetary claims require pricing-v1")
        elif self.algorithm_version is not None:
            raise ValueError("raw fact claims cannot identify a pricing algorithm")


def _require_text(value: object, name: str, *, maximum: int) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if len(value) > maximum:
        raise ValueError(f"{name} exceeds {maximum} code points")
    return value


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


def _require_utc(value: object, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
    return value


__all__ = [
    "EvidenceEntityType",
    "EvidenceRef",
    "FieldEvidence",
    "VerifiedClaim",
    "VerifiedClaimType",
]
