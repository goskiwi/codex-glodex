"""Pure run lifecycle transitions."""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from glodex.contracts import RunStatus


class RunState(StrEnum):
    """All lifecycle states, including the pre-run nonterminal states."""

    NEW = "NEW"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    NO_MATCH = "NO_MATCH"
    FAILED = "FAILED"


TERMINAL_STATES: Final[frozenset[RunState]] = frozenset(
    {RunState.COMPLETED, RunState.NO_MATCH, RunState.FAILED}
)
_ALLOWED_TRANSITIONS: Final[frozenset[tuple[RunState, RunState]]] = frozenset(
    {
        (RunState.NEW, RunState.RUNNING),
        (RunState.RUNNING, RunState.COMPLETED),
        (RunState.RUNNING, RunState.NO_MATCH),
        (RunState.RUNNING, RunState.FAILED),
    }
)


class InvalidRunTransition(ValueError):
    """Raised when a run attempts an unapproved or second terminal transition."""


def transition(current: RunState, target: RunState) -> RunState:
    """Return the target state only for an explicitly approved transition."""

    if (current, target) not in _ALLOWED_TRANSITIONS:
        raise InvalidRunTransition(f"invalid run transition: {current.value} -> {target.value}")
    return target


def state_for_status(status: RunStatus) -> RunState:
    """Map a public terminal status to the matching lifecycle state."""

    return RunState(status.value)


def status_for_state(state: RunState) -> RunStatus:
    """Map a terminal lifecycle state to the public status."""

    if state not in TERMINAL_STATES:
        raise ValueError(f"run state is not terminal: {state.value}")
    return RunStatus(state.value)


__all__ = [
    "TERMINAL_STATES",
    "InvalidRunTransition",
    "RunState",
    "state_for_status",
    "status_for_state",
    "transition",
]
