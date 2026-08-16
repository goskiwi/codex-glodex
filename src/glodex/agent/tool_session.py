"""Framework-free business authority behind the native ReAct tools.

LangGraph decides *which* native tool to call.  This module owns everything
the model must not be allowed to author: the original query, trusted intent,
retrieval vectors, candidate ownership, price facts, eligibility and the
final ``SearchService`` gate.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import zlib
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from typing import Literal

from Crypto.Cipher import AES
from pydantic import TypeAdapter, ValidationError

from glodex._json import compact_bytes, compact_dumps, loads_unique
from glodex.agent.catalog import (
    CandidateEligibility,
    CandidateManifest,
    CandidatePublicationFilter,
    CandidateStore,
    ValidatedCandidatePool,
    evaluate_candidate_pool,
)
from glodex.agent.contracts import (
    FULL_TOOL_SET,
    AgentAnswer,
    AgentAnswerKind,
    AgentCapabilities,
    AgentDemoResponse,
    AgentEventKind,
    AgentExecution,
    AgentFailureCode,
    AgentRunEvent,
    AgentRunRecord,
    AgentToolSummary,
    AgentTraceBullet,
    AgentTraceSource,
    CategoryInsightInput,
    CategoryInsightOutput,
    ChatFallbackOutput,
    ComparisonTarget,
    EmbeddingBatch,
    EmbeddingResult,
    EvidenceKind,
    ForkObjectiveCode,
    ForkReasonCode,
    InsightDepth,
    InsightStatus,
    ItemPickerOutput,
    ItemSearchInput,
    ItemSearchRuntimeResult,
    PlannerDecisionInput,
    PlannerFallbackReason,
    PlannerInput,
    PlannerIntentKind,
    PlannerOutput,
    Platform,
    PriceCompareOutput,
    PriceComparisonScope,
    PricePointStatus,
    SemanticAssertionCandidate,
    SemanticAssertionInput,
    SemanticAssertionOutput,
    ShippingCalcOutput,
    ShippingStatus,
    ShoppingSummaryOutput,
    ToolFailureCode,
    ToolName,
    WebSearchInput,
    WebSearchOutput,
)
from glodex.agent.ports import AgentEventObserver, EmbeddingPort, ToolPortError
from glodex.contracts import RunStatus, SearchRequest
from glodex.domain.catalog import CatalogBatch
from glodex.domain.evidence import EvidenceEntityType
from glodex.domain.intent import (
    BudgetCalculation,
    BudgetMax,
    Exclusion,
    InterpretedRequest,
    PreferredCriterion,
    SourceSpan,
    StockRequired,
    TargetCategory,
    budget_amount_evidence,
    budget_currency_evidence,
    validate_interpreted_request,
)
from glodex.tools.engine import (
    ChatFallbackInput,
    ItemPickerInput,
    ItemSearchToolInput,
    PriceCompareInput,
    SearchServiceFactory,
    ShippingCalcInput,
    ShippingRule,
    ShoppingSummaryInput,
    TargetCandidateGroup,
    ToolDependencies,
    execute_business_tool,
)

_LOGGER = logging.getLogger(__name__)

type CandidateManifestFactory = Callable[
    [tuple[ItemSearchRuntimeResult, ...]],
    CandidateManifest,
]
type ResourceDrainer = Callable[[], Awaitable[None]]
_CHECKPOINT_ACTION_BYTES = 2_048
_CHECKPOINT_CHUNK_CHARACTERS = 64
_CHECKPOINT_SNAPSHOT_MAX_BYTES = 4_194_304
_CHECKPOINT_SNAPSHOT_SCHEMA = "agent_session_snapshot_v4"
MODEL_RECEIPT_BYTE_LIMIT = 8 * 1_024
_RECOVERABLE_DISPATCH_CODES = frozenset(
    {
        AgentFailureCode.FORK_FAILED.value,
        AgentFailureCode.BUDGET_EXCEEDED.value,
        AgentFailureCode.INVALID_ACTION.value,
        AgentFailureCode.DEADLINE_EXCEEDED.value,
    }
)


@dataclass(frozen=True, slots=True)
class AgentLoopAction:
    """One model-selected native tool action stored in a private checkpoint."""

    tool_name: ToolName
    arguments: dict[str, object]

    def __post_init__(self) -> None:
        if type(self.tool_name) is not ToolName or type(self.arguments) is not dict:
            raise TypeError("AgentLoop checkpoint action is invalid")
        try:
            encoded = compact_bytes(self.arguments)
        except (TypeError, ValueError) as error:
            raise ValueError("AgentLoop checkpoint arguments are not JSON") from error
        if len(encoded) > _CHECKPOINT_ACTION_BYTES:
            raise ValueError("AgentLoop checkpoint action is too large")


@dataclass(frozen=True, slots=True)
class SemanticAssertionFailure:
    """One mismatch that the next Think/Reflect turn must correct."""

    platform: Platform
    target_query: str
    query: str
    top_k: int
    rejected_candidate_ids: tuple[str, ...]
    attempts: int

    def __post_init__(self) -> None:
        if type(self.platform) is not Platform:
            raise TypeError("semantic assertion failure platform is invalid")
        for name, value in (("target_query", self.target_query), ("query", self.query)):
            if type(value) is not str or not value or len(value) > 2_000:
                raise ValueError(f"semantic assertion failure {name} is invalid")
        if type(self.top_k) is not int or isinstance(self.top_k, bool) or not 1 <= self.top_k <= 50:
            raise ValueError("semantic assertion failure top_k is invalid")
        if type(self.rejected_candidate_ids) is not tuple or len(
            self.rejected_candidate_ids
        ) != len(set(self.rejected_candidate_ids)):
            raise ValueError("semantic assertion failure candidates are invalid")
        if type(self.attempts) is not int or isinstance(self.attempts, bool) or self.attempts < 1:
            raise ValueError("semantic assertion failure attempts are invalid")


@dataclass(frozen=True, slots=True)
class ChildContextSeed:
    """Verified parent facts that an isolated child may reuse privately.

    A child never receives its parent's messages or unbounded tool bodies.
    The parent resolves only the explicitly authorised opaque references into
    already-validated typed facts.  The seed is in-memory execution state;
    durable snapshots retain just the references in ``ChildTaskScope``.
    """

    context_refs: tuple[str, ...]
    plan: PlannerOutput
    interpreted_request: InterpretedRequest
    item_results: tuple[ItemSearchRuntimeResult, ...] = ()
    web_result: WebSearchOutput | None = None
    category_result: CategoryInsightOutput | None = None

    def __post_init__(self) -> None:
        if (
            type(self.context_refs) is not tuple
            or any(type(value) is not str or not value for value in self.context_refs)
            or len(self.context_refs) != len(set(self.context_refs))
            or len(self.context_refs) > 16
        ):
            raise ValueError("child context references are invalid")
        if (
            type(self.plan) is not PlannerOutput
            or self.plan.intent_kind is not PlannerIntentKind.SHOPPING
            or type(self.interpreted_request) is not InterpretedRequest
        ):
            raise TypeError("child context plan is invalid")
        if type(self.item_results) is not tuple or any(
            type(value) is not ItemSearchRuntimeResult for value in self.item_results
        ):
            raise TypeError("child context item results are invalid")
        platforms = tuple(result.platform for result in self.item_results)
        if len(platforms) != len(set(platforms)):
            raise ValueError("child context item platforms are invalid")
        if self.web_result is not None and type(self.web_result) is not WebSearchOutput:
            raise TypeError("child context web result is invalid")
        if (
            self.category_result is not None
            and type(self.category_result) is not CategoryInsightOutput
        ):
            raise TypeError("child context category result is invalid")

        available_refs = {
            candidate.candidate_id
            for result in self.item_results
            for candidate in result.candidates
        }
        if self.web_result is not None:
            available_refs.update(item.source_id for item in self.web_result.evidence)
        if self.category_result is not None and self.category_result.status is InsightStatus.FOUND:
            available_refs.add(_category_insight_reference(self.category_result))
        if not set(self.context_refs).issubset(available_refs):
            raise ValueError("child context references do not resolve to trusted facts")


@dataclass(frozen=True, slots=True)
class ChildTaskScope:
    """Runtime-owned authority for one homogeneous child AgentLoop.

    This is not a child role or a reduced tool schema.  Every child gets the
    same native tool catalogue and system prompt; the scope only bounds data
    and spend. Nested forks inherit that scope and the shared tree child/depth
    guard; each Loop keeps its own bounded model/tool counters.
    """

    allowed_platforms: tuple[Platform, ...]
    context_refs: tuple[str, ...]
    objective: ForkObjectiveCode
    reason: ForkReasonCode
    estimated_tool_calls: int
    depth: int
    context_seed: ChildContextSeed
    max_model_actions: int = 8
    max_tool_calls: int = 8
    allow_nested_fork: bool = True

    def __post_init__(self) -> None:
        if (
            type(self.allowed_platforms) is not tuple
            or not self.allowed_platforms
            or any(type(platform) is not Platform for platform in self.allowed_platforms)
            or len(self.allowed_platforms) != len(set(self.allowed_platforms))
        ):
            raise ValueError("child task scope platforms are invalid")
        if type(self.objective) is not ForkObjectiveCode or type(self.reason) is not ForkReasonCode:
            raise TypeError("child task scope objective is invalid")
        if (
            type(self.context_refs) is not tuple
            or any(type(value) is not str or not value for value in self.context_refs)
            or len(self.context_refs) != len(set(self.context_refs))
            or len(self.context_refs) > 16
        ):
            raise ValueError("child task scope context references are invalid")
        if (
            type(self.context_seed) is not ChildContextSeed
            or self.context_seed.context_refs != self.context_refs
        ):
            raise ValueError("child task scope context seed is invalid")
        if any(
            result.platform not in self.allowed_platforms
            for result in self.context_seed.item_results
        ):
            raise ValueError("child context item platform is outside task scope")
        for name, value in (
            ("estimated_tool_calls", self.estimated_tool_calls),
            ("depth", self.depth),
            ("max_model_actions", self.max_model_actions),
            ("max_tool_calls", self.max_tool_calls),
        ):
            if type(value) is not int or isinstance(value, bool) or value < 1:
                raise ValueError(f"child task scope {name} is invalid")
        if self.depth < 1 or type(self.allow_nested_fork) is not bool:
            raise ValueError("child task scope nesting is invalid")


@dataclass(frozen=True, slots=True)
class ChildHandoff:
    """Structured internal result returned by a completed child loop.

    It deliberately contains verified typed facts and stable identifiers, not
    the child's messages, prompt, chain of thought or raw provider body.
    """

    child_run_id: str
    status: Literal["COMPLETED", "FAILED", "ABORTED"]
    item_results: tuple[ItemSearchRuntimeResult, ...] = ()
    web_result: WebSearchOutput | None = None
    category_result: CategoryInsightOutput | None = None
    price_result: PriceCompareOutput | None = None
    shipping_result: ShippingCalcOutput | None = None
    picker_result: ItemPickerOutput | None = None
    publication_eligible_ids: tuple[str, ...] = ()
    semantic_assertion_failures: tuple[SemanticAssertionFailure, ...] = ()
    completed_tools: tuple[ToolName, ...] = ()
    candidate_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    safe_code: str | None = None

    def __post_init__(self) -> None:
        if type(self.child_run_id) is not str or not self.child_run_id:
            raise ValueError("child handoff run ID is invalid")
        if self.status not in {"COMPLETED", "FAILED", "ABORTED"}:
            raise ValueError("child handoff status is invalid")
        if type(self.item_results) is not tuple or any(
            type(value) is not ItemSearchRuntimeResult for value in self.item_results
        ):
            raise TypeError("child handoff item results are invalid")
        if self.web_result is not None and type(self.web_result) is not WebSearchOutput:
            raise TypeError("child handoff web result is invalid")
        if (
            self.category_result is not None
            and type(self.category_result) is not CategoryInsightOutput
        ):
            raise TypeError("child handoff category result is invalid")
        if self.price_result is not None and type(self.price_result) is not PriceCompareOutput:
            raise TypeError("child handoff price result is invalid")
        if (
            self.shipping_result is not None
            and type(self.shipping_result) is not ShippingCalcOutput
        ):
            raise TypeError("child handoff shipping result is invalid")
        if self.picker_result is not None and type(self.picker_result) is not ItemPickerOutput:
            raise TypeError("child handoff picker result is invalid")
        if self.shipping_result is not None and self.price_result is None:
            raise ValueError("child handoff shipping result requires price comparison")
        if self.picker_result is not None and self.shipping_result is None:
            raise ValueError("child handoff picker result requires shipping eligibility")
        if type(self.semantic_assertion_failures) is not tuple or any(
            type(value) is not SemanticAssertionFailure
            for value in self.semantic_assertion_failures
        ):
            raise TypeError("child handoff semantic assertion failures are invalid")
        if (
            type(self.completed_tools) is not tuple
            or any(type(value) is not ToolName for value in self.completed_tools)
            or len(self.completed_tools) != len(set(self.completed_tools))
        ):
            raise ValueError("child handoff completed tools are invalid")
        for values, name in (
            (self.candidate_ids, "candidate IDs"),
            (self.evidence_ids, "evidence IDs"),
            (self.publication_eligible_ids, "publication-eligible IDs"),
        ):
            if (
                type(values) is not tuple
                or any(type(value) is not str or not value for value in values)
                or len(values) != len(set(values))
                or len(values) > 32
            ):
                raise ValueError(f"child handoff {name} are invalid")
        if not set(self.publication_eligible_ids).issubset(self.candidate_ids):
            raise ValueError("child handoff publication IDs must resolve to candidates")
        if self.safe_code is not None and (type(self.safe_code) is not str or not self.safe_code):
            raise ValueError("child handoff safe code is invalid")


type DispatchExecutor = Callable[[], Awaitable[tuple[ChildHandoff, ...]]]


class ToolSessionError(RuntimeError):
    """A stable tool/session failure with no provider or model text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class AgentRuntimeConfig:
    """Trusted runtime facts and operational budgets for native tool inputs.

    Fork budgets are safety guardrails, not an Agent-role topology. Every
    loop receives the same tool schema and may fork a smaller homogeneous
    scope while the shared tree child/depth guard remains available. Model,
    tool and fork-batch counters are deliberately bounded per Loop.
    """

    capabilities: AgentCapabilities
    index_version: str
    ruleset_version: str
    calculation_date: str
    shipping_rules: tuple[ShippingRule, ...]
    fx_source_batch: CatalogBatch
    max_child_runs: int = 10
    max_parallel_children: int = 8
    max_fork_batches: int = 10

    def __post_init__(self) -> None:
        if type(self.capabilities) is not AgentCapabilities:
            raise TypeError("runtime capabilities must be exact AgentCapabilities")
        for name, value in (
            ("index_version", self.index_version),
            ("ruleset_version", self.ruleset_version),
        ):
            if type(value) is not str or not value:
                raise ValueError(f"runtime {name} must be non-empty")
        if type(self.calculation_date) is not str:
            raise TypeError("runtime calculation_date must be a string")
        try:
            if date.fromisoformat(self.calculation_date).isoformat() != self.calculation_date:
                raise ValueError
        except ValueError:
            raise ValueError("runtime calculation_date must be strict YYYY-MM-DD") from None
        if type(self.shipping_rules) is not tuple or any(
            type(rule) is not ShippingRule for rule in self.shipping_rules
        ):
            raise TypeError("runtime shipping_rules must contain exact rules")
        if type(self.fx_source_batch) is not CatalogBatch:
            raise TypeError("runtime FX source must be an exact CatalogBatch")
        for limit_name, limit in (
            ("max_child_runs", self.max_child_runs),
            ("max_parallel_children", self.max_parallel_children),
            ("max_fork_batches", self.max_fork_batches),
        ):
            if type(limit) is not int or isinstance(limit, bool) or not 1 <= limit <= 10:
                raise ValueError(f"runtime {limit_name} must be an integer from 1 to 10")
        if self.max_parallel_children > self.max_child_runs:
            raise ValueError("runtime parallel child budget exceeds tree child budget")


