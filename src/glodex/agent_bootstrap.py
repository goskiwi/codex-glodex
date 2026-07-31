"""Operator-only composition for the fixed M1d Agent runtime."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

from glodex.adapters.agent_indexes import (
    M1D_DEMO_VERSION,
    AgentIndexes,
    load_agent_indexes,
)
from glodex.adapters.agent_item_search import (
    DemoItemSource,
    LiveEbayItemSource,
    LiveEbayPreflightError,
    build_live_ebay_item_source,
)
from glodex.adapters.agent_live_http import (
    build_dashscope_embedding,
    build_tavily_web_search,
)
from glodex.adapters.deepseek_agent import DeepSeekActionSelector
from glodex.adapters.deepseek_http import (
    DeepSeekPreflightError,
    build_deepseek_transport,
)
from glodex.adapters.rule_intent import AgentRuleIntentInterpreter
from glodex.application.agent.catalog import (
    CandidateManifest,
    InMemoryCatalogGateway,
    ManifestRecord,
)
from glodex.application.agent.contracts import (
    PLATFORM_SET,
    AgentCapabilities,
    AgentExecution,
    DataMode,
    ItemSearchRuntimeResult,
    Platform,
    ToolFailureCode,
)
from glodex.application.agent.ports import AgentEventObserver, ItemSourcePort, ToolPortError
from glodex.application.agent.runtime import (
    AgentRuntimeConfig,
    AgentService,
)
from glodex.application.agent.tools import ShippingRule, ToolDependencies
from glodex.application.m2a_profile import M2aProfileEntry
from glodex.application.m2c_profile import M2cProfileEntry
from glodex.application.ports import Clock, RunIdProvider
from glodex.application.search_service import SearchService
from glodex.bootstrap import (
    SystemClock,
    UuidRunIdProvider,
    build_service,
)
from glodex.config import GlodexConfig
from glodex.contracts import SearchRequest

_CREDENTIALS_MISSING: Final = "AGENT_LIVE_CREDENTIALS_MISSING"
_CONFIG_INVALID: Final = "AGENT_LIVE_CONFIG_INVALID"
_INDEX_INVALID: Final = "AGENT_INDEX_INVALID"
_OUTPUT_ROOT_INVALID: Final = "AGENT_OUTPUT_ROOT_INVALID"
_PREFLIGHT_MESSAGES: Final = {
    _CREDENTIALS_MISSING: "Agent live credentials are missing.",
    _CONFIG_INVALID: "Agent live configuration is invalid.",
    _INDEX_INVALID: "Agent demo indexes are invalid.",
    _OUTPUT_ROOT_INVALID: "Agent live output root is invalid.",
}

if TYPE_CHECKING:
    from glodex.adapters.m2a_retrieval import OpenSearchCategoryInsight, OpenSearchItemSource
    from glodex.adapters.m2c_retrieval import (
        M2cOpenSearchCategoryInsight,
        M2cOpenSearchItemSource,
    )
    from glodex.application.durable.ports import RetrievalCachePort


type _PerRunServiceFactory = Callable[[SearchRequest], AgentService]


class _ManifestSource(ItemSourcePort, Protocol):
    def manifest_records_for(
        self,
        result: ItemSearchRuntimeResult,
    ) -> tuple[ManifestRecord, ...]: ...


class AgentPreflightError(RuntimeError):
    """One stable, path-free failure raised before Agent Run allocation."""

    def __init__(self, code: str) -> None:
        message = _PREFLIGHT_MESSAGES.get(code)
        if message is None:
            raise ValueError("unsupported Agent preflight error code")
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class PerRunAgentExecutor:
    """Create a fresh AgentService and item source for every root Run."""

    _service_factory: _PerRunServiceFactory

    def __post_init__(self) -> None:
        if not callable(self._service_factory):
            raise TypeError("per-run Agent service factory must be callable")

    async def execute(self, request: SearchRequest) -> AgentExecution:
        if type(request) is not SearchRequest:
            raise TypeError("Agent executor requires an exact SearchRequest")
        return await self._service_factory(request).execute(request)

    async def execute_run(
        self,
        request: SearchRequest,
        *,
        run_id: str,
        observer: AgentEventObserver | None = None,
    ) -> AgentExecution:
        if type(request) is not SearchRequest:
            raise TypeError("Agent executor requires an exact SearchRequest")
        return await self._service_factory(request).execute_run(
            request,
            run_id=run_id,
            observer=observer,
        )


@dataclass(frozen=True, slots=True)
class M2aAgentExecution:
    """One regular Agent execution plus M2a's safe, body-free retrieval projection."""

    execution: AgentExecution
    item_traces: tuple[tuple[int, int, tuple[str, ...], tuple[str, ...]], ...]
    category_traces: tuple[tuple[int, tuple[str, ...], tuple[str, ...]], ...]


