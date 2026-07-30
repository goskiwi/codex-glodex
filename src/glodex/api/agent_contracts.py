"""Strict HTTP status contract for the independent M1d Agent API."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import model_validator

from glodex.api.contracts import (
    ApiDTO,
    ApiError,
    EventCursor,
    ProjectionStatus,
    RunState,
)
from glodex.application.agent.contracts import AgentDemoResponse
from glodex.contracts import Identifier, RunStatus


class AgentRunStatusResponse(ApiDTO):
    """Canonical retained status for one Agent Run resource."""

    schema_version: Literal["glodex.agent-run.v1"] = "glodex.agent-run.v1"
    thread_id: Identifier
    run_id: Identifier
    state: RunState
    projection_status: ProjectionStatus
    last_event_id: EventCursor | None = None
    response: AgentDemoResponse | None = None
    error: ApiError | None = None

    @model_validator(mode="after")
    def state_payload_is_consistent(self) -> Self:
        if self.state in {RunState.ACCEPTED, RunState.RUNNING}:
            if self.response is not None or self.error is not None:
                raise ValueError("active Agent Run cannot expose response or error")
            return self

        if self.state is RunState.ABORTED:
            if self.response is not None:
                raise ValueError("ABORTED Agent Run cannot expose a business response")
            if self.error is None or self.error.code != "RUN_ABORTED":
                raise ValueError("ABORTED Agent Run requires the stable RUN_ABORTED error")
            return self

        expected_status = RunStatus(self.state.value)
        if self.response is None or self.response.status is not expected_status:
            raise ValueError("business terminal Agent Run requires a matching AgentDemoResponse")
        if self.response.run_id != self.run_id:
            raise ValueError("Agent Run and AgentDemoResponse run IDs must match")
        if self.error is not None:
            raise ValueError("business terminal Agent Run cannot expose transport error")
        return self


__all__ = ["AgentRunStatusResponse"]
