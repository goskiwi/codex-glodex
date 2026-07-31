"""The M2e composition that joins durable Profile/cache to the M2c BGE Agent."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from glodex.adapters.m2b_postgres import M2bPostgresStore
from glodex.adapters.m2b_redis import M2bRedisCache
from glodex.adapters.m2c_model_service import M2cModelServiceClient, M2cModelServiceError
from glodex.agent_bootstrap import build_m2c_agent_service
from glodex.application.agent.contracts import AgentExecution
from glodex.application.agent.ports import AgentEventObserver
from glodex.application.durable.contracts import DurableProfileSnapshot, DurableStoreError
from glodex.application.m2c_profile import (
    M2cProfileEntry,
    M2cProfileError,
    profile_entry_from_operator,
)
from glodex.config import GlodexConfig
from glodex.contracts import SearchRequest
from glodex.m2c_contract import M2C_MAX_EMBEDDING_TEXTS


class M2bM2cProfileError(RuntimeError):
    """The durable Profile stayed valid but could not be BGE-projected."""


@dataclass(frozen=True, slots=True)
class M2bM2cProfileCompiler:
    """Re-encode durable values without ever consuming a legacy stored vector."""

    gpu: M2cModelServiceClient

    def __post_init__(self) -> None:
        if type(self.gpu) is not M2cModelServiceClient:
            raise TypeError("M2b M2c Profile compiler requires the fixed GPU client")

    async def compile(self, snapshot: DurableProfileSnapshot) -> tuple[M2cProfileEntry, ...]:
        if type(snapshot) is not DurableProfileSnapshot:
            raise TypeError("M2b M2c Profile compiler requires an exact snapshot")
        if not snapshot.entries:
            return ()
        try:
            identity = await self.gpu.health()
            values = tuple(entry.value for entry in snapshot.entries)
            vectors: list[tuple[float, ...]] = []
            for start in range(0, len(values), M2C_MAX_EMBEDDING_TEXTS):
                batch = values[start : start + M2C_MAX_EMBEDDING_TEXTS]
                vectors.extend(await self.gpu.embed_texts(texts=batch, identity=identity))
            if len(vectors) != len(snapshot.entries):
                raise ValueError("M2c Profile embedding count drifted")
            return tuple(
                profile_entry_from_operator(
                    profile_id=entry.profile_id,
                    entry_id=entry.entry_id,
                    value=entry.value,
                    vector=vector,
                    model_manifest_digest=identity.manifest_digest,
                )
                for entry, vector in zip(snapshot.entries, vectors, strict=True)
            )
        except (M2cModelServiceError, M2cProfileError, ValueError):
            raise M2bM2cProfileError("M2B_M2C_PROFILE_DEGRADED") from None


@dataclass(frozen=True, slots=True)
class M2bM2cExecutor:
    """Build one M2c Agent from a Profile revision frozen by PostgreSQL."""

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
            raise TypeError("M2b M2c executor inputs are invalid")

    async def execute_run(
        self,
        request: SearchRequest,
        *,
        run_id: str,
        observer: AgentEventObserver | None = None,
    ) -> AgentExecution:
        if type(request) is not SearchRequest or type(run_id) is not str or not run_id:
            raise TypeError("M2b M2c execution inputs are invalid")
        run = await self.store.load_run(run_id=run_id)
        snapshot = await self._snapshot_for_run(run_id=run_id)
        entries = (
            ()
            if snapshot is None
            else await M2bM2cProfileCompiler(M2cModelServiceClient()).compile(snapshot)
        )
        service = await build_m2c_agent_service(
            self.config,
            profile_id=None if snapshot is None or not entries else snapshot.profile_id,
            profile_entries=entries,
            preflight_query=request.query,
            agent_root=self.agent_root,
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


__all__ = ["M2bM2cExecutor", "M2bM2cProfileCompiler", "M2bM2cProfileError"]
