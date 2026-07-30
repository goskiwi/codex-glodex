"""Black-box public contract acceptance evidence for the M2b durable surface."""

from __future__ import annotations

import pytest

from glodex.api.contracts import ApiError
from glodex.api.durable_agent_contracts import DurableRunStateDTO, DurableRunStatusResponse
from glodex.application.agent.contracts import AgentAnswer, AgentAnswerKind, AgentDemoResponse
from glodex.contracts import RunStatus

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.spec(
        "M2B-AC-001",
        "M2B-AC-002",
        "M2B-AC-003",
        "M2B-AC-004",
        "M2B-AC-005",
        "M2B-AC-006",
        "M2B-AC-007",
    ),
]


def test_public_durable_status_is_replayable_and_never_contains_private_storage_fields() -> None:
    response = AgentDemoResponse(
        run_id="run-m2b-acceptance",
        status=RunStatus.COMPLETED,
        answer=AgentAnswer(kind=AgentAnswerKind.CHAT_FALLBACK, text="Safe terminal summary."),
    )
    status = DurableRunStatusResponse(
        thread_id="thread-m2b-acceptance",
        run_id="run-m2b-acceptance",
        state=DurableRunStateDTO.COMPLETED,
        last_event_id="run-m2b-acceptance:2",
        response=response,
    )
    aborted = DurableRunStatusResponse(
        thread_id="thread-m2b-acceptance",
        run_id="run-m2b-acceptance",
        state=DurableRunStateDTO.ABORTED,
        error=ApiError(code="RUN_ABORTED", message="Run execution was aborted."),
    )
    rendered = status.model_dump(mode="json")

    assert rendered["last_event_id"] == "run-m2b-acceptance:2"
    assert aborted.response is None
    private_fields = {"request_payload", "runtime_payload", "user_vector", "profile_value"}
    assert private_fields.isdisjoint(rendered)