@dataclass(frozen=True, slots=True)
class M2aPerRunAgentExecutor:
    """Fresh M2a service owner that exposes a CLI-only safe retrieval trace."""

    _service_factory: Callable[
        [SearchRequest], tuple[AgentService, OpenSearchItemSource, OpenSearchCategoryInsight]
    ]

    async def execute(self, request: SearchRequest) -> AgentExecution:
        return (await self.execute_with_trace(request)).execution

    async def execute_run(
        self,
        request: SearchRequest,
        *,
        run_id: str,
        observer: AgentEventObserver | None = None,
    ) -> AgentExecution:
        """Run the unchanged M2a composition with an externally owned durable ID."""

        if type(request) is not SearchRequest or type(run_id) is not str or not run_id:
            raise TypeError("M2a durable execution inputs are invalid")
        service, _item_source, _category_source = self._service_factory(request)
        return await service.execute_run(request, run_id=run_id, observer=observer)

    async def execute_with_trace(self, request: SearchRequest) -> M2aAgentExecution:
        if type(request) is not SearchRequest:
            raise TypeError("M2a Agent executor requires an exact SearchRequest")
        service, item_source, category_source = self._service_factory(request)
        execution = await service.execute(request)
        return M2aAgentExecution(
            execution=execution,
            item_traces=tuple(
                (
                    trace.query_candidate_count,
                    trace.user_candidate_count,
                    trace.selected_ids,
                    tuple(code.value for code in trace.safe_codes),
                )
                for trace in item_source.trace_history
            ),
            category_traces=tuple(
                (
                    trace.query_candidate_count,
                    trace.selected_ids,
                    tuple(code.value for code in trace.safe_codes),
                )
                for trace in category_source.trace_history
            ),
        )


@dataclass(frozen=True, slots=True)
class M2cAgentExecution:
    """One unchanged Agent result plus only M2c opaque retrieval evidence."""

    execution: AgentExecution
    item_traces: tuple[tuple[int, int, tuple[str, ...], tuple[str, ...]], ...]
    category_traces: tuple[tuple[int, tuple[str, ...], tuple[str, ...]], ...]
    manifest_prefix: str


@dataclass(frozen=True, slots=True)
class M2cPerRunAgentExecutor:
    """Fresh M2c service owner isolated from M2a's model/index composition."""

    _service_factory: Callable[
        [SearchRequest], tuple[AgentService, M2cOpenSearchItemSource, M2cOpenSearchCategoryInsight]
    ]
    _manifest_prefix: str

    async def execute(self, request: SearchRequest) -> AgentExecution:
        return (await self.execute_with_trace(request)).execution

    async def execute_with_trace(self, request: SearchRequest) -> M2cAgentExecution:
        if type(request) is not SearchRequest:
            raise TypeError("M2c Agent executor requires an exact SearchRequest")
        service, item_source, category_source = self._service_factory(request)
        execution = await service.execute(request)
        return M2cAgentExecution(
            execution=execution,
            item_traces=tuple(
                (
                    trace.query_candidate_count,
                    trace.user_candidate_count,
                    trace.selected_ids,
                    tuple(code.value for code in trace.safe_codes),
                )
                for trace in item_source.trace_history
            ),
            category_traces=tuple(
                (
                    trace.query_candidate_count,
                    trace.selected_ids,
                    tuple(code.value for code in trace.safe_codes),
                )
                for trace in category_source.trace_history
            ),
            manifest_prefix=self._manifest_prefix,
        )


