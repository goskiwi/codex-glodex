"""Strict public HTTP contracts for the opt-in M2b durable Agent API."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import model_validator

from glodex.api.contracts import (
    ApiDTO,
    ApiError,
    EventCursor,
    NonEmptyText,
)
from glodex.application.agent.contracts import AgentDemoResponse
from glodex.contracts import Identifier, RunStatus, SearchRequest


class DurableRunStateDTO(StrEnum):
    """The M2b resource lifecycle, including durable recovery states."""

    ACCEPTED = "ACCEPTED"
    RUNNING = "RUNNING"
    RECOVERABLE = "RECOVERABLE"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    COMPLETED = "COMPLETED"
    NO_MATCH = "NO_MATCH"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


class DurableCreateRunRequest(ApiDTO):
    """One M2b durable Agent submission; profile selection remains optional."""

    thread_id: Identifier | None = None
    profile_id: Identifier | None = None
    request: SearchRequest


class DurableRunAccepted(ApiDTO):
    """Immediate acknowledgement of a transactionally reserved durable run."""

    schema_version: Literal["glodex.durable-agent-run.v1"] = "glodex.durable-agent-run.v1"
    thread_id: Identifier
    run_id: Identifier
    state: Literal[DurableRunStateDTO.ACCEPTED] = DurableRunStateDTO.ACCEPTED
    status_url: NonEmptyText
    events_url: NonEmptyText


class DurableRunStatusResponse(ApiDTO):
    """Safe durable state snapshot backed by PostgreSQL, never an internal row."""

    schema_version: Literal["glodex.durable-agent-run.v1"] = "glodex.durable-agent-run.v1"
    thread_id: Identifier
    run_id: Identifier
    state: DurableRunStateDTO
    last_event_id: EventCursor | None = None
    response: AgentDemoResponse | None = None
    error: ApiError | None = None

    @model_validator(mode="after")
    def state_payload_is_consistent(self) -> Self:
        if self.state in {
            DurableRunStateDTO.ACCEPTED,
            DurableRunStateDTO.RUNNING,
            DurableRunStateDTO.RECOVERABLE,
            DurableRunStateDTO.CANCEL_REQUESTED,
        }:
            if self.response is not None or self.error is not None:
                raise ValueError("active durable run cannot expose response or error")
            return self
        if self.state is DurableRunStateDTO.ABORTED:
            if self.response is not None:
                raise ValueError("aborted durable run cannot expose a business response")
            if self.error is None or self.error.code != "RUN_ABORTED":
                raise ValueError("aborted durable run requires RUN_ABORTED")
            return self
        expected_status = RunStatus(self.state.value)
        if self.response is None or self.response.status is not expected_status:
            raise ValueError("terminal durable run requires a matching AgentDemoResponse")
        if self.response.run_id != self.run_id:
            raise ValueError("durable run and AgentDemoResponse run IDs must match")
        if self.error is not None:
            raise ValueError("business terminal durable run cannot expose transport error")
        return self


__all__ = [
    "DurableCreateRunRequest",
    "DurableRunAccepted",
    "DurableRunStateDTO",
    "DurableRunStatusResponse",
]