@dataclass(frozen=True, slots=True)
class _SessionState:
    """Trusted state that is never serialized into a model message."""

    request: SearchRequest
    interpreted_request: InterpretedRequest | None
    required_baseline: InterpretedRequest | None
    capabilities: AgentCapabilities
    plan: PlannerOutput | None = None
    category_result: CategoryInsightOutput | None = None
    category_depth: InsightDepth | None = None
    web_result: WebSearchOutput | None = None
    item_results: tuple[ItemSearchRuntimeResult, ...] = ()
    semantic_assertion_failures: tuple[SemanticAssertionFailure, ...] = ()
    price_result: PriceCompareOutput | None = None
    shipping_result: ShippingCalcOutput | None = None
    publication_eligible_ids: tuple[str, ...] = ()
    picker_result: ItemPickerOutput | None = None


@dataclass(frozen=True, slots=True)
class AgentSessionSnapshot:
    """Encrypted, typed business state used for lossless AgentLoop recovery."""

    state: _SessionState
    completed_actions: tuple[AgentLoopAction, ...]
    pending_action: AgentLoopAction | None
    tool_counts: tuple[tuple[ToolName, int], ...]
    tool_outcomes: tuple[tuple[ToolName, str], ...]
    completed_receipts: tuple[str, ...]
    tool_executions: int
    embedding_batches: int
    tavily_calls: int
    child_runs: int
    fork_batches: int
    response: AgentDemoResponse | None
    handoff_ready: bool
    failure_code: str | None


_SESSION_SNAPSHOT_ADAPTER = TypeAdapter(AgentSessionSnapshot)


@dataclass(frozen=True, slots=True)
class AgentSessionCheckpointCodec:
    """AES-EAX codec for private typed state stored beside the safe journal."""

    key: bytes

    def __post_init__(self) -> None:
        if type(self.key) is not bytes or len(self.key) not in {16, 24, 32}:
            raise ValueError("Agent session checkpoint key must be 16, 24, or 32 bytes")

    def encode(self, snapshot: AgentSessionSnapshot) -> dict[str, object]:
        if type(snapshot) is not AgentSessionSnapshot:
            raise TypeError("Agent session checkpoint snapshot is invalid")
        compressed = zlib.compress(_SESSION_SNAPSHOT_ADAPTER.dump_json(snapshot), level=9)
        cipher = AES.new(self.key, AES.MODE_EAX)
        ciphertext, tag = cipher.encrypt_and_digest(compressed)
        encoded = base64.urlsafe_b64encode(cipher.nonce + tag + ciphertext).decode("ascii")
        encoded = encoded.rstrip("=")
        chunks = [
            encoded[start : start + _CHECKPOINT_CHUNK_CHARACTERS]
            for start in range(0, len(encoded), _CHECKPOINT_CHUNK_CHARACTERS)
        ]
        pages = [chunks[start : start + 64] for start in range(0, len(chunks), 64)]
        if not pages or len(pages) > 64:
            raise ValueError("Agent session checkpoint snapshot exceeds its safe bound")
        return {"schema": _CHECKPOINT_SNAPSHOT_SCHEMA, "pages": pages}

    def decode(self, value: object) -> AgentSessionSnapshot:
        if type(value) is not dict or set(value) != {"schema", "pages"}:
            raise ValueError("Agent session checkpoint snapshot shape is invalid")
        pages = value["pages"]
        if (
            value["schema"] != _CHECKPOINT_SNAPSHOT_SCHEMA
            or type(pages) is not list
            or not pages
            or len(pages) > 64
            or any(type(page) is not list or not page or len(page) > 64 for page in pages)
        ):
            raise ValueError("Agent session checkpoint snapshot is invalid")
        chunks = [chunk for page in pages for chunk in page]
        alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        if any(
            type(chunk) is not str
            or not chunk
            or len(chunk) > _CHECKPOINT_CHUNK_CHARACTERS
            or any(character not in alphabet for character in chunk)
            for chunk in chunks
        ):
            raise ValueError("Agent session checkpoint snapshot encoding is invalid")
        encoded = "".join(chunks)
        try:
            encrypted = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            if len(encrypted) < 33:
                raise ValueError
            cipher = AES.new(self.key, AES.MODE_EAX, nonce=encrypted[:16])
            compressed = cipher.decrypt_and_verify(encrypted[32:], encrypted[16:32])
            inflater = zlib.decompressobj()
            plaintext = inflater.decompress(compressed, _CHECKPOINT_SNAPSHOT_MAX_BYTES + 1)
            if inflater.unconsumed_tail or len(plaintext) > _CHECKPOINT_SNAPSHOT_MAX_BYTES:
                raise ValueError
            plaintext += inflater.flush()
            if len(plaintext) > _CHECKPOINT_SNAPSHOT_MAX_BYTES:
                raise ValueError
            snapshot = _SESSION_SNAPSHOT_ADAPTER.validate_json(plaintext)
        except Exception as error:
            raise ValueError("Agent session checkpoint snapshot cannot be decoded") from error
        if type(snapshot) is not AgentSessionSnapshot:
            raise ValueError("Agent session checkpoint snapshot type is invalid")
        return snapshot