async def build_agent_service(
    config: GlodexConfig,
    *,
    live_data: bool,
    output_root: Path | None,
    preflight_query: str,
    agent_root: Path | None = None,
    project_root: Path | None = None,
    ebay_environ: Mapping[str, str] | None = None,
    run_id_provider: RunIdProvider | None = None,
    clock: Clock | None = None,
) -> PerRunAgentExecutor:
    """Preflight the fixed composition and return a reusable per-Run executor."""

    _validate_composition_inputs(
        config=config,
        live_data=live_data,
        output_root=output_root,
        preflight_query=preflight_query,
        agent_root=agent_root,
        project_root=project_root,
        ebay_environ=ebay_environ,
    )
    effective_clock = SystemClock() if clock is None else clock
    effective_run_ids = UuidRunIdProvider() if run_id_provider is None else run_id_provider
    calculation_date = _calculation_date(effective_clock)

    if live_data:
        assert output_root is not None
        _live_source(
            query=preflight_query,
            output_root=output_root,
            environ=ebay_environ,
            project_root=project_root,
        )

    try:
        deepseek_transport = build_deepseek_transport()
    except DeepSeekPreflightError as error:
        code = (
            _CREDENTIALS_MISSING
            if error.code == "INTENT_LIVE_CREDENTIALS_MISSING"
            else _CONFIG_INVALID
        )
        raise AgentPreflightError(code) from None
    try:
        embedding_port = build_dashscope_embedding()
        web_search = build_tavily_web_search() if live_data else None
    except ToolPortError as error:
        if error.code in {
            ToolFailureCode.PROVIDER_UNAVAILABLE,
            ToolFailureCode.WEB_SEARCH_NOT_ENABLED,
        }:
            raise AgentPreflightError(_CREDENTIALS_MISSING) from None
        raise AgentPreflightError(_CONFIG_INVALID) from None

    resolved_agent_root = config.data_dir.parent / "agent" if agent_root is None else agent_root
    try:
        indexes = await load_agent_indexes(
            snapshot_root=config.data_dir,
            agent_root=resolved_agent_root,
            version=M1D_DEMO_VERSION,
        )
    except ToolPortError:
        raise AgentPreflightError(_INDEX_INVALID) from None
    if type(indexes) is not AgentIndexes:
        raise AgentPreflightError(_INDEX_INVALID)

    runtime_config = _runtime_config(
        indexes,
        live_data=live_data,
        calculation_date=calculation_date,
    )
    search_service_factory = _search_service_factory(
        config,
        run_id_provider=effective_run_ids,
        clock=effective_clock,
    )

    def new_service(request: SearchRequest) -> AgentService:
        if live_data:
            assert output_root is not None
            live_source = _live_source(
                query=request.query,
                output_root=output_root,
                environ=ebay_environ,
                project_root=project_root,
            )
            item_source: _ManifestSource = live_source
            resource_drainer = live_source.drain
        else:
            item_source = DemoItemSource(indexes)
            resource_drainer = None
        return AgentService(
            config=runtime_config,
            action_selector=DeepSeekActionSelector(deepseek_transport),
            intent_interpreter=AgentRuleIntentInterpreter(),
            run_id_provider=effective_run_ids,
            tool_dependencies=ToolDependencies(
                web_search=web_search,
                category_insight=indexes,
                item_source=item_source,
            ),
            embedding_port=embedding_port,
            candidate_manifest_factory=_manifest_factory(
                item_source,
                indexes=indexes,
                data_mode=runtime_config.capabilities.data_mode,
            ),
            search_service_factory=search_service_factory,
            resource_drainer=resource_drainer,
        )

    if not live_data:
        try:
            demo_source = DemoItemSource(indexes)
            _ = demo_source.manifest_records
        except ToolPortError:
            raise AgentPreflightError(_INDEX_INVALID) from None
    return PerRunAgentExecutor(new_service)


