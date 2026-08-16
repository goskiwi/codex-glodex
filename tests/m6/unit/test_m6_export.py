"""Offline M6 exporter evidence: opt-in, one attempt, and strict payload allowlist."""

from __future__ import annotations

import asyncio

import pytest

from glodex.observability.exporter import (
    DisabledTraceExporter,
    M6ExportDocument,
    build_m6_trace_exporter,
    export_terminal_trace,
)
from glodex.observability.runtime import (
    M6Operation,
    M6OperationOutcome,
    M6RunTrace,
    M6TraceEvent,
    M6TraceEventDraft,
    M6TraceEventKind,
)

pytestmark = [pytest.mark.unit, pytest.mark.spec("GLO-M6-P0-005")]


class _Store:
    def __init__(self) -> None:
        self.claimed = False
        self.load_calls = 0
        self.events: list[M6TraceEventDraft] = []
        self.trace = M6RunTrace(
            run_id="run-m6-export-1",
            terminal_state="COMPLETED",
            events=(
                M6TraceEvent(
                    run_id="run-m6-export-1",
                    sequence=1,
                    draft=M6TraceEventDraft(
                        kind=M6TraceEventKind.OPERATION_FINISHED,
                        operation=M6Operation.LLM_TOOL_CALL,
                        outcome=M6OperationOutcome.SUCCESS,
                        duration_ms=3,
                        version="test-model",
                    ),
                ),
                M6TraceEvent(
                    run_id="run-m6-export-1",
                    sequence=2,
                    draft=M6TraceEventDraft(
                        kind=M6TraceEventKind.TERMINAL,
                        safe_code="COMPLETED",
                    ),
                ),
            ),
        )

    async def load_m6_trace(self, *, run_id: str) -> M6RunTrace:
        assert run_id == self.trace.run_id
        self.load_calls += 1
        return self.trace

    async def claim_m6_trace_export(self, *, run_id: str) -> bool:
        assert run_id == self.trace.run_id
        if self.claimed:
            return False
        self.claimed = True
        return True

    async def append_m6_trace_event(self, *, run_id: str, draft: M6TraceEventDraft) -> M6TraceEvent:
        assert run_id == self.trace.run_id
        self.events.append(draft)
        return M6TraceEvent(run_id=run_id, sequence=3, draft=draft)


class _FailingExporter:
    def __init__(self) -> None:
        self.documents: list[M6ExportDocument] = []

    async def export_trace(self, *, document: M6ExportDocument) -> None:
        self.documents.append(document)
        raise TimeoutError("external transport timeout")


@pytest.mark.spec("GLO-M6-NFR-001")
def test_disabled_exporter_touches_neither_truth_store_nor_socket_configuration() -> None:
    async def scenario() -> None:
        store = _Store()
        await export_terminal_trace(
            store=store,
            exporter=build_m6_trace_exporter(enabled=False),
            run_id="run-m6-export-1",
        )
        assert store.load_calls == 0
        assert not store.claimed
        assert store.events == []

    asyncio.run(scenario())


@pytest.mark.acceptance
@pytest.mark.spec("M6-AC-003")
def test_failed_opt_in_export_is_recorded_once_as_a_safe_fact_without_private_fields() -> None:
    async def scenario() -> None:
        store = _Store()
        exporter = _FailingExporter()
        await export_terminal_trace(
            store=store,
            exporter=exporter,
            run_id="run-m6-export-1",
        )
        await export_terminal_trace(
            store=store,
            exporter=exporter,
            run_id="run-m6-export-1",
        )

        assert len(exporter.documents) == 1
        assert [event.safe_code for event in store.events] == ["TRACE_EXPORT_UNAVAILABLE"]
        rendered = exporter.documents[0].json_bytes().decode("utf-8").casefold()
        for forbidden in ("query", "prompt", "memory", "history", "credential", "vector", "score"):
            assert forbidden not in rendered
        assert "run-m6-export-1" not in rendered

    asyncio.run(scenario())


def test_enabled_export_requires_both_explicit_https_endpoint_and_credential() -> None:
    assert type(build_m6_trace_exporter(enabled=False)) is DisabledTraceExporter
    with pytest.raises(ValueError):
        build_m6_trace_exporter(enabled=True, endpoint=None, credential="token")
    with pytest.raises(ValueError):
        build_m6_trace_exporter(enabled=True, endpoint="http://example.test", credential="token")
