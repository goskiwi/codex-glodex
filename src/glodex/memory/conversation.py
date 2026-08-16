"""Resolve one thread's current shopping task from its last trusted terminal state."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from glodex.agent.contracts import AgentAnswerKind, AgentDemoResponse
from glodex.contracts import RunStatus, SearchRequest
from glodex.domain.intent import budget_source_spans
from glodex.memory.models import ConversationRole, ConversationTurn
from glodex.runtime.contracts import DurableRun
from glodex.runtime.request_payload import task_turns_from_durable_payload

_MAX_TURNS = 64
_MAX_QUERY_LENGTH = 2_000


class ConversationRequestError(ValueError):
    """The verified thread state cannot be represented by one bounded request."""


class ConversationRequestStorePort(Protocol):
    async def list_conversation_turns(
        self,
        *,
        thread_id: str,
        limit: int = 64,
    ) -> tuple[ConversationTurn, ...]: ...

    async def load_run(self, *, run_id: str) -> DurableRun: ...


@dataclass(frozen=True, slots=True)
class ConversationResolution:
    request: SearchRequest
    task_turns: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.request) is not SearchRequest or not self.task_turns:
            raise TypeError("conversation resolution is invalid")


@dataclass(frozen=True, slots=True)
class ConversationRequestResolver:
    """Compile previous verified criteria plus the newest user adjustment."""

    store: ConversationRequestStorePort

    def __post_init__(self) -> None:
        if not callable(self.store.list_conversation_turns) or not callable(self.store.load_run):
            raise TypeError("conversation request resolver store is invalid")

    async def resolve(self, *, thread_id: str, current: SearchRequest) -> ConversationResolution:
        if type(current) is not SearchRequest:
            raise TypeError("conversation resolution requires an exact SearchRequest")
        turns = await self.store.list_conversation_turns(thread_id=thread_id, limit=_MAX_TURNS)
        prior = next(
            (
                turn
                for turn in reversed(turns)
                if turn.role is ConversationRole.ASSISTANT and turn.terminal_run_id is not None
            ),
            None,
        )
        if prior is None:
            return ConversationResolution(request=current, task_turns=(current.query,))
        terminal_run_id = prior.terminal_run_id
        assert terminal_run_id is not None
        run = await self.store.load_run(run_id=terminal_run_id)
        if run.thread_id != thread_id or run.terminal_response is None:
            raise ConversationRequestError("trusted thread terminal state is unavailable")
        try:
            response = AgentDemoResponse.model_validate_json(
                json.dumps(run.terminal_response, ensure_ascii=False, separators=(",", ":"))
            )
        except Exception as error:
            raise ConversationRequestError("trusted thread terminal state is invalid") from error
        if (
            response.status not in {RunStatus.COMPLETED, RunStatus.NO_MATCH}
            or response.answer is None
            or response.answer.kind is not AgentAnswerKind.SHOPPING_SUMMARY
            or response.search_response is None
        ):
            return ConversationResolution(request=current, task_turns=(current.query,))
        try:
            previous_turns = task_turns_from_durable_payload(run.request_payload)
        except ValueError as error:
            raise ConversationRequestError("current thread task state is invalid") from error
        request, task_turns = merge_current_shopping_task(
            current=current,
            previous_turns=previous_turns,
        )
        return ConversationResolution(request=request, task_turns=task_turns)


def merge_current_shopping_task(
    *,
    current: SearchRequest,
    previous_turns: tuple[str, ...],
) -> tuple[SearchRequest, tuple[str, ...]]:
    """Create one bounded resolved prompt; later text is the authoritative adjustment."""

    if (
        type(current) is not SearchRequest
        or type(previous_turns) is not tuple
        or not previous_turns
        or any(type(value) is not str or not value.strip() for value in previous_turns)
    ):
        raise TypeError("conversation task inputs are invalid")
    task_turns = (*previous_turns, current.query)
    replaces_budget = bool(budget_source_spans(current.query))
    prior_for_execution = tuple(
        _redact_budget(value) if replaces_budget else value for value in previous_turns
    )
    lines = [f"ORIGINAL_USER_REQUEST: {prior_for_execution[0]}"]
    lines.extend(
        f"USER_ADJUSTMENT_{index}: {value}"
        for index, value in enumerate((*prior_for_execution[1:], current.query), 1)
    )
    query = "\n".join(lines)
    if len(query) > _MAX_QUERY_LENGTH:
        raise ConversationRequestError("resolved conversation query exceeds the request limit")
    return current.model_copy(update={"query": query}), task_turns


def _redact_budget(value: str) -> str:
    redacted = value
    for span in reversed(budget_source_spans(value)):
        redacted = f"{redacted[: span.start]}[SUPERSEDED_BUDGET]{redacted[span.end :]}"
    return redacted


__all__ = [
    "ConversationRequestError",
    "ConversationRequestResolver",
    "ConversationResolution",
    "merge_current_shopping_task",
]