async def build_m2a_agent_service(
    config: GlodexConfig,
    *,
    profile_id: str | None,
    preflight_query: str,
    profile_entries: tuple[M2aProfileEntry, ...] | None = None,
    agent_root: Path | None = None,
    run_id_provider: RunIdProvider | None = None,
    clock: Clock | None = None,
    retrieval_cache: RetrievalCachePort | None = None,
    cache_profile_binding: str = "none",
) -> M2aPerRunAgentExecutor:
    """Build the explicit M2a Agent composition without changing the M1d factory."""

    from glodex.adapters.dashscope_rerank import M2aRerankError, build_dashscope_reranker
    from glodex.adapters.m2a_indexes import M2aIndexError, verify_indexes
    from glodex.adapters.m2a_opensearch import M2aOpenSearch
    from glodex.adapters.m2a_profile_store import M2aProfileStore
    from glodex.adapters.m2a_retrieval import OpenSearchCategoryInsight, OpenSearchItemSource

    _validate_composition_inputs(
        config=config,
        live_data=False,
        output_root=None,
        preflight_query=preflight_query,
        agent_root=agent_root,
        project_root=None,
        ebay_environ=None,
    )
    if profile_id is not None and (
        type(profile_id) is not str or not profile_id or len(profile_id) > 64
    ):
        raise AgentPreflightError(_CONFIG_INVALID)
    if profile_entries is not None and (
        type(profile_entries) is not tuple
        or any(type(entry) is not M2aProfileEntry for entry in profile_entries)
        or (profile_id is None and profile_entries)
        or any(entry.profile_id != profile_id for entry in profile_entries)
    ):
        raise AgentPreflightError(_CONFIG_INVALID)
    effective_clock = SystemClock() if clock is None else clock
    effective_run_ids = UuidRunIdProvider() if run_id_provider is None else run_id_provider
    calculation_date = _calculation_date(effective_clock)
    try:
        deepseek_transport = build_deepseek_transport()
        embedding_port = build_dashscope_embedding()
        reranker = build_dashscope_reranker()
    except (DeepSeekPreflightError, ToolPortError, M2aRerankError) as error:
        if (
            isinstance(error, DeepSeekPreflightError)
            and error.code != "INTENT_LIVE_CREDENTIALS_MISSING"
        ):
            raise AgentPreflightError(_CONFIG_INVALID) from None
        raise AgentPreflightError(_CREDENTIALS_MISSING) from None

    resolved_agent_root = config.data_dir.parent / "agent" if agent_root is None else agent_root
    try:
        indexes = await load_agent_indexes(
            snapshot_root=config.data_dir,
            agent_root=resolved_agent_root,
            version=M1D_DEMO_VERSION,
        )
    except ToolPortError:
        raise AgentPreflightError(_INDEX_INVALID) from None
    client = M2aOpenSearch()
    try:
        await verify_indexes(client=client, indexes=indexes)
        entries = (
            profile_entries
            if profile_entries is not None
            else (
                ()
                if profile_id is None
                else await M2aProfileStore(client).list(profile_id=profile_id)
            )
        )
    except (M2aIndexError, M2aRerankError):
        await client.close()
        raise AgentPreflightError(_INDEX_INVALID) from None
    except Exception:
        await client.close()
        raise AgentPreflightError(_INDEX_INVALID) from None

    runtime_config = _runtime_config(indexes, live_data=False, calculation_date=calculation_date)
    search_service_factory = _search_service_factory(
        config,
        run_id_provider=effective_run_ids,
        clock=effective_clock,
    )

    def new_service(
        _request: SearchRequest,
    ) -> tuple[AgentService, OpenSearchItemSource, OpenSearchCategoryInsight]:
        item_source = OpenSearchItemSource(
            client=client,
            indexes=indexes,
            profile_entries=entries,
            reranker=reranker,
            retrieval_cache=retrieval_cache,
            cache_profile_binding=cache_profile_binding,
        )
        category_insight = OpenSearchCategoryInsight(
            client=client,
            indexes=indexes,
            reranker=reranker,
        )
        service = AgentService(
            config=runtime_config,
            action_selector=DeepSeekActionSelector(deepseek_transport),
            intent_interpreter=AgentRuleIntentInterpreter(),
            run_id_provider=effective_run_ids,
            tool_dependencies=ToolDependencies(
                web_search=None,
                category_insight=category_insight,
                item_source=item_source,
            ),
            embedding_port=embedding_port,
            candidate_manifest_factory=_manifest_factory(
                item_source,
                indexes=indexes,
                data_mode=runtime_config.capabilities.data_mode,
            ),
            search_service_factory=search_service_factory,
            resource_drainer=client.close,
        )
        return service, item_source, category_insight

    return M2aPerRunAgentExecutor(new_service)


