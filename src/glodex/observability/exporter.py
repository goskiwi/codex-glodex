"""Explicit, private-free M6 trace export with a disabled-by-default boundary.

The exporter is intentionally a terminal-side observation.  It has no access to
durable requests, shopping results, identities, or provider response bodies, and
it never retries a failed delivery.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Final, Protocol
from urllib.parse import urlsplit

import httpx

from glodex.observability.runtime import (
    CostEstimate,
    M6RunTrace,
    M6TraceEvent,
    M6TraceEventDraft,
    ProviderUsageReceipt,
)
from glodex.runtime.contracts import validate_identifier

M6_EXPORT_SCHEMA_VERSION: Final = "glodex.m6-export.v1"
_M6_EXPORT_TIMEOUT_SECONDS: Final = 2.0
_MAX_ENDPOINT_LENGTH: Final = 512
_MAX_EXPORT_CREDENTIAL_LENGTH: Final = 512


class M6TraceExporter(Protocol):
    """One narrow post-terminal export operation, with no retry contract."""

    async def export_trace(self, *, document: M6ExportDocument) -> None: ...


class M6TraceExportStorePort(Protocol):
    """The small Durable-owned truth surface needed by terminal export only."""

    async def load_m6_trace(self, *, run_id: str) -> M6RunTrace: ...

    async def claim_m6_trace_export(self, *, run_id: str) -> bool: ...

    async def append_m6_trace_event(
        self, *, run_id: str, draft: M6TraceEventDraft
    ) -> M6TraceEvent: ...


@dataclass(frozen=True, slots=True)
class M6ExportDocument:
    """Canonical outbound allowlist; deliberately omits run, thread and user IDs."""

    schema_version: str
    terminal_state: str
    events: tuple[dict[str, object], ...]

    def __post_init__(self) -> None:
        if (
            self.schema_version != M6_EXPORT_SCHEMA_VERSION
            or type(self.terminal_state) is not str
            or not self.terminal_state
            or type(self.events) is not tuple
            or not self.events
            or len(self.events) > 96
            or any(type(event) is not dict for event in self.events)
        ):
            raise ValueError("M6 export document is invalid")

    def json_bytes(self) -> bytes:
        """Encode exactly the immutable allowlist, without a caller payload slot."""

        return json.dumps(
            {
                "events": self.events,
                "schema_version": self.schema_version,
                "terminal_state": self.terminal_state,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class DisabledTraceExporter:
    """Default exporter: no configuration lookup, client construction, or socket."""

    async def export_trace(self, *, document: M6ExportDocument) -> None:
        if type(document) is not M6ExportDocument:
            raise TypeError("M6 export document is invalid")
        return None


@dataclass(frozen=True, slots=True)
class HttpM6TraceExporter:
    """A deliberately tiny opt-in HTTPS adapter with one bounded attempt."""

    endpoint: str
    credential: str = field(repr=False)

    def __post_init__(self) -> None:
        _validate_export_endpoint(self.endpoint)
        _validate_export_credential(self.credential)

    async def export_trace(self, *, document: M6ExportDocument) -> None:
        if type(document) is not M6ExportDocument:
            raise TypeError("M6 export document is invalid")
        timeout = httpx.Timeout(_M6_EXPORT_TIMEOUT_SECONDS)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.post(
                self.endpoint,
                content=document.json_bytes(),
                headers={
                    "authorization": f"Bearer {self.credential}",
                    "content-type": "application/json",
                },
            )
        if not 200 <= response.status_code < 300:
            raise RuntimeError("M6 export response is unavailable")


def build_m6_trace_exporter(
    *,
    enabled: bool,
    endpoint: str | None = None,
    credential: str | None = None,
) -> M6TraceExporter:
    """Construct HTTP only with all explicit settings; disabled ignores all inputs."""

    if type(enabled) is not bool:
        raise TypeError("M6 export enabled flag is invalid")
    if not enabled:
        return DisabledTraceExporter()
    if type(endpoint) is not str or type(credential) is not str:
        raise ValueError("enabled M6 export requires endpoint and credential")
    return HttpM6TraceExporter(endpoint=endpoint, credential=credential)


def m6_export_document(*, trace: M6RunTrace) -> M6ExportDocument:
    """Project a terminal safe trace through the fixed export allowlist."""

    if type(trace) is not M6RunTrace or trace.terminal_state is None:
        raise ValueError("M6 export requires a terminal trace")
    return M6ExportDocument(
        schema_version=M6_EXPORT_SCHEMA_VERSION,
        terminal_state=trace.terminal_state,
        events=tuple(_event_document(event) for event in trace.events),
    )


async def export_terminal_trace(
    *,
    store: M6TraceExportStorePort,
    exporter: M6TraceExporter,
    run_id: str,
) -> None:
    """Try exactly once after terminal; any failure is only a safe trace fact."""

    validate_identifier(run_id, name="M6 export run ID")
    if type(exporter) is DisabledTraceExporter:
        return
    if (
        not callable(getattr(store, "load_m6_trace", None))
        or not callable(getattr(store, "claim_m6_trace_export", None))
        or not callable(getattr(store, "append_m6_trace_event", None))
    ):
        raise TypeError("M6 export store is invalid")
    trace = await store.load_m6_trace(run_id=run_id)
    if trace.terminal_state is None or not await store.claim_m6_trace_export(run_id=run_id):
        return
    from glodex.observability.runtime import M6TraceEventDraft, M6TraceEventKind

    try:
        await exporter.export_trace(document=m6_export_document(trace=trace))
        code = "TRACE_EXPORT_REPORTED"
    except Exception:
        code = "TRACE_EXPORT_UNAVAILABLE"
    try:
        await store.append_m6_trace_event(
            run_id=run_id,
            draft=M6TraceEventDraft(kind=M6TraceEventKind.EXPORT, safe_code=code),
        )
    except Exception:
        return


def _event_document(event: M6TraceEvent) -> dict[str, object]:
    """Copy only the finite safe trace fields; no extensible metadata is accepted."""

    draft = event.draft
    result: dict[str, object] = {
        "cost": _cost_document(draft.cost),
        "duration_ms": draft.duration_ms,
        "kind": draft.kind.value,
        "operation": None if draft.operation is None else draft.operation.value,
        "outcome": None if draft.outcome is None else draft.outcome.value,
        "receipt": _receipt_document(draft.receipt),
        "safe_code": draft.safe_code,
        "version": draft.version,
    }
    return result


def _receipt_document(receipt: ProviderUsageReceipt | None) -> dict[str, object] | None:
    if receipt is None:
        return None
    return {
        "input_tokens": receipt.input_tokens,
        "model": receipt.model,
        "output_tokens": receipt.output_tokens,
        "provider": receipt.provider,
        "status": receipt.status.value,
        "total_tokens": receipt.total_tokens,
    }


def _cost_document(cost: CostEstimate | None) -> dict[str, object] | None:
    if cost is None:
        return None
    return {
        "currency": cost.currency,
        "input_micro_units": cost.input_micro_units,
        "output_micro_units": cost.output_micro_units,
        "price_table_version": cost.price_table_version,
        "status": cost.status.value,
    }


def _validate_export_endpoint(value: str) -> None:
    if type(value) is not str or not value or len(value) > _MAX_ENDPOINT_LENGTH:
        raise ValueError("M6 export endpoint is invalid")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("M6 export endpoint is invalid")


def _validate_export_credential(value: str) -> None:
    if (
        type(value) is not str
        or not value
        or len(value) > _MAX_EXPORT_CREDENTIAL_LENGTH
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise ValueError("M6 export credential is invalid")


__all__ = [
    "DisabledTraceExporter",
    "HttpM6TraceExporter",
    "M6ExportDocument",
    "M6TraceExportStorePort",
    "M6TraceExporter",
    "build_m6_trace_exporter",
    "export_terminal_trace",
    "m6_export_document",
]
