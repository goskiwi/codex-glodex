"""The only M2b composition that joins durable Profile/cache to the M2a Agent."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from glodex.adapters.m2a_opensearch import M2aOpenSearch
from glodex.adapters.m2b_postgres import M2bPostgresStore
from glodex.adapters.m2b_profile_projection import (
    DurableProfileProjection,
    DurableProfileProjectionError,
)
from glodex.adapters.m2b_redis import M2bRedisCache
from glodex.agent_bootstrap import build_m2a_agent_service
from glodex.application.agent.contracts import AgentExecution
from glodex.application.agent.ports import AgentEventObserver
from glodex.application.durable.contracts import DurableProfileSnapshot, DurableStoreError
from glodex.application.m2a_profile import M2aProfileEntry
from glodex.config import GlodexConfig
from glodex.contracts import SearchRequest


@dataclass(frozen=True, slots=True)
class M2bM2aExecutor:
    """Build a one-run M2a composition from the Profile revision reserved in PostgreSQL."""

    config: GlodexConfig
    store: M2bPostgresStore
    cache: M2bRedisCache
    agent_root: Path | None = None

    def __post_init__(self) -> None:
        if (
            type(self.config) is not GlodexConfig
            or type(self.store) is not M2bPostgresStore
            or type(self.cache) is not M2bRedisCache
            or (self.agent_root is not None and not isinstance(self.agent_root, Path))
        ):
            raise TypeError("M2b M2a executor inputs are invalid")

    async def execute_run(
        self,
        request: SearchRequest,
        *,
        run_id: str,
        observer: AgentEventObserver | None = None,
    ) -> AgentExecution:
        if type(request) is not SearchRequest or type(run_id) is not str or not run_id:
            raise TypeError("M2b M2a execution inputs are invalid")
        run = await self.store.load_run(run_id=run_id)
        snapshot = await self._snapshot_for_run(run_id=run_id)
        entries = await self._project_entries(snapshot)
        service = await build_m2a_agent_service(
            self.config,
            profile_id=None if snapshot is None or not entries else snapshot.profile_id,
            profile_entries=entries,
            preflight_query=request.query,
            agent_root=self.agent_root,
            retrieval_cache=self.cache,
            cache_profile_binding=(
                "none"
                if snapshot is None or not entries
                else f"{snapshot.profile_id}-{snapshot.revision}"
            ),
        )
        return await service.execute_run(request, run_id=run.run_id, observer=observer)

    async def _snapshot_for_run(self, *, run_id: str) -> DurableProfileSnapshot | None:
        run = await self.store.load_run(run_id=run_id)
        if run.profile_id is None:
            if run.profile_revision != 0:
                raise DurableStoreError("M2B_PROFILE_REVISION_DRIFT")
            return None
        snapshot = await self.store.profile_snapshot(profile_id=run.profile_id)
        if snapshot.revision != run.profile_revision:
            raise DurableStoreError("M2B_PROFILE_REVISION_DRIFT")
        return snapshot

    async def _project_entries(
        self,
        snapshot: DurableProfileSnapshot | None,
    ) -> tuple[M2aProfileEntry, ...]:
        if snapshot is None or not snapshot.entries:
            return ()
        client = M2aOpenSearch()
        try:
            await DurableProfileProjection(client).project(snapshot)
            return snapshot.entries
        except DurableProfileProjectionError:
            # A disposable ANN projection is never durable truth. Query Hybrid still runs.
            return ()
        finally:
            await client.close()


__all__ = ["M2bM2aExecutor"]
