"""Framework-free ports injected into the application service."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from glodex.contracts import SearchRequest
from glodex.domain.catalog import CatalogBatch
from glodex.domain.eligibility import EligibleProduct
from glodex.domain.intent import InterpretedRequest, PreferredCriterion


class RunIdProvider(Protocol):
    """Generate one opaque identifier for each established run."""

    def next_run_id(self) -> str: ...


class Clock(Protocol):
    """Provide UTC wall time and monotonic elapsed-time readings."""

    def now_utc(self) -> datetime: ...

    def monotonic_ns(self) -> int: ...


class IntentInterpreter(Protocol):
    """Interpret a request asynchronously; its concrete model arrives in Phase B."""

    async def interpret(self, request: SearchRequest) -> InterpretedRequest: ...


class CatalogGateway(Protocol):
    """Load and validate one immutable catalog snapshot asynchronously."""

    async def load(
        self,
        snapshot_version: str,
        *,
        display_currency: str | None = None,
        budget_currency: str | None = None,
    ) -> CatalogBatch: ...


class QueryRanker(Protocol):
    """Score every eligible candidate asynchronously without changing membership."""

    async def rank(
        self,
        query: str,
        preferred: tuple[PreferredCriterion, ...],
        candidates: tuple[EligibleProduct, ...],
    ) -> tuple[object, ...]: ...


__all__ = [
    "CatalogGateway",
    "Clock",
    "IntentInterpreter",
    "QueryRanker",
    "RunIdProvider",
]
