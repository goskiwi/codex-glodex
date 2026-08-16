"""Public M6 safe-trace and aggregate operations DTOs; no private content fields."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from glodex.api.contracts import ApiDTO
from glodex.contracts import Identifier

M6SafeCode = Annotated[
    str,
    StringConstraints(min_length=1, max_length=96, pattern=r"^[A-Z0-9_]+$"),
]
M6Version = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
]


class M6UsageReceiptView(ApiDTO):
    status: Literal["REPORTED", "UNAVAILABLE"]
    provider: M6Version | None = None
    model: M6Version | None = None
    input_tokens: Annotated[int, Field(ge=0, le=10_000_000)] | None = None
    output_tokens: Annotated[int, Field(ge=0, le=10_000_000)] | None = None
    total_tokens: Annotated[int, Field(ge=0, le=10_000_000)] | None = None


class M6CostEstimateView(ApiDTO):
    status: Literal["REPORTED", "UNAVAILABLE"]
    price_table_version: M6Version | None = None
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] | None = None
    input_micro_units: Annotated[int, Field(ge=0)] | None = None
    output_micro_units: Annotated[int, Field(ge=0)] | None = None


class M6TraceEventView(ApiDTO):
    sequence: Annotated[int, Field(ge=1, le=96)]
    kind: Literal["RUN_STARTED", "OPERATION_FINISHED", "TERMINAL", "ALERT", "EXPORT"]
    operation: (
        Literal[
            "llm_tool_call",
            "llm_judge",
            "llm_reflect",
            "llm_summary",
            "llm_translation",
            "bge_embedding",
            "bge_rerank",
            "opensearch_retrieval",
        ]
        | None
    ) = None
    outcome: Literal["SUCCESS", "FAILURE", "REJECTED"] | None = None
    safe_code: M6SafeCode | None = None
    duration_ms: Annotated[int, Field(ge=0, le=120_000)] | None = None
    version: M6Version | None = None
    receipt: M6UsageReceiptView | None = None
    cost: M6CostEstimateView | None = None


class M6TraceView(ApiDTO):
    schema_version: Literal["glodex.m6-trace.v2"] = "glodex.m6-trace.v2"
    run_id: Identifier
    terminal_state: M6SafeCode | None = None
    events: Annotated[tuple[M6TraceEventView, ...], Field(max_length=96)]


class M6CostTotalView(ApiDTO):
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
    micro_units: Annotated[int, Field(ge=0)]


class M6BreakerView(ApiDTO):
    operation: Literal[
        "llm_tool_call",
        "llm_judge",
        "llm_reflect",
        "llm_summary",
        "llm_translation",
        "bge_embedding",
        "bge_rerank",
        "opensearch_retrieval",
    ]
    state: Literal["CLOSED", "OPEN", "HALF_OPEN"]


class M6AlertView(ApiDTO):
    code: Literal[
        "BREAKER_OPEN",
        "FAILURE_RATE_HIGH",
        "RECEIPT_UNAVAILABLE",
        "RETENTION_FAILED",
    ]


class M6OperationsView(ApiDTO):
    schema_version: Literal["glodex.m6-operations.v2"] = "glodex.m6-operations.v2"
    window: Literal["1h", "24h"]
    completed_count: Annotated[int, Field(ge=0)]
    no_match_count: Annotated[int, Field(ge=0)]
    failed_count: Annotated[int, Field(ge=0)]
    aborted_count: Annotated[int, Field(ge=0)]
    operation_count: Annotated[int, Field(ge=0)]
    operation_failure_count: Annotated[int, Field(ge=0)]
    receipt_reported_count: Annotated[int, Field(ge=0)]
    receipt_unavailable_count: Annotated[int, Field(ge=0)]
    cost_totals: Annotated[tuple[M6CostTotalView, ...], Field(max_length=8)]
    breakers: Annotated[tuple[M6BreakerView, ...], Field(max_length=8)]
    alerts: Annotated[tuple[M6AlertView, ...], Field(max_length=4)]


__all__ = [
    "M6AlertView",
    "M6BreakerView",
    "M6CostEstimateView",
    "M6CostTotalView",
    "M6OperationsView",
    "M6TraceEventView",
    "M6TraceView",
    "M6UsageReceiptView",
]
