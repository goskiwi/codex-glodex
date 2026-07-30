"""Immutable Agent state, exact resource budgets, and canonical byte accounting."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum, StrEnum
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

from glodex.application.agent.contracts import (
    AgentCapabilities,
    CategoryInsightOutput,
    ForkResult,
    ItemPickerOutput,
    ItemSearchRuntimeResult,
    PlannerOutput,
    PriceCompareOutput,
    ShippingCalcOutput,
    ToolName,
    WebSearchOutput,
)
from glodex.contracts import SearchRequest
from glodex.domain.intent import InterpretedRequest

if TYPE_CHECKING:
    from glodex.application.agent.catalog import CandidateStore, InMemoryCatalogGateway

ROOT_MODEL_ACTION_LIMIT = 10
ROOT_TOOL_EXECUTION_LIMIT = 10
CHILD_RUN_LIMIT = 4
CHILD_CONCURRENCY_LIMIT = 4
FORK_DEPTH_LIMIT = 2
TREE_DEEPSEEK_LIMIT = 14
TREE_BUSINESS_TOOL_LIMIT = 11
TREE_DISPATCH_LIMIT = 2
TOOL_RESULT_BYTE_LIMIT = 512 * 1024
CANDIDATE_STORE_BYTE_LIMIT = 2 * 1024 * 1024
OBSERVATION_BYTE_LIMIT = 8 * 1024
TREE_OBSERVATION_BYTE_LIMIT = 32 * 1024
DEEPSEEK_BODY_BYTE_LIMIT = 65_536
DEEPSEEK_OUTPUT_TOKEN_LIMIT = 1_024
CHILD_DEADLINE_SECONDS = 45
AGENT_DEADLINE_SECONDS = 240

type LedgerCounterName = Literal[
    "root_model_actions",
    "root_tool_executions",
    "child_runs",
    "tree_deepseek_calls",
    "tree_business_tool_executions",
    "tree_dispatch_executions",
    "tavily_calls",
    "dashscope_batches",
    "ebay_auth_calls",
    "ebay_browse_calls",
]


class BudgetExceeded(RuntimeError):
    """A safe, stable resource-limit failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def canonical_json_bytes(value: object) -> bytes:
    """Serialize trusted typed values with the single M1d byte-counting format."""

    normalized = _canonical_value(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def require_byte_limit(value: object, *, maximum: int, code: str) -> bytes:
    """Return canonical bytes or fail on the first one-more byte."""

    if type(maximum) is not int or isinstance(maximum, bool) or maximum < 0:
        raise ValueError("maximum must be a non-negative integer")
    encoded = canonical_json_bytes(value)
    if len(encoded) > maximum:
        raise BudgetExceeded(code)
    return encoded


def _canonical_value(value: object) -> object:
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is float:
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("canonical JSON does not support non-finite floats")
        return value
    if type(value) is Decimal:
        if not value.is_finite():
            raise ValueError("canonical JSON does not support non-finite Decimals")
        return format(value, "f")
    if type(value) is datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("canonical JSON datetimes must be UTC")
        return value.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        return _canonical_value(value.model_dump(mode="python"))
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _canonical_value(getattr(value, field.name)) for field in fields(value)}
    if type(value) is tuple or type(value) is list:
        return [_canonical_value(item) for item in value]
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError("canonical JSON mappings require string keys")
            normalized[key] = _canonical_value(item)
        return normalized
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class AgentBudgetLedger:
    """Run-tree usage counters whose replacement methods enforce exact maxima."""

    root_model_actions: int = 0
    root_tool_executions: int = 0
    child_runs: int = 0
    tree_deepseek_calls: int = 0
    tree_business_tool_executions: int = 0
    tree_dispatch_executions: int = 0
    tavily_calls: int = 0
    dashscope_batches: int = 0
    ebay_auth_calls: int = 0
    ebay_browse_calls: int = 0
    observation_bytes: int = 0
    tool_successes: tuple[tuple[ToolName, int], ...] = ()

    def __post_init__(self) -> None:
        scalar_limits = (
            ("root_model_actions", self.root_model_actions, ROOT_MODEL_ACTION_LIMIT),
            ("root_tool_executions", self.root_tool_executions, ROOT_TOOL_EXECUTION_LIMIT),
            ("child_runs", self.child_runs, CHILD_RUN_LIMIT),
            ("tree_deepseek_calls", self.tree_deepseek_calls, TREE_DEEPSEEK_LIMIT),
            (
                "tree_business_tool_executions",
                self.tree_business_tool_executions,
                TREE_BUSINESS_TOOL_LIMIT,
            ),
            (
                "tree_dispatch_executions",
                self.tree_dispatch_executions,
                TREE_DISPATCH_LIMIT,
            ),
            ("tavily_calls", self.tavily_calls, 1),
            ("dashscope_batches", self.dashscope_batches, 1),
            ("ebay_auth_calls", self.ebay_auth_calls, 1),
            ("ebay_browse_calls", self.ebay_browse_calls, 1),
            (
                "observation_bytes",
                self.observation_bytes,
                TREE_OBSERVATION_BYTE_LIMIT,
            ),
        )
        for name, value, maximum in scalar_limits:
            if type(value) is not int or isinstance(value, bool) or not 0 <= value <= maximum:
                raise ValueError(f"{name} must be between zero and {maximum}")
        if type(self.tool_successes) is not tuple or any(
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not ToolName
            or type(item[1]) is not int
            or isinstance(item[1], bool)
            or item[1] < 1
            for item in self.tool_successes
        ):
            raise TypeError("tool_successes must contain exact positive ToolName counts")
        tools = tuple(item[0] for item in self.tool_successes)
        if len(tools) != len(set(tools)):
            raise ValueError("tool_successes cannot repeat a tool")

    def consume_root_model_action(self) -> AgentBudgetLedger:
        return self._increment(
            "root_model_actions",
            ROOT_MODEL_ACTION_LIMIT,
            "ROOT_MODEL_ACTION_LIMIT",
            also=("tree_deepseek_calls", TREE_DEEPSEEK_LIMIT, "TREE_DEEPSEEK_LIMIT"),
        )

    def consume_child_model_action(self) -> AgentBudgetLedger:
        return self._increment(
            "tree_deepseek_calls",
            TREE_DEEPSEEK_LIMIT,
            "TREE_DEEPSEEK_LIMIT",
        )

    def consume_root_tool_execution(self) -> AgentBudgetLedger:
        return self._increment(
            "root_tool_executions",
            ROOT_TOOL_EXECUTION_LIMIT,
            "ROOT_TOOL_EXECUTION_LIMIT",
        )

    def consume_child_run(self) -> AgentBudgetLedger:
        return self._increment("child_runs", CHILD_RUN_LIMIT, "CHILD_RUN_LIMIT")

    def consume_business_tool(self, tool_name: ToolName) -> AgentBudgetLedger:
        if tool_name not in BUSINESS_TOOLS:
            raise ValueError("business tool accounting requires a business tool")
        next_ledger = self._increment(
            "tree_business_tool_executions",
            TREE_BUSINESS_TOOL_LIMIT,
            "TREE_BUSINESS_TOOL_LIMIT",
        )
        counts = dict(next_ledger.tool_successes)
        current = counts.get(tool_name, 0)
        maximum = 4 if tool_name is ToolName.ITEM_SEARCH else 1
        if current >= maximum:
            raise BudgetExceeded(f"{tool_name.value.upper()}_TOOL_LIMIT")
        counts[tool_name] = current + 1
        ordered = tuple(
            (candidate, counts[candidate]) for candidate in BUSINESS_TOOLS if candidate in counts
        )
        return replace(next_ledger, tool_successes=ordered)

    def consume_dispatch(self) -> AgentBudgetLedger:
        return self._increment(
            "tree_dispatch_executions",
            TREE_DISPATCH_LIMIT,
            "TREE_DISPATCH_LIMIT",
        )

    def consume_tavily(self) -> AgentBudgetLedger:
        return self._increment("tavily_calls", 1, "TAVILY_CALL_LIMIT")

    def consume_dashscope(self) -> AgentBudgetLedger:
        return self._increment("dashscope_batches", 1, "DASHSCOPE_CALL_LIMIT")

    def consume_ebay_auth(self) -> AgentBudgetLedger:
        return self._increment("ebay_auth_calls", 1, "EBAY_AUTH_LIMIT")

    def consume_ebay_browse(self) -> AgentBudgetLedger:
        return self._increment("ebay_browse_calls", 1, "EBAY_BROWSE_LIMIT")

    def add_observation_bytes(self, byte_count: int) -> AgentBudgetLedger:
        if type(byte_count) is not int or isinstance(byte_count, bool) or byte_count < 0:
            raise ValueError("observation byte count must be a non-negative integer")
        if byte_count > OBSERVATION_BYTE_LIMIT:
            raise BudgetExceeded("OBSERVATION_BYTE_LIMIT")
        if self.observation_bytes + byte_count > TREE_OBSERVATION_BYTE_LIMIT:
            raise BudgetExceeded("TREE_OBSERVATION_BYTE_LIMIT")
        return replace(self, observation_bytes=self.observation_bytes + byte_count)

    def _increment(
        self,
        field_name: LedgerCounterName,
        maximum: int,
        code: str,
        *,
        also: tuple[LedgerCounterName, int, str] | None = None,
    ) -> AgentBudgetLedger:
        current = self._counter_value(field_name)
        if current >= maximum:
            raise BudgetExceeded(code)
        also_value: tuple[LedgerCounterName, int] | None = None
        if also is not None:
            also_name, also_maximum, also_code = also
            also_current = self._counter_value(also_name)
            if also_current >= also_maximum:
                raise BudgetExceeded(also_code)
            also_value = (also_name, also_current + 1)
        result = self._replace_counter(field_name, current + 1)
        if also_value is not None:
            result = result._replace_counter(*also_value)
        return result

    def _counter_value(self, field_name: LedgerCounterName) -> int:
        match field_name:
            case "root_model_actions":
                return self.root_model_actions
            case "root_tool_executions":
                return self.root_tool_executions
            case "child_runs":
                return self.child_runs
            case "tree_deepseek_calls":
                return self.tree_deepseek_calls
            case "tree_business_tool_executions":
                return self.tree_business_tool_executions
            case "tree_dispatch_executions":
                return self.tree_dispatch_executions
            case "tavily_calls":
                return self.tavily_calls
            case "dashscope_batches":
                return self.dashscope_batches
            case "ebay_auth_calls":
                return self.ebay_auth_calls
            case "ebay_browse_calls":
                return self.ebay_browse_calls

    def _replace_counter(
        self,
        field_name: LedgerCounterName,
        value: int,
    ) -> AgentBudgetLedger:
        match field_name:
            case "root_model_actions":
                return replace(self, root_model_actions=value)
            case "root_tool_executions":
                return replace(self, root_tool_executions=value)
            case "child_runs":
                return replace(self, child_runs=value)
            case "tree_deepseek_calls":
                return replace(self, tree_deepseek_calls=value)
            case "tree_business_tool_executions":
                return replace(self, tree_business_tool_executions=value)
            case "tree_dispatch_executions":
                return replace(self, tree_dispatch_executions=value)
            case "tavily_calls":
                return replace(self, tavily_calls=value)
            case "dashscope_batches":
                return replace(self, dashscope_batches=value)
            case "ebay_auth_calls":
                return replace(self, ebay_auth_calls=value)
            case "ebay_browse_calls":
                return replace(self, ebay_browse_calls=value)


