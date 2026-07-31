"""Strict M2b durable-runtime values with no database or HTTP dependency."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from glodex.application.agent.state import canonical_json_bytes
from glodex.application.m2a_profile import M2aProfileEntry

M2B_SCHEMA_VERSION: Final = "glodex.m2b.v1"
M2B_CONTEXT_VERSION: Final = "glodex.m2b-context.v1"
M2B_RETRIEVAL_CACHE_TTL_SECONDS: Final = 900
M2B_CONTEXT_CACHE_TTL_SECONDS: Final = 300
M2B_CACHE_VALUE_MAX_BYTES: Final = 32_768
M2B_CACHE_TIMEOUT_SECONDS: Final = 0.25
_IDENTIFIER_CHARACTERS: Final = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"


class DurableRunState(StrEnum):
    """One M2b resource state, including recovery-only active states."""

    ACCEPTED = "ACCEPTED"
    RUNNING = "RUNNING"
    RECOVERABLE = "RECOVERABLE"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    COMPLETED = "COMPLETED"
    NO_MATCH = "NO_MATCH"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


class DurableCheckpointState(StrEnum):
    """Certainty of the most recent private checkpoint."""

    CONFIRMED = "CONFIRMED"
    REMOTE_PENDING = "REMOTE_PENDING"
    TERMINAL = "TERMINAL"
    CANCELLED = "CANCELLED"


TERMINAL_RUN_STATES: Final = frozenset(
    {
        DurableRunState.COMPLETED,
        DurableRunState.NO_MATCH,
        DurableRunState.FAILED,
        DurableRunState.ABORTED,
    }
)


class DurableStoreError(RuntimeError):
    """Stable local-store failure that never includes a driver response."""


class DurableCacheMiss(RuntimeError):
    """A cache-only miss/degradation that must not affect business correctness."""


def canonical_hash(value: object) -> str:
    """Hash one bounded canonical durable payload."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def validate_identifier(value: object, *, name: str) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > 64
        or any(character not in _IDENTIFIER_CHARACTERS for character in value)
    ):
        raise ValueError(f"{name} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class DurableRun:
    """Private durable Run state shared by the application and storage adapters."""

    run_id: str
    thread_id: str
    state: DurableRunState
    attempt: int
    event_sequence: int
    asset_version: str
    config_fingerprint: str
    profile_id: str | None
    profile_revision: int
    request_payload: dict[str, object]
    terminal_response: dict[str, object] | None
    terminal_error_code: str | None
    cancel_requested: bool

    def __post_init__(self) -> None:
        validate_identifier(self.run_id, name="run ID")
        validate_identifier(self.thread_id, name="thread ID")
        if self.profile_id is not None:
            validate_identifier(self.profile_id, name="profile ID")
        if (
            type(self.attempt) is not int
            or self.attempt < 1
            or type(self.event_sequence) is not int
            or self.event_sequence < 0
            or type(self.profile_revision) is not int
            or self.profile_revision < 0
            or type(self.request_payload) is not dict
            or (self.terminal_response is not None and type(self.terminal_response) is not dict)
            or (self.terminal_error_code is not None and type(self.terminal_error_code) is not str)
            or type(self.cancel_requested) is not bool
        ):
            raise ValueError("durable run row is invalid")


@dataclass(frozen=True, slots=True)
class DurableEvent:
    """One safe public event stored under a strict per-Run sequence."""

    run_id: str
    sequence: int
    payload: dict[str, object]
    payload_hash: str

    def __post_init__(self) -> None:
        validate_identifier(self.run_id, name="run ID")
        if (
            type(self.sequence) is not int
            or self.sequence < 1
            or type(self.payload) is not dict
            or self.payload_hash != canonical_hash(self.payload)
        ):
            raise ValueError("durable event is invalid")


@dataclass(frozen=True, slots=True)
class DurableCheckpoint:
    """One hash-closed private state snapshot at a safe runtime boundary."""

    run_id: str
    checkpoint_number: int
    state: DurableCheckpointState
    event_sequence: int
    runtime_payload: dict[str, object]
    asset_version: str
    config_fingerprint: str
    profile_revision: int
    payload_hash: str

    def __post_init__(self) -> None:
        validate_identifier(self.run_id, name="run ID")
        if (
            type(self.checkpoint_number) is not int
            or isinstance(self.checkpoint_number, bool)
            or self.checkpoint_number < 1
            or type(self.event_sequence) is not int
            or isinstance(self.event_sequence, bool)
            or self.event_sequence < 0
            or type(self.runtime_payload) is not dict
            or type(self.profile_revision) is not int
            or isinstance(self.profile_revision, bool)
            or self.profile_revision < 0
        ):
            raise ValueError("durable checkpoint is invalid")
        for name, value in (
            ("asset version", self.asset_version),
            ("config fingerprint", self.config_fingerprint),
            ("payload hash", self.payload_hash),
        ):
            if type(value) is not str or not value or len(value) > 128:
                raise ValueError(f"{name} is invalid")
        if self.payload_hash != canonical_hash(self.payload_for_hash):
            raise ValueError("durable checkpoint hash does not match")

    @property
    def payload_for_hash(self) -> dict[str, object]:
        return {
            "asset_version": self.asset_version,
            "checkpoint_number": self.checkpoint_number,
            "config_fingerprint": self.config_fingerprint,
            "event_sequence": self.event_sequence,
            "profile_revision": self.profile_revision,
            "runtime_payload": self.runtime_payload,
            "run_id": self.run_id,
            "schema_version": M2B_SCHEMA_VERSION,
            "state": self.state.value,
        }


@dataclass(frozen=True, slots=True)
class DurableProfileSnapshot:
    """One immutable Profile revision bound to a durable Agent run."""

    profile_id: str
    revision: int
    entries: tuple[M2aProfileEntry, ...]

    def __post_init__(self) -> None:
        validate_identifier(self.profile_id, name="profile ID")
        if (
            type(self.revision) is not int
            or isinstance(self.revision, bool)
            or self.revision < 0
            or type(self.entries) is not tuple
            or any(type(entry) is not M2aProfileEntry for entry in self.entries)
            or any(entry.profile_id != self.profile_id for entry in self.entries)
        ):
            raise ValueError("durable profile snapshot is invalid")
        entry_ids = tuple(entry.entry_id for entry in self.entries)
        if len(entry_ids) != len(set(entry_ids)) or len(entry_ids) > 16:
            raise ValueError("durable profile snapshot entries are invalid")


@dataclass(frozen=True, slots=True)
class DurableCacheValue:
    """A bounded opaque-ID cache value that remains safe to discard."""

    namespace: str
    version: str
    identities: tuple[str, ...]
    metadata: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        if self.namespace not in {"retrieval", "context"} or self.version != M2B_SCHEMA_VERSION:
            raise ValueError("durable cache namespace is invalid")
        if (
            type(self.identities) is not tuple
            or len(self.identities) > 40
            or len(self.identities) != len(set(self.identities))
            or type(self.metadata) is not tuple
            or len(self.metadata) > 12
        ):
            raise ValueError("durable cache identities are invalid")
        for identity in self.identities:
            validate_identifier(identity, name="cache identity")
        names = tuple(name for name, _value in self.metadata)
        if len(names) != len(set(names)) or any(
            type(name) is not str
            or not name
            or type(value) is not int
            or isinstance(value, bool)
            or value < 0
            for name, value in self.metadata
        ):
            raise ValueError("durable cache metadata is invalid")


def cache_key(*, namespace: str, material: object) -> str:
    """Return a versioned digest-only Redis key with no readable private text."""

    if namespace not in {"retrieval", "context"}:
        raise ValueError("durable cache namespace is invalid")
    return f"m2b:{namespace}:v1:{canonical_hash(material)}"


__all__ = [
    "M2B_CACHE_TIMEOUT_SECONDS",
    "M2B_CACHE_VALUE_MAX_BYTES",
    "M2B_CONTEXT_CACHE_TTL_SECONDS",
    "M2B_CONTEXT_VERSION",
    "M2B_RETRIEVAL_CACHE_TTL_SECONDS",
    "M2B_SCHEMA_VERSION",
    "TERMINAL_RUN_STATES",
    "DurableCacheMiss",
    "DurableCacheValue",
    "DurableCheckpoint",
    "DurableCheckpointState",
    "DurableEvent",
    "DurableProfileSnapshot",
    "DurableRun",
    "DurableRunState",
    "DurableStoreError",
    "cache_key",
    "canonical_hash",
    "validate_identifier",
]
