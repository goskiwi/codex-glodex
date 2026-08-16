"""Strict v3 durable run-tree contracts with no database or HTTP dependency.

The public durable boundary owns *run identity* and safe recovery state.  Its
events and typed action journal never store model transcripts, raw tool
bodies, user queries beyond the already private request row, or chain-of-thought.
The separate private LangGraph schema stores only AES-encrypted graph state.
A child branch, when retained in
durable history, is represented as an ordinary durable run rather than an
event attached to its root.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol

from glodex.agent.state import canonical_json_bytes

DURABLE_SCHEMA_VERSION: Final = "glodex.durable.v3"
DURABLE_CONTEXT_VERSION: Final = "glodex.durable-context.v3"
AGENT_DURABLE_CONTEXT_VERSION: Final = "glodex.agent-durable-context.v3"
DURABLE_RETRIEVAL_CACHE_TTL_SECONDS: Final = 900
DURABLE_CONTEXT_CACHE_TTL_SECONDS: Final = 900
DURABLE_CACHE_VALUE_MAX_BYTES: Final = 32_768
DURABLE_CACHE_TIMEOUT_SECONDS: Final = 0.25
_IDENTIFIER_CHARACTERS: Final = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
_DIGEST_CHARACTERS: Final = "0123456789abcdef"
DURABLE_MAX_TREE_RUNS: Final = 11
_MAX_SAFE_JOURNAL_ENTRIES: Final = 64
_MAX_SAFE_HANDOFFS: Final = 10
_MAX_SAFE_RUNTIME_BYTES: Final = 262_144


class LoopKind(StrEnum):
    """One role in the durable run tree; the native ReAct runner creates roots only."""

    ROOT = "root"
    CHILD = "child"


class DurableRunState(StrEnum):
    """One durable run state, including recovery-only active states."""

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


def validate_digest(value: object, *, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _DIGEST_CHARACTERS for character in value)
    ):
        raise ValueError(f"{name} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class DurableRun:
    """Private durable identity and state for exactly one root or child loop."""

    run_id: str
    thread_id: str
    root_run_id: str
    parent_run_id: str | None
    child_id: str | None
    depth: int
    loop_kind: LoopKind
    task_scope_digest: str | None
    state: DurableRunState
    attempt: int
    event_sequence: int
    asset_version: str
    config_fingerprint: str
    request_payload: dict[str, object]
    terminal_response: dict[str, object] | None
    terminal_error_code: str | None
    cancel_requested: bool

    def __post_init__(self) -> None:
        validate_identifier(self.run_id, name="run ID")
        validate_identifier(self.thread_id, name="thread ID")
        validate_identifier(self.root_run_id, name="root run ID")
        if self.parent_run_id is not None:
            validate_identifier(self.parent_run_id, name="parent run ID")
        if self.child_id is not None:
            validate_identifier(self.child_id, name="child ID")
        if type(self.loop_kind) is not LoopKind:
            raise TypeError("durable run loop kind is invalid")
        if type(self.depth) is not int or isinstance(self.depth, bool) or not 0 <= self.depth <= 2:
            raise ValueError("durable run depth is invalid")
        if self.task_scope_digest is not None:
            validate_digest(self.task_scope_digest, name="task scope digest")
        if self.loop_kind is LoopKind.ROOT:
            if (
                self.run_id != self.root_run_id
                or self.parent_run_id is not None
                or self.child_id is not None
                or self.depth != 0
                or self.task_scope_digest is not None
            ):
                raise ValueError("root durable run tree identity is invalid")
        elif (
            self.run_id == self.root_run_id
            or self.parent_run_id is None
            or self.child_id is None
            or self.depth < 1
            or self.task_scope_digest is None
        ):
            raise ValueError("child durable run tree identity is invalid")
        if (
            type(self.state) is not DurableRunState
            or type(self.attempt) is not int
            or isinstance(self.attempt, bool)
            or self.attempt < 1
            or type(self.event_sequence) is not int
            or isinstance(self.event_sequence, bool)
            or self.event_sequence < 0
            or type(self.request_payload) is not dict
            or (self.terminal_response is not None and type(self.terminal_response) is not dict)
            or (self.terminal_error_code is not None and type(self.terminal_error_code) is not str)
            or type(self.cancel_requested) is not bool
        ):
            raise ValueError("durable run row is invalid")

    @property
    def tree_payload(self) -> dict[str, object]:
        """Return only stable tree identity, never request or terminal content."""

        return agent_run_tree_payload(
            root_run_id=self.root_run_id,
            parent_run_id=self.parent_run_id,
            depth=self.depth,
            loop_kind=self.loop_kind,
            task_scope_digest=self.task_scope_digest,
            child_id=self.child_id,
        )


@dataclass(frozen=True, slots=True)
class DurableEvent:
    """One safe public event stored under a strict sequence of its owning run."""

    run_id: str
    sequence: int
    payload: dict[str, object]
    payload_hash: str

    def __post_init__(self) -> None:
        validate_identifier(self.run_id, name="run ID")
        if (
            type(self.sequence) is not int
            or isinstance(self.sequence, bool)
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
    payload_hash: str

    def __post_init__(self) -> None:
        validate_identifier(self.run_id, name="run ID")
        if (
            type(self.checkpoint_number) is not int
            or isinstance(self.checkpoint_number, bool)
            or self.checkpoint_number < 1
            or type(self.state) is not DurableCheckpointState
            or type(self.event_sequence) is not int
            or isinstance(self.event_sequence, bool)
            or self.event_sequence < 0
            or type(self.runtime_payload) is not dict
        ):
            raise ValueError("durable checkpoint is invalid")
        validate_runtime_payload(self.runtime_payload, run_id=self.run_id)
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
            "runtime_payload": self.runtime_payload,
            "run_id": self.run_id,
            "schema_version": DURABLE_SCHEMA_VERSION,
            "state": self.state.value,
        }


@dataclass(frozen=True, slots=True)
class DurableRuntimeSnapshot:
    """The only safe application state an Agent run may checkpoint.

    All strings are identifier-like references or stable codes.  This is
    intentionally insufficient to persist free-form model reasoning or page
    content; recovery must reconstruct those only through verified typed state.
    """

    phase: str
    journal: tuple[str, ...] = ()
    budget: dict[str, int] | None = None
    local_state: dict[str, object] | None = None
    handoffs: tuple[dict[str, object], ...] = ()

    def __post_init__(self) -> None:
        validate_identifier(self.phase, name="runtime phase")
        _validate_safe_journal(self.journal)
        _validate_budget(self.budget or {})
        _validate_safe_value(self.local_state or {}, depth=0)
        _validate_handoffs(self.handoffs)

    @property
    def payload_fields(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "journal": list(self.journal),
            "budget": {} if self.budget is None else self.budget,
            "local_state": {} if self.local_state is None else self.local_state,
            "handoffs": list(self.handoffs),
        }


class DurableCheckpointWriter(Protocol):
    """Agent-facing sink for confirmed/private runtime checkpoints.

    Agent code receives this through :class:`DurableExecutionContext`.  The
    implementation remains in the coordinator so a child may checkpoint its
    own run without accessing the storage adapter directly.
    """

    async def persist(
        self,
        *,
        run_id: str,
        snapshot: DurableRuntimeSnapshot,
        state: DurableCheckpointState = DurableCheckpointState.CONFIRMED,
    ) -> DurableCheckpoint: ...

    async def reserve_child(
        self,
        *,
        run_id: str,
        thread_id: str,
        parent_run_id: str,
        child_id: str,
        depth: int,
        task_scope_digest: str,
        initial_snapshot: DurableRuntimeSnapshot,
    ) -> DurableRun: ...


@dataclass(frozen=True, slots=True)
class DurableExecutionContext:
    """Explicit v2 input passed from the coordinator into an Agent service.

    ``checkpoint`` is the last confirmed checkpoint for the requested root
    run.  ``tree`` and ``child_checkpoints`` give a resumed root enough safe
    information to restore or downgrade its children without replaying an
    uncertain remote action.  ``resume_payload`` is exactly that validated
    root checkpoint payload.
    """

    run: DurableRun
    checkpoint: DurableCheckpoint
    tree: tuple[DurableRun, ...]
    child_checkpoints: tuple[DurableCheckpoint, ...]
    resume_payload: dict[str, object]
    checkpoint_writer: DurableCheckpointWriter

    def __post_init__(self) -> None:
        if type(self.run) is not DurableRun or self.run.loop_kind is not LoopKind.ROOT:
            raise TypeError("durable execution context requires an exact root run")
        if (
            type(self.checkpoint) is not DurableCheckpoint
            or self.checkpoint.run_id != self.run.run_id
            or self.checkpoint.state is not DurableCheckpointState.CONFIRMED
        ):
            raise ValueError("durable execution context checkpoint is invalid")
        _validate_tree(self.tree, root_run_id=self.run.run_id)
        if type(self.child_checkpoints) is not tuple or any(
            type(checkpoint) is not DurableCheckpoint for checkpoint in self.child_checkpoints
        ):
            raise TypeError("durable execution context child checkpoints are invalid")
        child_run_ids = {item.run_id for item in self.tree if item.loop_kind is LoopKind.CHILD}
        checkpoint_run_ids = tuple(checkpoint.run_id for checkpoint in self.child_checkpoints)
        if len(checkpoint_run_ids) != len(set(checkpoint_run_ids)) or not set(
            checkpoint_run_ids
        ).issubset(child_run_ids):
            raise TypeError("durable execution context child checkpoints are invalid")
        if type(self.resume_payload) is not dict:
            raise TypeError("durable execution context resume payload is invalid")
        validate_runtime_payload(self.resume_payload, run_id=self.run.run_id)
        if not callable(getattr(self.checkpoint_writer, "persist", None)) or not callable(
            getattr(self.checkpoint_writer, "reserve_child", None)
        ):
            raise TypeError("durable execution context checkpoint writer is invalid")


@dataclass(frozen=True, slots=True)
class DurableCacheValue:
    """A bounded opaque-ID cache value that remains safe to discard."""

    namespace: str
    version: str
    identities: tuple[str, ...]
    metadata: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        if self.namespace not in {"retrieval", "context"} or self.version != DURABLE_SCHEMA_VERSION:
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
    return f"durable:{namespace}:v3:{canonical_hash(material)}"


def agent_run_tree_payload(
    *,
    root_run_id: str,
    parent_run_id: str | None,
    depth: int,
    loop_kind: LoopKind,
    task_scope_digest: str | None,
    child_id: str | None,
) -> dict[str, object]:
    """Assemble only stable identity metadata for one durable tree node."""

    validate_identifier(root_run_id, name="root run ID")
    if parent_run_id is not None:
        validate_identifier(parent_run_id, name="parent run ID")
    if type(depth) is not int or isinstance(depth, bool) or not 0 <= depth <= 2:
        raise ValueError("run tree depth is invalid")
    if type(loop_kind) is not LoopKind:
        raise TypeError("run tree loop kind must be an exact LoopKind")
    if task_scope_digest is not None:
        validate_digest(task_scope_digest, name="task scope digest")
    if child_id is not None:
        validate_identifier(child_id, name="child ID")
    if loop_kind is LoopKind.ROOT and (
        parent_run_id is not None
        or child_id is not None
        or depth != 0
        or task_scope_digest is not None
    ):
        raise ValueError("root run tree payload is invalid")
    if loop_kind is LoopKind.CHILD and (
        parent_run_id is None or child_id is None or depth < 1 or task_scope_digest is None
    ):
        raise ValueError("child run tree payload is invalid")
    return {
        "root_run_id": root_run_id,
        "parent_run_id": parent_run_id,
        "depth": depth,
        "loop_kind": loop_kind.value,
        "task_scope_digest": task_scope_digest,
        "child_id": child_id,
    }


def runtime_checkpoint_payload(
    *,
    run: DurableRun,
    tree: tuple[DurableRun, ...],
    snapshot: DurableRuntimeSnapshot,
) -> dict[str, object]:
    """Build one exact v2 safe recovery payload for ``run``.

    The payload holds a safe journal, budget counters, local typed-state
    references, handoff references and a compact current run-tree view.  It is
    deliberately the only accepted checkpoint payload shape.
    """

    if type(run) is not DurableRun or type(snapshot) is not DurableRuntimeSnapshot:
        raise TypeError("runtime checkpoint payload inputs are invalid")
    _validate_tree(tree, root_run_id=run.root_run_id)
    if run.run_id not in {item.run_id for item in tree}:
        raise ValueError("checkpoint run must belong to its tree")
    payload = {
        "schema_version": AGENT_DURABLE_CONTEXT_VERSION,
        "tree": {
            **run.tree_payload,
            "runs": [_safe_tree_run(item) for item in tree],
        },
        **snapshot.payload_fields,
    }
    validate_runtime_payload(payload, run_id=run.run_id)
    return payload


def validate_runtime_payload(payload: object, *, run_id: str) -> None:
    """Fail closed unless a payload is exactly the safe v2 recovery shape."""

    validate_identifier(run_id, name="run ID")
    if type(payload) is not dict or frozenset(payload) != {
        "schema_version",
        "tree",
        "phase",
        "journal",
        "budget",
        "local_state",
        "handoffs",
    }:
        raise ValueError("durable runtime payload shape is invalid")
    if payload["schema_version"] != AGENT_DURABLE_CONTEXT_VERSION:
        raise ValueError("durable runtime payload version is invalid")
    _validate_runtime_tree(payload["tree"], run_id=run_id)
    validate_identifier(payload["phase"], name="runtime phase")
    _validate_safe_journal(payload["journal"])
    _validate_budget(payload["budget"])
    _validate_safe_value(payload["local_state"], depth=0)
    _validate_handoffs(payload["handoffs"])
    if len(canonical_json_bytes(payload)) > _MAX_SAFE_RUNTIME_BYTES:
        raise ValueError("durable runtime payload exceeds its safe bound")


def _validate_runtime_tree(value: object, *, run_id: str) -> None:
    if type(value) is not dict or frozenset(value) != {
        "root_run_id",
        "parent_run_id",
        "depth",
        "loop_kind",
        "task_scope_digest",
        "child_id",
        "runs",
    }:
        raise ValueError("durable runtime tree is invalid")
    try:
        loop_kind = LoopKind(value["loop_kind"])
    except (TypeError, ValueError) as error:
        raise ValueError("durable runtime tree loop kind is invalid") from error
    node = agent_run_tree_payload(
        root_run_id=value["root_run_id"],
        parent_run_id=value["parent_run_id"],
        depth=value["depth"],
        loop_kind=loop_kind,
        task_scope_digest=value["task_scope_digest"],
        child_id=value["child_id"],
    )
    records = value["runs"]
    if type(records) is not list:
        raise ValueError("durable runtime tree runs are invalid")
    tree = tuple(_tree_run_from_safe(item) for item in records)
    _validate_tree(tree, root_run_id=cast_str(node["root_run_id"]))
    current = next((item for item in tree if item.run_id == run_id), None)
    if current is None or current.tree_payload != node:
        raise ValueError("durable runtime tree does not match checkpoint run")


def _validate_tree(tree: object, *, root_run_id: str) -> None:
    validate_identifier(root_run_id, name="root run ID")
    if (
        type(tree) is not tuple
        or not tree
        or len(tree) > DURABLE_MAX_TREE_RUNS
        or any(type(item) is not DurableRun for item in tree)
    ):
        raise ValueError("durable run tree is invalid")
    runs = tuple(tree)
    if len({item.run_id for item in runs}) != len(runs):
        raise ValueError("durable run tree run IDs must be unique")
    root = next((item for item in runs if item.run_id == root_run_id), None)
    if root is None or root.loop_kind is not LoopKind.ROOT:
        raise ValueError("durable run tree root is invalid")
    for item in runs:
        if item.root_run_id != root_run_id:
            raise ValueError("durable run tree root identity is invalid")
        if item.loop_kind is LoopKind.CHILD and item.parent_run_id not in {
            candidate.run_id for candidate in runs
        }:
            raise ValueError("durable child parent is missing from its tree")


def _safe_tree_run(run: DurableRun) -> dict[str, object]:
    return {
        "run_id": run.run_id,
        "thread_id": run.thread_id,
        "root_run_id": run.root_run_id,
        "parent_run_id": run.parent_run_id,
        "child_id": run.child_id,
        "depth": run.depth,
        "loop_kind": run.loop_kind.value,
        "task_scope_digest": run.task_scope_digest,
        "state": run.state.value,
        "event_sequence": run.event_sequence,
    }


def _tree_run_from_safe(value: object) -> DurableRun:
    if type(value) is not dict or frozenset(value) != {
        "run_id",
        "thread_id",
        "root_run_id",
        "parent_run_id",
        "child_id",
        "depth",
        "loop_kind",
        "task_scope_digest",
        "state",
        "event_sequence",
    }:
        raise ValueError("durable runtime tree record is invalid")
    try:
        return DurableRun(
            run_id=cast_str(value["run_id"]),
            thread_id=cast_str(value["thread_id"]),
            root_run_id=cast_str(value["root_run_id"]),
            parent_run_id=cast_optional_str(value["parent_run_id"]),
            child_id=cast_optional_str(value["child_id"]),
            depth=cast_int(value["depth"]),
            loop_kind=LoopKind(value["loop_kind"]),
            task_scope_digest=cast_optional_str(value["task_scope_digest"]),
            state=DurableRunState(value["state"]),
            attempt=1,
            event_sequence=cast_int(value["event_sequence"]),
            asset_version="tree",
            config_fingerprint="tree",
            request_payload={},
            terminal_response=None,
            terminal_error_code=None,
            cancel_requested=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("durable runtime tree record is invalid") from error


def _validate_safe_journal(value: object) -> None:
    if not isinstance(value, (tuple, list)) or len(value) > _MAX_SAFE_JOURNAL_ENTRIES:
        raise ValueError("durable safe journal is invalid")
    if any(type(entry) is not str for entry in value):
        raise ValueError("durable safe journal entries are invalid")
    for entry in value:
        validate_identifier(entry, name="safe journal entry")


def _validate_budget(value: object) -> None:
    if type(value) is not dict or len(value) > 24:
        raise ValueError("durable runtime budget is invalid")
    for key, amount in value.items():
        validate_identifier(key, name="budget key")
        if type(amount) is not int or isinstance(amount, bool) or amount < 0 or amount > 1_000_000:
            raise ValueError("durable runtime budget amount is invalid")


def _validate_handoffs(value: object) -> None:
    if not isinstance(value, (tuple, list)) or len(value) > _MAX_SAFE_HANDOFFS:
        raise ValueError("durable runtime handoffs are invalid")
    for handoff in value:
        _validate_safe_value(handoff, depth=0)


def _validate_safe_value(value: object, *, depth: int) -> None:
    if depth > 4:
        raise ValueError("durable runtime state nesting is invalid")
    if value is None or type(value) is bool:
        return
    if type(value) is int and not isinstance(value, bool):
        if -1_000_000 <= value <= 1_000_000:
            return
        raise ValueError("durable runtime integer is invalid")
    if type(value) is str:
        validate_identifier(value, name="safe runtime value")
        return
    if isinstance(value, (tuple, list)):
        if len(value) > 64:
            raise ValueError("durable runtime collection is invalid")
        for item in value:
            _validate_safe_value(item, depth=depth + 1)
        return
    if type(value) is dict:
        if len(value) > 32:
            raise ValueError("durable runtime mapping is invalid")
        for key, item in value.items():
            validate_identifier(key, name="safe runtime key")
            _validate_safe_value(item, depth=depth + 1)
        return
    raise ValueError("durable runtime value is invalid")


def cast_str(value: object) -> str:
    if type(value) is not str:
        raise TypeError("safe value is not a string")
    return value


def cast_optional_str(value: object) -> str | None:
    if value is None:
        return None
    return cast_str(value)


def cast_int(value: object) -> int:
    if type(value) is not int or isinstance(value, bool):
        raise TypeError("safe value is not an integer")
    return value


__all__ = [
    "AGENT_DURABLE_CONTEXT_VERSION",
    "DURABLE_CACHE_TIMEOUT_SECONDS",
    "DURABLE_CACHE_VALUE_MAX_BYTES",
    "DURABLE_CONTEXT_CACHE_TTL_SECONDS",
    "DURABLE_CONTEXT_VERSION",
    "DURABLE_MAX_TREE_RUNS",
    "DURABLE_RETRIEVAL_CACHE_TTL_SECONDS",
    "DURABLE_SCHEMA_VERSION",
    "TERMINAL_RUN_STATES",
    "DurableCacheMiss",
    "DurableCacheValue",
    "DurableCheckpoint",
    "DurableCheckpointState",
    "DurableCheckpointWriter",
    "DurableEvent",
    "DurableExecutionContext",
    "DurableRun",
    "DurableRunState",
    "DurableRuntimeSnapshot",
    "DurableStoreError",
    "LoopKind",
    "agent_run_tree_payload",
    "cache_key",
    "canonical_hash",
    "runtime_checkpoint_payload",
    "validate_digest",
    "validate_identifier",
    "validate_runtime_payload",
]
