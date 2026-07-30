"""Operator-only composition for the fixed M1d Agent runtime."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Final

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
from glodex.application.agent.ports import AgentEventObserver, ToolPortError
from glodex.application.agent.runtime import (
    AgentRuntimeConfig,
    AgentService,
)
from glodex.application.agent.tools import ShippingRule, ToolDependencies
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

type _PerRunServiceFactory = Callable[[SearchRequest], AgentService]
type _ManifestSource = DemoItemSource | LiveEbayItemSource


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
    "PerRunAgentExecutor",
    "build_agent_service",
]
