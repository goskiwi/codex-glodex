"""Narrow M2b storage/cache ports; no generic data-access abstraction."""

from __future__ import annotations

from typing import Protocol

from glodex.application.durable.contracts import (
    DurableCacheValue,
    DurableCheckpoint,
    DurableEvent,
    DurableProfileSnapshot,
    DurableRun,
    DurableRunState,
)
from glodex.application.m2a_profile import M2aProfileEntry


class RetrievalCachePort(Protocol):
    """Optional cache of opaque retrieval/context projections only."""

    async def get(self, *, key: str, namespace: str) -> DurableCacheValue | None: ...

    async def set(self, *, key: str, value: DurableCacheValue, ttl_seconds: int) -> None: ...


class DurableStorePort(Protocol):
    """The finite persistence surface required by the M2b coordinator."""

    async def reserve_run(
        self,
        *,
        run_id: str,
        thread_id: str,
        request_payload: dict[str, object],
        asset_version: str,
        config_fingerprint: str,
        profile_id: str | None,
        profile_revision: int,
    ) -> DurableRun: ...

    async def load_run(self, *, run_id: str) -> DurableRun: ...

    async def load_events(
        self,
        *,
        run_id: str,
        after_sequence: int = 0,
    ) -> tuple[DurableEvent, ...]: ...

    async def latest_checkpoint(self, *, run_id: str) -> DurableCheckpoint | None: ...

    async def append_event_checkpoint(
        self,
        *,
        event: DurableEvent | None,
        checkpoint: DurableCheckpoint,
        next_state: DurableRunState,
    ) -> DurableRun: ...

    async def finish_run(
        self,
        *,
        event: DurableEvent,
        checkpoint: DurableCheckpoint,
        state: DurableRunState,
        response: dict[str, object] | None,
        error_code: str | None,
    ) -> DurableRun: ...

    async def request_cancel(self, *, run_id: str) -> DurableRun: ...

    async def set_recoverable(self, *, run_id: str) -> DurableRun: ...

    async def profile_snapshot(self, *, profile_id: str) -> DurableProfileSnapshot: ...

    async def set_profile_entry(
        self,
        *,
        entry: M2aProfileEntry,
        embedding_model: str,
    ) -> DurableProfileSnapshot: ...

    async def delete_profile_entry(
        self,
        *,
        profile_id: str,
        entry_id: str,
    ) -> DurableProfileSnapshot: ...


__all__ = ["DurableStorePort", "RetrievalCachePort"]
