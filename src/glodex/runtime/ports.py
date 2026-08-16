"""Narrow Durable storage/cache ports; no generic data-access abstraction."""

from __future__ import annotations

from typing import Protocol

from glodex.observability.runtime import (
    M6OperationsView,
    M6OperationsWindow,
    M6RunTrace,
    M6TraceEvent,
    M6TraceEventDraft,
)
from glodex.runtime.contracts import (
    DurableCacheValue,
    DurableCheckpoint,
    DurableEvent,
    DurableRun,
    DurableRunState,
)


class RetrievalCachePort(Protocol):
    """Optional cache of opaque retrieval/context projections only."""

    async def get(self, *, key: str, namespace: str) -> DurableCacheValue | None: ...

    async def set(self, *, key: str, value: DurableCacheValue, ttl_seconds: int) -> None: ...


class DurableStorePort(Protocol):
    """The finite persistence surface required by the v2 tree coordinator."""

    async def reserve_root_run(
        self,
        *,
        run_id: str,
        thread_id: str,
        request_payload: dict[str, object],
        asset_version: str,
        config_fingerprint: str,
    ) -> DurableRun: ...

    async def reserve_child_run(
        self,
        *,
        run_id: str,
        thread_id: str,
        root_run_id: str,
        parent_run_id: str,
        child_id: str,
        depth: int,
        task_scope_digest: str,
        request_payload: dict[str, object],
        asset_version: str,
        config_fingerprint: str,
    ) -> DurableRun: ...

    async def load_run(self, *, run_id: str) -> DurableRun: ...

    async def load_run_tree(self, *, root_run_id: str) -> tuple[DurableRun, ...]: ...

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


class M6TraceStorePort(Protocol):
    """Finite M6 safe-trace persistence surface, owned by Durable only."""

    async def ensure_m6_trace(self, *, run_id: str) -> M6RunTrace: ...

    async def append_m6_trace_event(
        self,
        *,
        run_id: str,
        draft: M6TraceEventDraft,
    ) -> M6TraceEvent: ...


class M6OperationsCachePort(Protocol):
    """Optional, rebuildable aggregate cache with no run, owner, or text surface."""

    async def get_m6_operations_projection(
        self, *, window: M6OperationsWindow
    ) -> M6OperationsView | None: ...

    async def set_m6_operations_projection(
        self,
        *,
        value: M6OperationsView,
        ttl_seconds: int,
    ) -> None: ...


__all__ = [
    "DurableStorePort",
    "M6OperationsCachePort",
    "M6TraceStorePort",
    "RetrievalCachePort",
]
