"""Black-box public contract acceptance evidence for the durable runtime surface."""

from __future__ import annotations

import pytest

from glodex.agent.contracts import AgentAnswer, AgentAnswerKind, AgentDemoResponse
from glodex.api.contracts import ApiError
from glodex.api.durable_contracts import DurableRunStateDTO, DurableRunStatusResponse
from glodex.api.sse_cursor import InvalidSseCursor, parse_sse_cursor
from glodex.contracts import RunStatus

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.spec(
        "DURABLE-AC-001",
        "DURABLE-AC-002",
        "DURABLE-AC-003",
        "DURABLE-AC-004",
        "DURABLE-AC-005",
        "DURABLE-AC-006",
        "DURABLE-AC-007",
    ),
]


def test_public_durable_status_is_replayable_and_never_contains_private_storage_fields() -> None:
    response = AgentDemoResponse(
        run_id="run-durable-acceptance",
        status=RunStatus.COMPLETED,
        answer=AgentAnswer(kind=AgentAnswerKind.CHAT_FALLBACK, text="Safe terminal summary."),
    )
    status = DurableRunStatusResponse(
        thread_id="thread-durable-acceptance",
        run_id="run-durable-acceptance",
        state=DurableRunStateDTO.COMPLETED,
        last_event_id="run-durable-acceptance:2",
        response=response,
    )
    aborted = DurableRunStatusResponse(
        thread_id="thread-durable-acceptance",
        run_id="run-durable-acceptance",
        state=DurableRunStateDTO.ABORTED,
        error=ApiError(code="RUN_ABORTED", message="Run execution was aborted."),
    )
    rendered = status.model_dump(mode="json")

    assert rendered["last_event_id"] == "run-durable-acceptance:2"
    assert aborted.response is None
    private_fields = {"request_payload", "runtime_payload", "user_vector", "profile_value"}
    assert private_fields.isdisjoint(rendered)


def test_durable_sse_cursor_is_strict_without_an_event_protocol_adapter() -> None:
    assert (
        parse_sse_cursor(
            "run-durable-acceptance:2",
            run_id="run-durable-acceptance",
            last_sequence=2,
        )
        == 2
    )

    for value in ("run-durable-acceptance:0", "other-run:1", "run-durable-acceptance:3"):
        with pytest.raises(InvalidSseCursor):
            parse_sse_cursor(value, run_id="run-durable-acceptance", last_sequence=2)
