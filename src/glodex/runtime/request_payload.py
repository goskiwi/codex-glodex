"""Canonical durable envelope for display text and resolved execution input."""

from __future__ import annotations

from glodex.contracts import SearchRequest

_SCHEMA_VERSION = "glodex.conversation-request.v2"
_PAYLOAD_KEYS = {"schema_version", "display_query", "task_turns", "effective_request"}


def durable_request_payload(
    *,
    request: SearchRequest,
    display_query: str,
    task_turns: tuple[str, ...],
) -> dict[str, object]:
    """Keep the literal user turn separate from the resolved shopping task."""

    if type(request) is not SearchRequest:
        raise TypeError("durable request requires an exact SearchRequest")
    _validate_display_query(display_query)
    _validate_task_turns(task_turns, display_query=display_query)
    return {
        "schema_version": _SCHEMA_VERSION,
        "display_query": display_query,
        "task_turns": list(task_turns),
        "effective_request": request.model_dump(mode="json"),
    }


def search_request_from_durable_payload(payload: dict[str, object]) -> SearchRequest:
    """Read the current durable request envelope."""

    _validate_envelope(payload)
    effective = payload["effective_request"]
    if type(effective) is not dict:
        raise ValueError("durable effective request is invalid")
    return SearchRequest.model_validate(effective)


def display_query_from_durable_payload(payload: dict[str, object]) -> str:
    """Return the literal turn shown in history and considered for durable memory."""

    _validate_envelope(payload)
    display_query = payload["display_query"]
    assert type(display_query) is str
    return display_query


def task_turns_from_durable_payload(payload: dict[str, object]) -> tuple[str, ...]:
    """Return the bounded user instructions that currently constitute the task."""

    _validate_envelope(payload)
    values = payload["task_turns"]
    assert type(values) is list
    return tuple(values)


def _validate_envelope(payload: dict[str, object]) -> None:
    if (
        type(payload) is not dict
        or set(payload) != _PAYLOAD_KEYS
        or payload.get("schema_version") != _SCHEMA_VERSION
    ):
        raise ValueError("durable request envelope is invalid")
    display_query = payload.get("display_query")
    if type(display_query) is not str:
        raise ValueError("durable display query is invalid")
    _validate_display_query(display_query)
    task_turns = payload.get("task_turns")
    if type(task_turns) is not list or any(type(value) is not str for value in task_turns):
        raise ValueError("durable task turns are invalid")
    _validate_task_turns(tuple(task_turns), display_query=display_query)


def _validate_display_query(value: str) -> None:
    if type(value) is not str or not value.strip() or value != value.strip():
        raise ValueError("durable display query is invalid")
    if len(value) > 2_000 or "\0" in value:
        raise ValueError("durable display query is invalid")


def _validate_task_turns(values: tuple[str, ...], *, display_query: str) -> None:
    if type(values) is not tuple or not values or len(values) > 64:
        raise ValueError("durable task turns are invalid")
    for value in values:
        _validate_display_query(value)
    if values[-1] != display_query:
        raise ValueError("durable task turns must end with the display query")


__all__ = [
    "display_query_from_durable_payload",
    "durable_request_payload",
    "search_request_from_durable_payload",
    "task_turns_from_durable_payload",
]
