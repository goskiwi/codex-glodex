from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime

import pytest

from glodex.application.journal import RunEventKind, RunJournal, StageResult
from glodex.application.state import (
    InvalidRunTransition,
    RunState,
    transition,
)
from glodex.contracts import RunStatus

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec("GLO-P0-011", "GLO-NFR-007", "GLO-NFR-008"),
]

NOW = datetime(2026, 7, 23, 4, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (RunState.NEW, RunState.RUNNING),
        (RunState.RUNNING, RunState.COMPLETED),
        (RunState.RUNNING, RunState.NO_MATCH),
        (RunState.RUNNING, RunState.FAILED),
    ],
)
def test_only_approved_state_transitions_are_allowed(
    current: RunState,
    target: RunState,
) -> None:
    assert transition(current, target) is target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (RunState.NEW, RunState.NEW),
        (RunState.NEW, RunState.NO_MATCH),
        (RunState.RUNNING, RunState.RUNNING),
        (RunState.NO_MATCH, RunState.FAILED),
        (RunState.FAILED, RunState.FAILED),
        (RunState.COMPLETED, RunState.RUNNING),
    ],
)
def test_invalid_and_duplicate_terminal_transitions_fail(
    current: RunState,
    target: RunState,
) -> None:
    with pytest.raises(InvalidRunTransition, match=f"{current.value} -> {target.value}"):
        transition(current, target)


def test_journal_builds_contiguous_events_and_one_matching_terminal() -> None:
    journal = RunJournal.new(run_id="run-1", snapshot_version="m0-v1")
    journal = journal.start(NOW)
    journal = journal.stage_started("walking_skeleton", NOW)
    journal = journal.stage_completed(
        "walking_skeleton",
        NOW,
        duration_ms=2,
        result=StageResult(before=0, after=0),
    )
    journal = journal.finish(RunStatus.NO_MATCH, NOW)

    assert journal.state is RunState.NO_MATCH
    assert tuple(event.sequence for event in journal.events) == (1, 2, 3, 4)
    assert tuple(event.kind for event in journal.events) == (
        RunEventKind.RUN_STARTED,
        RunEventKind.STAGE_STARTED,
        RunEventKind.STAGE_COMPLETED,
        RunEventKind.RUN_NO_MATCH,
    )
    assert journal.terminal_status is RunStatus.NO_MATCH

    with pytest.raises(InvalidRunTransition):
        journal.finish(RunStatus.FAILED, NOW)
    with pytest.raises(FrozenInstanceError):
        journal.state = RunState.FAILED  # type: ignore[misc]


def test_journal_rejects_noncontiguous_sequence_and_naive_time() -> None:
    running = RunJournal.new(run_id="run-1", snapshot_version="m0-v1").start(NOW)
    broken_event = replace(running.events[0], sequence=2)

    with pytest.raises(ValueError, match="contiguous"):
        RunJournal(
            run_id=running.run_id,
            snapshot_version=running.snapshot_version,
            state=running.state,
            events=(broken_event,),
        )
    with pytest.raises(ValueError, match="UTC"):
        RunJournal.new(run_id="run-1", snapshot_version="m0-v1").start(datetime(2026, 7, 23, 4, 0))


def test_stage_completion_requires_a_matching_started_stage() -> None:
    running = RunJournal.new(run_id="run-1", snapshot_version="m0-v1").start(NOW)

    with pytest.raises(ValueError, match="not active"):
        running.stage_completed(
            "catalog",
            NOW,
            duration_ms=0,
            result=StageResult(before=0, after=0),
        )


def test_stage_result_rejects_negative_or_increasing_funnel_counts() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        StageResult(before=-1, after=0)
    with pytest.raises(ValueError, match="cannot exceed"):
        StageResult(before=1, after=2)
