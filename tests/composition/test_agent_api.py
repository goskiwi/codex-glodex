"""Composition contracts for the only live durable Agent entrypoint."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from glodex.composition import agent_api
from glodex.config import GlodexConfig
from glodex.interview_catalog.runtime import CategoryKnowledgeIdentity
from glodex.memory.identity import LocalUser, password_matches
from glodex.retrieval.current_product import CurrentProductGatewayIdentity

PROJECT_ROOT = Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.spec(
        *(f"GLO-AGENT_COMPOSITION-P0-{index:03d}" for index in range(1, 7)),
        *(f"GLO-AGENT_COMPOSITION-NFR-{index:03d}" for index in range(1, 5)),
        *(f"AGENT_COMPOSITION-AC-{index:03d}" for index in range(1, 4)),
    ),
]


@dataclass
class _Store:
    opened: int = 0
    closed: int = 0
    health_checked: int = 0

    async def open(self) -> None:
        self.opened += 1

    async def health(self) -> None:
        self.health_checked += 1

    async def close(self) -> None:
        self.closed += 1

    async def thread_owner(self, *, thread_id: str) -> str | None:
        del thread_id
        return None

    async def list_conversation_turns(self, *, thread_id: str, limit: int = 64) -> tuple[()]:
        del thread_id, limit
        return ()

    async def append_conversation_turn(self, **kwargs: object) -> None:
        del kwargs

    async def write_explicit_reflect_memory(self, **kwargs: object) -> tuple[()]:
        del kwargs
        return ()


@dataclass
class _Cache:
    health_checked: int = 0
    closed: int = 0

    async def health(self) -> None:
        self.health_checked += 1

    async def close(self) -> None:
        self.closed += 1

    async def get_user_context_projection(self, *, key: str) -> None:
        del key
        return None

    async def set_user_context_projection(self, **kwargs: object) -> None:
        del kwargs


@dataclass
class _GraphCheckpoints:
    opened: int = 0
    closed: int = 0
    saver: InMemorySaver = field(default_factory=InMemorySaver)

    async def open(self) -> None:
        self.opened += 1

    async def close(self) -> None:
        self.closed += 1

    def require_saver(self) -> InMemorySaver:
        return self.saver


async def _unused_web_search(_query: str, _limit: int) -> tuple[()]:
    raise AssertionError("web search must not run during composition")


@dataclass(frozen=True)
class _Embedding:
    async def embed(self, request: object) -> object:
        raise AssertionError("category index build is replaced in this composition test")


@dataclass(frozen=True)
class _CategoryInsight:
    async def health(self) -> CategoryKnowledgeIdentity:
        return CategoryKnowledgeIdentity(
            index_alias="category-knowledge",
            index_version="knowledge-v1",
            document_count=6,
        )

    async def retrieve(self, request: object) -> object:
        raise AssertionError("category retrieval must not run during composition")


@dataclass
class _ItemSource:
    identity: CurrentProductGatewayIdentity

    async def health(self) -> CurrentProductGatewayIdentity:
        return self.identity

    async def search(
        self,
        request: object,
        *,
        query_vector: object,
        preference_vector: object,
    ) -> object:
        del request, query_vector, preference_vector
        raise AssertionError("item search must not run during composition")


@dataclass(frozen=True)
class _Relevance:
    async def read_relevant(self, **kwargs: object) -> tuple[()]:
        del kwargs
        return ()


def _config() -> GlodexConfig:
    return GlodexConfig(
        data_dir=Path("data/snapshots"),
        default_snapshot="m0-v1",
        default_locale="zh-CN",
        default_currency="CNY",
        default_top_k=3,
        fingerprint="a" * 64,
    )


def test_summary_config_is_bound_to_the_gateway_snapshot_and_cny() -> None:
    configured = agent_api._synthetic_search_config(_config())

    assert configured.default_snapshot == "synthetic-interview-commerce-v1"
    assert configured.default_currency == "CNY"
    assert configured.fingerprint != _config().fingerprint


def test_default_local_account_is_created_once_and_kept_stable() -> None:
    class _DefaultAccountStore:
        account: tuple[LocalUser, str] | None = None
        writes = 0

        async def password_hash_for_username(
            self, *, username: str
        ) -> tuple[LocalUser, str] | None:
            assert username == "kkqq"
            return self.account

        async def ensure_local_user(self, *, user: LocalUser, password_hash: str) -> LocalUser:
            self.writes += 1
            self.account = (user, password_hash)
            return user

    store = _DefaultAccountStore()
    asyncio.run(agent_api._ensure_default_local_account(store))  # type: ignore[arg-type]
    asyncio.run(agent_api._ensure_default_local_account(store))  # type: ignore[arg-type]

    assert store.writes == 1
    assert store.account is not None
    assert store.account[0].username == "kkqq"
    assert password_matches(password="123", password_hash=store.account[1])


def test_build_agent_api_preflights_identities_before_constructing_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item_manifest = tmp_path / "manifest.json"
    item_manifest.write_text("{}\n", encoding="utf-8")
    item_digest = hashlib.sha256(item_manifest.read_bytes()).hexdigest()
    catalog_digest = "b" * 64
    model_digest = "c" * 64
    binding_path = tmp_path / "binding.json"
    binding_path.write_text(
        json.dumps(
            {
                "schema_version": "glodex.current-product-binding.v2",
                "current_catalog": {"source_binding_sha256": catalog_digest},
                "current_item_vectors": {
                    "manifest_path": str(item_manifest.resolve()),
                    "manifest_sha256": item_digest,
                },
                "opensearch": {
                    "index_alias": "current-products",
                    "pipeline_id": "hybrid-v1",
                },
            }
        ),
        encoding="utf-8",
    )
    gateway_identity = CurrentProductGatewayIdentity(
        catalog_binding_sha256=catalog_digest,
        item_vectors_manifest_sha256=item_digest,
        retrieval_model_manifest_digest=model_digest,
        index_alias="current-products",
        search_pipeline="hybrid-v1",
        data_mode="SYNTHETIC_INTERVIEW",
        commerce_ruleset_version="synthetic-multiplatform-cny-v2",
    )
    store = _Store()
    cache = _Cache()
    graph_checkpoints = _GraphCheckpoints()
    app = object()
    captured: dict[str, Any] = {}

    monkeypatch.setenv("LANGGRAPH_AES_KEY", "0" * 32)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.setattr(agent_api, "validate_llm_configuration", lambda: None)
    monkeypatch.setattr(agent_api, "DurablePostgresStore", lambda: store)
    monkeypatch.setattr(agent_api, "DurableRedisCache", lambda: cache)
    monkeypatch.setattr(
        agent_api,
        "PostgresGraphCheckpointStore",
        lambda: graph_checkpoints,
    )
    monkeypatch.setattr(agent_api, "build_tavily_search", lambda: _unused_web_search)

    class _ModelClient:
        async def health(self) -> object:
            return SimpleNamespace(manifest_digest=model_digest)

    monkeypatch.setattr(agent_api, "RetrievalModelClient", _ModelClient)
    monkeypatch.setattr(agent_api, "RetrievalEmbedding", lambda *_args: _Embedding())
    monkeypatch.setattr(agent_api, "RetrievalReranker", lambda *_args: object())
    monkeypatch.setattr(agent_api, "BgeMemoryRetriever", lambda **_kwargs: _Relevance())
    monkeypatch.setattr(
        agent_api,
        "CurrentProductItemSource",
        lambda _endpoint: _ItemSource(gateway_identity),
    )

    def create_app(**kwargs: object) -> object:
        captured.update(kwargs)
        return app

    monkeypatch.setattr(agent_api, "CategoryKnowledgeInsight", lambda _endpoint: _CategoryInsight())
    monkeypatch.setattr(agent_api, "create_durable_agent_app", create_app)

    result = asyncio.run(
        agent_api.build_agent_api(
            settings=agent_api.AgentApiSettings(
                current_product_binding=binding_path,
                item_vectors_manifest=item_manifest,
            ),
            config=_config(),
        )
    )

    assert result is app
    assert captured["executor"].__class__.__name__ == "ReActAgentService"
    assert captured["executor"]._context_loader is not None
    assert captured["terminal_writer"].__class__.__name__ == "MemoryTerminalWriter"
    assert store.opened == store.health_checked == 1
    assert store.closed >= 1
    assert cache.health_checked == 1
    assert cache.closed >= 1
    assert graph_checkpoints.opened == 1
    assert graph_checkpoints.closed >= 1
    assert captured["executor"]._config.capabilities.web_search_enabled is True
    item_source = captured["executor"]._tool_dependencies.item_source
    assert item_source.__class__.__name__ == "DigitalFirstItemSource"
    assert isinstance(item_source._historical, _ItemSource)
    assert (
        captured["executor"]._tool_dependencies.semantic_assertion.__class__.__name__
        == "DeepSeekSemanticAssertion"
    )


def test_build_agent_api_rejects_before_app_when_vector_manifest_differs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item_manifest = tmp_path / "manifest.json"
    item_manifest.write_text("{}\n", encoding="utf-8")
    binding_path = tmp_path / "binding.json"
    binding_path.write_text(
        json.dumps(
            {
                "schema_version": "glodex.current-product-binding.v2",
                "current_catalog": {"source_binding_sha256": "b" * 64},
                "current_item_vectors": {
                    "manifest_path": str(item_manifest.resolve()),
                    "manifest_sha256": "d" * 64,
                },
                "opensearch": {
                    "index_alias": "current-products",
                    "pipeline_id": "hybrid-v1",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(agent_api, "validate_llm_configuration", lambda: None)

    with pytest.raises(agent_api.AgentApiCompositionError):
        asyncio.run(
            agent_api.build_agent_api(
                settings=agent_api.AgentApiSettings(
                    current_product_binding=binding_path,
                    item_vectors_manifest=item_manifest,
                ),
                config=_config(),
            )
        )
