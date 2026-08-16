"""Post-terminal authenticated history and one-shot LLM memory reflection."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from glodex.agent.contracts import AgentDemoResponse
from glodex.agent.presentation import agent_display_summary
from glodex.bootstrap import SystemClock
from glodex.contracts import RunStatus
from glodex.memory.llm_reflection import LlmMemoryReflector
from glodex.memory.models import (
    ConversationRole,
    ConversationTurn,
    MemoryCandidate,
    UserMemoryEntry,
)
from glodex.memory.semantics import may_contain_explicit_long_term_memory
from glodex.observability.runtime import M6OperationRecorder, M6OperationStorePort
from glodex.runtime.contracts import DurableRun
from glodex.runtime.request_payload import display_query_from_durable_payload

_LOGGER = logging.getLogger(__name__)
_MEMORY_TERMINAL_WRITE_FAILED = "MEMORY_TERMINAL_WRITE_FAILED"


class MemoryTerminalStorePort(Protocol):
    async def thread_owner(self, *, thread_id: str) -> str | None: ...

    async def list_conversation_turns(
        self,
        *,
        thread_id: str,
        limit: int = 64,
    ) -> tuple[ConversationTurn, ...]: ...

    async def append_conversation_turn(
        self,
        *,
        thread_id: str,
        role: ConversationRole,
        display_content: str,
        terminal_run_id: str | None = None,
    ) -> ConversationTurn: ...

    async def write_explicit_reflect_memory(
        self,
        *,
        user_id: str,
        candidates: tuple[MemoryCandidate, ...],
        source_thread_id: str,
        source_ordinal: int,
    ) -> tuple[UserMemoryEntry, ...]: ...


@dataclass(frozen=True, slots=True)
class MemoryTerminalWriter:
    """Persist trusted assistant display text then reflect only the current user input once."""

    store: MemoryTerminalStorePort
    reflector: LlmMemoryReflector
    m6_store: M6OperationStorePort | None = None

    def __post_init__(self) -> None:
        if (
            not callable(self.store.thread_owner)
            or not callable(self.store.list_conversation_turns)
            or not callable(self.store.append_conversation_turn)
            or not callable(self.store.write_explicit_reflect_memory)
            or type(self.reflector) is not LlmMemoryReflector
            or (
                self.m6_store is not None
                and (
                    not callable(self.m6_store.acquire_m6_breaker)
                    or not callable(self.m6_store.record_m6_breaker)
                    or not callable(self.m6_store.append_m6_trace_event)
                )
            )
        ):
            raise TypeError("user-memory terminal writer inputs are invalid")

    async def write_terminal(self, *, run: DurableRun, response: AgentDemoResponse) -> None:
        if type(run) is not DurableRun or type(response) is not AgentDemoResponse:
            raise TypeError("user-memory terminal write inputs are invalid")
        if (
            response.status not in {RunStatus.COMPLETED, RunStatus.NO_MATCH}
            or response.answer is None
        ):
            return
        try:
            display = agent_display_summary(response)
            query = _query_from_run(run)
            owner = await self.store.thread_owner(thread_id=run.thread_id)
            if owner is None:
                return
            prior_turns = await self.store.list_conversation_turns(
                thread_id=run.thread_id, limit=64
            )
            source_ordinal = _current_user_ordinal(prior_turns, query=query)
            if source_ordinal is None:
                return
            await self.store.append_conversation_turn(
                thread_id=run.thread_id,
                role=ConversationRole.ASSISTANT,
                display_content=display.text,
                terminal_run_id=run.run_id,
            )
            if not may_contain_explicit_long_term_memory(query):
                return
            recorder = (
                None
                if self.m6_store is None
                else M6OperationRecorder(
                    store=self.m6_store,
                    run_id=run.run_id,
                    clock=SystemClock(),
                )
            )
            candidates = await self.reflector.reflect(
                current_user_content=query,
                terminal_status=response.status.value,
                m6_recorder=recorder,
            )
            if candidates:
                await self.store.write_explicit_reflect_memory(
                    user_id=owner,
                    candidates=candidates,
                    source_thread_id=run.thread_id,
                    source_ordinal=source_ordinal,
                )
        except Exception:
            _LOGGER.warning(
                "terminal memory write failed",
                extra={"safe_code": _MEMORY_TERMINAL_WRITE_FAILED},
            )


def _query_from_run(run: DurableRun) -> str:
    return display_query_from_durable_payload(run.request_payload)


def _current_user_ordinal(
    turns: tuple[ConversationTurn, ...],
    *,
    query: str,
) -> int | None:
    for turn in reversed(turns):
        if turn.role is ConversationRole.USER and turn.display_content == query:
            return turn.ordinal
    return None


__all__ = ["MemoryTerminalWriter"]
