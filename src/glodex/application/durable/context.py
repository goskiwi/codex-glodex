"""Deterministic, redacted context-cache facts for M2b recovery observability."""

from __future__ import annotations

from dataclasses import dataclass

from glodex.application.agent.contracts import AgentRunEvent
from glodex.application.durable.contracts import (
    M2B_CONTEXT_VERSION,
    M2B_SCHEMA_VERSION,
    DurableCacheValue,
    canonical_hash,
)

_MAX_EVENTS = 64


@dataclass(frozen=True, slots=True)
class DurableContextDigest:
    """A bounded digest of safe event facts, never a prompt or a tool body."""

    digest: str
    event_count: int
    tool_count: int
    terminal_count: int

    def __post_init__(self) -> None:
        if (
            type(self.digest) is not str
            or len(self.digest) != 64
            or type(self.event_count) is not int
            or not 0 <= self.event_count <= _MAX_EVENTS
            or type(self.tool_count) is not int
            or not 0 <= self.tool_count <= _MAX_EVENTS
            or type(self.terminal_count) is not int
            or not 0 <= self.terminal_count <= 1
        ):
            raise ValueError("durable context digest is invalid")

    def cache_value(self) -> DurableCacheValue:
        return DurableCacheValue(
            namespace="context",
            version=M2B_SCHEMA_VERSION,
            identities=(self.digest,),
            metadata=(
                ("event_count", self.event_count),
                ("terminal_count", self.terminal_count),
                ("tool_count", self.tool_count),
            ),
        )


def digest_safe_events(events: tuple[AgentRunEvent, ...]) -> DurableContextDigest:
    """Hash only schema-approved event facts in their confirmed order."""

    if (
        type(events) is not tuple
        or len(events) > _MAX_EVENTS
        or any(type(event) is not AgentRunEvent for event in events)
    ):
        raise ValueError("durable context events are invalid")
    facts = tuple(
        {
            "child_id": event.child_id,
            "depth": event.depth,
            "kind": event.kind.value,
            "round": event.round,
            "safe_code": event.safe_code,
            "scope": event.scope.value,
            "status": event.status,
            "tool_name": None if event.tool_name is None else event.tool_name.value,
        }
        for event in events
    )
    payload = {"context_version": M2B_CONTEXT_VERSION, "events": facts}
    return DurableContextDigest(
        digest=canonical_hash(payload),
        event_count=len(events),
        tool_count=sum(1 for event in events if event.tool_name is not None),
        terminal_count=sum(
            1 for event in events if event.kind.value in {"AGENT_RESULT", "AGENT_ERROR"}
        ),
    )


__all__ = ["DurableContextDigest", "digest_safe_events"]
