"""M2c profile storage with strict model-manifest binding and redacted operator listing."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from glodex.adapters.m2a_opensearch import M2aOpenSearch, M2aOpenSearchError
from glodex.adapters.m2c_indexes import M2C_PROFILE_SCHEMA, PROFILE_ALIAS
from glodex.application.m2c_profile import M2cProfileEntry, M2cProfileError
from glodex.m2c_contract import M2cModelIdentity


class M2cProfileStore:
    """One M2c-only profile alias; M2a/M2b vectors can never be read as profiles."""

    def __init__(self, client: M2aOpenSearch) -> None:
        if type(client) is not M2aOpenSearch:
            raise TypeError("M2c profile store requires exact M2aOpenSearch")
        self._client = client

    async def set(self, entry: M2cProfileEntry) -> None:
        if type(entry) is not M2cProfileEntry:
            raise TypeError("M2c profile store requires exact profile entry")
        try:
            await self._client.index_document(
                alias=PROFILE_ALIAS,
                identity=entry.document_id,
                body={
                    "entry_id": entry.entry_id,
                    "kind": entry.kind,
                    "model_manifest_digest": entry.model_manifest_digest,
                    "profile_id": entry.profile_id,
                    "schema_version": M2C_PROFILE_SCHEMA,
                    "scope": entry.scope,
                    "user_vector": list(entry.user_vector),
                    "value": entry.value,
                },
            )
        except M2aOpenSearchError:
            raise M2cProfileError("M2C_PROFILE_DEGRADED") from None

    async def delete(self, *, profile_id: str, entry_id: str) -> bool:
        try:
            probe = M2cProfileEntry(
                profile_id=profile_id,
                entry_id=entry_id,
                scope="soft",
                kind="preference",
                value="probe",
                user_vector=_unit_vector(),
                model_manifest_digest="0" * 64,
            )
            return await self._client.delete_document(
                alias=PROFILE_ALIAS, identity=probe.document_id
            )
        except (M2aOpenSearchError, M2cProfileError):
            raise M2cProfileError("M2C_PROFILE_DEGRADED") from None

    async def list(
        self,
        *,
        profile_id: str,
        identity: M2cModelIdentity,
    ) -> tuple[M2cProfileEntry, ...]:
        if type(profile_id) is not str or type(identity) is not M2cModelIdentity:
            raise M2cProfileError("M2C_PROFILE_INVALID")
        try:
            response = await self._client.search(
                alias=PROFILE_ALIAS,
                body={
                    "_source": [
                        "entry_id",
                        "kind",
                        "model_manifest_digest",
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
            return _parse_entries(response, profile_id=profile_id, identity=identity)
        except M2cProfileError:
            raise
        except M2aOpenSearchError:
            raise M2cProfileError("M2C_PROFILE_DEGRADED") from None
        except Exception:
            raise M2cProfileError("M2C_PROFILE_DEGRADED") from None


def _parse_entries(
    response: Mapping[str, object],
    *,
    profile_id: str,
    identity: M2cModelIdentity,
) -> tuple[M2cProfileEntry, ...]:
    hits_root = response.get("hits")
    if type(hits_root) is not dict:
        raise M2cProfileError("M2C_PROFILE_DEGRADED")
    hits = cast("dict[str, object]", hits_root).get("hits")
    if type(hits) is not list or len(hits) > 16:
        raise M2cProfileError("M2C_PROFILE_DEGRADED")
    entries: list[M2cProfileEntry] = []
    for raw_hit in hits:
        if type(raw_hit) is not dict:
            raise M2cProfileError("M2C_PROFILE_DEGRADED")
        source = cast("dict[str, object]", raw_hit).get("_source")
        if type(source) is not dict:
            raise M2cProfileError("M2C_PROFILE_DEGRADED")
        item = cast("dict[str, object]", source)
        if item.get("model_manifest_digest") != identity.manifest_digest:
            raise M2cProfileError("M2C_PROFILE_MODEL_MISMATCH")
        if item.get("schema_version") != M2C_PROFILE_SCHEMA or item.get("profile_id") != profile_id:
            raise M2cProfileError("M2C_PROFILE_DEGRADED")
        vector = item.get("user_vector")
        if type(vector) is not list or any(type(value) not in (int, float) for value in vector):
            raise M2cProfileError("M2C_PROFILE_DEGRADED")
        entries.append(
            M2cProfileEntry(
                profile_id=profile_id,
                entry_id=_text(item.get("entry_id")),
                scope=_text(item.get("scope")),
                kind=_text(item.get("kind")),
                value=_text(item.get("value")),
                user_vector=tuple(float(value) for value in vector),
                model_manifest_digest=identity.manifest_digest,
            )
        )
    if len({entry.entry_id for entry in entries}) != len(entries):
        raise M2cProfileError("M2C_PROFILE_DEGRADED")
    return tuple(entries)


def _text(value: object) -> str:
    if type(value) is not str:
        raise M2cProfileError("M2C_PROFILE_DEGRADED")
    return value


def _unit_vector() -> tuple[float, ...]:
    return (1.0, *(0.0 for _ in range(1_023)))


__all__ = ["M2cProfileStore"]
