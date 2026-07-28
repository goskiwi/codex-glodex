"""Unit coverage for strict Last-Event-ID parsing."""

from __future__ import annotations

import pytest

from glodex.api.events import InvalidEventCursor, parse_event_cursor

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec(
        "GLO-M1-P0-005",
        "GLO-M1-NFR-003",
        "GLO-M1-NFR-005",
        "GLO-M1-NFR-007",
    ),
]


def test_missing_cursor_starts_before_the_first_public_event() -> None:
    assert parse_event_cursor(None, run_id="run-cursor-001", last_sequence=4) == 0


@pytest.mark.parametrize("sequence", (1, 2, 17))
def test_valid_cursor_returns_the_sequence_to_resume_after(sequence: int) -> None:
    assert (
        parse_event_cursor(
            f"run-cursor-001:{sequence}",
            run_id="run-cursor-001",
            last_sequence=17,
        )
        == sequence
    )


@pytest.mark.parametrize(
    "value",
    (
        "",
        " ",
        "run-cursor-001",
        "run-cursor-001:",
        ":1",
        "run-cursor-001:0",
        "run-cursor-001:01",
        "run-cursor-001:-1",
        "run-cursor-001:+1",
        "run-cursor-001:1.0",
        "run-cursor-001:\uff11",
        "run:cursor:1",
        "run-cursor-001:1\n",
        "run-cursor-001:" + ("9" * 200),
    ),
)
def test_malformed_cursor_is_rejected_without_echoing_the_value(value: str) -> None:
    with pytest.raises(InvalidEventCursor) as captured:
        parse_event_cursor(value, run_id="run-cursor-001", last_sequence=17)

    assert str(captured.value) == "Invalid event cursor."


def test_cursor_for_another_run_is_rejected() -> None:
    with pytest.raises(InvalidEventCursor, match=r"^Invalid event cursor\.$"):
        parse_event_cursor(
            "run-cursor-other:2",
            run_id="run-cursor-001",
            last_sequence=4,
        )


def test_cursor_ahead_of_the_buffer_is_rejected() -> None:
    with pytest.raises(InvalidEventCursor, match=r"^Invalid event cursor\.$"):
        parse_event_cursor(
            "run-cursor-001:5",
            run_id="run-cursor-001",
            last_sequence=4,
        )


def test_positive_cursor_is_invalid_when_the_run_has_no_events() -> None:
    with pytest.raises(InvalidEventCursor):
        parse_event_cursor(
            "run-cursor-001:1",
            run_id="run-cursor-001",
            last_sequence=0,
        )
