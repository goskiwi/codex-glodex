"""Unit coverage for the pure journal-to-public-event projection."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from glodex.api.events import (
    EventProjector,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StateSnapshotEvent,
    StepDegradedEvent,
    StepFinishedEvent,
    StepStartedEvent,
)
from glodex.application.journal import RunEvent, RunEventKind, RunJournal, StageResult
from glodex.application.search_service import SearchExecution
from glodex.contracts import Issue, IssueSeverity, RunStatus, SearchResponse

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec(
        "GLO-M1-P0-003",
        "GLO-M1-P0-004",
        "GLO-M1-NFR-002",
        "GLO-M1-NFR-003",
        "GLO-M1-NFR-007",
    ),
]

THREAD_ID = "thread-projection-001"
RUN_ID = "run-projection-001"
OCCURRED_AT = datetime(1970, 1, 2, 0, 0, 0, 123_456, tzinfo=UTC)
TERMINAL_AT = datetime(1970, 1, 3, 0, 0, 0, 987_654, tzinfo=UTC)
STAGE_NAMES = (
    "intent",
    "snapshot",
    "aggregation",
    "eligibility",
    "ranking",
    "result_assembly",
)


def _event(
    kind: RunEventKind,
    *,
    stage: str = "run",
    result: StageResult | None = None,
) -> RunEvent:
    return RunEvent(
        run_id=RUN_ID,
        kind=kind,
        stage=stage,
        sequence=99,
        snapshot_version="m0-v1",
        occurred_at=OCCURRED_AT,
        result=result,
    )


def _execution(
    status: RunStatus,
    *,
    warnings: tuple[Issue, ...] = (),
) -> SearchExecution:
    journal = RunJournal.new(run_id=RUN_ID, snapshot_version="m0-v1")
    journal = journal.start(OCCURRED_AT)
    journal = journal.finish(status, TERMINAL_AT)
    response = SearchResponse(
        run_id=RUN_ID,
        status=status,
        snapshot_version="m0-v1",
        config_fingerprint="0" * 64,
        algorithm_version="projection-test-v1",
        warnings=warnings,
    )
    return SearchExecution(response=response, journal=journal)


def test_project_journal_maps_live_kinds_with_independent_sequence_and_epoch_ms() -> None:
    projector = EventProjector()

    started = projector.project_journal(
        THREAD_ID,
        _event(RunEventKind.RUN_STARTED),
        7,
    )
    step_started = projector.project_journal(
        THREAD_ID,
        _event(RunEventKind.STAGE_STARTED, stage="ranking"),
        8,
    )
    step_finished = projector.project_journal(
        THREAD_ID,
        _event(
            RunEventKind.STAGE_COMPLETED,
            stage="ranking",
            result=StageResult(before=3, after=3),
        ),
        9,
    )
    degraded = projector.project_journal(
        THREAD_ID,
        _event(
            RunEventKind.STAGE_DEGRADED,
            stage="ranking",
            result=StageResult(
                before=3,
                after=3,
                degraded=True,
                issue_codes=("ranking.degraded",),
            ),
        ),
        10,
    )

    assert isinstance(started, RunStartedEvent)
    assert isinstance(step_started, StepStartedEvent)
    assert isinstance(step_finished, StepFinishedEvent)
    assert isinstance(degraded, StepDegradedEvent)
    assert (started.sequence, step_started.sequence, step_finished.sequence, degraded.sequence) == (
        7,
        8,
        9,
        10,
    )
    assert started.timestamp == 86_400_123
    assert step_started.step_name == "ranking"
    assert step_finished.step_name == "ranking"
    assert degraded.step_name == "ranking"
    assert degraded.issue_codes == ("ranking.degraded",)


@pytest.mark.parametrize("stage_name", STAGE_NAMES)
def test_project_journal_preserves_all_six_service_stage_names(stage_name: str) -> None:
    projected = EventProjector().project_journal(
        THREAD_ID,
        _event(RunEventKind.STAGE_STARTED, stage=stage_name),
        1,
    )

    assert isinstance(projected, StepStartedEvent)
    assert projected.step_name == stage_name


@pytest.mark.parametrize(
    "terminal_kind",
    (
        RunEventKind.RUN_COMPLETED,
        RunEventKind.RUN_NO_MATCH,
        RunEventKind.RUN_FAILED,
    ),
)
def test_project_journal_rejects_terminal_journal_events(terminal_kind: RunEventKind) -> None:
    with pytest.raises(ValueError, match="terminal journal"):
        EventProjector().project_journal(THREAD_ID, _event(terminal_kind), 1)


def test_project_execution_builds_snapshot_and_finished_as_one_strict_pair() -> None:
    execution = _execution(RunStatus.NO_MATCH)

    projected = EventProjector().project_execution(
        THREAD_ID,
        execution,
        first_sequence=11,
    )

    assert len(projected) == 2
    snapshot, terminal = projected
    assert isinstance(snapshot, StateSnapshotEvent)
    assert isinstance(terminal, RunFinishedEvent)
    assert snapshot.sequence == 11
    assert terminal.sequence == 12
    assert snapshot.timestamp == terminal.timestamp == 172_800_987
    assert snapshot.snapshot.response is execution.response
    assert terminal.result.status is RunStatus.NO_MATCH


def test_failed_execution_uses_first_public_error_issue_deterministically() -> None:
    first_error = Issue(
        code="intent.invalid",
        stage="intent",
        message="The interpreted request is invalid.",
        severity=IssueSeverity.ERROR,
    )
    execution = _execution(
        RunStatus.FAILED,
        warnings=(
            Issue(code="catalog.warning", stage="snapshot", message="Safe warning."),
            first_error,
            Issue(
                code="snapshot.invalid",
                stage="snapshot",
                message="The snapshot is invalid.",
                severity=IssueSeverity.ERROR,
            ),
        ),
    )

    snapshot, terminal = EventProjector().project_execution(
        THREAD_ID,
        execution,
        first_sequence=1,
    )

    assert isinstance(snapshot, StateSnapshotEvent)
    assert isinstance(terminal, RunErrorEvent)
    assert terminal.code == first_error.code
    assert terminal.message == first_error.message


def test_failed_execution_without_error_issue_uses_fixed_safe_fallback() -> None:
    execution = _execution(
        RunStatus.FAILED,
        warnings=(Issue(code="catalog.warning", stage="snapshot", message="Safe warning."),),
    )

    snapshot, terminal = EventProjector().project_execution(
        THREAD_ID,
        execution,
        first_sequence=3,
    )

    assert isinstance(snapshot, StateSnapshotEvent)
    assert isinstance(terminal, RunErrorEvent)
    assert terminal.code == "RUN_FAILED"
    assert terminal.message == "Run execution failed."
    assert (snapshot.sequence, terminal.sequence) == (3, 4)


def test_project_aborted_uses_only_the_fixed_transport_error() -> None:
    projected = EventProjector().project_aborted(
        THREAD_ID,
        RUN_ID,
        sequence=5,
    )

    assert isinstance(projected, RunErrorEvent)
    assert projected.thread_id == THREAD_ID
    assert projected.run_id == RUN_ID
    assert projected.sequence == 5
    assert projected.timestamp >= 0
    assert projected.code == "RUN_ABORTED"
    assert projected.message == "Run execution was aborted."
