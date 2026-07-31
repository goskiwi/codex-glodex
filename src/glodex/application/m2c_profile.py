"""Typed soft preferences that are bound to exactly one M2c BGE manifest."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Final

from glodex.application.agent.contracts import EmbeddingResult

M2C_PROFILE_SCOPE: Final = "soft"
M2C_PROFILE_KIND: Final = "preference"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MAX_VALUE_LENGTH: Final = 512


class M2cProfileError(ValueError):
    """A stable M2c profile validation or model-binding failure."""


@dataclass(frozen=True, slots=True)
class M2cProfileEntry:
    profile_id: str
    entry_id: str
    scope: str
    kind: str
    value: str
    user_vector: tuple[float, ...]
    model_manifest_digest: str

    def __post_init__(self) -> None:
        if (
            _IDENTIFIER.fullmatch(self.profile_id) is None
            or _IDENTIFIER.fullmatch(self.entry_id) is None
            or self.scope != M2C_PROFILE_SCOPE
            or self.kind != M2C_PROFILE_KIND
            or type(self.value) is not str
            or not self.value.strip()
            or self.value != self.value.strip()
            or len(self.value) > _MAX_VALUE_LENGTH
            or "\0" in self.value
            or _DIGEST.fullmatch(self.model_manifest_digest) is None
        ):
            raise M2cProfileError("M2C_PROFILE_INVALID")
        try:
            EmbeddingResult(vectors=(self.user_vector,))
        except Exception:
            raise M2cProfileError("M2C_PROFILE_INVALID") from None

    @property
    def document_id(self) -> str:
        return f"{self.profile_id}::{self.entry_id}"

    @property
    def safe_identity(self) -> str:
        return hashlib.sha256(self.document_id.encode("utf-8")).hexdigest()[:16]


def entry_id_for(value: str) -> str:
    if type(value) is not str or not value.strip() or len(value) > _MAX_VALUE_LENGTH:
        raise M2cProfileError("M2C_PROFILE_INVALID")
    return f"pref-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:20]}"


def profile_entry_from_operator(
    *,
    profile_id: str,
    value: str,
    vector: tuple[float, ...],
    model_manifest_digest: str,
    entry_id: str | None = None,
) -> M2cProfileEntry:
    return M2cProfileEntry(
        profile_id=profile_id,
        entry_id=entry_id_for(value) if entry_id is None else entry_id,
        scope=M2C_PROFILE_SCOPE,
        kind=M2C_PROFILE_KIND,
        value=value,
        user_vector=vector,
        model_manifest_digest=model_manifest_digest,
    )


def profile_conflicts_with_current_query(*, entry: M2cProfileEntry, query: str) -> bool:
    if type(entry) is not M2cProfileEntry or type(query) is not str:
        raise TypeError("M2c profile conflict judge requires exact inputs")
    normalized = " ".join(query.casefold().split())
    value = " ".join(entry.value.casefold().split())
    return bool(value and (f"不要{value}" in normalized or f"不需要{value}" in normalized))


__all__ = [
    "M2C_PROFILE_KIND",
    "M2C_PROFILE_SCOPE",
    "M2cProfileEntry",
    "M2cProfileError",
    "entry_id_for",
    "profile_conflicts_with_current_query",
    "profile_entry_from_operator",
]
