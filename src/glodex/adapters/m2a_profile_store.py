"""Profile-index adapter; values are visible only to explicit local M2a operator commands."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from glodex.adapters.m2a_indexes import M2A_PROFILE_SCHEMA, PROFILE_ALIAS
from glodex.adapters.m2a_opensearch import M2aOpenSearch, M2aOpenSearchError
from glodex.application.m2a_profile import M2aProfileEntry, M2aProfileError


class M2aProfileStore:
    """Read/write only one schema-versioned profile alias with fixed document shape."""

    def __init__(self, client: M2aOpenSearch) -> None:
        if type(client) is not M2aOpenSearch:
            raise TypeError("M2a profile store requires exact M2aOpenSearch")
        self._client = client

    async def set(self, entry: M2aProfileEntry) -> None:
        if type(entry) is not M2aProfileEntry:
            raise TypeError("M2a profile store requires an exact profile entry")
        try:
            await self._client.index_document(
                alias=PROFILE_ALIAS,
                identity=entry.document_id,
                body={
                    "entry_id": entry.entry_id,
                    "kind": entry.kind,
                    "profile_id": entry.profile_id,
                    "schema_version": M2A_PROFILE_SCHEMA,
                    "scope": entry.scope,
                    "user_vector": list(entry.user_vector),
                    "value": entry.value,
                },
            )
        except M2aOpenSearchError:
            raise M2aProfileError("M2A_PROFILE_DEGRADED") from None

    async def delete(self, *, profile_id: str, entry_id: str) -> bool:
        try:
            probe = M2aProfileEntry(
                profile_id=profile_id,
                entry_id=entry_id,
                scope="soft",
                kind="preference",
                value="probe",
                user_vector=_unit_vector(),
            )
            return await self._client.delete_document(
                alias=PROFILE_ALIAS,
                identity=probe.document_id,
            )
        except (M2aOpenSearchError, M2aProfileError):
            raise M2aProfileError("M2A_PROFILE_DEGRADED") from None

    async def list(self, *, profile_id: str) -> tuple[M2aProfileEntry, ...]:
        if type(profile_id) is not str:
            raise M2aProfileError("M2A_PROFILE_INVALID")
        try:
            response = await self._client.search(
                alias=PROFILE_ALIAS,
                body={
                    "_source": [
                        "entry_id",
                        "kind",
                        "profile_id",
                        "schema_version",
                        "scope",
                        "user_vector",
                        "value",
                    ],
                    "query": {"term": {"profile_id": profile_id}},
                    "size": 16,
                    "sort": [{"entry_id": "asc"}],
                },
            )
            return _parse_entries(response, profile_id=profile_id)
        except M2aProfileError:
            raise
        except M2aOpenSearchError:
            raise M2aProfileError("M2A_PROFILE_DEGRADED") from None
        except Exception:
            raise M2aProfileError("M2A_PROFILE_DEGRADED") from None


def _parse_entries(
    response: Mapping[str, object],
    *,
    profile_id: str,
) -> tuple[M2aProfileEntry, ...]:
    hits_root = response.get("hits")
    if type(hits_root) is not dict:
        raise M2aProfileError("M2A_PROFILE_DEGRADED")
    hits = cast("dict[str, object]", hits_root).get("hits")
    if type(hits) is not list or len(hits) > 16:
        raise M2aProfileError("M2A_PROFILE_DEGRADED")
    entries: list[M2aProfileEntry] = []
    for raw_hit in hits:
        if type(raw_hit) is not dict:
            raise M2aProfileError("M2A_PROFILE_DEGRADED")
        source = cast("dict[str, object]", raw_hit).get("_source")
        if type(source) is not dict:
            raise M2aProfileError("M2A_PROFILE_DEGRADED")
        item = cast("dict[str, object]", source)
        if item.get("schema_version") != M2A_PROFILE_SCHEMA or item.get("profile_id") != profile_id:
            raise M2aProfileError("M2A_PROFILE_DEGRADED")
        vector = item.get("user_vector")
        if type(vector) is not list or any(type(value) not in {int, float} for value in vector):
            raise M2aProfileError("M2A_PROFILE_DEGRADED")
        entries.append(
            M2aProfileEntry(
                profile_id=profile_id,
                entry_id=_text(item.get("entry_id")),
                scope=_text(item.get("scope")),
                kind=_text(item.get("kind")),
                value=_text(item.get("value")),
                user_vector=tuple(float(value) for value in vector),
            )
        )
    if len({entry.entry_id for entry in entries}) != len(entries):
        raise M2aProfileError("M2A_PROFILE_DEGRADED")
    return tuple(entries)


def _text(value: object) -> str:
    if type(value) is not str:
        raise M2aProfileError("M2A_PROFILE_DEGRADED")
    return value


def _unit_vector() -> tuple[float, ...]:
    return (1.0, *(0.0 for _ in range(1_023)))


__all__ = ["M2aProfileStore"]