BUSINESS_TOOLS = frozenset(tool for tool in ToolName if tool is not ToolName.DISPATCH_TOOL)


class AgentPhase(StrEnum):
    NEEDS_PLAN = "NEEDS_PLAN"
    EVIDENCE_OR_ITEM = "EVIDENCE_OR_ITEM"
    NEEDS_PRICE_COMPARE = "NEEDS_PRICE_COMPARE"
    NEEDS_SHIPPING = "NEEDS_SHIPPING"
    NEEDS_PICKER = "NEEDS_PICKER"
    NEEDS_SUMMARY = "NEEDS_SUMMARY"
    TERMINAL = "TERMINAL"


class AgentTerminal(StrEnum):
    COMPLETED = "COMPLETED"
    NO_MATCH = "NO_MATCH"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class AgentToolState:
    """Explicit immutable facts visible to the root or one isolated child."""

    request: SearchRequest
    interpreted_request: InterpretedRequest
    required_baseline: InterpretedRequest
    capabilities: AgentCapabilities
    phase: AgentPhase = AgentPhase.NEEDS_PLAN
    plan: PlannerOutput | None = None
    ledger: AgentBudgetLedger = AgentBudgetLedger()
    action_signatures: tuple[str, ...] = ()
    category_result: CategoryInsightOutput | None = None
    web_result: WebSearchOutput | None = None
    item_results: tuple[ItemSearchRuntimeResult, ...] = ()
    price_result: PriceCompareOutput | None = None
    shipping_result: ShippingCalcOutput | None = None
    fork_results: tuple[ForkResult, ...] = ()
    publication_eligible_ids: tuple[str, ...] = ()
    picker_result: ItemPickerOutput | None = None
    terminal: AgentTerminal | None = None

    def __post_init__(self) -> None:
        if type(self.request) is not SearchRequest:
            raise TypeError("state request must be an exact SearchRequest")
        if type(self.interpreted_request) is not InterpretedRequest:
            raise TypeError("state intent must be an exact InterpretedRequest")
        if type(self.required_baseline) is not InterpretedRequest:
            raise TypeError("state baseline must be an exact InterpretedRequest")
        if type(self.capabilities) is not AgentCapabilities:
            raise TypeError("state capabilities must be exact AgentCapabilities")
        if type(self.phase) is not AgentPhase:
            raise TypeError("state phase must be an exact AgentPhase")
        if type(self.ledger) is not AgentBudgetLedger:
            raise TypeError("state ledger must be an exact AgentBudgetLedger")
        if type(self.action_signatures) is not tuple or any(
            type(signature) is not str or not signature for signature in self.action_signatures
        ):
            raise TypeError("state action signatures must contain non-empty strings")
        if len(self.action_signatures) != len(set(self.action_signatures)):
            raise ValueError("state action signatures must be unique")
        if type(self.item_results) is not tuple or any(
            type(item) is not ItemSearchRuntimeResult for item in self.item_results
        ):
            raise TypeError("state item results must contain exact results")
        if type(self.fork_results) is not tuple or any(
            type(result) is not ForkResult for result in self.fork_results
        ):
            raise TypeError("state fork results must contain exact ForkResult values")
        if type(self.publication_eligible_ids) is not tuple or any(
            type(candidate_id) is not str or not candidate_id
            for candidate_id in self.publication_eligible_ids
        ):
            raise TypeError("publication eligible IDs must be non-empty strings")
        if len(self.publication_eligible_ids) != len(set(self.publication_eligible_ids)):
            raise ValueError("publication eligible IDs must be unique")
        if self.phase is AgentPhase.TERMINAL and self.terminal is None:
            raise ValueError("terminal phase requires a terminal status")
        if self.phase is not AgentPhase.TERMINAL and self.terminal is not None:
            raise ValueError("terminal status requires terminal phase")


