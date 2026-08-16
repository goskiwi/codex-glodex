"""Strict parsing for durable SSE replay cursors."""

from __future__ import annotations

import re

_CURSOR_PATTERN = re.compile(r"(?P<run_id>[A-Za-z0-9][A-Za-z0-9._/-]*):(?P<sequence>[1-9][0-9]*)\Z")
_MAX_CURSOR_LENGTH = 160


class InvalidSseCursor(ValueError):
    """Raised when a Last-Event-ID cannot name a retained durable event."""


def parse_sse_cursor(
    value: str | None,
    *,
    run_id: str,
    last_sequence: int,
) -> int:
    """Return the retained sequence after which durable SSE replay begins."""

    if isinstance(last_sequence, bool) or not isinstance(last_sequence, int) or last_sequence < 0:
        raise ValueError("last_sequence must be a non-negative integer")
    if value is None:
        return 0
    if type(value) is not str or not 3 <= len(value) <= _MAX_CURSOR_LENGTH:
        raise InvalidSseCursor("Invalid event cursor.")

    match = _CURSOR_PATTERN.fullmatch(value)
    if match is None or match["run_id"] != run_id:
        raise InvalidSseCursor("Invalid event cursor.")

    sequence = int(match["sequence"])
    if sequence > last_sequence:
        raise InvalidSseCursor("Invalid event cursor.")
    return sequence


__all__ = ["InvalidSseCursor", "parse_sse_cursor"]
