"""Fixed-loopback, fail-open M2b Redis cache adapter."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import Any, Final, cast
from urllib.parse import urlsplit

from glodex.application.durable.contracts import (
    M2B_CACHE_TIMEOUT_SECONDS,
    M2B_CACHE_VALUE_MAX_BYTES,
    M2B_SCHEMA_VERSION,
    DurableCacheValue,
)

_DEFAULT_REDIS_URL: Final = "redis://127.0.0.1:6380/0"


def validate_redis_url(value: object) -> str:
    """Accept only the one M2b local Redis endpoint."""

    if type(value) is not str or not value or len(value) > 128:
        raise ValueError("M2b Redis URL is invalid")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "redis"
        or parsed.hostname != "127.0.0.1"
        or parsed.port != 6380
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/0"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("M2b Redis URL must be the fixed loopback service")
    return _DEFAULT_REDIS_URL


class M2bRedisCache:
    """A bounded optional cache; every driver error is a cache miss/no-op."""

    def __init__(self, *, url: str = _DEFAULT_REDIS_URL, client: Any | None = None) -> None:
        self._url = validate_redis_url(url)
        self._client = client

    async def open(self) -> None:
        if self._client is not None:
            return
        try:
            from redis.asyncio import Redis

            self._client = Redis.from_url(
                self._url,
                decode_responses=False,
                socket_connect_timeout=M2B_CACHE_TIMEOUT_SECONDS,
                socket_timeout=M2B_CACHE_TIMEOUT_SECONDS,
                retry_on_timeout=False,
            )
        except Exception:
            self._client = None

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            await client.aclose()
        except Exception:
            return

    async def get(self, *, key: str, namespace: str) -> DurableCacheValue | None:
        if not _valid_key(key=key, namespace=namespace):
            return None
        await self.open()
        if self._client is None:
            return None
        try:
            async with asyncio.timeout(M2B_CACHE_TIMEOUT_SECONDS):
                raw = await self._client.get(key)
            return _decode(raw, namespace=namespace)
        except Exception:
            return None

    async def set(self, *, key: str, value: DurableCacheValue, ttl_seconds: int) -> None:
        if (
            not _valid_key(key=key, namespace=value.namespace)
            or type(ttl_seconds) is not int
            or isinstance(ttl_seconds, bool)
            or ttl_seconds not in {300, 900}
        ):
            return
        await self.open()
        if self._client is None:
            return
        try:
            payload = json.dumps(
                asdict(value),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(payload) > M2B_CACHE_VALUE_MAX_BYTES:
                return
            async with asyncio.timeout(M2B_CACHE_TIMEOUT_SECONDS):
                await self._client.set(key, payload, ex=ttl_seconds)
        except Exception:
            return


def _valid_key(*, key: object, namespace: object) -> bool:
    return (
        type(key) is str
        and type(namespace) is str
        and namespace in {"retrieval", "context"}
        and len(key) == len(f"m2b:{namespace}:v1:") + 64
        and key.startswith(f"m2b:{namespace}:v1:")
        and all(character in "0123456789abcdef" for character in key.rsplit(":", 1)[-1])
    )


def _decode(raw: object, *, namespace: str) -> DurableCacheValue | None:
    if type(raw) is not bytes or not raw or len(raw) > M2B_CACHE_VALUE_MAX_BYTES:
        return None
    try:
        payload = json.loads(raw)
        if type(payload) is not dict:
            return None
        decoded = cast("dict[str, object]", payload)
        value_namespace = decoded.get("namespace")
        version = decoded.get("version")
        identities = decoded.get("identities")
        metadata = decoded.get("metadata", [])
        if (
            type(value_namespace) is not str
            or type(version) is not str
            or type(identities) is not list
            or any(type(item) is not str for item in identities)
            or type(metadata) is not list
        ):
            return None
        parsed_metadata: list[tuple[str, int]] = []
        for item in metadata:
            if (
                type(item) is not list
                or len(item) != 2
                or type(item[0]) is not str
                or type(item[1]) is not int
                or isinstance(item[1], bool)
            ):
                return None
            parsed_metadata.append((item[0], item[1]))
        value = DurableCacheValue(
            namespace=value_namespace,
            version=version,
            identities=tuple(identities),
            metadata=tuple(parsed_metadata),
        )
        if value.namespace != namespace or value.version != M2B_SCHEMA_VERSION:
            return None
        return value
    except Exception:
        return None


__all__ = ["M2bRedisCache", "validate_redis_url"]