@dataclass(slots=True)
class RuntimeResources:
    """The only mutable root owner of run-scoped candidate and vector resources."""

    candidate_store: CandidateStore | None = None
    query_vectors: tuple[tuple[str, tuple[float, ...]], ...] = ()
    final_gateway: InMemoryCatalogGateway | None = None

    def install_candidate_store(self, store: CandidateStore) -> None:
        if self.candidate_store is not None:
            raise RuntimeError("candidate store is already installed")
        self.candidate_store = store

    def clear(self) -> None:
        if self.candidate_store is not None:
            self.candidate_store.clear()
        self.candidate_store = None
        self.query_vectors = ()
        if self.final_gateway is not None:
            self.final_gateway.clear()
        self.final_gateway = None


__all__ = [
    "AGENT_DEADLINE_SECONDS",
    "CANDIDATE_STORE_BYTE_LIMIT",
    "CHILD_CONCURRENCY_LIMIT",
    "CHILD_DEADLINE_SECONDS",
    "CHILD_RUN_LIMIT",
    "DEEPSEEK_BODY_BYTE_LIMIT",
    "DEEPSEEK_OUTPUT_TOKEN_LIMIT",
    "FORK_DEPTH_LIMIT",
    "OBSERVATION_BYTE_LIMIT",
    "ROOT_MODEL_ACTION_LIMIT",
    "ROOT_TOOL_EXECUTION_LIMIT",
    "TOOL_RESULT_BYTE_LIMIT",
    "TREE_BUSINESS_TOOL_LIMIT",
    "TREE_DEEPSEEK_LIMIT",
    "TREE_DISPATCH_LIMIT",
    "TREE_OBSERVATION_BYTE_LIMIT",
    "AgentBudgetLedger",
    "AgentPhase",
    "AgentTerminal",
    "AgentToolState",
    "BudgetExceeded",
    "RuntimeResources",
    "canonical_json_bytes",
    "require_byte_limit",
]
