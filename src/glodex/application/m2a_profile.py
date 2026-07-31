"""Explicit, local and typed M2a preference-memory contracts."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Final

from glodex.application.agent.contracts import EmbeddingResult

M2A_PROFILE_SCOPE: Final = "soft"
M2A_PROFILE_KIND: Final = "preference"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
_MAX_VALUE_LENGTH: Final = 512


class M2aProfileError(ValueError):
    """A stable validation error for operator-maintained M2a profile entries."""


@dataclass(frozen=True, slots=True)
class M2aProfileEntry:
    profile_id: str
    entry_id: str
    scope: str
    kind: str
    value: str
    user_vector: tuple[float, ...]

    def __post_init__(self) -> None:
        if (
            _IDENTIFIER.fullmatch(self.profile_id) is None
            or _IDENTIFIER.fullmatch(self.entry_id) is None
            or self.scope != M2A_PROFILE_SCOPE
            or self.kind != M2A_PROFILE_KIND
            or type(self.value) is not str
            or not self.value.strip()
            or self.value != self.value.strip()
            or len(self.value) > _MAX_VALUE_LENGTH
            or "\0" in self.value
        ):
            raise M2aProfileError("M2A_PROFILE_INVALID")
        try:
            EmbeddingResult(vectors=(self.user_vector,))
        except Exception:
            raise M2aProfileError("M2A_PROFILE_INVALID") from None

    @property
    def document_id(self) -> str:
        return f"{self.profile_id}::{self.entry_id}"

    @property
    def safe_identity(self) -> str:
        return hashlib.sha256(self.document_id.encode("utf-8")).hexdigest()[:16]


def entry_id_for(value: str) -> str:
    """Return a deterministic opaque entry identifier without storing inferred data."""

    if type(value) is not str or not value.strip() or len(value) > _MAX_VALUE_LENGTH:
        raise M2aProfileError("M2A_PROFILE_INVALID")
    return f"pref-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:20]}"


def profile_entry_from_operator(
    *,
    profile_id: str,
    value: str,
    vector: tuple[float, ...],
    entry_id: str | None = None,
) -> M2aProfileEntry:
    """Construct exactly one allowed local soft-preference entry."""

    identifier = entry_id_for(value) if entry_id is None else entry_id
    return M2aProfileEntry(
        profile_id=profile_id,
        entry_id=identifier,
        scope=M2A_PROFILE_SCOPE,
        kind=M2A_PROFILE_KIND,
        value=value,
        user_vector=vector,
    )


def profile_conflicts_with_current_query(*, entry: M2aProfileEntry, query: str) -> bool:
    """Conservative deterministic guard for explicit contradictions in the current request.

    The M1d runtime owns the full Required/Hard-Gate decision.  This local guard only
    rejects a typed preference when the query explicitly excludes its complete value;
    it never turns a preference into a required shopping constraint.
    """

    if type(entry) is not M2aProfileEntry or type(query) is not str:
        raise TypeError("M2a profile conflict judge requires exact inputs")
    normalized = " ".join(query.casefold().split())
    value = " ".join(entry.value.casefold().split())
    return bool(value and (f"不要{value}" in normalized or f"不需要{value}" in normalized))


__all__ = [
    "M2A_PROFILE_KIND",
    "M2A_PROFILE_SCOPE",
    "M2aProfileEntry",
    "M2aProfileError",
    "entry_id_for",
    "profile_conflicts_with_current_query",
    "profile_entry_from_operator",
]
