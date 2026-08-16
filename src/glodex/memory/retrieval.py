"""Current-manifest BGE relevance for owner-scoped user-memory memory."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

from glodex.memory.identity import validate_user_id
from glodex.memory.models import UserMemoryEntry
from glodex.observability.runtime import (
    M6CircuitOpen,
    M6Operation,
    M6OperationLease,
    M6OperationRecorder,
)
from glodex.retrieval.contracts import RETRIEVAL_MODEL_MAX_EMBEDDING_TEXTS, RetrievalModelIdentity
from glodex.retrieval.model_service import RetrievalModelClient, RetrievalModelError

_MIN_MEMORY_COSINE = 0.30


class MemoryStorePort(Protocol):
    async def list_active_memory(
        self,
        *,
        user_id: str,
        limit: int = 64,
    ) -> tuple[UserMemoryEntry, ...]: ...


@dataclass(frozen=True, slots=True)
class BgeMemoryRetriever:
    """Re-encode query and active memory on every read; no stale vector is consumed."""

    store: MemoryStorePort
    gpu: RetrievalModelClient
    m6_recorder: M6OperationRecorder | None = None

    def __post_init__(self) -> None:
        if (
            not callable(self.store.list_active_memory)
            or type(self.gpu) is not RetrievalModelClient
            or (self.m6_recorder is not None and type(self.m6_recorder) is not M6OperationRecorder)
        ):
            raise TypeError("user-memory BGE memory retriever inputs are invalid")

    async def read_relevant(
        self,
        *,
        user_id: str,
        query: str,
        top_k: int = 5,
    ) -> tuple[UserMemoryEntry, ...]:
        """Return a score-free stable top-k result, degrading to no memory on BGE failure."""

        validate_user_id(user_id)
        if (
            type(query) is not str
            or not query.strip()
            or len(query) > 2_000
            or "\0" in query
            or type(top_k) is not int
            or isinstance(top_k, bool)
            or not 1 <= top_k <= 5
        ):
            raise ValueError("user-memory memory relevance input is invalid")
        try:
            entries = await self.store.list_active_memory(user_id=user_id, limit=128)
            if type(entries) is not tuple or any(
                type(entry) is not UserMemoryEntry or entry.user_id != user_id for entry in entries
            ):
                raise ValueError("user-memory active store result is invalid")
            if not entries:
                return ()
            identity = await self.gpu.health()
            query_vectors = await self._embed(texts=(query,), identity=identity)
            if len(query_vectors) != 1:
                raise ValueError("user-memory query vector count drifted")
            values = tuple(entry.content for entry in entries)
            vectors: list[tuple[float, ...]] = []
            for start in range(0, len(values), RETRIEVAL_MODEL_MAX_EMBEDDING_TEXTS):
                vectors.extend(
                    await self._embed(
                        texts=values[start : start + RETRIEVAL_MODEL_MAX_EMBEDDING_TEXTS],
                        identity=identity,
                    )
                )
            if len(vectors) != len(entries):
                raise ValueError("user-memory memory vector count drifted")
            scored = tuple(
                (entry, _cosine(query_vectors[0], vector))
                for entry, vector in zip(entries, vectors, strict=True)
            )
            return tuple(
                entry
                for entry, _score in sorted(
                    (item for item in scored if item[1] >= _MIN_MEMORY_COSINE),
                    key=lambda item: (-item[1], item[0].entry_id),
                )[:top_k]
            )
        except (RetrievalModelError, ValueError):
            return ()

    async def _embed(
        self,
        *,
        texts: tuple[str, ...],
        identity: RetrievalModelIdentity,
    ) -> tuple[tuple[float, ...], ...]:
        """Record each actual BGE embedding batch, without exposing its text or vectors."""

        lease = await _m6_acquire(recorder=self.m6_recorder)
        try:
            vectors = await self.gpu.embed_texts(texts=texts, identity=identity)
        except RetrievalModelError:
            await _m6_failure(
                recorder=self.m6_recorder,
                lease=lease,
                version=identity.embedding_model,
            )
            raise
        await _m6_success(
            recorder=self.m6_recorder,
            lease=lease,
            version=identity.embedding_model,
        )
        return vectors


async def _m6_acquire(*, recorder: M6OperationRecorder | None) -> M6OperationLease | None:
    if recorder is None:
        return None
    try:
        return await recorder.acquire(operation=M6Operation.BGE_EMBEDDING)
    except M6CircuitOpen:
        raise RetrievalModelError() from None
    except Exception:
        return None


async def _m6_success(
    *,
    recorder: M6OperationRecorder | None,
    lease: M6OperationLease | None,
    version: str,
) -> None:
    if recorder is None or lease is None:
        return
    try:
        await recorder.success(lease=lease, version=version)
    except Exception:
        return


async def _m6_failure(
    *,
    recorder: M6OperationRecorder | None,
    lease: M6OperationLease | None,
    version: str,
) -> None:
    if recorder is None or lease is None:
        return
    try:
        await recorder.failure(
            lease=lease,
            safe_code="RETRIEVAL_MODEL_UNAVAILABLE",
            retryable_failure=True,
            version=version,
        )
    except Exception:
        return


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("user-memory memory vector shape drifted")
    numerator = sum(first * second for first, second in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not math.isfinite(numerator) or left_norm <= 0.0 or right_norm <= 0.0:
        raise ValueError("user-memory memory vector is invalid")
    score = numerator / (left_norm * right_norm)
    if not math.isfinite(score):
        raise ValueError("user-memory memory vector is invalid")
    return score


__all__ = ["BgeMemoryRetriever"]
