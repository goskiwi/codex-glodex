"""Small, shared JSON primitives for trusted protocol boundaries."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Never


def compact_dumps(value: object) -> str:
    """Return deterministic, Unicode-preserving JSON without display whitespace."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compact_bytes(value: object) -> bytes:
    return compact_dumps(value).encode("utf-8")


def loads_unique(text: str | bytes, *, reject_constants: bool = False) -> object:
    """Load JSON while rejecting ambiguous duplicate object keys."""

    return json.loads(
        text,
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant if reject_constants else None,
    )


def _unique_object(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> Never:
    raise ValueError(f"non-standard JSON constant: {value}")


__all__ = ["compact_bytes", "compact_dumps", "loads_unique"]
