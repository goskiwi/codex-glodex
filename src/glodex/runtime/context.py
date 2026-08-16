"""Deterministic, redacted context cache facts for durable Agent runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from glodex.agent.contracts import AgentEventKind, AgentRunEvent
from glodex.runtime.contracts import (
    DURABLE_CONTEXT_VERSION,
    DURABLE_SCHEMA_VERSION,
    DurableCacheValue,
    cache_key,
    canonical_hash,
    validate_identifier,
)

_MAX_EVENTS: Final = 64
_MAX_THREAD_RUNS: Final = 6
_RECENT_THREAD_RUNS: Final = 3
_MAX_ACTION_IDENTITIES: Final = 16
_RUN_PREFIX: Final = "ctxrun-"
_BREAKPOINT_PREFIX: Final = "breakpoint-"
_SAFE_PREFIXES: Final = ("terminal-", "tool-", "code-", _BREAKPOINT_PREFIX)


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
            version=DURABLE_SCHEMA_VERSION,
            identities=(self.digest,),
            metadata=(
                ("event_count", self.event_count),
                ("terminal_count", self.terminal_count),
                ("tool_count", self.tool_count),
            ),
        )


@dataclass(frozen=True, slots=True)
class AgentRunContext:
    """Private, bounded selector hints reconstructed from safe cache facts."""

    identities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.identities) is not tuple or len(self.identities) > _MAX_ACTION_IDENTITIES:
            raise ValueError("agent run context is invalid")
        for identity in self.identities:
            validate_identifier(identity, name="agent context identity")


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
    payload = {"context_version": DURABLE_CONTEXT_VERSION, "events": facts}
    return DurableContextDigest(
        digest=canonical_hash(payload),
        event_count=len(events),
        tool_count=sum(1 for event in events if event.tool_name is not None),
        terminal_count=sum(
            1
            for event in events
            if event.kind in {AgentEventKind.AGENT_RESULT, AgentEventKind.AGENT_ERROR}
        ),
    )


def thread_context_key(*, thread_id: str) -> str:
    """Hash the private thread ID only as cache-key material."""

    validate_identifier(thread_id, name="thread ID")
    return cache_key(
        namespace="context", material={"kind": "thread-context", "thread_id": thread_id}
    )


def update_thread_context(
    *,
    previous: DurableCacheValue | None,
    run_id: str,
    events: tuple[AgentRunEvent, ...],
) -> DurableCacheValue:
    """Merge one terminal safe digest, folding old records at a fixed breakpoint."""

    validate_identifier(run_id, name="run ID")
    digest = digest_safe_events(events)
    marker = f"{_RUN_PREFIX}{canonical_hash({'run_id': run_id, 'digest': digest.digest})[:48]}"
    old = _normal_context(previous)
    old_markers = tuple(item for item in old.identities if item.startswith(_RUN_PREFIX))
    if marker in old_markers:
        return old
    old_breakpoints = tuple(item for item in old.identities if item.startswith(_BREAKPOINT_PREFIX))
    markers = (*old_markers, marker)
    compressed = _metadata(old).get("compressed", 0)
    breakpoints = old_breakpoints[-1:]
    if len(markers) > _MAX_THREAD_RUNS:
        dropped, markers = markers[:-_RECENT_THREAD_RUNS], markers[-_RECENT_THREAD_RUNS:]
        compressed += len(dropped)
        breakpoint_material = {"prior": old_breakpoints, "dropped": dropped}
        breakpoint = f"{_BREAKPOINT_PREFIX}{canonical_hash(breakpoint_material)[:48]}"
        breakpoints = (breakpoint,)
    safe_facts = _safe_facts(events)
    old_facts = tuple(
        item for item in old.identities if item.startswith(("terminal-", "tool-", "code-"))
    )
    identities = (*breakpoints, *markers, *tuple(sorted(set((*old_facts, *safe_facts)))))
    identities = identities[:40]
    prior = _metadata(old)
    metadata = (
        ("compressed", compressed),
        ("event_count", prior.get("event_count", 0) + digest.event_count),
        ("run_count", prior.get("run_count", 0) + 1),
        ("terminal_count", prior.get("terminal_count", 0) + digest.terminal_count),
        ("tool_count", prior.get("tool_count", 0) + digest.tool_count),
    )
    return DurableCacheValue(
        namespace="context",
        version=DURABLE_SCHEMA_VERSION,
        identities=identities,
        metadata=metadata,
    )


def agent_run_context(value: DurableCacheValue | None) -> AgentRunContext:
    """Return only readable safe hints; cache loss is an empty private context."""

    try:
        context = _normal_context(value)
    except Exception:
        return AgentRunContext()
    return AgentRunContext(
        identities=tuple(
            identity for identity in context.identities if identity.startswith(_SAFE_PREFIXES)
        )[:_MAX_ACTION_IDENTITIES]
    )


def _normal_context(value: DurableCacheValue | None) -> DurableCacheValue:
    if value is None:
        return DurableCacheValue(namespace="context", version=DURABLE_SCHEMA_VERSION, identities=())
    if type(value) is not DurableCacheValue or value.namespace != "context":
        raise ValueError("thread context is invalid")
    return value


def _metadata(value: DurableCacheValue) -> dict[str, int]:
    return dict(value.metadata)


def _safe_facts(events: tuple[AgentRunEvent, ...]) -> tuple[str, ...]:
    identities: set[str] = set()
    for event in events:
        if event.tool_name is not None:
            identities.add(f"tool-{event.tool_name.value}")
        if event.safe_code is not None:
            identities.add(f"code-{event.safe_code}")
        if event.kind in {AgentEventKind.AGENT_RESULT, AgentEventKind.AGENT_ERROR}:
            identities.add(f"terminal-{event.status}")
    return tuple(sorted(identities))


__all__ = [
    "AgentRunContext",
    "DurableContextDigest",
    "agent_run_context",
    "digest_safe_events",
    "thread_context_key",
    "update_thread_context",
]
