"""Typed user-memory blacklist rules that can safely affect candidate publication."""

from __future__ import annotations

import re
from dataclasses import dataclass

from glodex.agent.catalog import ValidatedCandidatePool
from glodex.agent.contracts import Candidate, Platform
from glodex.memory.models import UserMemoryCategory, UserMemoryEntry
from glodex.memory.normalization import normalize_candidate_fact

_RULE = re.compile(r"(?P<field>item_id|brand|material|platform):(?P<value>[^:\x00]{1,128})\Z")
_ALLOWED_FIELDS = frozenset({"item_id", "brand", "material", "platform"})


@dataclass(frozen=True, slots=True)
class VerifiedBlacklistRule:
    """One exact, user-authored constraint against a trusted candidate fact."""

    field: str
    value: str

    def __post_init__(self) -> None:
        if (
            self.field not in _ALLOWED_FIELDS
            or type(self.value) is not str
            or not self.value
            or self.value != self.value.strip()
            or len(self.value) > 128
            or "\x00" in self.value
        ):
            raise ValueError("verified blacklist rule is invalid")
        if self.field == "platform":
            try:
                Platform(self.value.casefold())
            except ValueError:
                raise ValueError("verified blacklist platform is invalid") from None

    @property
    def normalized_value(self) -> str:
        return self.value.casefold()

    @property
    def label(self) -> str:
        """Return the bounded private-context representation, never an entry ID."""

        return f"{self.field}:{self.normalized_value}"

    def matches(self, candidate: Candidate) -> bool:
        """Match only facts already validated against the canonical product record."""

        if type(candidate) is not Candidate:
            raise TypeError("blacklist candidate must be exact")
        if self.field == "item_id":
            return candidate.item_id.casefold() == self.normalized_value
        if self.field == "platform":
            return candidate.platform.value == self.normalized_value
        aliases = {"材质": "material", "品牌": "brand"}
        attributes = {
            aliases.get(attribute.name.casefold(), attribute.name.casefold()): attribute.value
            for attribute in candidate.attributes
        }
        candidate_value = attributes.get(self.field)
        if candidate_value is None:
            return False
        actual = normalize_candidate_fact(field=self.field, value=candidate_value)
        expected = normalize_candidate_fact(field=self.field, value=self.normalized_value)
        return actual == expected


@dataclass(frozen=True, slots=True)
class VerifiedBlacklistGuard:
    """A deterministic publication filter built only from strictly parseable entries."""

    rules: tuple[VerifiedBlacklistRule, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.rules) is not tuple
            or any(type(rule) is not VerifiedBlacklistRule for rule in self.rules)
            or len(self.rules) > 64
            or len(self.rules) != len(set(self.rules))
        ):
            raise ValueError("verified blacklist guard is invalid")

    @classmethod
    def from_entries(cls, entries: tuple[UserMemoryEntry, ...]) -> VerifiedBlacklistGuard:
        """Discard unstructured "avoid" text: it is context, not a hard predicate."""

        if type(entries) is not tuple or any(
            type(entry) is not UserMemoryEntry for entry in entries
        ):
            raise TypeError("blacklist entries must be exact")
        rules = tuple(
            rule
            for entry in entries
            if entry.category is UserMemoryCategory.BLACKLIST
            for rule in (_parse_rule(entry.content),)
            if rule is not None
        )
        return cls(rules=tuple(dict.fromkeys(rules)))

    @property
    def private_labels(self) -> tuple[str, ...]:
        return tuple(rule.label for rule in self.rules)

    def excluded_candidate_ids(self, *, pool: ValidatedCandidatePool) -> tuple[str, ...]:
        """Return candidate IDs in pool order so selection and final publication agree."""

        if type(pool) is not ValidatedCandidatePool:
            raise TypeError("blacklist pool must be exact")
        if not self.rules:
            return ()
        return tuple(
            candidate.candidate_id
            for candidate in pool.candidates
            if any(rule.matches(candidate) for rule in self.rules)
        )


def _parse_rule(content: str) -> VerifiedBlacklistRule | None:
    match = _RULE.fullmatch(content)
    if match is None:
        return None
    try:
        return VerifiedBlacklistRule(
            field=match.group("field"),
            value=match.group("value").strip(),
        )
    except ValueError:
        return None


__all__ = ["VerifiedBlacklistGuard", "VerifiedBlacklistRule"]