async def build_m2c_agent_service(
    config: GlodexConfig,
    *,
    profile_id: str | None,
    preflight_query: str,
    profile_entries: tuple[M2cProfileEntry, ...] | None = None,
    agent_root: Path | None = None,
    run_id_provider: RunIdProvider | None = None,
    clock: Clock | None = None,
) -> M2cPerRunAgentExecutor:
    """Build the explicit BGE-only Agent path without importing M2a's composition."""

    from glodex.adapters.m2a_opensearch import M2aOpenSearch
    from glodex.adapters.m2c_indexes import M2cIndexError, verify_indexes
    from glodex.adapters.m2c_model_service import (
        M2cEmbedding,
        M2cModelServiceClient,
        M2cModelServiceError,
        M2cReranker,
    )
    from glodex.adapters.m2c_profile_store import M2cProfileStore
    from glodex.adapters.m2c_retrieval import (
        M2cOpenSearchCategoryInsight,
        M2cOpenSearchItemSource,
    )
    from glodex.application.m2c_profile import M2cProfileError

    _validate_composition_inputs(
        config=config,
        live_data=False,
        output_root=None,
        preflight_query=preflight_query,
        agent_root=agent_root,
        project_root=None,
        ebay_environ=None,
    )
    if profile_id is not None and (
        type(profile_id) is not str or not profile_id or len(profile_id) > 64
    ):
        raise AgentPreflightError(_CONFIG_INVALID)
    if profile_entries is not None and (
        type(profile_entries) is not tuple
        or any(type(entry) is not M2cProfileEntry for entry in profile_entries)
        or (profile_id is None and profile_entries)
        or any(entry.profile_id != profile_id for entry in profile_entries)
    ):
        raise AgentPreflightError(_CONFIG_INVALID)
    effective_clock = SystemClock() if clock is None else clock
    effective_run_ids = UuidRunIdProvider() if run_id_provider is None else run_id_provider
    calculation_date = _calculation_date(effective_clock)
    try:
        deepseek_transport = build_deepseek_transport()
    except DeepSeekPreflightError as error:
        if error.code != "INTENT_LIVE_CREDENTIALS_MISSING":
            raise AgentPreflightError(_CONFIG_INVALID) from None
        raise AgentPreflightError(_CREDENTIALS_MISSING) from None

    resolved_agent_root = config.data_dir.parent / "agent" if agent_root is None else agent_root
    try:
        indexes = await load_agent_indexes(
            snapshot_root=config.data_dir,
            agent_root=resolved_agent_root,
            version=M1D_DEMO_VERSION,
        )
    except ToolPortError:
        raise AgentPreflightError(_INDEX_INVALID) from None
    gpu = M2cModelServiceClient()
    client = M2aOpenSearch()
    profile_safe_codes: tuple[ToolFailureCode, ...] = ()
    try:
        identity = await gpu.health()
        await verify_indexes(client=client, indexes=indexes, identity=identity)
        if profile_entries is not None:
            entries = profile_entries
        elif profile_id is None:
            entries = ()
        else:
            try:
                entries = await M2cProfileStore(client).list(
                    profile_id=profile_id, identity=identity
                )
            except M2cProfileError as error:
                entries = ()
                profile_safe_codes = (
                    ToolFailureCode.M2C_PROFILE_MODEL_MISMATCH
                    if str(error) == "M2C_PROFILE_MODEL_MISMATCH"
                    else ToolFailureCode.M2C_USER_EMBEDDING_DEGRADED,
                )
    except (M2cIndexError, M2cModelServiceError):
        await client.close()
        raise AgentPreflightError(_INDEX_INVALID) from None
    except Exception:
        await client.close()
        raise AgentPreflightError(_INDEX_INVALID) from None

    runtime_config = _runtime_config(indexes, live_data=False, calculation_date=calculation_date)
    search_service_factory = _search_service_factory(
        config,
        run_id_provider=effective_run_ids,
        clock=effective_clock,
    )
    embedding_port = M2cEmbedding(gpu, identity)
    reranker = M2cReranker(gpu, identity)

    def new_service(
        _request: SearchRequest,
    ) -> tuple[AgentService, M2cOpenSearchItemSource, M2cOpenSearchCategoryInsight]:
        item_source = M2cOpenSearchItemSource(
            client=client,
            indexes=indexes,
            profile_entries=entries,
            reranker=reranker,
            initial_safe_codes=profile_safe_codes,
        )
        category_insight = M2cOpenSearchCategoryInsight(
            client=client,
            indexes=indexes,
            reranker=reranker,
        )
        service = AgentService(
            config=runtime_config,
            action_selector=DeepSeekActionSelector(deepseek_transport),
            intent_interpreter=AgentRuleIntentInterpreter(),
            run_id_provider=effective_run_ids,
            tool_dependencies=ToolDependencies(
                web_search=None,
                category_insight=category_insight,
                item_source=item_source,
            ),
            embedding_port=embedding_port,
            candidate_manifest_factory=_manifest_factory(
                item_source,
                indexes=indexes,
                data_mode=runtime_config.capabilities.data_mode,
            ),
            search_service_factory=search_service_factory,
            resource_drainer=client.close,
        )
        return service, item_source, category_insight

    return M2cPerRunAgentExecutor(new_service, identity.manifest_digest[:16])


