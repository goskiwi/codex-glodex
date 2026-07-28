"""Strict versioned HTTP contracts for the M1a API adapter."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

from glodex.contracts import Identifier, RunStatus, SearchRequest, SearchResponse

NonEmptyText = Annotated[str, StringConstraints(min_length=1, max_length=2_000)]
EventCursor = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*:[1-9][0-9]*$",
    ),
]


class ApiDTO(BaseModel):
    """Base for immutable HTTP DTOs that reject coercion and extension."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class RunState(StrEnum):
    """Transport lifecycle including states outside the M0 business response."""

    ACCEPTED = "ACCEPTED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    NO_MATCH = "NO_MATCH"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


class ProjectionStatus(StrEnum):
    """Health of the optional public event projection."""

    OK = "OK"
    DEGRADED = "DEGRADED"


class ApiFieldError(ApiDTO):
    """One safe field-level validation problem."""

    field: NonEmptyText
    code: Identifier
    message: NonEmptyText


class ApiError(ApiDTO):
    """Stable machine error without internal exception details."""

    code: Identifier
    message: NonEmptyText
    field_errors: tuple[ApiFieldError, ...] = ()


class ApiErrorEnvelope(ApiDTO):
    """Versioned envelope used for every HTTP 4xx/5xx response."""

    schema_version: Literal["glodex.error.v1"] = "glodex.error.v1"
    type: Literal["api_error"] = "api_error"
    error: ApiError


class CreateRunRequest(ApiDTO):
    """Transport wrapper around the approved M0 SearchRequest."""

    thread_id: Identifier | None = None
    request: SearchRequest


class RunAccepted(ApiDTO):
    """Immediate response after a valid Run has been reserved."""

    schema_version: Literal["glodex.run.v1"] = "glodex.run.v1"
    thread_id: Identifier
    run_id: Identifier
    state: Literal[RunState.ACCEPTED] = RunState.ACCEPTED
    status_url: NonEmptyText
    events_url: NonEmptyText


class RunStatusResponse(ApiDTO):
    """Public status snapshot backed by one in-memory Run resource."""

    schema_version: Literal["glodex.run.v1"] = "glodex.run.v1"
    thread_id: Identifier
    run_id: Identifier
    state: RunState
    projection_status: ProjectionStatus
    last_event_id: EventCursor | None = None
    response: SearchResponse | None = None
    error: ApiError | None = None

    @model_validator(mode="after")
    def state_payload_is_consistent(self) -> Self:
        if self.state in {RunState.ACCEPTED, RunState.RUNNING}:
            if self.response is not None or self.error is not None:
                raise ValueError("active Run cannot expose response or error")
            return self

        if self.state is RunState.ABORTED:
            if self.response is not None:
                raise ValueError("ABORTED Run cannot expose a business response")
            if self.error is None or self.error.code != "RUN_ABORTED":
                raise ValueError("ABORTED Run requires the stable RUN_ABORTED error")
            return self

        expected_status = RunStatus(self.state.value)
        if self.response is None or self.response.status is not expected_status:
            raise ValueError("business terminal Run requires a matching SearchResponse")
        if self.response.run_id != self.run_id:
            raise ValueError("Run resource and SearchResponse run_id must match")
        if self.error is not None:
            raise ValueError("business terminal Run cannot expose a transport error")
        return self


__all__ = [
    "ApiError",
    "ApiErrorEnvelope",
    "ApiFieldError",
    "CreateRunRequest",
    "EventCursor",
    "ProjectionStatus",
    "RunAccepted",
    "RunState",
    "RunStatusResponse",
]
