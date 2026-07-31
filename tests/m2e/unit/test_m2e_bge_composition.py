"""GPU-free M2e composition evidence for the durable BGE cutover."""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from glodex.adapters import m2b_m2c_executor, m2b_postgres, m2c_model_service
from glodex.adapters.m2b_m2c_executor import M2bM2cProfileCompiler
from glodex.adapters.m2c_model_service import M2cModelServiceClient
from glodex.api.agent_events import AgentEventProjector
from glodex.application.durable.contracts import (
    DurableCheckpoint,
    DurableCheckpointState,
    DurableProfileSnapshot,
    DurableRun,
    DurableRunState,
)
from glodex.application.durable.runtime import DurableAgentCoordinator, _checkpoint_for
from glodex.application.m2a_profile import M2aProfileEntry
from glodex.cli import _m2e_asset_version, _run_m2b_profile
from glodex.contracts import SearchRequest
from glodex.m2c_contract import M2C_EMBEDDING_MODEL, M2C_RERANKER_MODEL, M2cModelIdentity

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec(
        "GLO-M2E-P0-001",
        "GLO-M2E-P0-002",
        "GLO-M2E-P0-003",
        "GLO-M2E-P0-004",
        "GLO-M2E-P0-005",
        "GLO-M2E-P0-006",
        "GLO-M2E-NFR-001",
        "GLO-M2E-NFR-002",
        "GLO-M2E-NFR-003",
        "GLO-M2E-NFR-004",
    ),
]


def _identity() -> M2cModelIdentity:
    return M2cModelIdentity(
        manifest_digest="c" * 64,
        embedding_model=M2C_EMBEDDING_MODEL,
        reranker_model=M2C_RERANKER_MODEL,
        dimension=1024,
        max_embedding_texts=8,
        max_text_characters=2000,
        max_rerank_documents=40,
        max_query_characters=512,
        device_class="cuda",
        gpu_model_class="a100",
    )


def _vector(axis: int) -> tuple[float, ...]:
    return tuple(1.0 if index == axis else 0.0 for index in range(1024))


def _snapshot() -> DurableProfileSnapshot:
    return DurableProfileSnapshot(
        profile_id="durable-profile",
        revision=4,
        entries=tuple(
            M2aProfileEntry(
                profile_id="durable-profile",
                entry_id=f"pref-{index:02d}",
                scope="soft",
                kind="preference",
                value=f"preference-{index}",
                user_vector=_vector((index % 7) + 1),
            )
            for index in range(9)
        ),
    )


def _gpu_client(batches: list[tuple[str, ...]]) -> M2cModelServiceClient:
    identity = _identity()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/health":
            return httpx.Response(
                200,
                json={
                    "schemaVersion": identity.schema_version,
                    "manifestDigest": identity.manifest_digest,
                    "embeddingModel": identity.embedding_model,
                    "rerankerModel": identity.reranker_model,
                    "dimension": identity.dimension,
                    "maxEmbeddingTexts": identity.max_embedding_texts,
                    "maxTextCharacters": identity.max_text_characters,
                    "maxRerankDocuments": identity.max_rerank_documents,
                    "maxQueryCharacters": identity.max_query_characters,
                    "deviceClass": identity.device_class,
                    "gpuModelClass": identity.gpu_model_class,
                },
            )
        payload = json.loads(request.content)
        texts = tuple(payload["texts"])
        batches.append(texts)
        return httpx.Response(
            200,
            json={
                "manifestDigest": identity.manifest_digest,
                "embeddings": [list(_vector(0)) for _ in texts],
            },
        )

    return M2cModelServiceClient(http_transport=httpx.MockTransport(handler))


@pytest.mark.acceptance
@pytest.mark.spec("M2E-AC-002")
def test_durable_profile_is_reencoded_from_values_and_never_uses_legacy_vectors() -> None:
    batches: list[tuple[str, ...]] = []
    entries = asyncio.run(M2bM2cProfileCompiler(_gpu_client(batches)).compile(_snapshot()))

    assert batches == [
        tuple(f"preference-{index}" for index in range(8)),
        ("preference-8",),
    ]
    assert [entry.entry_id for entry in entries] == [f"pref-{index:02d}" for index in range(9)]
    assert all(entry.model_manifest_digest == "c" * 64 for entry in entries)
    assert all(entry.user_vector == _vector(0) for entry in entries)
    assert all(
        entry.user_vector != source.user_vector
        for entry, source in zip(entries, _snapshot().entries, strict=True)
    )