def _validate_composition_inputs(
    *,
    config: object,
    live_data: object,
    output_root: object,
    preflight_query: object,
    agent_root: object,
    project_root: object,
    ebay_environ: object,
) -> None:
    if type(config) is not GlodexConfig:
        raise TypeError("Agent composition requires an exact GlodexConfig")
    if type(live_data) is not bool:
        raise TypeError("live_data must be an exact bool")
    if type(preflight_query) is not str or not preflight_query.strip():
        raise AgentPreflightError(_CONFIG_INVALID)
    if live_data:
        if not isinstance(output_root, Path) or not output_root.is_absolute():
            raise AgentPreflightError(_OUTPUT_ROOT_INVALID)
    elif output_root is not None:
        raise AgentPreflightError(_CONFIG_INVALID)
    if agent_root is not None and not isinstance(agent_root, Path):
        raise AgentPreflightError(_CONFIG_INVALID)
    if project_root is not None and not isinstance(project_root, Path):
        raise AgentPreflightError(_CONFIG_INVALID)
    if ebay_environ is not None and (
        not isinstance(ebay_environ, Mapping)
        or any(
            type(key) is not str or type(value) is not str for key, value in ebay_environ.items()
        )
    ):
        raise AgentPreflightError(_CONFIG_INVALID)


def _calculation_date(clock: Clock) -> str:
    try:
        now = clock.now_utc()
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("clock is not UTC")
        return now.date().isoformat()
    except Exception:
        raise AgentPreflightError(_CONFIG_INVALID) from None


