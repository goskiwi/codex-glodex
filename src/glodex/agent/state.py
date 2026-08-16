"""Shared bounded-value helpers for shopping tools.

This module intentionally contains no Agent loop, selector state, fork state,
or model protocol.  The native LangGraph session owns its private live state.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel

TOOL_RESULT_BYTE_LIMIT = 512 * 1024
CANDIDATE_STORE_BYTE_LIMIT = 2 * 1024 * 1024
ROOT_AGENT_DEADLINE_SECONDS = 240
CHILD_AGENT_DEADLINE_SECONDS = 90


class BudgetExceeded(RuntimeError):
    """A stable resource-limit failure without user or provider data."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def canonical_json_bytes(value: object) -> bytes:
    """Serialize trusted typed values with the one bounded canonical form."""

    normalized = _canonical_value(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def require_byte_limit(value: object, *, maximum: int, code: str) -> bytes:
    """Return canonical bytes or fail at the first byte over the limit."""

    if type(maximum) is not int or isinstance(maximum, bool) or maximum < 0:
        raise ValueError("maximum must be a non-negative integer")
    encoded = canonical_json_bytes(value)
    if len(encoded) > maximum:
        raise BudgetExceeded(code)
    return encoded


def _canonical_value(value: object) -> object:
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is float:
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("canonical JSON does not support non-finite floats")
        return value
    if type(value) is Decimal:
        if not value.is_finite():
            raise ValueError("canonical JSON does not support non-finite Decimals")
        return format(value, "f")
    if type(value) is datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("canonical JSON datetimes must be UTC")
        return value.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        return _canonical_value(value.model_dump(mode="python"))
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _canonical_value(getattr(value, field.name)) for field in fields(value)}
    if type(value) is tuple or type(value) is list:
        return [_canonical_value(item) for item in value]
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError("canonical JSON mappings require string keys")
            normalized[key] = _canonical_value(item)
        return normalized
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


__all__ = [
    "CANDIDATE_STORE_BYTE_LIMIT",
    "CHILD_AGENT_DEADLINE_SECONDS",
    "ROOT_AGENT_DEADLINE_SECONDS",
    "TOOL_RESULT_BYTE_LIMIT",
    "BudgetExceeded",
    "canonical_json_bytes",
    "require_byte_limit",
]
