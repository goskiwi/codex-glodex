"""M2b-only projection from durable PostgreSQL Profile to M2a User ANN."""

from __future__ import annotations

from dataclasses import dataclass

from glodex.adapters.m2a_opensearch import M2aOpenSearch
from glodex.adapters.m2a_profile_store import M2aProfileStore
from glodex.application.durable.contracts import DurableProfileSnapshot
from glodex.application.m2a_profile import M2aProfileError


class DurableProfileProjectionError(RuntimeError):
    """The durable Profile remains valid, but its ANN projection did not."""


@dataclass(frozen=True, slots=True)
class DurableProfileProjection:
    """Write only already-validated snapshot entries to M2a's disposable index."""

    client: M2aOpenSearch

    def __post_init__(self) -> None:
        if type(self.client) is not M2aOpenSearch:
            raise TypeError("durable Profile projection requires exact M2a OpenSearch")

    async def project(self, snapshot: DurableProfileSnapshot) -> int:
        if type(snapshot) is not DurableProfileSnapshot:
            raise TypeError("durable Profile projection requires an exact snapshot")
        profile_store = M2aProfileStore(self.client)
        try:
            for entry in snapshot.entries:
                await profile_store.set(entry)
        except M2aProfileError as error:
            raise DurableProfileProjectionError("M2B_PROFILE_PROJECTION_DEGRADED") from error
        return len(snapshot.entries)


__all__ = ["DurableProfileProjection", "DurableProfileProjectionError"]