def _live_source(
    *,
    query: str,
    output_root: Path,
    environ: Mapping[str, str] | None,
    project_root: Path | None,
) -> LiveEbayItemSource:
    try:
        return build_live_ebay_item_source(
            query=query,
            output_root=output_root,
            environ=environ,
            project_root=project_root,
        )
    except LiveEbayPreflightError as error:
        if error.code.value == "CAPTURE_CREDENTIALS_MISSING":
            code = _CREDENTIALS_MISSING
        elif error.code.value == "CAPTURE_CONFIG_INVALID":
            code = _OUTPUT_ROOT_INVALID
        else:
            code = _CONFIG_INVALID
        raise AgentPreflightError(code) from None


def _runtime_config(
    indexes: AgentIndexes,
    *,
    live_data: bool,
    calculation_date: str,
) -> AgentRuntimeConfig:
    capabilities = AgentCapabilities(
        data_mode=(DataMode.LIVE_MARKETPLACE if live_data else DataMode.DEMO_SNAPSHOT),
        available_platforms=((Platform.EBAY,) if live_data else PLATFORM_SET),
        supported_categories=(("phone",) if live_data else ("phone", "laptop", "tablet")),
        web_search_enabled=live_data,
        embedding_enabled=True,
        live_ebay_enabled=live_data,
    )
    rules = tuple(
        ShippingRule(
            platform=rule.platform,
            flat_shipping=rule.base_shipping,
            duty_rate=rule.duty_rate,
            duty_threshold=rule.duty_threshold,
            effective_date=rule.effective_date.isoformat(),
            eta_days_min=rule.eta_min_days,
            eta_days_max=rule.eta_max_days,
        )
        for rule in indexes.shipping_rules
    )
    try:
        return AgentRuntimeConfig(
            capabilities=capabilities,
            index_version=indexes.index_version,
            ruleset_version=indexes.ruleset_version,
            calculation_date=calculation_date,
            shipping_rules=rules,
            fx_source_batch=indexes.batch,
        )
    except Exception:
        raise AgentPreflightError(_INDEX_INVALID) from None


def _manifest_factory(
    source: _ManifestSource,
    *,
    indexes: AgentIndexes,
    data_mode: DataMode,
) -> Callable[[tuple[ItemSearchRuntimeResult, ...]], CandidateManifest]:
    def build(results: tuple[ItemSearchRuntimeResult, ...]) -> CandidateManifest:
        if (
            type(results) is not tuple
            or not results
            or any(type(result) is not ItemSearchRuntimeResult for result in results)
        ):
            raise ToolPortError(ToolFailureCode.ITEM_SOURCE_INVALID)
        if data_mode is DataMode.LIVE_MARKETPLACE and (
            len(results) != 1 or results[0].platform is not Platform.EBAY
        ):
            raise ToolPortError(ToolFailureCode.ITEM_SOURCE_INVALID)
        records = tuple(
            record for result in results for record in source.manifest_records_for(result)
        )
        snapshot_version = (
            results[0].platform_sub_batch.snapshot_version
            if data_mode is DataMode.LIVE_MARKETPLACE
            else indexes.snapshot_version
        )
        return CandidateManifest(
            data_mode=data_mode,
            snapshot_version=snapshot_version,
            records=records,
        )

    return build


def _search_service_factory(
    config: GlodexConfig,
    *,
    run_id_provider: RunIdProvider,
    clock: Clock,
) -> Callable[[InMemoryCatalogGateway], SearchService]:
    def build(gateway: InMemoryCatalogGateway) -> SearchService:
        if type(gateway) is not InMemoryCatalogGateway:
            raise TypeError("Agent search factory requires InMemoryCatalogGateway")
        return build_service(
            config,
            run_id_provider=run_id_provider,
            clock=clock,
            intent_interpreter=AgentRuleIntentInterpreter(),
            catalog_gateway=gateway,
        )

    return build


__all__ = [
    "AgentPreflightError",
    "M2aAgentExecution",
    "M2aPerRunAgentExecutor",
    "M2cAgentExecution",
    "M2cPerRunAgentExecutor",
    "PerRunAgentExecutor",
    "build_agent_service",
    "build_m2a_agent_service",
    "build_m2c_agent_service",
]