@dataclass(slots=True)
class ShoppingToolSession:
    """One mutable, serialized authority for one native-tool Agent run."""

    request: SearchRequest
    run_id: str
    config: AgentRuntimeConfig
    tool_dependencies: ToolDependencies
    embedding_port: EmbeddingPort | None
    candidate_manifest_factory: CandidateManifestFactory
    search_service_factory: SearchServiceFactory
    checkpoint_codec: AgentSessionCheckpointCodec
    observer: AgentEventObserver | None = None
    publication_filter: CandidatePublicationFilter | None = None
    preference_text: str | None = None
    resource_drainer: ResourceDrainer | None = None
    task_scope: ChildTaskScope | None = None
    dispatch_enabled: bool = False
    _state: _SessionState | None = field(default=None, init=False)
    _candidate_store: CandidateStore | None = field(default=None, init=False)
    _eligibility: CandidateEligibility | None = field(default=None, init=False)
    _query_vectors: dict[str, tuple[float, ...]] = field(default_factory=dict, init=False)
    _tool_counts: dict[ToolName, int] = field(default_factory=dict, init=False)
    _tool_outcomes: dict[ToolName, str] = field(default_factory=dict, init=False)
    _events: list[AgentRunEvent] = field(default_factory=list, init=False)
    _model_calls: int = field(default=0, init=False)
    _tool_executions: int = field(default=0, init=False)
    _embedding_batches: int = field(default=0, init=False)
    _tavily_calls: int = field(default=0, init=False)
    _current_round: int | None = field(default=None, init=False)
    _selected_tool: ToolName | None = field(default=None, init=False)
    _model_streaming_emitted: bool = field(default=False, init=False)
    _tool_started_in_round: bool = field(default=False, init=False)
    _completed_actions: list[AgentLoopAction] = field(default_factory=list, init=False)
    _completed_receipts: list[str] = field(default_factory=list, init=False)
    _pending_action: AgentLoopAction | None = field(default=None, init=False)
    _response: AgentDemoResponse | None = field(default=None, init=False)
    _handoff_ready: bool = field(default=False, init=False)
    _failure_code: str | None = field(default=None, init=False)
    _child_runs: int = field(default=0, init=False)
    _fork_batches: int = field(default=0, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _closed: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if type(self.request) is not SearchRequest:
            raise TypeError("tool session requires an exact SearchRequest")
        if type(self.run_id) is not str or not self.run_id:
            raise ValueError("tool session run_id must be non-empty")
        if type(self.config) is not AgentRuntimeConfig:
            raise TypeError("tool session requires exact runtime configuration")
        if type(self.tool_dependencies) is not ToolDependencies:
            raise TypeError("tool session requires exact tool dependencies")
        if self.embedding_port is not None and not callable(
            getattr(self.embedding_port, "embed", None)
        ):
            raise TypeError("tool session embedding port is invalid")
        if not callable(self.candidate_manifest_factory) or not callable(
            self.search_service_factory
        ):
            raise TypeError("tool session factories must be callable")
        if type(self.checkpoint_codec) is not AgentSessionCheckpointCodec:
            raise TypeError("tool session checkpoint codec is invalid")
        if self.observer is not None and not callable(getattr(self.observer, "on_event", None)):
            raise TypeError("tool session observer is invalid")
        if self.publication_filter is not None and not callable(
            getattr(self.publication_filter, "excluded_candidate_ids", None)
        ):
            raise TypeError("tool session publication filter is invalid")
        if self.preference_text is not None and (
            type(self.preference_text) is not str
            or not self.preference_text
            or len(self.preference_text) > 2_000
            or "\0" in self.preference_text
        ):
            raise ValueError("tool session preference text is invalid")
        if self.resource_drainer is not None and not callable(self.resource_drainer):
            raise TypeError("tool session resource drainer is invalid")
        if self.task_scope is not None and type(self.task_scope) is not ChildTaskScope:
            raise TypeError("tool session task scope is invalid")
        if type(self.dispatch_enabled) is not bool:
            raise TypeError("tool session dispatch flag is invalid")

    @property
    def events(self) -> tuple[AgentRunEvent, ...]:
        return tuple(self._events)

    @property
    def failed(self) -> bool:
        return self._failure_code is not None

    @property
    def failure_code(self) -> str | None:
        return self._failure_code

    def fail(self, code: str) -> None:
        """Mark the run terminally failed with one stable, public-safe code."""

        if type(code) is not str or not code:
            raise TypeError("tool session failure code must be non-empty")
        self._fail(code)

    @property
    def terminal_response(self) -> AgentDemoResponse | None:
        return self._response

    @property
    def handoff_ready(self) -> bool:
        """Whether this child completed local work and returned to its root."""

        return self._handoff_ready

    async def open(self) -> None:
        """Open an empty trusted state before the first model Think step."""

        if self._state is not None:
            raise RuntimeError("tool session is already open")
        seed = None if self.task_scope is None else self.task_scope.context_seed
        self._state = _SessionState(
            request=self.request,
            interpreted_request=None if seed is None else seed.interpreted_request,
            required_baseline=None if seed is None else seed.interpreted_request,
            capabilities=self.config.capabilities,
            plan=None if seed is None else seed.plan,
            item_results=(() if seed is None else seed.item_results),
            web_result=(None if seed is None else seed.web_result),
            category_result=(None if seed is None else seed.category_result),
            category_depth=(
                None
                if seed is None or seed.category_result is None
                else _inferred_category_depth(seed.category_result)
            ),
        )

    def agent_started(self) -> None:
        self._emit(AgentRunEvent(kind=AgentEventKind.AGENT_STARTED, run_id=self.run_id))

    def model_started(self) -> None:
        """Map an ``astream_events`` model start to one safe public event."""

        if self.failed or self._response is not None:
            return
        if self._model_calls >= self._model_action_limit():
            self._fail(AgentFailureCode.BUDGET_EXCEEDED.value)
            return
        self._model_calls += 1
        self._current_round = self._model_calls
        self._selected_tool = None
        self._model_streaming_emitted = False
        self._tool_started_in_round = False
        self._emit(
            AgentRunEvent(
                kind=AgentEventKind.MODEL_STARTED,
                run_id=self.run_id,
                round=self._model_calls,
            )
        )

    def model_streaming(self) -> None:
        """Publish one safe streaming signal for the current model round.

        Raw provider chunks can contain partial tool arguments or private model
        text.  The public contract records only that this round produced a live
        stream, and deduplicates the potentially large token sequence.
        """

        if (
            self.failed
            or self._response is not None
            or self._current_round is None
            or self._selected_tool is not None
            or self._model_streaming_emitted
        ):
            return
        self._model_streaming_emitted = True
        self._emit(
            AgentRunEvent(
                kind=AgentEventKind.MODEL_STREAMING,
                run_id=self.run_id,
                round=self._current_round,
            )
        )

    def model_finished(self, tool_calls: object) -> None:
        """Accept exactly one model-selected native action."""

        if self.failed or self._response is not None:
            return
        if self._current_round is None or type(tool_calls) is not list:
            self._fail(AgentFailureCode.MODEL_INVALID.value)
            return
        if len(tool_calls) != 1:
            self._fail(AgentFailureCode.INVALID_ACTION.value)
            return
        parsed: list[tuple[ToolName, dict[object, object]]] = []
        for call in tool_calls:
            name = call.get("name") if type(call) is dict else None
            arguments = call.get("args") if type(call) is dict else None
            if type(name) is not str or type(arguments) is not dict:
                self._fail(AgentFailureCode.INVALID_ACTION.value)
                return
            try:
                candidate = ToolName(name)
            except (TypeError, ValueError):
                self._fail(AgentFailureCode.INVALID_ACTION.value)
                return
            parsed.append((candidate, call))
        tool_name, call = parsed[0]
        arguments = call["args"]
        assert type(arguments) is dict
        self._selected_tool = tool_name
        try:
            self._pending_action = AgentLoopAction(
                tool_name=tool_name,
                arguments=loads_unique(compact_dumps(arguments)),
            )
        except (TypeError, ValueError):
            self._fail(AgentFailureCode.MODEL_INVALID.value)
            return
        self._emit(
            AgentRunEvent(
                kind=AgentEventKind.MODEL_FINISHED,
                run_id=self.run_id,
                round=self._current_round,
                tool_name=tool_name,
            )
        )

    def native_tools(self) -> tuple[ToolName, ...]:
        """Return the identical native schema for root and homogeneous children."""

        if self.failed or self._response is not None or self._state is None:
            return ()
        return FULL_TOOL_SET

    def _unmet_preconditions(
        self,
        tool_name: ToolName,
        arguments: Mapping[str, object],
    ) -> tuple[str, ...]:
        """Validate one proposed side effect without turning state into a route.

        This method answers only whether *this* invocation can safely run. It
        never derives or returns a list of "next" tools: the complete native
        schema stays bound to the model on every Think turn.
        """

        expected_arguments: dict[ToolName, tuple[str, ...]] = {
            ToolName.PLANNER: ("decision_json",),
            ToolName.CHAT_FALLBACK: (),
            ToolName.WEB_SEARCH: ("evidence_kind",),
            ToolName.CATEGORY_INSIGHT: ("category", "depth"),
            ToolName.ITEM_SEARCH: ("platform", "query"),
            ToolName.PRICE_COMPARE: (),
            ToolName.SHIPPING_CALC: (),
            ToolName.ITEM_PICKER: (),
            ToolName.SHOPPING_SUMMARY: (),
            ToolName.DISPATCH_TOOL: (),
            ToolName.PARALLEL_DISPATCH_TOOL: (),
        }
        if tuple(sorted(arguments)) != expected_arguments[tool_name]:
            return ("native_tool_schema",)

        state = self._require_state()
        plan = state.plan
        if tool_name is ToolName.PLANNER:
            if plan is not None:
                return ("trusted_plan_already_exists",)
            try:
                decision_json = arguments["decision_json"]
                if type(decision_json) is not str:
                    raise TypeError("planner decision must be serialized JSON")
                decision = PlannerDecisionInput.model_validate_json(decision_json)
            except (KeyError, TypeError, ValueError, ValidationError):
                return ("grounded_planner_decision",)
            if not _coordinated_preferences_are_complete(state.request.query, decision):
                return ("complete_coordinated_preferences",)
            return ()
        if plan is None:
            return ("trusted_plan",)
        if plan.intent_kind is PlannerIntentKind.UNSUPPORTED_OR_NON_SHOPPING:
            return () if tool_name is ToolName.CHAT_FALLBACK else ("shopping_intent",)
        if (
            state.category_result is not None
            and state.category_result.status is InsightStatus.NO_INSIGHT
        ):
            if tool_name is ToolName.WEB_SEARCH:
                if not state.capabilities.web_search_enabled:
                    return ("web_search_capability",)
                return (
                    () if state.web_result is None else ("independent_evidence_already_observed",)
                )
            if not state.capabilities.web_search_enabled:
                return () if tool_name is ToolName.CHAT_FALLBACK else ("cold_start_evidence",)
            if state.web_result is None:
                return ("cold_start_evidence",)
            if not state.web_result.evidence:
                return () if tool_name is ToolName.CHAT_FALLBACK else ("cold_start_evidence",)
        if not state.capabilities.embedding_enabled:
            return () if tool_name is ToolName.CHAT_FALLBACK else ("embedding_capability",)

        if self.task_scope is not None:
            return self._child_unmet_preconditions(
                tool_name=tool_name,
                arguments=arguments,
                state=state,
            )

        if tool_name is ToolName.CHAT_FALLBACK:
            return ("fallback_condition",)
        if tool_name is ToolName.WEB_SEARCH:
            if not state.capabilities.web_search_enabled:
                return ("web_search_capability",)
            return () if state.web_result is None else ("independent_evidence_already_observed",)
        if tool_name is ToolName.CATEGORY_INSIGHT:
            try:
                category = _argument_text(arguments["category"])
                depth = InsightDepth(_argument_text(arguments["depth"]))
            except (KeyError, ToolSessionError):
                return ("grounded_category_phrase",)
            except ValueError:
                return ("category_insight_depth",)
            if not _category_phrase_is_grounded(state.request.query, category):
                return ("grounded_category_phrase",)
            return (
                ()
                if _category_insight_can_run(state, category=category, depth=depth)
                else ("category_insight_already_observed",)
            )
        if tool_name is ToolName.ITEM_SEARCH:
            if state.picker_result is not None:
                return ("candidate_selection_already_observed",)
            if (
                state.category_result is not None
                and state.category_result.status is InsightStatus.FOUND
                and state.category_result.confidence < Decimal("0.5")
                and state.capabilities.web_search_enabled
                and state.web_result is None
            ):
                return ("independent_category_evidence",)
            try:
                platform = Platform(_argument_text(arguments["platform"]))
                query = _argument_text(arguments["query"]).strip()
            except (ToolSessionError, ValueError):
                return ("planned_platform",)
            if not query or len(query) > 512:
                return ("retrieval_query",)
            next_search = _next_item_search(
                state,
                platform=platform,
                allow_refinement=self._item_search_can_refine(state, platform=platform),
            )
            if next_search is not None:
                target_query, _ = next_search
                previous_failure = next(
                    (
                        item
                        for item in state.semantic_assertion_failures
                        if item.platform is platform and item.target_query == target_query
                    ),
                    None,
                )
                if previous_failure is not None and query == previous_failure.query:
                    return ("revised_retrieval_query",)
                if query in self._item_search_queries(state, platform=platform):
                    return ("revised_retrieval_query",)
            return (
                ()
                if platform in self._available_item_search_platforms(state)
                else ("unobserved_planned_platform",)
            )
        if tool_name is ToolName.PRICE_COMPARE:
            if not state.item_results:
                return ("trusted_candidates",)
            if any(failure.attempts < 2 for failure in state.semantic_assertion_failures):
                return ("semantically_validated_candidates",)
            return () if state.price_result is None else ("price_comparison_already_observed",)
        if tool_name is ToolName.SHIPPING_CALC:
            if state.price_result is None:
                return ("trusted_price_comparison",)
            return () if state.shipping_result is None else ("shipping_advisory_already_observed",)
        if tool_name is ToolName.ITEM_PICKER:
            if state.price_result is None:
                return ("trusted_price_comparison",)
            if state.shipping_result is None or self._eligibility is None:
                return ("trusted_shipping_eligibility",)
            return () if state.picker_result is None else ("candidate_selection_already_observed",)
        if tool_name is ToolName.SHOPPING_SUMMARY:
            return (
                ()
                if self._eligibility is not None and state.picker_result is not None
                else ("trusted_selection",)
            )
        if tool_name in {ToolName.DISPATCH_TOOL, ToolName.PARALLEL_DISPATCH_TOOL}:
            return () if self._can_offer_dispatch(state) else ("fork_authority",)
        return ("native_tool_schema",)

    def _child_unmet_preconditions(
        self,
        *,
        tool_name: ToolName,
        arguments: Mapping[str, object],
        state: _SessionState,
    ) -> tuple[str, ...]:
        """Apply the common tool schema inside the child's immutable data scope."""

        if tool_name is ToolName.CHAT_FALLBACK:
            return ("fallback_condition",)
        if tool_name is ToolName.WEB_SEARCH:
            if not state.capabilities.web_search_enabled:
                return ("web_search_capability",)
            return () if state.web_result is None else ("independent_evidence_already_observed",)
        if tool_name is ToolName.CATEGORY_INSIGHT:
            try:
                category = _argument_text(arguments["category"])
                depth = InsightDepth(_argument_text(arguments["depth"]))
            except (KeyError, ToolSessionError):
                return ("grounded_category_phrase",)
            except ValueError:
                return ("category_insight_depth",)
            if not _category_phrase_is_grounded(state.request.query, category):
                return ("grounded_category_phrase",)
            return (
                ()
                if _category_insight_can_run(state, category=category, depth=depth)
                else ("category_insight_already_observed",)
            )
        if tool_name is ToolName.ITEM_SEARCH:
            if (
                state.category_result is not None
                and state.category_result.status is InsightStatus.FOUND
                and state.category_result.confidence < Decimal("0.5")
                and state.capabilities.web_search_enabled
                and state.web_result is None
            ):
                return ("independent_category_evidence",)
            try:
                platform = Platform(_argument_text(arguments["platform"]))
                query = _argument_text(arguments["query"]).strip()
            except (ToolSessionError, ValueError):
                return ("scoped_platform",)
            if not query or len(query) > 512:
                return ("retrieval_query",)
            next_search = _next_item_search(
                state,
                platform=platform,
                allow_refinement=self._item_search_can_refine(state, platform=platform),
            )
            if next_search is not None:
                target_query, _ = next_search
                previous_failure = next(
                    (
                        item
                        for item in state.semantic_assertion_failures
                        if item.platform is platform and item.target_query == target_query
                    ),
                    None,
                )
                if previous_failure is not None and query == previous_failure.query:
                    return ("revised_retrieval_query",)
                if query in self._item_search_queries(state, platform=platform):
                    return ("revised_retrieval_query",)
            return (
                ()
                if platform in self._available_item_search_platforms(state)
                else ("unobserved_scoped_platform",)
            )
        if tool_name is ToolName.PRICE_COMPARE:
            if not state.item_results:
                return ("trusted_candidates",)
            if any(failure.attempts < 2 for failure in state.semantic_assertion_failures):
                return ("semantically_validated_candidates",)
            return () if state.price_result is None else ("price_comparison_already_observed",)
        if tool_name is ToolName.SHIPPING_CALC:
            if state.price_result is None:
                return ("trusted_price_comparison",)
            return () if state.shipping_result is None else ("shipping_advisory_already_observed",)
        if tool_name is ToolName.ITEM_PICKER:
            if state.price_result is None:
                return ("trusted_price_comparison",)
            if state.shipping_result is None or self._eligibility is None:
                return ("trusted_shipping_eligibility",)
            return () if state.picker_result is None else ("candidate_selection_already_observed",)
        if tool_name is ToolName.SHOPPING_SUMMARY:
            return () if self._child_has_completed_work() else ("verified_local_work",)
        if tool_name in {ToolName.DISPATCH_TOOL, ToolName.PARALLEL_DISPATCH_TOOL}:
            return () if self._can_offer_dispatch(state) else ("fork_authority",)
        return ("native_tool_schema",)

    def _reject_selected_tool(self, tool_name: ToolName, unmet_facts: tuple[str, ...]) -> str:
        """Return a bounded reflection observation without running business code."""

        pending_action = self._pending_action
        if (
            self._selected_tool is not tool_name
            or self._tool_started_in_round
            or pending_action is None
            or pending_action.tool_name is not tool_name
        ):
            raise ToolSessionError(AgentFailureCode.INVALID_ACTION.value)
        self._tool_started_in_round = True
        self._emit(
            AgentRunEvent(
                kind=AgentEventKind.TOOL_STARTED,
                run_id=self.run_id,
                tool_name=tool_name,
            )
        )
        self._emit_tool_finished(tool_name, AgentFailureCode.INVALID_ACTION.value)
        receipt = _receipt(
            tool_name=tool_name,
            status="rejected",
            observation={
                "reason": "PRECONDITION_NOT_MET",
                "unmet_facts": list(unmet_facts),
                "trusted_state": self._safe_observation(),
                "instruction": (
                    "Reflect on the trusted state, correct this invocation when its arguments "
                    "are incomplete, or choose a different native action."
                ),
            },
        )
        # A rejected invocation has no business side effect, but its observation
        # is still a confirmed AgentLoop turn that the next Reflect step needs.
        self._completed_actions.append(pending_action)
        self._completed_receipts.append(receipt)
        self._pending_action = None
        return receipt

    async def invoke_dispatch(
        self,
        executor: DispatchExecutor,
        *,
        tool_name: ToolName,
    ) -> str:
        """Execute the only meta-tool that may create homogeneous children.

        The root/child graph remains responsible for choosing this tool.  This
        session owns the same start/finish accounting and the verified merge
        boundary that ordinary tools use, while the supplied coordinator owns
        child identities, concurrency and durable lifecycle events.
        """

        if not callable(executor) or tool_name not in {
            ToolName.DISPATCH_TOOL,
            ToolName.PARALLEL_DISPATCH_TOOL,
        }:
            raise TypeError("dispatch executor must be callable")
        async with self._lock:
            repeated_receipt = self._reject_repeated_action(tool_name)
            if repeated_receipt is not None:
                return repeated_receipt
            unmet_facts = self._unmet_preconditions(tool_name, {})
            if unmet_facts:
                return self._reject_selected_tool(tool_name, unmet_facts)
            try:
                self._begin_tool(tool_name)
                handoffs = await executor()
                self._merge_child_handoffs(handoffs)
            except asyncio.CancelledError:
                self._record_tool(tool_name, AgentFailureCode.RUN_ABORTED.value)
                self._emit_tool_finished(tool_name, AgentFailureCode.RUN_ABORTED.value)
                raise
            except Exception as error:
                code = _safe_code(error)
                self._record_tool(tool_name, code)
                self._emit_tool_finished(tool_name, code)
                if code not in _RECOVERABLE_DISPATCH_CODES:
                    self._fail(code)
                    return _receipt(tool_name=tool_name, status="failed", safe_code=code)
                if self._pending_action is None or self._pending_action.tool_name is not tool_name:
                    self._fail(AgentFailureCode.INTERNAL_ERROR.value)
                    return _receipt(
                        tool_name=tool_name,
                        status="failed",
                        safe_code=AgentFailureCode.INTERNAL_ERROR.value,
                    )
                self._completed_actions.append(self._pending_action)
                self._pending_action = None
                receipt = _receipt(
                    tool_name=tool_name,
                    status="failed",
                    safe_code=code,
                    observation={
                        "instruction": (
                            "The fork did not complete. Continue in the current loop, narrow "
                            "the work, or choose another native action."
                        )
                    },
                )
                self._completed_receipts.append(receipt)
                return receipt
            self._record_tool(tool_name, "SUCCESS")
            self._emit_tool_finished(tool_name, "SUCCESS")
            if self._pending_action is None or self._pending_action.tool_name is not tool_name:
                self._fail(AgentFailureCode.INTERNAL_ERROR.value)
                return _receipt(tool_name=tool_name, status="failed")
            self._completed_actions.append(self._pending_action)
            self._pending_action = None
            if self._handoff_ready:
                receipt = _receipt(
                    tool_name=tool_name,
                    status="handoff",
                    observation=self._safe_observation(),
                )
            else:
                receipt = _receipt(
                    tool_name=tool_name,
                    status="ok",
                    child_count=len(handoffs),
                    observation=self._safe_observation(handoffs=handoffs),
                )
            self._completed_receipts.append(receipt)
            return receipt

    def fork_context(self) -> tuple[tuple[Platform, ...], tuple[str, ...]]:
        """Return only verified state needed to authorise a fork demand."""

        state = self._require_state()
        refs: list[str] = []
        for result in state.item_results:
            refs.extend(candidate.candidate_id for candidate in result.candidates)
        if state.web_result is not None:
            refs.extend(item.source_id for item in state.web_result.evidence)
        if (
            state.category_result is not None
            and state.category_result.status is InsightStatus.FOUND
        ):
            refs.append(_category_insight_reference(state.category_result))
        return (
            self._planned_platforms(),
            tuple(dict.fromkeys(refs))[:16],
        )

    def model_context(self) -> dict[str, object]:
        """Return one bounded structured state projection for the next model turn."""

        payload: dict[str, object] = {
            "completed_tools": [tool.value for tool in self._tool_counts],
            "tool_outcomes": {tool.value: outcome for tool, outcome in self._tool_outcomes.items()},
            "observation": _compact_observation(self._safe_observation()),
        }
        loop_warning = self._loop_warning()
        if loop_warning is not None:
            payload["loop_warning"] = loop_warning
        if len(compact_bytes(payload)) <= MODEL_RECEIPT_BYTE_LIMIT:
            return payload
        compact = {
            "completed_tools": payload["completed_tools"],
            "tool_outcomes": payload["tool_outcomes"],
            "observation": {"compacted": True},
        }
        if loop_warning is not None:
            compact["loop_warning"] = loop_warning
        return compact

    def _loop_warning(self) -> dict[str, object] | None:
        """Warn about tool-level oscillation that is not an exact repeat."""

        recent = self._completed_actions[-6:]
        counts = Counter(action.tool_name for action in recent)
        repeated = next((tool for tool, count in counts.items() if count >= 4), None)
        if repeated is None:
            return None
        return {
            "code": AgentFailureCode.LOOP_DETECTED.value,
            "tool": repeated.value,
            "instruction": (
                "This tool appeared at least four times in the last six completed actions. "
                "Choose a different route or terminate with a trusted summary."
            ),
        }

    def child_context_seed(
        self,
        *,
        context_refs: tuple[str, ...],
        allowed_platforms: tuple[Platform, ...],
    ) -> ChildContextSeed:
        """Resolve an authorised reference set into private typed child state.

        This is the sole parent-to-child context bridge.  It intentionally
        returns no model messages, tool receipts or free-form text.
        """

        if (
            type(context_refs) is not tuple
            or any(type(value) is not str or not value for value in context_refs)
            or len(context_refs) != len(set(context_refs))
        ):
            raise ToolSessionError(AgentFailureCode.INVALID_ACTION.value)
        if (
            type(allowed_platforms) is not tuple
            or not allowed_platforms
            or any(type(platform) is not Platform for platform in allowed_platforms)
            or len(allowed_platforms) != len(set(allowed_platforms))
        ):
            raise ToolSessionError(AgentFailureCode.INVALID_ACTION.value)
        state = self._require_state()
        if state.plan is None or state.interpreted_request is None:
            raise ToolSessionError(AgentFailureCode.INVALID_ACTION.value)
        refs = set(context_refs)
        item_results = tuple(
            scoped
            for result in state.item_results
            if (
                scoped := _slice_item_result(
                    result,
                    selected_candidate_ids=refs,
                )
            )
            is not None
        )
        if any(result.platform not in allowed_platforms for result in item_results):
            raise ToolSessionError(AgentFailureCode.INVALID_ACTION.value)
        web_result = None
        if state.web_result is not None:
            scoped_evidence = tuple(
                item for item in state.web_result.evidence if item.source_id in refs
            )
            if scoped_evidence:
                web_result = WebSearchOutput(evidence=scoped_evidence)

        category_result = None
        if (
            state.category_result is not None
            and state.category_result.status is InsightStatus.FOUND
            and _category_insight_reference(state.category_result) in refs
        ):
            category_result = state.category_result
        try:
            return ChildContextSeed(
                context_refs=context_refs,
                plan=state.plan,
                interpreted_request=state.interpreted_request,
                item_results=item_results,
                web_result=web_result,
                category_result=category_result,
            )
        except (TypeError, ValueError) as error:
            raise ToolSessionError(AgentFailureCode.INVALID_ACTION.value) from error

    def record_child_runs(self, count: int) -> None:
        if type(count) is not int or isinstance(count, bool) or count < 1:
            raise ValueError("child run count is invalid")
        if self._child_runs + count > self.config.max_child_runs:
            raise ToolSessionError(AgentFailureCode.BUDGET_EXCEEDED.value)
        self._child_runs += count

    def record_fork_event(self, event: AgentRunEvent) -> None:
        """Append a coordinator-authored safe lifecycle event to this record."""

        if type(event) is not AgentRunEvent:
            raise TypeError("fork event must be exact")
        self._emit(event)

    def child_handoff(
        self,
        *,
        status: Literal["COMPLETED", "FAILED", "ABORTED"],
        safe_code: str | None = None,
    ) -> ChildHandoff:
        """Compile this loop's verified result without leaking its transcript."""

        state = self._require_state()
        candidate_ids = tuple(
            dict.fromkeys(
                candidate.candidate_id
                for result in state.item_results
                for candidate in result.candidates
            )
        )[:32]
        evidence_ids: list[str] = []
        if state.web_result is not None:
            evidence_ids.extend(item.source_id for item in state.web_result.evidence)
        if (
            state.category_result is not None
            and state.category_result.status is InsightStatus.FOUND
        ):
            evidence_ids.append(_category_insight_reference(state.category_result))
        verified_tools = tuple(
            tool_name
            for tool_name, present in (
                (
                    ToolName.WEB_SEARCH,
                    state.web_result is not None
                    and self._tool_counts.get(ToolName.WEB_SEARCH, 0) > 0,
                ),
                (
                    ToolName.CATEGORY_INSIGHT,
                    state.category_result is not None
                    and self._tool_counts.get(ToolName.CATEGORY_INSIGHT, 0) > 0,
                ),
                (
                    ToolName.ITEM_SEARCH,
                    bool(state.item_results) and self._tool_counts.get(ToolName.ITEM_SEARCH, 0) > 0,
                ),
                (ToolName.PRICE_COMPARE, state.price_result is not None),
                (ToolName.SHIPPING_CALC, state.shipping_result is not None),
                (ToolName.ITEM_PICKER, state.picker_result is not None),
                (ToolName.SHOPPING_SUMMARY, self._handoff_ready),
                (
                    ToolName.DISPATCH_TOOL,
                    self._tool_counts.get(ToolName.DISPATCH_TOOL, 0) > 0,
                ),
                (
                    ToolName.PARALLEL_DISPATCH_TOOL,
                    self._tool_counts.get(ToolName.PARALLEL_DISPATCH_TOOL, 0) > 0,
                ),
            )
            if present
        )
        return ChildHandoff(
            child_run_id=self.run_id,
            status=status,
            item_results=state.item_results,
            web_result=state.web_result,
            category_result=state.category_result,
            price_result=state.price_result,
            shipping_result=state.shipping_result,
            picker_result=state.picker_result,
            publication_eligible_ids=state.publication_eligible_ids,
            semantic_assertion_failures=state.semantic_assertion_failures,
            completed_tools=verified_tools,
            candidate_ids=candidate_ids,
            evidence_ids=tuple(dict.fromkeys(evidence_ids))[:32],
            safe_code=safe_code,
        )

    async def invoke(self, tool_name: ToolName, **arguments: object) -> str:
        """Execute exactly one approved native tool and return a safe receipt."""

        if type(tool_name) is not ToolName:
            raise TypeError("native tool name must be an exact ToolName")
        async with self._lock:
            repeated_receipt = self._reject_repeated_action(tool_name)
            if repeated_receipt is not None:
                return repeated_receipt
            unmet_facts = self._unmet_preconditions(tool_name, arguments)
            if unmet_facts:
                return self._reject_selected_tool(tool_name, unmet_facts)
            try:
                self._begin_tool(tool_name)
                await self._invoke_locked(tool_name, arguments)
            except asyncio.CancelledError:
                self._record_tool(tool_name, AgentFailureCode.RUN_ABORTED.value)
                self._emit_tool_finished(tool_name, AgentFailureCode.RUN_ABORTED.value)
                raise
            except Exception as error:
                self._tool_started_in_round = True
                code = _safe_code(error)
                if code == ToolFailureCode.TOOL_RESULT_TOO_LARGE.value:
                    _LOGGER.warning(
                        "native tool result exceeded its safe limit",
                        extra={"safe_code": code, "tool_name": tool_name.value},
                    )
                else:
                    _LOGGER.exception("native tool execution failed: %s", tool_name.value)
                self._record_tool(tool_name, code)
                self._emit_tool_finished(tool_name, code)
                if code == ToolFailureCode.TOOL_RESULT_TOO_LARGE.value:
                    pending_action = self._pending_action
                    if pending_action is None or pending_action.tool_name is not tool_name:
                        self._fail(AgentFailureCode.INTERNAL_ERROR.value)
                        return _receipt(
                            tool_name=tool_name,
                            status="failed",
                            safe_code=AgentFailureCode.INTERNAL_ERROR.value,
                        )
                    self._completed_actions.append(pending_action)
                    self._pending_action = None
                    receipt = _receipt(
                        tool_name=tool_name,
                        status="rejected",
                        safe_code=code,
                        observation={
                            "instruction": (
                                "The tool result exceeded its safe limit. Narrow the query or "
                                "reduce top_k, then try again."
                            )
                        },
                    )
                    self._completed_receipts.append(receipt)
                    return receipt
                self._fail(code)
                return _receipt(tool_name=tool_name, status="failed", safe_code=code)
            self._record_tool(tool_name, "SUCCESS")
            self._emit_tool_finished(tool_name, "SUCCESS")
            if self._pending_action is None or self._pending_action.tool_name is not tool_name:
                self._fail(AgentFailureCode.INTERNAL_ERROR.value)
                return _receipt(tool_name=tool_name, status="failed")
            self._completed_actions.append(self._pending_action)
            self._pending_action = None
            receipt = _receipt(
                tool_name=tool_name,
                status="terminal" if self._response is not None else "ok",
                platforms=self._planned_platforms() if tool_name is ToolName.PLANNER else (),
                observation=self._safe_observation(),
            )
            self._completed_receipts.append(receipt)
            return receipt

    def _reject_repeated_action(self, tool_name: ToolName) -> str | None:
        """Fail before side effects when a Loop repeats an exact completed action."""

        pending_action = self._pending_action
        if (
            self._selected_tool is not tool_name
            or self._tool_started_in_round
            or pending_action is None
            or pending_action.tool_name is not tool_name
        ):
            return None
        repeated = any(
            action == pending_action and not _is_rejected_receipt(receipt)
            for action, receipt in zip(
                self._completed_actions,
                self._completed_receipts,
                strict=True,
            )
        )
        if not repeated:
            return None

        self._tool_started_in_round = True
        self._emit(
            AgentRunEvent(
                kind=AgentEventKind.TOOL_STARTED,
                run_id=self.run_id,
                tool_name=tool_name,
            )
        )
        code = AgentFailureCode.LOOP_DETECTED.value
        self._emit_tool_finished(tool_name, code)
        receipt = _receipt(tool_name=tool_name, status="failed", safe_code=code)
        self._completed_actions.append(pending_action)
        self._completed_receipts.append(receipt)
        self._pending_action = None
        self._fail(code)
        return receipt

    def checkpoint_local_state(self) -> dict[str, object]:
        """Return the bounded typed state required to reconstruct this loop."""

        state = self._require_state()
        return {
            "agent_model_calls": self._model_calls,
            "agent_session_snapshot": self.checkpoint_codec.encode(
                AgentSessionSnapshot(
                    state=state,
                    completed_actions=tuple(self._completed_actions),
                    pending_action=self._pending_action,
                    tool_counts=tuple(sorted(self._tool_counts.items())),
                    tool_outcomes=tuple(sorted(self._tool_outcomes.items())),
                    completed_receipts=tuple(self._completed_receipts),
                    tool_executions=self._tool_executions,
                    embedding_batches=self._embedding_batches,
                    tavily_calls=self._tavily_calls,
                    child_runs=self._child_runs,
                    fork_batches=self._fork_batches,
                    response=self._response,
                    handoff_ready=self._handoff_ready,
                    failure_code=self._failure_code,
                )
            ),
        }

    def restore_checkpoint_snapshot(
        self,
        value: object,
    ) -> tuple[tuple[AgentLoopAction, ...], AgentLoopAction | None, tuple[str, ...]]:
        """Restore completed typed state without invoking a model, port, or tool."""

        snapshot = self.checkpoint_codec.decode(value)
        if snapshot.state.request != self.request:
            raise ValueError("Agent session checkpoint request does not match")
        if snapshot.state.capabilities != self.config.capabilities:
            raise ValueError("Agent session checkpoint capabilities do not match")
        if (
            type(snapshot.completed_actions) is not tuple
            or any(type(action) is not AgentLoopAction for action in snapshot.completed_actions)
            or len(snapshot.completed_receipts) != len(snapshot.completed_actions)
        ):
            raise ValueError("Agent session checkpoint receipts are inconsistent")
        self._completed_actions = list(snapshot.completed_actions)
        self._pending_action = snapshot.pending_action
        self._state = snapshot.state
        self._tool_counts = dict(snapshot.tool_counts)
        self._tool_outcomes = dict(snapshot.tool_outcomes)
        self._completed_receipts = list(snapshot.completed_receipts)
        self._tool_executions = snapshot.tool_executions
        self._embedding_batches = snapshot.embedding_batches
        self._tavily_calls = snapshot.tavily_calls
        self._child_runs = snapshot.child_runs
        self._fork_batches = snapshot.fork_batches
        self._response = snapshot.response
        self._handoff_ready = snapshot.handoff_ready
        self._failure_code = snapshot.failure_code
        self._candidate_store = None
        self._eligibility = None
        if snapshot.state.shipping_result is not None:
            self._restore_eligibility(snapshot.state)
        return (
            snapshot.completed_actions,
            snapshot.pending_action,
            snapshot.completed_receipts,
        )

    def restore_model_call_count(self, value: int) -> None:
        """Retain abandoned model attempts in the resumed loop budget."""

        if type(value) is not int or isinstance(value, bool) or value < self._model_calls:
            raise ValueError("AgentLoop restored model call count is invalid")
        self._model_calls = value
        self._current_round = None
        self._selected_tool = None
        self._model_streaming_emitted = False
        self._tool_started_in_round = False

    async def close(self) -> bool:
        """Release per-run sources; ``False`` means cleanup failed safely."""

        if self._closed:
            return True
        self._closed = True
        if self._candidate_store is not None:
            self._candidate_store.clear()
            self._candidate_store = None
        if self.resource_drainer is None:
            return True
        try:
            await self.resource_drainer()
        except Exception:
            return False
        return True

    def finalize(self) -> AgentExecution:
        """Create the only public terminal result after the graph stops."""

        summary = self._tool_summary()
        response = self._response
        if response is None:
            code = self._failure_code or AgentFailureCode.MODEL_INVALID.value
            response = AgentDemoResponse(
                run_id=self.run_id,
                status=RunStatus.FAILED,
                tool_summary=summary,
            )
            self._emit(
                AgentRunEvent(
                    kind=AgentEventKind.AGENT_ERROR,
                    run_id=self.run_id,
                    status="FAILED",
                    safe_code=code,
                )
            )
        else:
            response = AgentDemoResponse.model_validate(
                response.model_dump() | {"tool_summary": summary}
            )
            self._emit(
                AgentRunEvent(
                    kind=AgentEventKind.AGENT_RESULT,
                    run_id=self.run_id,
                    status=response.status.value,
                )
            )
        terminal_code = self._failure_code if response.status is RunStatus.FAILED else None
        record = AgentRunRecord(
            run_id=self.run_id,
            status=response.status,
            model_calls=self._model_calls,
            tool_calls=self._tool_executions,
            child_runs=self._child_runs,
            terminal_code=terminal_code,
            tool_summary=summary,
            events=tuple(self._events),
        )
        return AgentExecution(response=response, record=record)

    def _begin_tool(self, tool_name: ToolName) -> None:
        if self.failed:
            raise ToolSessionError(self._failure_code or AgentFailureCode.INTERNAL_ERROR.value)
        if self._response is not None:
            raise ToolSessionError(AgentFailureCode.INVALID_ACTION.value)
        if self._selected_tool is not tool_name or self._tool_started_in_round:
            raise ToolSessionError(AgentFailureCode.INVALID_ACTION.value)
        self._tool_started_in_round = True
        self._emit(
            AgentRunEvent(
                kind=AgentEventKind.TOOL_STARTED,
                run_id=self.run_id,
                tool_name=tool_name,
            )
        )
        if tool_name is ToolName.ITEM_SEARCH:
            plan = self._require_state().plan
            query_count = 1 if plan is None else len(_required_search_queries(plan))
            platform_count = max(1, len(self._planned_platforms()))
            maximum = max(3, platform_count * query_count * 2)
        elif tool_name in {ToolName.DISPATCH_TOOL, ToolName.PARALLEL_DISPATCH_TOOL}:
            maximum = self.config.max_fork_batches
        else:
            maximum = 1
        local_tool_count = sum(
            action.tool_name is tool_name and not _is_rejected_receipt(receipt)
            for action, receipt in zip(
                self._completed_actions,
                self._completed_receipts,
                strict=True,
            )
        )
        if (
            self._tool_executions >= self._tool_execution_limit()
            or local_tool_count >= maximum
            or (
                tool_name in {ToolName.DISPATCH_TOOL, ToolName.PARALLEL_DISPATCH_TOOL}
                and self._fork_batches >= self.config.max_fork_batches
            )
            or (tool_name is ToolName.WEB_SEARCH and self._tavily_calls >= 1)
        ):
            raise ToolSessionError(AgentFailureCode.BUDGET_EXCEEDED.value)
        self._tool_executions += 1
        if tool_name is ToolName.WEB_SEARCH:
            self._tavily_calls += 1
        if tool_name in {ToolName.DISPATCH_TOOL, ToolName.PARALLEL_DISPATCH_TOOL}:
            self._fork_batches += 1

    async def _invoke_locked(
        self,
        tool_name: ToolName,
        arguments: Mapping[str, object],
    ) -> None:
        match tool_name:
            case ToolName.PLANNER:
                _require_arguments(arguments, ("decision_json",))
                await self._planner(arguments["decision_json"])
            case ToolName.CHAT_FALLBACK:
                _require_arguments(arguments, ())
                await self._chat_fallback()
            case ToolName.WEB_SEARCH:
                _require_arguments(arguments, ("evidence_kind",))
                await self._web_search(_argument_text(arguments["evidence_kind"]))
            case ToolName.CATEGORY_INSIGHT:
                _require_arguments(arguments, ("category", "depth"))
                await self._category_insight(
                    _argument_text(arguments["category"]),
                    _argument_text(arguments["depth"]),
                )
            case ToolName.ITEM_SEARCH:
                _require_arguments(arguments, ("platform", "query"))
                await self._item_search(
                    _argument_text(arguments["query"]),
                    _argument_text(arguments["platform"]),
                )
            case ToolName.PRICE_COMPARE:
                _require_arguments(arguments, ())
                await self._price_compare()
            case ToolName.SHIPPING_CALC:
                _require_arguments(arguments, ())
                await self._shipping_calc()
            case ToolName.ITEM_PICKER:
                _require_arguments(arguments, ())
                await self._item_picker()
            case ToolName.SHOPPING_SUMMARY:
                _require_arguments(arguments, ())
                await self._shopping_summary()
            case ToolName.DISPATCH_TOOL | ToolName.PARALLEL_DISPATCH_TOOL:
                raise ToolSessionError(AgentFailureCode.INVALID_ACTION.value)

    async def _planner(self, decision_json: object) -> None:
        state = self._require_state()
        if type(decision_json) is not str:
            raise ToolSessionError(AgentFailureCode.INVALID_ACTION.value)
        try:
            decision = PlannerDecisionInput.model_validate_json(decision_json)
            interpreted = _compile_planner_intent(state.request.query, decision)
            comparison_targets = _compile_comparison_targets(state.request.query, decision)
            validation = validate_interpreted_request(state.request.query, interpreted)
        except (TypeError, ValueError, ValidationError) as error:
            raise ToolSessionError(AgentFailureCode.MODEL_INVALID.value) from error
        if not validation.is_valid or validation.interpreted_request is None:
            raise ToolSessionError(AgentFailureCode.MODEL_INVALID.value)
        interpreted = validation.interpreted_request
        result = await execute_business_tool(
            ToolName.PLANNER,
            PlannerInput(
                request=state.request,
                interpreted_request=interpreted,
                required_baseline=interpreted,
                capabilities=state.capabilities,
                declared_intent=decision.intent_kind,
                requested_platforms=decision.requested_platforms,
                search_query=decision.search_query,
                comparison_targets=comparison_targets,
            ),
            self.tool_dependencies,
        )
        if type(result) is not PlannerOutput:
            raise ToolSessionError(AgentFailureCode.INTERNAL_ERROR.value)
        if self.task_scope is not None and result.intent_kind is PlannerIntentKind.SHOPPING:
            scoped_platforms = tuple(
                platform
                for platform in result.platforms
                if platform in self.task_scope.allowed_platforms
            )
            if not scoped_platforms:
                raise ToolSessionError(AgentFailureCode.INVALID_ACTION.value)
            result = replace(result, platforms=scoped_platforms)
        next_state = replace(
            state,
            plan=result,
            interpreted_request=interpreted,
            required_baseline=interpreted,
        )
        self._state = next_state

    async def _chat_fallback(self) -> None:
        state = self._require_state()
        plan = state.plan
        if plan is None:
            raise ToolSessionError(AgentFailureCode.INVALID_ACTION.value)
        reason = plan.fallback_reason
        if reason is None and (
            not state.capabilities.embedding_enabled
            or (
                state.category_result is not None
                and state.category_result.status is InsightStatus.NO_INSIGHT
            )
        ):
            reason = PlannerFallbackReason.UNSUPPORTED_CATEGORY
        if reason is None:
            raise ToolSessionError(AgentFailureCode.INVALID_ACTION.value)
        result = await execute_business_tool(
            ToolName.CHAT_FALLBACK,
            ChatFallbackInput(reason_code=reason),
            self.tool_dependencies,
        )
        if type(result) is not ChatFallbackOutput:
            raise ToolSessionError(AgentFailureCode.INTERNAL_ERROR.value)
        if self.task_scope is not None:
            self._handoff_ready = True
            return
        self._response = AgentDemoResponse(
            run_id=self.run_id,
            status=RunStatus.COMPLETED,
            answer=AgentAnswer(kind=AgentAnswerKind.CHAT_FALLBACK, text=result.answer),
        )

    async def _web_search(self, evidence_kind: str) -> None:
        try:
            kind = EvidenceKind(evidence_kind)
        except (TypeError, ValueError):
            raise ToolSessionError(AgentFailureCode.INVALID_ACTION.value) from None
        result = await execute_business_tool(
            ToolName.WEB_SEARCH,
            WebSearchInput(query=self.request.query[:512], evidence_kind=kind, max_results=8),
            self.tool_dependencies,
        )
        if type(result) is not WebSearchOutput:
            raise ToolSessionError(AgentFailureCode.INTERNAL_ERROR.value)
        self._state = replace(self._require_state(), web_result=result)

    async def _category_insight(self, category: str, depth: str) -> None:
        try:
            insight_depth = InsightDepth(depth)
        except (TypeError, ValueError):
            raise ToolSessionError(AgentFailureCode.INVALID_ACTION.value) from None
        state = self._require_state()
        if not _category_phrase_is_grounded(state.request.query, category):
            raise ToolSessionError(AgentFailureCode.INVALID_ACTION.value)
        result = await execute_business_tool(
            ToolName.CATEGORY_INSIGHT,
            CategoryInsightInput(
                category=category,
                depth=insight_depth,
            ),
            self.tool_dependencies,
        )
        if type(result) is not CategoryInsightOutput:
            raise ToolSessionError(AgentFailureCode.INTERNAL_ERROR.value)
        # Category knowledge contributes non-exhaustive observations, but the
        # exact user-grounded category argument remains the product authority.
        # A model-authored planner query may translate or broaden recall; it is
        # never allowed to persist as an invented subtype after grounding.
        if result.category != category:
            result = result.model_copy(update={"category": category})
        plan = state.plan
        if plan is not None and not plan.comparison_targets:
            plan = replace(plan, search_query=category)
        self._state = replace(
            state,
            plan=plan,
            category_result=result,
            category_depth=insight_depth,
        )

    async def _item_search(self, query_text: str, platform_text: str) -> None:
        try:
            platform = Platform(platform_text)
        except (TypeError, ValueError):
            raise ToolSessionError(AgentFailureCode.INVALID_ACTION.value) from None
        state = self._require_state()
        plan = state.plan
        next_search = _next_item_search(
            state,
            platform=platform,
            allow_refinement=self._item_search_can_refine(state, platform=platform),
        )
        if (
            plan is None
            or plan.intent_kind is not PlannerIntentKind.SHOPPING
            or next_search is None
        ):
            raise ToolSessionError(AgentFailureCode.INVALID_ACTION.value)
        target_query, top_k = next_search
        query = query_text.strip()
        if not query or len(query) > 512:
            raise ToolSessionError(AgentFailureCode.INVALID_ACTION.value)
        query = _preference_augmented_query(
            query,
            self._require_interpreted(state).preferred,
        )
        request = self._item_search_input(
            state,
            query=query,
            platform=platform,
            top_k=top_k,
        )
        vector_texts = (
            (request.query,)
            if self.preference_text is None
            else (request.query, self.preference_text)
        )
        vectors = await self._vectors_for(vector_texts)
        result = await execute_business_tool(
            ToolName.ITEM_SEARCH,
            ItemSearchToolInput(
                request=request,
                data_mode=state.capabilities.data_mode,
                query_vector=vectors[0],
                preference_vector=(None if len(vectors) == 1 else vectors[1]),
            ),
            self.tool_dependencies,
        )
        if type(result) is not ItemSearchRuntimeResult:
            raise ToolSessionError(AgentFailureCode.INTERNAL_ERROR.value)
        insight = state.category_result
        if insight is None or (
            insight.status is InsightStatus.NO_INSIGHT
            and (state.web_result is None or not state.web_result.evidence)
        ):
            raise ToolSessionError(AgentFailureCode.INVALID_ACTION.value)
        failure: SemanticAssertionFailure | None = None
        previous_failure = next(
            (
                item
                for item in state.semantic_assertion_failures
                if item.platform is platform and item.target_query == target_query
            ),
            None,
        )
        if result.candidates:
            # ``target_query`` and ``query`` are model-authored retrieval hints.  They may
            # improve recall, but they must never silently become new product constraints.
            # For an ordinary category request the category is exact text grounded in the
            # locked user message, so it is the authority for target-vs-accessory checking.
            # Explicit comparison targets are also exact grounded spans and retain their
            # per-target identity here.
            semantic_target = _semantic_assertion_target(
                category=request.category,
                target_query=target_query,
                comparison_targets=plan.comparison_targets,
            )
            assertion = await self.tool_dependencies.semantic_assertion.verify(
                SemanticAssertionInput(
                    query=semantic_target,
                    category=request.category,
                    components=insight.components,
                    candidates=tuple(
                        SemanticAssertionCandidate(
                            candidate_id=candidate.candidate_id,
                            title=candidate.title,
                            attributes=candidate.attributes,
                        )
                        for candidate in result.candidates
                    ),
                )
            )
            if type(assertion) is not SemanticAssertionOutput:
                raise ToolSessionError(AgentFailureCode.INTERNAL_ERROR.value)
            relevant_ids = set(assertion.relevant_candidate_ids)
            rejected_ids = tuple(
                candidate.candidate_id
                for candidate in result.candidates
                if candidate.candidate_id not in relevant_ids
            )
            if not relevant_ids:
                failure = SemanticAssertionFailure(
                    platform=platform,
                    target_query=target_query,
                    query=query,
                    top_k=top_k,
                    rejected_candidate_ids=rejected_ids,
                    attempts=1 if previous_failure is None else previous_failure.attempts + 1,
                )
            result = _retain_semantically_relevant_candidates(result, assertion)
        else:
            failure = SemanticAssertionFailure(
                platform=platform,
                target_query=target_query,
                query=query,
                top_k=top_k,
                rejected_candidate_ids=(),
                attempts=1 if previous_failure is None else previous_failure.attempts + 1,
            )
        result = replace(result, target_query=target_query)
        previous_result = next(
            (
                item
                for item in reversed(state.item_results)
                if item.platform is platform and item.target_query == target_query
            ),
            None,
        )
        # A model-chosen refinement is another observation, not permission to
        # erase an earlier verified candidate pool.  Keep the last good facts
        # when the revised query is empty or semantically rejected; the failure
        # receipt still drives Reflect and one bounded changed-query retry.
        retained_result = (
            previous_result
            if not result.candidates and previous_result is not None and previous_result.candidates
            else result
        )
        self._invalidate_candidate_workspace()
        self._state = replace(
            state,
            item_results=(
                *(
                    item
                    for item in state.item_results
                    if not (item.platform is platform and item.target_query == target_query)
                ),
                retained_result,
            ),
            semantic_assertion_failures=(
                *(
                    item
                    for item in state.semantic_assertion_failures
                    if not (item.platform is platform and item.target_query == target_query)
                ),
                *((failure,) if failure is not None else ()),
            ),
            price_result=None,
            shipping_result=None,
            picker_result=None,
            publication_eligible_ids=(),
        )

    async def _price_compare(self) -> None:
        store = self._ensure_candidate_store()
        result = await execute_business_tool(
            ToolName.PRICE_COMPARE,
            PriceCompareInput(pool=store.pool, top_n=12),
            self.tool_dependencies,
        )
        if type(result) is not PriceCompareOutput:
            raise ToolSessionError(AgentFailureCode.INTERNAL_ERROR.value)
        self._state = replace(self._require_state(), price_result=result)

    async def _shipping_calc(self) -> None:
        state = self._require_state()
        store = self._ensure_candidate_store()
        prices = state.price_result
        if prices is None:
            raise ToolSessionError(AgentFailureCode.INVALID_ACTION.value)
        result = await execute_business_tool(
            ToolName.SHIPPING_CALC,
            ShippingCalcInput(
                pool=store.pool,
                price_points=prices,
                destination_country="CN",
                rules=self.config.shipping_rules,
                ruleset_version=self.config.ruleset_version,
                calculation_date=self.config.calculation_date,
            ),
            self.tool_dependencies,
        )
        if type(result) is not ShippingCalcOutput:
            raise ToolSessionError(AgentFailureCode.INTERNAL_ERROR.value)
        excluded_ids = set(
            ()
            if self.publication_filter is None
            else self.publication_filter.excluded_candidate_ids(pool=store.pool)
        )
        excluded_ids.update(_unpriced_candidate_ids(store.pool, prices))
        if prices.comparison_scope is PriceComparisonScope.NO_COMPARABLE_GROUP:
            excluded_ids.update(candidate.candidate_id for candidate in store.pool.candidates)
        elif prices.comparison_scope is PriceComparisonScope.COMPARABLE_PRODUCT_GROUPS:
            included_ids = {point.candidate_id for point in prices.ranked}
            excluded_ids.update(
                candidate.candidate_id
                for candidate in store.pool.candidates
                if candidate.candidate_id not in included_ids
            )
        excluded = tuple(
            candidate.candidate_id
            for candidate in store.pool.candidates
            if candidate.candidate_id in excluded_ids
        )
        eligibility = evaluate_candidate_pool(
            store.pool,
            self._require_interpreted(state),
            display_currency=state.request.display_currency,
            excluded_candidate_ids=excluded,
        )
        self._eligibility = eligibility
        self._state = replace(
            state,
            shipping_result=result,
            publication_eligible_ids=eligibility.eligible_candidate_ids,
        )

    async def _item_picker(self) -> None:
        state = self._require_state()
        if self._eligibility is None or state.price_result is None or state.shipping_result is None:
            raise ToolSessionError(AgentFailureCode.INVALID_ACTION.value)
        interpreted = self._require_interpreted(state)
        result = await execute_business_tool(
            ToolName.ITEM_PICKER,
            ItemPickerInput(
                eligibility=self._eligibility,
                prices=state.price_result,
                shipping=state.shipping_result,
                preferred=interpreted.preferred,
                category_insight=(
                    state.category_result
                    if state.category_result is not None
                    and state.category_result.status is InsightStatus.FOUND
                    else None
                ),
                target_candidate_groups=_target_candidate_groups(state),
                max_items=state.request.top_k,
                budget=next(
                    (
                        criterion
                        for criterion in interpreted.required
                        if type(criterion) is BudgetMax
                    ),
                    None,
                ),
            ),
            self.tool_dependencies,
        )
        if type(result) is not ItemPickerOutput:
            raise ToolSessionError(AgentFailureCode.INTERNAL_ERROR.value)
        self._state = replace(state, picker_result=result)

    async def _shopping_summary(self) -> None:
        state = self._require_state()
        if self.task_scope is not None:
            if not self._child_has_completed_work():
                raise ToolSessionError(AgentFailureCode.INVALID_ACTION.value)
            # ``shopping_summary`` is intentionally the common terminal tool
            # in the shared schema. For a child it closes only the child loop:
            # the parent receives its typed facts and alone owns the global
            # comparison, selection and user-visible final response.
            self._handoff_ready = True
            return
        if self._eligibility is None or state.picker_result is None:
            raise ToolSessionError(AgentFailureCode.INVALID_ACTION.value)
        result = await execute_business_tool(
            ToolName.SHOPPING_SUMMARY,
            ShoppingSummaryInput(
                agent_run_id=self.run_id,
                request=state.request,
                interpreted_request=self._require_interpreted(state),
                eligibility=self._eligibility,
                picker=state.picker_result,
                fx_source_batch=self.config.fx_source_batch,
                search_service_factory=self.search_service_factory,
            ),
            self.tool_dependencies,
        )
        if type(result) is not ShoppingSummaryOutput:
            raise ToolSessionError(AgentFailureCode.INTERNAL_ERROR.value)
        search_response = result.search_response
        if search_response.status not in {RunStatus.COMPLETED, RunStatus.NO_MATCH}:
            raise ToolSessionError(AgentFailureCode.INTERNAL_ERROR.value)
        evidence_ids = tuple(
            dict.fromkeys(
                evidence.evidence_id
                for search_result in search_response.results
                for evidence in search_result.evidence
            )
        )
        self._response = AgentDemoResponse(
            run_id=self.run_id,
            status=search_response.status,
            answer=AgentAnswer(kind=AgentAnswerKind.SHOPPING_SUMMARY, text=result.answer),
            search_response=search_response,
            selected_product_ids=result.selected_product_ids,
            evidence_ids=evidence_ids,
            web_evidence=(() if state.web_result is None else state.web_result.evidence),
            landed_cost_advisories=(
                () if state.shipping_result is None else state.shipping_result.advisories
            ),
        )

    async def _vectors_for(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        missing = tuple(text for text in texts if text not in self._query_vectors)
        if missing:
            if self.embedding_port is None:
                raise ToolSessionError("PROVIDER_UNAVAILABLE")
            if self._embedding_batches >= 4:
                raise ToolSessionError(AgentFailureCode.BUDGET_EXCEEDED.value)
            self._embedding_batches += 1
            try:
                result = await self.embedding_port.embed(EmbeddingBatch(texts=missing))
            except ToolPortError as error:
                raise ToolSessionError(error.code.value) from None
            if type(result) is not EmbeddingResult or len(result.vectors) != len(missing):
                raise ToolSessionError("PROVIDER_RESPONSE_INVALID")
            self._query_vectors.update(zip(missing, result.vectors, strict=True))
        return tuple(self._query_vectors[text] for text in texts)

    def _item_search_input(
        self,
        state: _SessionState,
        *,
        query: str,
        platform: Platform,
        top_k: int,
    ) -> ItemSearchInput:
        if type(query) is not str or not query.strip() or len(query) > 2_000:
            raise ToolSessionError(AgentFailureCode.INVALID_ACTION.value)
        if type(top_k) is not int or isinstance(top_k, bool) or not 1 <= top_k <= 50:
            raise ToolSessionError(AgentFailureCode.INVALID_ACTION.value)
        insight = state.category_result
        if insight is None or (
            insight.status is InsightStatus.NO_INSIGHT
            and (state.web_result is None or not state.web_result.evidence)
        ):
            raise ToolSessionError(AgentFailureCode.INVALID_ACTION.value)
        interpreted = self._require_interpreted(state)
        budget = next(
            (criterion for criterion in interpreted.required if type(criterion) is BudgetMax),
            None,
        )
        minimum: Decimal | None = None
        maximum: Decimal | None = None
        if budget is not None:
            currency = budget.currency or state.request.display_currency
            rates = self.config.fx_source_batch.exchange_rates
            if rates is None:
                raise ToolSessionError("ITEM_SOURCE_INVALID")
            rate = next((item for item in rates.rates if item.currency == currency), None)
            if rate is None:
                raise ToolSessionError("ITEM_SOURCE_INVALID")
            minimum = (
                None if budget.lower_bound is None else budget.lower_bound * rate.base_per_unit
            )
            maximum = budget.upper_bound * rate.base_per_unit
        return ItemSearchInput(
            query=query,
            platform=platform,
            category=insight.category,
            min_landed_cost_cny=minimum,
            max_landed_cost_cny=maximum,
            top_k=top_k,
        )

    def _available_item_search_platforms(self, state: _SessionState) -> tuple[Platform, ...]:
        """Return platforms with an unobserved target or a bounded failed target retry."""

        if state.category_result is None or (
            state.category_result.status is InsightStatus.NO_INSIGHT
            and (state.web_result is None or not state.web_result.evidence)
        ):
            return ()
        return tuple(
            platform
            for platform in self._planned_platforms()
            if _next_item_search(
                state,
                platform=platform,
                allow_refinement=self._item_search_can_refine(state, platform=platform),
            )
            is not None
        )

    def _item_search_queries(
        self,
        state: _SessionState,
        *,
        platform: Platform,
    ) -> tuple[str, ...]:
        """Return observed model-authored queries without inventing a search plan."""

        queries = [
            result.retrieval_query.strip()
            for result in state.item_results
            if result.platform is platform and result.retrieval_query.strip()
        ]
        queries.extend(
            query
            for action in self._completed_actions
            if action.tool_name is ToolName.ITEM_SEARCH
            and action.arguments.get("platform") == platform.value
            and (query := str(action.arguments.get("query", "")).strip())
        )
        return tuple(dict.fromkeys(queries))

    def _item_search_can_refine(
        self,
        state: _SessionState,
        *,
        platform: Platform,
    ) -> bool:
        """Permit one model-chosen query revision after a successful observation."""

        plan = state.plan
        return (
            plan is not None
            and len(_required_search_queries(plan)) == 1
            and len(self._item_search_queries(state, platform=platform)) < 2
        )

    def _ensure_candidate_store(self) -> CandidateStore:
        if self._candidate_store is not None:
            return self._candidate_store
        state = self._require_state()
        if not state.item_results:
            raise ToolSessionError(AgentFailureCode.INVALID_ACTION.value)
        try:
            manifest = self.candidate_manifest_factory(state.item_results)
            if (
                type(manifest) is not CandidateManifest
                or manifest.data_mode is not state.capabilities.data_mode
            ):
                raise ValueError
            store = CandidateStore(manifest)
            store.merge(state.item_results)
        except (ToolPortError, TypeError, ValueError, RuntimeError) as error:
            _LOGGER.exception("candidate store rejected verified item-search facts")
            raise ToolSessionError("ITEM_SOURCE_INVALID") from error
        self._candidate_store = store
        return store

    def _invalidate_candidate_workspace(self) -> None:
        if self._candidate_store is not None:
            self._candidate_store.clear()
        self._candidate_store = None
        self._eligibility = None

    def _restore_eligibility(self, state: _SessionState) -> None:
        """Rebuild the deterministic candidate gate from checkpointed typed facts."""

        prices = state.price_result
        if prices is None or state.shipping_result is None:
            raise ValueError("Agent session checkpoint shipping state is inconsistent")
        store = self._ensure_candidate_store()
        excluded_ids = set(
            ()
            if self.publication_filter is None
            else self.publication_filter.excluded_candidate_ids(pool=store.pool)
        )
        excluded_ids.update(_unpriced_candidate_ids(store.pool, prices))
        if prices.comparison_scope is PriceComparisonScope.NO_COMPARABLE_GROUP:
            excluded_ids.update(candidate.candidate_id for candidate in store.pool.candidates)
        elif prices.comparison_scope is PriceComparisonScope.COMPARABLE_PRODUCT_GROUPS:
            included_ids = {point.candidate_id for point in prices.ranked}
            excluded_ids.update(
                candidate.candidate_id
                for candidate in store.pool.candidates
                if candidate.candidate_id not in included_ids
            )
        eligibility = evaluate_candidate_pool(
            store.pool,
            self._require_interpreted(state),
            display_currency=state.request.display_currency,
            excluded_candidate_ids=tuple(excluded_ids),
        )
        if eligibility.eligible_candidate_ids != state.publication_eligible_ids:
            raise ValueError("Agent session checkpoint eligibility does not match")
        self._eligibility = eligibility

    def _planned_platforms(self) -> tuple[Platform, ...]:
        plan = self._require_state().plan
        if plan is None or plan.intent_kind is not PlannerIntentKind.SHOPPING:
            return ()
        platforms = plan.platforms
        if self.task_scope is not None:
            platforms = tuple(
                platform for platform in platforms if platform in self.task_scope.allowed_platforms
            )
        return platforms

    def _can_offer_dispatch(self, _state: _SessionState) -> bool:
        if (
            not self.dispatch_enabled
            or self._fork_batches >= self.config.max_fork_batches
            or (self.task_scope is not None and not self.task_scope.allow_nested_fork)
        ):
            return False
        plan = _state.plan
        return (
            plan is not None
            and plan.intent_kind is PlannerIntentKind.SHOPPING
            and bool(self._planned_platforms())
        )

    def _model_action_limit(self) -> int:
        return 14 if self.task_scope is None else self.task_scope.max_model_actions

    def _tool_execution_limit(self) -> int:
        return 13 if self.task_scope is None else self.task_scope.max_tool_calls

    def _child_has_completed_work(self) -> bool:
        """Require a child to execute verified local work before handoff."""

        self._require_state()
        return any(
            self._tool_counts.get(tool_name, 0) > 0
            for tool_name in (
                ToolName.WEB_SEARCH,
                ToolName.CATEGORY_INSIGHT,
                ToolName.ITEM_SEARCH,
                ToolName.PRICE_COMPARE,
                ToolName.SHIPPING_CALC,
                ToolName.ITEM_PICKER,
                ToolName.DISPATCH_TOOL,
                ToolName.PARALLEL_DISPATCH_TOOL,
            )
        )

    def _merge_child_handoffs(self, handoffs: tuple[ChildHandoff, ...]) -> None:
        if not handoffs or any(type(handoff) is not ChildHandoff for handoff in handoffs):
            raise ToolSessionError(AgentFailureCode.FORK_FAILED.value)
        successful = tuple(
            handoff
            for handoff in handoffs
            if handoff.status == "COMPLETED" or handoff.completed_tools
        )
        if not successful:
            failure_codes = {handoff.safe_code for handoff in handoffs if handoff.safe_code}
            code = (
                next(iter(failure_codes))
                if len(failure_codes) == 1
                else AgentFailureCode.FORK_FAILED.value
            )
            raise ToolSessionError(code)
        # Root terminal summaries must retain contributing child retrievals
        # without exposing the child's private reflection transcript. Count
        # each child once for every successful retrieval kind it contributed.
        contributing_child_tools = (
            ToolName.WEB_SEARCH,
            ToolName.CATEGORY_INSIGHT,
            ToolName.ITEM_SEARCH,
            ToolName.PRICE_COMPARE,
            ToolName.SHIPPING_CALC,
            ToolName.ITEM_PICKER,
        )
        for handoff in successful:
            for tool_name in contributing_child_tools:
                if tool_name in handoff.completed_tools:
                    self._record_tool(tool_name, "SUCCESS")
        state = self._require_state()
        results_by_key = {
            (result.platform, result.target_query): result for result in state.item_results
        }
        for handoff in successful:
            for result in handoff.item_results:
                key = (result.platform, result.target_query)
                results_by_key[key] = result
        final_item_results = tuple(results_by_key.values())
        items_changed = final_item_results != state.item_results
        failures_by_key = {
            (failure.platform, failure.target_query): failure
            for failure in state.semantic_assertion_failures
        }
        for handoff in successful:
            for failure in handoff.semantic_assertion_failures:
                key = (failure.platform, failure.target_query)
                previous = failures_by_key.get(key)
                if previous is None or failure.attempts > previous.attempts:
                    failures_by_key[key] = failure
        adopted_handoff: ChildHandoff | None = None
        if len(successful) == 1:
            candidate = successful[0]
            candidate_results = {
                (result.platform, result.target_query): result for result in candidate.item_results
            }
            if candidate_results == results_by_key:
                adopted_handoff = candidate

        merged_category_result = (
            state.category_result
            if state.category_result is not None
            else next(
                (
                    handoff.category_result
                    for handoff in successful
                    if handoff.category_result is not None
                ),
                None,
            )
        )
        self._invalidate_candidate_workspace()
        self._state = replace(
            state,
            item_results=final_item_results,
            web_result=state.web_result
            if state.web_result is not None
            else next(
                (handoff.web_result for handoff in successful if handoff.web_result is not None),
                None,
            ),
            category_result=merged_category_result,
            category_depth=(
                state.category_depth
                if state.category_result is not None
                else None
                if merged_category_result is None
                else _inferred_category_depth(merged_category_result)
            ),
            semantic_assertion_failures=tuple(failures_by_key.values()),
            price_result=(
                adopted_handoff.price_result
                if adopted_handoff is not None
                else None
                if items_changed
                else state.price_result
            ),
            shipping_result=(
                adopted_handoff.shipping_result
                if adopted_handoff is not None
                else None
                if items_changed
                else state.shipping_result
            ),
            picker_result=(
                adopted_handoff.picker_result
                if adopted_handoff is not None
                else None
                if items_changed
                else state.picker_result
            ),
            publication_eligible_ids=(
                adopted_handoff.publication_eligible_ids
                if adopted_handoff is not None
                else ()
                if items_changed
                else state.publication_eligible_ids
            ),
        )
        if self._state.shipping_result is not None:
            self._restore_eligibility(self._state)

    def _safe_observation(
        self,
        *,
        handoffs: tuple[ChildHandoff, ...] = (),
    ) -> dict[str, object]:
        """Summarize trusted state for the next Think without a transcript."""

        state = self._require_state()
        observation: dict[str, object] = {}
        if state.plan is not None and state.plan.search_query is not None:
            observation["search_query"] = state.plan.search_query
        if state.category_result is not None:
            category: dict[str, object] = {
                "status": state.category_result.status.value,
                "category": state.category_result.category,
                "confidence": str(state.category_result.confidence),
            }
            if state.category_result.status is InsightStatus.FOUND:
                category["context_ref"] = _category_insight_reference(state.category_result)
                category["components"] = list(state.category_result.components)
                category["bestsellers"] = [
                    {
                        "name": item.name,
                        "typical_price_cny": str(item.typical_price_cny),
                        "why_popular": item.why_popular,
                    }
                    for item in state.category_result.bestsellers[:3]
                ]
                category["attributes"] = [
                    {
                        "name": item.name,
                        "distribution": {
                            key: str(value) for key, value in list(item.distribution.items())[:4]
                        },
                    }
                    for item in state.category_result.attributes[:4]
                ]
                category["price_tiers"] = [
                    {
                        "tier": item.tier,
                        "range_cny": [str(value) for value in item.range_cny],
                        "notes": item.notes,
                    }
                    for item in state.category_result.price_tiers
                ]
            observation["category"] = category
        if state.web_result is not None:
            observation["web"] = {
                "evidence_count": len(state.web_result.evidence),
                "evidence_ids": [item.source_id for item in state.web_result.evidence[:4]],
            }
        if state.item_results:
            observation["items"] = [
                _safe_item_observation(result) for result in state.item_results[:4]
            ]
        if state.price_result is not None:
            observation["price_summary"] = {
                "count": len(state.price_result.ranked),
                "exact": sum(
                    point.status is PricePointStatus.EXACT for point in state.price_result.ranked
                ),
                "unknown_fx": sum(
                    point.status is PricePointStatus.UNKNOWN_FX
                    for point in state.price_result.ranked
                ),
            }
        if state.shipping_result is not None:
            observation["shipping_summary"] = {
                "destination_country": state.shipping_result.destination_country,
                "count": len(state.shipping_result.advisories),
                "exact": sum(
                    advisory.item_status is ShippingStatus.EXACT
                    for advisory in state.shipping_result.advisories
                ),
                "estimate": sum(
                    advisory.item_status is ShippingStatus.ESTIMATE
                    for advisory in state.shipping_result.advisories
                ),
                "unknown": sum(
                    advisory.item_status is ShippingStatus.UNKNOWN
                    for advisory in state.shipping_result.advisories
                ),
            }
        if state.picker_result is not None:
            picker_observation: dict[str, object] = {
                "picks": [
                    {
                        "candidate_id": pick.candidate_id,
                        "title": pick.title,
                        "platform": pick.platform.value,
                        "preferences": [
                            {
                                "preference": assessment.preference,
                                "status": assessment.status.value,
                                "reason": assessment.reason,
                                "evidence_ids": list(assessment.evidence_ids),
                            }
                            for assessment in pick.preference_assessments
                        ],
                    }
                    for pick in state.picker_result.picks
                ],
                "comparison_targets": [
                    target.search_query
                    for target in (() if state.plan is None else state.plan.comparison_targets)
                ],
            }
            if any(
                assessment.status.value == "UNKNOWN"
                for pick in state.picker_result.picks
                for assessment in pick.preference_assessments
            ):
                picker_observation["reflect_instruction"] = (
                    "Some soft preferences remain UNKNOWN after the bounded retrieval and "
                    "selection pass. Finish with shopping_summary and preserve UNKNOWN in the "
                    "result; do not claim that the preference is verified."
                )
            observation["picker"] = picker_observation
        if state.semantic_assertion_failures:
            observation["assertions_failed"] = [
                {
                    "type": "semantic",
                    "tool": ToolName.ITEM_SEARCH.value,
                    "platform": failure.platform.value,
                    "target_query": failure.target_query,
                    "reason": "retrieval contains products unrelated to the locked user query",
                    "rejected_candidate_count": len(failure.rejected_candidate_ids),
                    "previous_query": failure.query,
                    "previous_top_k": failure.top_k,
                    "reflect_instruction": (
                        "Consecutive severe retrieval drift reached the retry limit; do not "
                        "search this platform again. Hand off the typed no-candidate result "
                        "through shopping_summary."
                        if failure.attempts >= 2 and self.task_scope is not None
                        else "Consecutive severe retrieval drift reached the retry limit; do "
                        "not search this platform again. Treat the empty result as verified "
                        "input to the remaining trusted no-match gates."
                        if failure.attempts >= 2
                        else "Revise the query without dropping the target product, hard "
                        "constraints, or soft preferences, then call item_search again for "
                        "this platform."
                    ),
                }
                for failure in state.semantic_assertion_failures
            ]
        if handoffs:
            observation["handoffs"] = [
                {
                    "status": handoff.status,
                    "platforms": [result.platform.value for result in handoff.item_results],
                    "candidate_count": len(handoff.candidate_ids),
                    "candidate_ids": list(handoff.candidate_ids[:4]),
                    "evidence_count": len(handoff.evidence_ids),
                    "evidence_ids": list(handoff.evidence_ids[:4]),
                    "completed_tools": [tool.value for tool in handoff.completed_tools],
                    "has_price_result": handoff.price_result is not None,
                    "has_shipping_result": handoff.shipping_result is not None,
                    "has_picker_result": handoff.picker_result is not None,
                }
                for handoff in handoffs
            ]
        return observation

    def _record_tool(self, tool_name: ToolName, outcome: str) -> None:
        self._tool_counts[tool_name] = self._tool_counts.get(tool_name, 0) + 1
        previous = self._tool_outcomes.get(tool_name)
        if previous is None or (previous == "SUCCESS" and outcome != "SUCCESS"):
            self._tool_outcomes[tool_name] = outcome
        elif previous != "SUCCESS" and outcome != "SUCCESS":
            self._tool_outcomes[tool_name] = min(previous, outcome)

    def _tool_summary(self) -> tuple[AgentToolSummary, ...]:
        return tuple(
            AgentToolSummary(
                tool_name=tool_name,
                call_count=self._tool_counts[tool_name],
                safe_outcome=self._tool_outcomes[tool_name],
            )
            for tool_name in ToolName
            if tool_name in self._tool_counts
        )

    def _emit_tool_finished(self, tool_name: ToolName, code: str) -> None:
        platforms: tuple[Platform, ...] = ()
        candidate_count: int | None = None
        if tool_name is ToolName.ITEM_SEARCH and code == "SUCCESS":
            pending = self._pending_action
            platform_value = None if pending is None else pending.arguments.get("platform")
            if type(platform_value) is not str:
                raise ToolSessionError(AgentFailureCode.INTERNAL_ERROR.value)
            try:
                platform = Platform(platform_value)
            except (TypeError, ValueError):
                raise ToolSessionError(AgentFailureCode.INTERNAL_ERROR.value) from None
            result = next(
                (
                    item
                    for item in reversed(self._require_state().item_results)
                    if item.platform is platform
                ),
                None,
            )
            if result is None:
                raise ToolSessionError(AgentFailureCode.INTERNAL_ERROR.value)
            platforms = (platform,)
            candidate_count = len(result.candidates)
        self._emit(
            AgentRunEvent(
                kind=AgentEventKind.TOOL_FINISHED,
                run_id=self.run_id,
                tool_name=tool_name,
                safe_code=code,
                platforms=platforms,
                candidate_count=candidate_count,
                trace_bullets=self._trace_bullets_for_tool(tool_name, code),
            )
        )

    def _trace_bullets_for_tool(
        self,
        tool_name: ToolName,
        code: str,
    ) -> tuple[AgentTraceBullet, ...]:
        """Project bounded, source-labelled facts from trusted session state.

        These values are deliberately not model narration.  They are copied
        only from the validated user intent, typed tool outputs, or verified
        runtime state after the tool has finished.
        """

        if code != "SUCCESS":
            return (
                AgentTraceBullet(
                    label="执行结果",
                    value=code,
                    source=AgentTraceSource.VERIFIED_STATE,
                ),
            )

        state = self._require_state()
        bullets: list[AgentTraceBullet] = []

        def add(label: str, value: str, source: AgentTraceSource) -> None:
            normalized = " ".join(value.split()).strip()
            if normalized and len(bullets) < 6:
                bullets.append(
                    AgentTraceBullet(
                        label=label,
                        value=normalized[:192],
                        source=source,
                    )
                )

        if tool_name is ToolName.PLANNER:
            interpreted = self._require_interpreted(state)
            for criterion in interpreted.required:
                if type(criterion) is TargetCategory:
                    label = "目标商品"
                elif type(criterion) is BudgetMax:
                    label = "预算"
                elif type(criterion) is StockRequired:
                    label = "库存要求"
                elif type(criterion) is Exclusion:
                    label = "排除项"
                else:
                    continue
                add(label, criterion.source_span.text, AgentTraceSource.USER_INPUT)
            for preferred in interpreted.preferred:
                add("偏好", preferred.source_span.text, AgentTraceSource.USER_INPUT)
            if state.plan is not None:
                add(
                    "检索平台",
                    "、".join(platform.value for platform in state.plan.platforms),
                    AgentTraceSource.VERIFIED_STATE,
                )
        elif tool_name is ToolName.WEB_SEARCH and state.web_result is not None:
            add(
                "独立证据",
                f"已核验 {len(state.web_result.evidence)} 条网页证据",
                AgentTraceSource.TRUSTED_TOOL,
            )
        elif tool_name is ToolName.CATEGORY_INSIGHT and state.category_result is not None:
            result = state.category_result
            add("目标商品", result.category, AgentTraceSource.USER_INPUT)
            add(
                "类目知识",
                "已加载" if result.status is InsightStatus.FOUND else "暂无可用资料",
                AgentTraceSource.TRUSTED_TOOL,
            )
            if result.components:
                add(
                    "结构化资料",
                    f"已读取 {len(result.components)} 项核心组成与规格信息",
                    AgentTraceSource.TRUSTED_TOOL,
                )
        elif tool_name is ToolName.ITEM_SEARCH:
            pending = self._pending_action
            platform_value = None if pending is None else pending.arguments.get("platform")
            if type(platform_value) is not str:
                return ()
            try:
                platform = Platform(platform_value)
            except (TypeError, ValueError):
                return ()
            item_result = next(
                (item for item in reversed(state.item_results) if item.platform is platform),
                None,
            )
            if item_result is not None:
                add("检索平台", platform.value, AgentTraceSource.TRUSTED_TOOL)
                add(
                    "可信候选",
                    f"召回 {item_result.total_recall} 个, "
                    f"语义核验后保留 {len(item_result.candidates)} 个",
                    AgentTraceSource.TRUSTED_TOOL,
                )
        elif tool_name is ToolName.PRICE_COMPARE and state.price_result is not None:
            add(
                "价格核验",
                f"已换算并比较 {len(state.price_result.ranked)} 个候选",
                AgentTraceSource.TRUSTED_TOOL,
            )
            add(
                "比较范围",
                "全部可信候选"
                if state.price_result.comparison_scope is PriceComparisonScope.ALL_RETRIEVED
                else "同款可比候选",
                AgentTraceSource.TRUSTED_TOOL,
            )
        elif tool_name is ToolName.SHIPPING_CALC and state.shipping_result is not None:
            add(
                "到手成本",
                f"已核算 {len(state.shipping_result.advisories)} 个候选",
                AgentTraceSource.TRUSTED_TOOL,
            )
            add(
                "报价口径",
                "商品价与估算费用合计后的参考到手价",
                AgentTraceSource.VERIFIED_STATE,
            )
        elif tool_name is ToolName.ITEM_PICKER and state.picker_result is not None:
            add(
                "最终候选",
                f"保留 {len(state.picker_result.picks)} 个可展示商品",
                AgentTraceSource.TRUSTED_TOOL,
            )
            for pick in state.picker_result.picks[:2]:
                add(pick.platform.value, pick.title, AgentTraceSource.TRUSTED_TOOL)
        elif tool_name is ToolName.SHOPPING_SUMMARY:
            if self.task_scope is not None:
                add("任务结果", "独立检索任务已交接", AgentTraceSource.VERIFIED_STATE)
            elif self._response is not None:
                add("研究状态", self._response.status.value, AgentTraceSource.VERIFIED_STATE)
                add(
                    "展示结果",
                    f"返回 {len(self._response.selected_product_ids)} 个商品",
                    AgentTraceSource.TRUSTED_TOOL,
                )
        elif tool_name is ToolName.CHAT_FALLBACK:
            add("研究结果", "当前请求不进入商品检索", AgentTraceSource.VERIFIED_STATE)

        return tuple(bullets)

    def _emit(self, event: AgentRunEvent) -> None:
        self._events.append(event)
        if self.observer is not None:
            with suppress(Exception):
                self.observer.on_event(event)

    def _fail(self, code: str) -> None:
        if self._failure_code is None:
            self._failure_code = code

    def _require_state(self) -> _SessionState:
        if self._state is None:
            raise ToolSessionError(AgentFailureCode.INVALID_ACTION.value)
        return self._state

    @staticmethod
    def _require_interpreted(state: _SessionState) -> InterpretedRequest:
        if type(state.interpreted_request) is not InterpretedRequest:
            raise ToolSessionError(AgentFailureCode.INVALID_ACTION.value)
        return state.interpreted_request


def _compile_planner_intent(
    query: str,
    decision: PlannerDecisionInput,
) -> InterpretedRequest:
    """Compile model-authored planning facts without inferring any missing fact."""

    if not _coordinated_preferences_are_complete(query, decision):
        raise ValueError("planner omitted a coordinated preference")

    required: list[BudgetMax | StockRequired | Exclusion] = []
    preferred: list[PreferredCriterion] = []
    if decision.budget is not None:
        upper_bound = Decimal(decision.budget.maximum)
        base_amount = Decimal(decision.budget.base_amount)
        lower_bound = (
            None if decision.budget.lower_bound is None else Decimal(decision.budget.lower_bound)
        )
        quoted_amounts = list(budget_amount_evidence(decision.budget.source_text))
        if base_amount in quoted_amounts:
            quoted_amounts.remove(base_amount)
        explicit_maximum = (
            None
            if decision.budget.explicit_maximum is None
            else Decimal(decision.budget.explicit_maximum)
        )
        if explicit_maximum is None and upper_bound in quoted_amounts:
            explicit_maximum = upper_bound
        if explicit_maximum is not None and explicit_maximum in quoted_amounts:
            quoted_amounts.remove(explicit_maximum)
        grounded_allowances: list[Decimal] = []
        for value in (Decimal(item) for item in decision.budget.allowance_amounts):
            if value in quoted_amounts:
                quoted_amounts.remove(value)
                grounded_allowances.append(value)
            elif explicit_maximum is None:
                grounded_allowances.append(value)
        named_currencies = budget_currency_evidence(decision.budget.source_text)
        currency = named_currencies[0] if len(named_currencies) == 1 else None
        required.append(
            BudgetMax(
                mode=decision.budget.mode,
                target_amount=(base_amount if decision.budget.mode == "around" else upper_bound),
                lower_bound=lower_bound,
                upper_bound=upper_bound,
                currency=currency,
                source_span=_grounded_source_span(query, decision.budget.source_text),
                calculation=(
                    BudgetCalculation(
                        base_amount=base_amount,
                        allowance_amounts=tuple(grounded_allowances),
                        explicit_maximum=explicit_maximum,
                    )
                    if decision.budget.mode == "maximum"
                    and (grounded_allowances or explicit_maximum is not None)
                    else None
                ),
            )
        )
    if decision.stock_source_text is not None:
        required.append(
            StockRequired(
                source_span=_grounded_source_span(query, decision.stock_source_text),
            )
        )
    required.extend(
        Exclusion(
            value=criterion.value,
            source_span=_grounded_source_span(query, criterion.source_text),
        )
        for criterion in decision.exclusions
    )
    # Soft preferences are useful planner structure, but never authority for
    # importing private-store meaning. Keep only verbatim current-query spans;
    # the model's bounded semantic value drives verified attribute matching.
    preferred.extend(
        PreferredCriterion(
            value=criterion.source_text,
            source_span=_grounded_source_span(query, criterion.source_text),
        )
        for criterion in decision.preferences
        if criterion.source_text in query
    )
    required.sort(key=lambda criterion: (criterion.source_span.start, criterion.kind))
    preferred.sort(key=lambda criterion: (criterion.source_span.start, criterion.value))
    return InterpretedRequest(
        required=tuple(required),
        preferred=tuple(preferred),
        parser_version="agent-loop-v1",
    )


def _category_phrase_is_grounded(query: str, category: str) -> bool:
    """Require model-selected category scope to be quoted from the user.

    The check has no category vocabulary. It prevents an Agent from silently
    narrowing an ambiguous product request to an invented subtype or form
    factor before consulting category knowledge.
    """

    if type(query) is not str or type(category) is not str:
        return False
    normalized = " ".join(category.split())
    return bool(normalized) and normalized.casefold() in query.casefold()


_PREFERENCE_CLAUSE_BOUNDARIES = frozenset(",，、。；;！？!?：:\n")  # noqa: RUF001
_PREFERENCE_COORDINATOR = re.compile(
    r"(?:&|和|与|及|以及|并|并且|and|or)\s*$",
    re.IGNORECASE,
)


def _coordinated_preferences_are_complete(
    query: str,
    decision: PlannerDecisionInput,
) -> bool:
    """Reject a quoted final conjunct when an earlier conjunct was omitted.

    This is a language-structure guard, not a product vocabulary parser. It
    never decides what a preference means and knows no category attributes. It
    only verifies that, once the model declares a preference after a coordinator,
    the preceding coordinated text is also covered by a declared preference.
    """

    spans = tuple(
        _grounded_source_span(query, criterion.source_text)
        for criterion in decision.preferences
        if criterion.source_text in query
    )
    for span in spans:
        clause_start = span.start
        while clause_start > 0 and query[clause_start - 1] not in _PREFERENCE_CLAUSE_BOUNDARIES:
            clause_start -= 1
        prefix = query[clause_start : span.start]
        coordinator = _PREFERENCE_COORDINATOR.search(prefix)
        if coordinator is None or not prefix[: coordinator.start()].strip():
            continue
        coordinator_start = clause_start + coordinator.start()
        if not any(
            other is not span and other.start < coordinator_start and other.end > clause_start
            for other in spans
        ):
            return False
    return True


def _compile_comparison_targets(
    query: str,
    decision: PlannerDecisionInput,
) -> tuple[ComparisonTarget, ...]:
    """Ground every model-declared comparison target in the locked query."""

    targets: list[ComparisonTarget] = []
    occupied: list[tuple[int, int]] = []
    for target in decision.comparison_targets:
        start = query.find(target.source_text)
        while start >= 0:
            end = start + len(target.source_text)
            if all(end <= used_start or start >= used_end for used_start, used_end in occupied):
                break
            start = query.find(target.source_text, start + 1)
        if start < 0:
            raise ValueError("planner comparison target evidence must occur distinctly")
        span = SourceSpan(start=start, end=end, text=target.source_text)
        occupied.append((span.start, span.end))
        targets.append(ComparisonTarget(source_span=span, search_query=target.source_text))
    return tuple(targets)


def _required_search_queries(plan: PlannerOutput) -> tuple[str, ...]:
    if type(plan) is not PlannerOutput or plan.intent_kind is not PlannerIntentKind.SHOPPING:
        return ()
    if plan.comparison_targets:
        return tuple(target.search_query for target in plan.comparison_targets)
    return () if plan.search_query is None else (plan.search_query,)


def _semantic_assertion_target(
    *,
    category: str,
    target_query: str,
    comparison_targets: tuple[ComparisonTarget, ...],
) -> str:
    """Return only user-grounded text as the semantic rejection authority.

    Planner and child search queries are deliberately free to translate or broaden
    wording for catalog recall.  That freedom cannot authorize them to narrow the
    user's requested product.  Exact comparison targets are already source-grounded;
    all other searches are checked against the exact category phrase selected from
    the locked current message.
    """

    if any(target.search_query == target_query for target in comparison_targets):
        return target_query
    return category


def _target_candidate_groups(state: _SessionState) -> tuple[TargetCandidateGroup, ...]:
    plan = state.plan
    if plan is None or not plan.comparison_targets:
        return ()
    return tuple(
        TargetCandidateGroup(
            target_query=target.search_query,
            candidate_ids=tuple(
                dict.fromkeys(
                    candidate.candidate_id
                    for result in state.item_results
                    if result.target_query == target.search_query
                    for candidate in result.candidates
                )
            ),
        )
        for target in plan.comparison_targets
    )


def _preference_augmented_query(
    query: str,
    preferred: tuple[PreferredCriterion, ...],
) -> str:
    """Keep explicit current-turn preferences in the bounded retrieval query."""

    parts = [query.strip()]
    normalized = query.casefold()
    for criterion in preferred:
        value = criterion.source_span.text.strip()
        if not value or value.casefold() in normalized:
            continue
        candidate = " ".join((*parts, value))
        if len(candidate) > 512:
            break
        parts.append(value)
        normalized = candidate.casefold()
    return " ".join(parts)


def _next_item_search(
    state: _SessionState,
    *,
    platform: Platform,
    allow_refinement: bool = False,
) -> tuple[str, int] | None:
    plan = state.plan
    if (
        plan is None
        or plan.intent_kind is not PlannerIntentKind.SHOPPING
        or platform not in plan.platforms
    ):
        return None
    required_queries = _required_search_queries(plan)
    for target_query in required_queries:
        failure = next(
            (
                item
                for item in state.semantic_assertion_failures
                if item.platform is platform and item.target_query == target_query
            ),
            None,
        )
        if failure is not None and failure.attempts < 2:
            return target_query, state.request.top_k
    completed_targets = {
        result.target_query
        for result in state.item_results
        if result.platform is platform and result.candidates
    }
    pending = tuple(
        target_query
        for target_query in _required_search_queries(plan)
        if target_query not in completed_targets
    )
    for query in pending:
        failure = next(
            (
                item
                for item in state.semantic_assertion_failures
                if item.platform is platform and item.target_query == query
            ),
            None,
        )
        if failure is None:
            return query, state.request.top_k
        if failure.attempts < 2:
            return query, state.request.top_k
    if allow_refinement and len(required_queries) == 1:
        return required_queries[0], state.request.top_k
    return None


def _grounded_source_span(query: str, source_text: str) -> SourceSpan:
    start = query.find(source_text)
    if start < 0:
        raise ValueError("planner evidence must occur in the locked query")
    return SourceSpan(start=start, end=start + len(source_text), text=source_text)


def _category_insight_reference(result: CategoryInsightOutput) -> str:
    if type(result) is not CategoryInsightOutput or result.status is not InsightStatus.FOUND:
        raise ToolSessionError("ITEM_SOURCE_INVALID")
    digest = hashlib.sha256(result.model_dump_json().encode("utf-8")).hexdigest()[:24]
    return "category-insight-" + digest


def _inferred_category_depth(result: CategoryInsightOutput) -> InsightDepth:
    return InsightDepth.DEEP if result.attributes else InsightDepth.QUICK


def _category_insight_can_run(
    state: _SessionState,
    *,
    category: str,
    depth: InsightDepth,
) -> bool:
    result = state.category_result
    if result is None:
        return True
    if " ".join(result.category.split()).casefold() != " ".join(category.split()).casefold():
        return False
    observed_depth = state.category_depth or _inferred_category_depth(result)
    return observed_depth is InsightDepth.QUICK and depth is InsightDepth.DEEP


def _slice_item_result(
    result: ItemSearchRuntimeResult,
    *,
    selected_candidate_ids: set[str],
) -> ItemSearchRuntimeResult | None:
    """Project one result to exactly the candidate facts granted to a child."""

    selected = tuple(
        candidate
        for candidate in result.candidates
        if candidate.candidate_id in selected_candidate_ids
    )
    if not selected:
        return None
    omitted_returned = len(result.candidates) - len(selected)
    source = result.platform_sub_batch
    product_ids = {candidate.item_id for candidate in selected}
    products = tuple(product for product in source.products if product.product_id in product_ids)
    if {product.product_id for product in products} != product_ids:
        raise ValueError("scoped item result product closure is incomplete")
    offers = tuple(offer for offer in source.offers if offer.product_id in product_ids)
    if not offers:
        raise ValueError("scoped item result offer closure is incomplete")
    evidence_ids = {
        *(binding.evidence_id for product in products for binding in product.field_evidence),
        *(attribute.evidence_id for product in products for attribute in product.attributes),
        *(binding.evidence_id for offer in offers for binding in offer.field_evidence),
    }
    if source.exchange_rates is not None:
        evidence_ids.update(rate.evidence_id for rate in source.exchange_rates.rates)
    evidence = tuple(
        evidence for evidence in source.evidence if evidence.evidence_id in evidence_ids
    )
    if {evidence.evidence_id for evidence in evidence} != evidence_ids:
        raise ValueError("scoped item result evidence closure is incomplete")
    return ItemSearchRuntimeResult(
        platform=result.platform,
        target_query=result.target_query,
        retrieval_query=result.retrieval_query,
        candidates=selected,
        platform_sub_batch=CatalogBatch(
            snapshot_version=source.snapshot_version,
            products=products,
            offers=offers,
            evidence=evidence,
            exchange_rates=source.exchange_rates,
        ),
        total_recall=result.total_recall - omitted_returned,
        returned_before_semantic_filter=(result.returned_before_semantic_filter - omitted_returned),
        truncated=result.truncated,
    )


def _safe_item_observation(result: ItemSearchRuntimeResult) -> dict[str, object]:
    """Return a compact, data-only view of one verified platform result."""

    attribute_counts: dict[str, int] = {}
    for candidate in result.candidates:
        for attribute in candidate.attributes:
            value = _safe_observation_term(attribute.value)
            if value is None:
                continue
            key = f"{attribute.name}={value}"
            attribute_counts[key] = attribute_counts.get(key, 0) + 1
    return {
        "platform": result.platform.value,
        "target_query": result.target_query,
        "retrieval_query": result.retrieval_query,
        "candidate_count": len(result.candidates),
        "total_recall": result.total_recall,
        "truncated": result.truncated,
        "semantic_rejected_count": (
            result.returned_before_semantic_filter - len(result.candidates)
        ),
        "candidate_ids": [candidate.candidate_id for candidate in result.candidates[:4]],
        "titles": [candidate.title for candidate in result.candidates[:4]],
        "candidates": [
            {
                "candidate_id": candidate.candidate_id,
                "title": candidate.title,
                "attributes": {
                    attribute.name: value
                    for attribute in candidate.attributes[:8]
                    if (value := _safe_observation_term(attribute.value)) is not None
                },
            }
            for candidate in result.candidates[:4]
        ],
        "attribute_counts": dict(sorted(attribute_counts.items())[:8]),
    }


def _unpriced_candidate_ids(
    pool: ValidatedCandidatePool,
    prices: PriceCompareOutput,
) -> tuple[str, ...]:
    priced_ids = {point.candidate_id for point in prices.ranked}
    return tuple(
        candidate.candidate_id
        for candidate in pool.candidates
        if candidate.candidate_id not in priced_ids
    )


def _retain_semantically_relevant_candidates(
    result: ItemSearchRuntimeResult,
    assertion: SemanticAssertionOutput,
) -> ItemSearchRuntimeResult:
    """Remove semantic drift before candidates can enter price comparison."""

    observed_ids = {candidate.candidate_id for candidate in result.candidates}
    relevant_ids = set(assertion.relevant_candidate_ids)
    if not relevant_ids.issubset(observed_ids):
        raise ToolSessionError("SEMANTIC_ASSERTION_INVALID")
    candidates = tuple(
        candidate for candidate in result.candidates if candidate.candidate_id in relevant_ids
    )
    product_ids = {candidate.item_id for candidate in candidates}
    offers = tuple(
        offer for offer in result.platform_sub_batch.offers if offer.product_id in product_ids
    )
    offer_ids = {offer.offer_id for offer in offers}
    evidence = tuple(
        item
        for item in result.platform_sub_batch.evidence
        if item.entity_type is EvidenceEntityType.EXCHANGE_RATE
        or (item.entity_type is EvidenceEntityType.PRODUCT and item.product_id in product_ids)
        or (
            item.entity_type is EvidenceEntityType.OFFER
            and item.product_id in product_ids
            and item.offer_id in offer_ids
        )
    )
    batch = CatalogBatch(
        snapshot_version=result.platform_sub_batch.snapshot_version,
        products=tuple(
            product
            for product in result.platform_sub_batch.products
            if product.product_id in product_ids
        ),
        offers=offers,
        evidence=evidence,
        exchange_rates=result.platform_sub_batch.exchange_rates,
        quarantine_issues=result.platform_sub_batch.quarantine_issues,
        fatal_issues=result.platform_sub_batch.fatal_issues,
    )
    return ItemSearchRuntimeResult(
        platform=result.platform,
        target_query=result.target_query,
        retrieval_query=result.retrieval_query,
        candidates=candidates,
        platform_sub_batch=batch,
        total_recall=result.total_recall,
        returned_before_semantic_filter=len(result.candidates),
        truncated=result.truncated,
    )


def _safe_observation_term(value: str) -> str | None:
    """Keep a short data token; control text never enters a model receipt."""

    if (
        type(value) is not str
        or not value
        or len(value) > 64
        or any(character in value for character in ("\0", "\n", "\r", "\t"))
    ):
        return None
    return value


def _require_arguments(arguments: Mapping[str, object], expected: tuple[str, ...]) -> None:
    if tuple(sorted(arguments)) != tuple(sorted(expected)):
        raise ToolSessionError(AgentFailureCode.INVALID_ACTION.value)


def _argument_text(value: object) -> str:
    if type(value) is not str:
        raise ToolSessionError(AgentFailureCode.INVALID_ACTION.value)
    return value


def _receipt(
    *,
    tool_name: ToolName,
    status: str,
    platforms: tuple[Platform, ...] = (),
    child_count: int = 0,
    safe_code: str | None = None,
    observation: Mapping[str, object] | None = None,
) -> str:
    """Return the only model-visible, bounded observation from a tool call."""

    payload: dict[str, object] = {
        "status": status,
        "tool": tool_name.value,
    }
    if platforms:
        payload["platforms"] = [platform.value for platform in platforms]
    if child_count:
        payload["child_count"] = child_count
    if safe_code is not None:
        payload["safe_code"] = safe_code
    if observation:
        payload["observation"] = dict(observation)
    encoded = compact_dumps(payload)
    if len(encoded.encode("utf-8")) <= MODEL_RECEIPT_BYTE_LIMIT:
        return encoded
    if observation:
        payload["observation"] = _compact_observation(observation)
        encoded = compact_dumps(payload)
        if len(encoded.encode("utf-8")) <= MODEL_RECEIPT_BYTE_LIMIT:
            return encoded
        payload["observation"] = {"compacted": True}
        encoded = compact_dumps(payload)
    if len(encoded.encode("utf-8")) > MODEL_RECEIPT_BYTE_LIMIT:
        raise ToolSessionError(AgentFailureCode.BUDGET_EXCEEDED.value)
    return encoded


def _is_rejected_receipt(value: str) -> bool:
    """Keep confirmed rejections in history without charging a tool execution."""

    try:
        payload = loads_unique(value)
    except (TypeError, ValueError) as error:
        raise ToolSessionError("DURABLE_CHECKPOINT_INVALID") from error
    if type(payload) is not dict:
        raise ToolSessionError("DURABLE_CHECKPOINT_INVALID")
    status = payload.get("status")
    if type(status) is not str:
        raise ToolSessionError("DURABLE_CHECKPOINT_INVALID")
    return status == "rejected"


def _compact_observation(observation: Mapping[str, object]) -> dict[str, object]:
    """Reduce optional display detail while retaining decision-critical state."""

    compact: dict[str, object] = {}
    search_query = observation.get("search_query")
    if type(search_query) is str:
        compact["search_query"] = search_query
    category = observation.get("category")
    if type(category) is dict:
        compact["category"] = {
            key: category[key] for key in ("status", "category", "context_ref") if key in category
        }
    web = observation.get("web")
    if type(web) is dict:
        compact["web"] = {key: web[key] for key in ("evidence_count", "evidence_ids") if key in web}
    items = observation.get("items")
    if type(items) is list:
        compact["items"] = [
            {
                key: item[key]
                for key in (
                    "platform",
                    "target_query",
                    "retrieval_query",
                    "candidate_count",
                    "total_recall",
                    "truncated",
                    "semantic_rejected_count",
                    "candidate_ids",
                )
                if key in item
            }
            for item in items[:4]
            if type(item) is dict
        ]
    for summary_name in ("price_summary", "shipping_summary"):
        summary = observation.get(summary_name)
        if type(summary) is dict:
            compact[summary_name] = dict(summary)
    picker = observation.get("picker")
    if type(picker) is dict:
        picks = picker.get("picks")
        compact["picker"] = {
            "picks": [
                {
                    key: pick[key]
                    for key in ("candidate_id", "platform", "preferences")
                    if key in pick
                }
                for pick in (picks[:3] if type(picks) is list else [])
                if type(pick) is dict
            ],
            "comparison_targets": picker.get("comparison_targets", []),
        }
    assertions = observation.get("assertions_failed")
    if type(assertions) is list:
        compact["assertions_failed"] = [
            {
                key: failure[key]
                for key in (
                    "type",
                    "tool",
                    "platform",
                    "target_query",
                    "previous_query",
                    "previous_top_k",
                    "rejected_candidate_count",
                    "reflect_instruction",
                )
                if key in failure
            }
            for failure in assertions[:4]
            if type(failure) is dict
        ]
    handoffs = observation.get("handoffs")
    if type(handoffs) is list:
        compact["handoffs"] = [
            {
                key: handoff[key]
                for key in (
                    "status",
                    "platforms",
                    "candidate_count",
                    "candidate_ids",
                    "completed_tools",
                    "has_price_result",
                    "has_shipping_result",
                    "has_picker_result",
                )
                if key in handoff
            }
            for handoff in handoffs[:4]
            if type(handoff) is dict
        ]
    return compact


def _safe_code(error: Exception) -> str:
    if isinstance(error, ToolSessionError):
        return error.code
    if isinstance(error, ToolPortError):
        return error.code.value
    if isinstance(error, TimeoutError):
        return AgentFailureCode.DEADLINE_EXCEEDED.value
    return AgentFailureCode.INTERNAL_ERROR.value


__all__ = [
    "MODEL_RECEIPT_BYTE_LIMIT",
    "AgentLoopAction",
    "AgentRuntimeConfig",
    "AgentSessionCheckpointCodec",
    "AgentSessionSnapshot",
    "CandidateManifestFactory",
    "ChildContextSeed",
    "ChildHandoff",
    "ChildTaskScope",
    "DispatchExecutor",
    "ResourceDrainer",
    "ShoppingToolSession",
    "ToolSessionError",
]