def test_m2e_asset_version_comes_only_from_the_verified_bge_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def health(_: M2cModelServiceClient) -> M2cModelIdentity:
        return _identity()

    monkeypatch.setattr(m2c_model_service.M2cModelServiceClient, "health", health)

    assert asyncio.run(_m2e_asset_version()) == "m2b-m2c-agent-cccccccccccccccc"


def test_m2b_profile_write_uses_bge_embedding_not_dashscope(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    class Store:
        async def set_profile_entry(
            self, *, entry: M2aProfileEntry, embedding_model: str
        ) -> object:
            captured["entry"] = entry
            captured["embedding_model"] = embedding_model
            return SimpleNamespace(revision=5)

        async def close(self) -> None:
            return None

    class Gpu:
        async def health(self) -> M2cModelIdentity:
            return _identity()

        async def embed_texts(
            self, *, texts: tuple[str, ...], identity: M2cModelIdentity
        ) -> tuple[tuple[float, ...], ...]:
            assert texts == ("preference",)
            assert identity == _identity()
            return (_vector(0),)

    monkeypatch.setattr(m2b_postgres, "M2bPostgresStore", Store)
    monkeypatch.setattr(m2c_model_service, "M2cModelServiceClient", Gpu)

    result = asyncio.run(
        _run_m2b_profile(
            SimpleNamespace(
                action="set",
                live=True,
                value="preference",
                profile="durable",
                entry_id=None,
            )
        )
    )

    assert result == 0
    assert captured["embedding_model"] == M2C_EMBEDDING_MODEL
    assert isinstance(captured["entry"], M2aProfileEntry)
    assert captured["entry"].user_vector == _vector(0)
    assert "preference" not in capsys.readouterr().out


class _Clock:
    def now_utc(self) -> datetime:
        return datetime(2026, 7, 31, 12, 0, tzinfo=UTC)

    def monotonic_ns(self) -> int:
        return 1


class _ResumeStore:
    def __init__(self, run: DurableRun, checkpoint: DurableCheckpoint) -> None:
        self.run, self.checkpoint = run, checkpoint

    async def load_run(self, *, run_id: str) -> DurableRun:
        assert run_id == self.run.run_id
        return self.run

    async def latest_checkpoint(self, *, run_id: str) -> DurableCheckpoint:
        assert run_id == self.run.run_id
        return self.checkpoint

    async def finish_run(self, **values: object) -> DurableRun:
        event = values["event"]
        checkpoint = values["checkpoint"]
        assert event.run_id == self.run.run_id
        assert checkpoint.run_id == self.run.run_id
        self.run = replace(
            self.run,
            state=DurableRunState.ABORTED,
            event_sequence=event.sequence,
            terminal_error_code=str(values["error_code"]),
        )
        self.checkpoint = checkpoint
        return self.run


class _Executor:
    async def execute_run(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("identity mismatch must abort before execution")


@pytest.mark.acceptance
@pytest.mark.spec("M2E-AC-003")
def test_resume_aborts_an_active_run_when_the_m2e_identity_changes() -> None:
    request = SearchRequest(query="query")
    original = DurableRun(
        run_id="run-identity",
        thread_id="thread-identity",
        state=DurableRunState.ACCEPTED,
        attempt=1,
        event_sequence=0,
        asset_version="m2b-m2a-agent-v1",
        config_fingerprint="a" * 64,
        profile_id=None,
        profile_revision=0,
        request_payload=request.model_dump(mode="json"),
        terminal_response=None,
        terminal_error_code=None,
        cancel_requested=False,
    )
    checkpoint = _checkpoint_for(
        run=original,
        number=1,
        state=DurableCheckpointState.CONFIRMED,
        event_sequence=0,
        phase="ACCEPTED",
    )
    store = _ResumeStore(original, checkpoint)
    coordinator = DurableAgentCoordinator(
        store=store,  # type: ignore[arg-type]
        executor=_Executor(),  # type: ignore[arg-type]
        projector=AgentEventProjector(clock=_Clock()),
        asset_version="m2b-m2c-agent-cccccccccccccccc",
        config_fingerprint="a" * 64,
    )

    resumed = asyncio.run(coordinator.resume(run_id=original.run_id))

    assert resumed.state is DurableRunState.ABORTED
    assert resumed.terminal_error_code == "DURABLE_RUNTIME_IDENTITY_MISMATCH"


@pytest.mark.acceptance
@pytest.mark.spec("M2E-AC-001")
def test_m2e_composition_has_no_dashscope_or_m2a_executor_dependency() -> None:
    executor_source = inspect.getsource(m2b_m2c_executor)
    profile_source = inspect.getsource(_run_m2b_profile)

    assert "dashscope" not in executor_source.casefold()
    assert "M2bM2aExecutor" not in executor_source
    assert "build_dashscope_embedding" not in profile_source
