"""Live composition root for the durable native shopping Agent API."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from glodex.agent.catalog import CandidateManifest, InMemoryCatalogGateway
from glodex.agent.contracts import (
    CURRENT_PRODUCT_PLATFORMS,
    AgentCapabilities,
    DataMode,
    ItemSearchRuntimeResult,
)
from glodex.agent.graph import ReActAgentService
from glodex.agent.llm import validate_llm_configuration
from glodex.agent.preference_assessment import DeepSeekPreferenceAssessment
from glodex.agent.semantic_assertion import DeepSeekSemanticAssertion
from glodex.agent.shopping_summary import DeepSeekShoppingSummary
from glodex.agent.tool_session import AgentRuntimeConfig, AgentSessionCheckpointCodec
from glodex.agent.web_search import TavilyWebSearchPort
from glodex.api.agent_events import AgentEventProjector
from glodex.api.durable import create_durable_agent_app
from glodex.application.search_service import SearchService
from glodex.bootstrap import SystemClock, UuidRunIdProvider
from glodex.config import GlodexConfig, load_config
from glodex.domain.catalog import CatalogBatch, ExchangeRate, ExchangeRateTable
from glodex.domain.evidence import EvidenceEntityType, EvidenceRef
from glodex.domain.intent import InterpretedRequest
from glodex.facts.evidence import build_tavily_search
from glodex.infrastructure.postgres import DurablePostgresStore
from glodex.infrastructure.redis import DurableRedisCache
from glodex.interview_catalog.runtime import CategoryKnowledgeInsight
from glodex.llm.config import load_llm_configuration
from glodex.llm.openai_compatible_http import build_json_completion_transport
from glodex.memory.context import UserContextBuilder, UserPrivateContext
from glodex.memory.identity import LocalUser, hash_password, new_user_id, password_matches
from glodex.memory.llm_reflection import LlmMemoryReflector, LlmThreadSummarizer
from glodex.memory.retrieval import BgeMemoryRetriever
from glodex.memory.terminal_writer import MemoryTerminalWriter
from glodex.retrieval.current_product import (
    CurrentProductGatewayIdentity,
    CurrentProductItemSource,
    build_current_product_manifest,
)
from glodex.retrieval.deterministic_ranker import DeterministicQueryRanker
from glodex.retrieval.digital_catalog import (
    DigitalCatalogItemSource,
    DigitalFirstItemSource,
)
from glodex.retrieval.model_service import (
    RetrievalEmbedding,
    RetrievalModelClient,
    RetrievalReranker,
)
from glodex.runtime.graph_checkpoint import PostgresGraphCheckpointStore
from glodex.tools.engine import ToolDependencies

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_BINDING = _PROJECT_ROOT / "opensearch/current-product/current_product_binding.json"
_DEFAULT_ITEM_VECTORS_MANIFEST = _PROJECT_ROOT / "data/current-product/item-vectors-manifest.json"
_DIGEST_LENGTH = 64
_SYNTHETIC_DATA_MODE = "SYNTHETIC_INTERVIEW"
_SYNTHETIC_RULESET = "synthetic-multiplatform-cny-v2"
_SYNTHETIC_SNAPSHOT = "synthetic-interview-commerce-v1"
_DEFAULT_LOCAL_USERNAME = "kkqq"
_DEFAULT_LOCAL_PASSWORD = "123"
_SYNTHETIC_FX_RATES = (
    ("CNY", Decimal("1.00")),
    ("USD", Decimal("7.20")),
    ("EUR", Decimal("7.85")),
    ("SGD", Decimal("5.30")),
)


class AgentApiCompositionError(RuntimeError):
    """A secret-free startup rejection before the listening socket is bound."""

    def __init__(self, code: str = "AGENT_API_PREFLIGHT_FAILED") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class AgentApiSettings:
    """Immutable locations and loopback endpoint for one live composition."""

    current_product_binding: Path = _DEFAULT_BINDING
    item_vectors_manifest: Path = _DEFAULT_ITEM_VECTORS_MANIFEST
    current_product_endpoint: str = "http://127.0.0.1:18085"
    category_knowledge_endpoint: str = "http://127.0.0.1:18087"
    calculation_date: str = "2026-08-09"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.current_product_binding, Path)
            or not isinstance(self.item_vectors_manifest, Path)
            or type(self.current_product_endpoint) is not str
            or type(self.category_knowledge_endpoint) is not str
            or type(self.calculation_date) is not str
        ):
            raise TypeError("Agent API settings are invalid")


@dataclass(frozen=True, slots=True)
class _BindingIdentity:
    catalog_binding_sha256: str
    item_vectors_manifest_path: Path
    item_vectors_manifest_sha256: str
    index_alias: str
    search_pipeline: str


@dataclass(frozen=True, slots=True)
class _BoundIntent:
    value: InterpretedRequest

    async def interpret(self, request: object) -> InterpretedRequest:
        del request
        return self.value


async def build_agent_api(
    *,
    settings: AgentApiSettings | None = None,
    config: GlodexConfig | None = None,
) -> Any:
    """Preflight all live identities, then construct the durable API app."""

    resolved = settings or AgentApiSettings()
    store = DurablePostgresStore()
    cache = DurableRedisCache()
    graph_checkpoints = PostgresGraphCheckpointStore()
    try:
        application_config = config or load_config()
        summary_config = _synthetic_search_config(application_config)
        validate_llm_configuration()
        llm_configuration = load_llm_configuration()
        binding = _load_binding_identity(
            resolved.current_product_binding,
            item_vectors_manifest=resolved.item_vectors_manifest,
        )
        if _sha256_file(binding.item_vectors_manifest_path) != (
            binding.item_vectors_manifest_sha256
        ):
            raise ValueError("item-vector manifest identity differs from binding")

        model_client = RetrievalModelClient()
        model_identity = await model_client.health()
        embedding = RetrievalEmbedding(model_client, model_identity)
        reranker = RetrievalReranker(model_client, model_identity)
        historical_item_source = CurrentProductItemSource(resolved.current_product_endpoint)
        item_source = DigitalFirstItemSource(
            recent=DigitalCatalogItemSource(
                _PROJECT_ROOT / "data/digital-interview-v1/products.json",
                reranker=reranker,
            ),
            historical=historical_item_source,
        )
        gateway_identity = await item_source.health()
        _require_gateway_identity(
            actual=gateway_identity,
            expected=binding,
            retrieval_model_manifest_digest=model_identity.manifest_digest,
        )
        category_insight = CategoryKnowledgeInsight(resolved.category_knowledge_endpoint)
        category_identity = await category_insight.health()
        fx_source_batch = _synthetic_fx_batch()
        await store.open()
        await store.health()
        await cache.health()
        await graph_checkpoints.open()
        await graph_checkpoints.close()

        web_search = TavilyWebSearchPort(build_tavily_search())

        fingerprint = _fingerprint(
            {
                "category_knowledge_index_alias": category_identity.index_alias,
                "category_knowledge_index_version": category_identity.index_version,
                "category_knowledge_document_count": str(category_identity.document_count),
                "catalog_binding_sha256": binding.catalog_binding_sha256,
                "item_vectors_manifest_sha256": binding.item_vectors_manifest_sha256,
                "retrieval_model_manifest_digest": model_identity.manifest_digest,
                "index_alias": binding.index_alias,
                "search_pipeline": binding.search_pipeline,
                "commerce_data_mode": gateway_identity.data_mode,
                "commerce_ruleset_version": gateway_identity.commerce_ruleset_version,
                "web_search_provider": "tavily",
                "semantic_assertion_model": llm_configuration.model_name,
                "application_config": summary_config.fingerprint,
            }
        )
        runtime_config = AgentRuntimeConfig(
            capabilities=AgentCapabilities(
                data_mode=DataMode.SYNTHETIC_INTERVIEW,
                available_platforms=CURRENT_PRODUCT_PLATFORMS,
                web_search_enabled=True,
                embedding_enabled=True,
            ),
            index_version=binding.item_vectors_manifest_sha256,
            ruleset_version=_SYNTHETIC_RULESET,
            calculation_date=resolved.calculation_date,
            shipping_rules=(),
            fx_source_batch=fx_source_batch,
        )
        llm_transport = build_json_completion_transport(configuration=llm_configuration)
        context_builder = UserContextBuilder(
            store=store,
            relevance=BgeMemoryRetriever(store=store, gpu=model_client),
            summarizer=LlmThreadSummarizer(
                llm_transport,
                model_name=llm_configuration.model_name,
            ),
            cache=cache,
        )

        async def load_user_context(
            thread_id: str,
            current_user_input: str,
        ) -> UserPrivateContext:
            owner = await store.thread_owner(thread_id=thread_id)
            if owner is None:
                raise ValueError("durable Agent thread has no authenticated owner")
            return await context_builder.build(
                user_id=owner,
                thread_id=thread_id,
                current_user_input=current_user_input,
            )

        terminal_writer = MemoryTerminalWriter(
            store=store,
            reflector=LlmMemoryReflector(
                llm_transport,
                model_name=llm_configuration.model_name,
            ),
        )
        executor = ReActAgentService(
            config=runtime_config,
            run_id_provider=UuidRunIdProvider(),
            tool_dependencies=ToolDependencies(
                semantic_assertion=DeepSeekSemanticAssertion(
                    llm_transport,
                    model_name=llm_configuration.model_name,
                ),
                shopping_summary=DeepSeekShoppingSummary(
                    llm_transport,
                    model_name=llm_configuration.model_name,
                ),
                category_insight=category_insight,
                web_search=web_search,
                item_source=item_source,
                preference_assessment=DeepSeekPreferenceAssessment(
                    llm_transport,
                    model_name=llm_configuration.model_name,
                ),
            ),
            embedding_port=embedding,
            candidate_manifest_factory=_manifest_factory,
            search_service_factory=_search_service_factory(summary_config),
            checkpointer_provider=graph_checkpoints.require_saver,
            session_checkpoint_codec=AgentSessionCheckpointCodec(
                os.environ["LANGGRAPH_AES_KEY"].encode("utf-8")
            ),
            context_loader=load_user_context,
        )

        async def start_runtime_dependencies() -> None:
            await graph_checkpoints.open()
            await _ensure_default_local_account(store)

        return create_durable_agent_app(
            executor=executor,
            store=store,
            projector=AgentEventProjector(clock=SystemClock()),
            asset_version="digital-product-" + fingerprint[:16],
            config_fingerprint=fingerprint,
            context_cache=cache,
            terminal_writer=terminal_writer,
            startup_callback=start_runtime_dependencies,
            shutdown_callback=lambda: _close_runtime_dependencies(
                graph_checkpoints=graph_checkpoints,
                cache=cache,
            ),
        )
    except Exception as error:
        await cache.close()
        await store.close()
        await graph_checkpoints.close()
        if isinstance(error, AgentApiCompositionError):
            raise
        raise AgentApiCompositionError() from error
    finally:
        # Preflight must not leave connections behind. The durable app lifespan
        # opens these same owners again after uvicorn has committed to startup.
        await cache.close()
        await store.close()
        await graph_checkpoints.close()


async def _close_runtime_dependencies(
    *,
    graph_checkpoints: PostgresGraphCheckpointStore,
    cache: DurableRedisCache,
) -> None:
    try:
        await graph_checkpoints.close()
    finally:
        await cache.close()


async def _ensure_default_local_account(store: DurablePostgresStore) -> None:
    existing = await store.password_hash_for_username(username=_DEFAULT_LOCAL_USERNAME)
    if existing is not None and password_matches(
        password=_DEFAULT_LOCAL_PASSWORD,
        password_hash=existing[1],
    ):
        return
    await store.ensure_local_user(
        user=LocalUser(user_id=new_user_id(), username=_DEFAULT_LOCAL_USERNAME),
        password_hash=hash_password(_DEFAULT_LOCAL_PASSWORD),
    )


def _manifest_factory(
    results: tuple[ItemSearchRuntimeResult, ...],
) -> CandidateManifest:
    return build_current_product_manifest(results)


def _search_service_factory(
    config: GlodexConfig,
) -> Callable[[InMemoryCatalogGateway, InterpretedRequest], SearchService]:
    def build(gateway: InMemoryCatalogGateway, intent: InterpretedRequest) -> SearchService:
        if type(gateway) is not InMemoryCatalogGateway:
            raise TypeError("summary gateway is invalid")
        return SearchService(
            config=config,
            run_id_provider=UuidRunIdProvider(),
            clock=SystemClock(),
            intent_interpreter=_BoundIntent(intent),
            catalog_gateway=gateway,
            query_ranker=DeterministicQueryRanker(),
        )

    return build


def _synthetic_search_config(config: GlodexConfig) -> GlodexConfig:
    values = {
        "data_dir": str(config.data_dir),
        "default_snapshot": _SYNTHETIC_SNAPSHOT,
        "default_locale": config.default_locale,
        "default_currency": "CNY",
        "default_top_k": config.default_top_k,
    }
    return config.model_copy(
        update={
            "default_snapshot": _SYNTHETIC_SNAPSHOT,
            "default_currency": "CNY",
            "fingerprint": _fingerprint(values),
        }
    )


def _load_binding_identity(path: Path, *, item_vectors_manifest: Path) -> _BindingIdentity:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        current_catalog = value["current_catalog"]
        current_vectors = value["current_item_vectors"]
        opensearch = value["opensearch"]
        identity = _BindingIdentity(
            catalog_binding_sha256=current_catalog["source_binding_sha256"],
            item_vectors_manifest_path=item_vectors_manifest,
            item_vectors_manifest_sha256=current_vectors["manifest_sha256"],
            index_alias=opensearch["index_alias"],
            search_pipeline=opensearch["pipeline_id"],
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise ValueError("current-product binding is invalid") from error
    if (
        value.get("schema_version") != "glodex.current-product-binding.v2"
        or any(
            type(digest) is not str
            or len(digest) != _DIGEST_LENGTH
            or any(char not in "0123456789abcdef" for char in digest)
            for digest in (
                identity.catalog_binding_sha256,
                identity.item_vectors_manifest_sha256,
            )
        )
        or not identity.item_vectors_manifest_path.is_absolute()
        or type(identity.index_alias) is not str
        or not identity.index_alias
        or type(identity.search_pipeline) is not str
        or not identity.search_pipeline
    ):
        raise ValueError("current-product binding identity is invalid")
    return identity


def _require_gateway_identity(
    *,
    actual: CurrentProductGatewayIdentity,
    expected: _BindingIdentity,
    retrieval_model_manifest_digest: str,
) -> None:
    if (
        actual.catalog_binding_sha256 != expected.catalog_binding_sha256
        or actual.item_vectors_manifest_sha256 != expected.item_vectors_manifest_sha256
        or actual.retrieval_model_manifest_digest != retrieval_model_manifest_digest
        or actual.index_alias != expected.index_alias
        or actual.search_pipeline != expected.search_pipeline
        or actual.data_mode != _SYNTHETIC_DATA_MODE
        or actual.commerce_ruleset_version != _SYNTHETIC_RULESET
    ):
        raise ValueError("current-product gateway identity differs from composition")


def _synthetic_fx_batch() -> CatalogBatch:
    captured_at = datetime(2026, 8, 1, tzinfo=UTC)
    evidence = tuple(
        EvidenceRef(
            evidence_id="ev-synthetic-fx-" + currency.lower(),
            snapshot_version=_SYNTHETIC_SNAPSHOT,
            entity_type=EvidenceEntityType.EXCHANGE_RATE,
            product_id=None,
            offer_id=None,
            currency=currency,
            field_path="exchange_rate.base_per_unit",
            provider_id="synthetic-interview-fx",
            source_uri="urn:glodex:synthetic-interview:fx:" + currency,
            captured_at=captured_at,
        )
        for currency, _rate in _SYNTHETIC_FX_RATES
    )
    return CatalogBatch(
        snapshot_version=_SYNTHETIC_SNAPSHOT,
        evidence=evidence,
        exchange_rates=ExchangeRateTable(
            snapshot_version=_SYNTHETIC_SNAPSHOT,
            base_currency="CNY",
            rates=tuple(
                ExchangeRate(
                    snapshot_version=_SYNTHETIC_SNAPSHOT,
                    currency=currency,
                    base_per_unit=rate,
                    minor_units=2,
                    evidence_id="ev-synthetic-fx-" + currency.lower(),
                    snapshot_ordinal=ordinal,
                )
                for ordinal, (currency, rate) in enumerate(_SYNTHETIC_FX_RATES)
            ),
        ),
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fingerprint(value: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


__all__ = ["AgentApiCompositionError", "AgentApiSettings", "build_agent_api"]
