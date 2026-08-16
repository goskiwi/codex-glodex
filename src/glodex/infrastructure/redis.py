"""Fixed-loopback, fail-open Durable Redis cache adapter."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from hashlib import sha256
from typing import Any, Final, cast
from urllib.parse import urlsplit

from glodex.memory.context import UserContextProjection
from glodex.memory.models import (
    ConversationRole,
    ConversationTurn,
    ThreadSummary,
)
from glodex.observability.runtime import (
    M6AlertCode,
    M6AlertView,
    M6BreakerState,
    M6BreakerView,
    M6CostTotal,
    M6Operation,
    M6OperationsView,
    M6OperationsWindow,
)
from glodex.runtime.contracts import (
    DURABLE_CACHE_TIMEOUT_SECONDS,
    DURABLE_CACHE_VALUE_MAX_BYTES,
    DURABLE_SCHEMA_VERSION,
    DurableCacheValue,
)

_DEFAULT_REDIS_URL: Final = "redis://127.0.0.1:6380/0"
_USER_CONTEXT_PROJECTION_VERSION: Final = "glodex.user-context-projection.v2"
_M6_OPERATIONS_PROJECTION_VERSION: Final = "glodex.m6.operations-projection.v1"


def validate_redis_url(value: object) -> str:
    """Accept only the one Durable local Redis endpoint."""

    if type(value) is not str or not value or len(value) > 128:
        raise ValueError("Durable Redis URL is invalid")
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
        raise ValueError("Durable Redis URL must be the fixed loopback service")
    return _DEFAULT_REDIS_URL


class DurableRedisCache:
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
                socket_connect_timeout=DURABLE_CACHE_TIMEOUT_SECONDS,
                socket_timeout=DURABLE_CACHE_TIMEOUT_SECONDS,
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

    async def health(self) -> None:
        """Require one exact Redis PING for startup preflight."""

        await self.open()
        if self._client is None:
            raise RuntimeError("DURABLE_CACHE_UNAVAILABLE")
        try:
            async with asyncio.timeout(DURABLE_CACHE_TIMEOUT_SECONDS):
                response = await self._client.ping()
            if response is not True:
                raise ValueError("Redis PING response is invalid")
        except Exception as error:
            raise RuntimeError("DURABLE_CACHE_UNAVAILABLE") from error

    async def get(self, *, key: str, namespace: str) -> DurableCacheValue | None:
        if not _valid_key(key=key, namespace=namespace):
            return None
        await self.open()
        if self._client is None:
            return None
        try:
            async with asyncio.timeout(DURABLE_CACHE_TIMEOUT_SECONDS):
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
            if len(payload) > DURABLE_CACHE_VALUE_MAX_BYTES:
                return
            async with asyncio.timeout(DURABLE_CACHE_TIMEOUT_SECONDS):
                await self._client.set(key, payload, ex=ttl_seconds)
        except Exception:
            return

    async def get_user_context_projection(self, *, key: str) -> UserContextProjection | None:
        """Read a private user-memory projection only from the hashed context namespace."""

        if not _valid_key(key=key, namespace="context"):
            return None
        await self.open()
        if self._client is None:
            return None
        try:
            async with asyncio.timeout(DURABLE_CACHE_TIMEOUT_SECONDS):
                raw = await self._client.get(key)
            return _decode_user_context_projection(raw)
        except Exception:
            return None

    async def set_user_context_projection(
        self,
        *,
        key: str,
        value: UserContextProjection,
        ttl_seconds: int,
    ) -> None:
        """Store a bounded history projection; failure deliberately remains a cache miss."""

        if (
            not _valid_key(key=key, namespace="context")
            or type(value) is not UserContextProjection
            or type(ttl_seconds) is not int
            or isinstance(ttl_seconds, bool)
            or ttl_seconds != 900
        ):
            return
        await self.open()
        if self._client is None:
            return
        try:
            payload = _encode_user_context_projection(value)
            if len(payload) > DURABLE_CACHE_VALUE_MAX_BYTES:
                return
            async with asyncio.timeout(DURABLE_CACHE_TIMEOUT_SECONDS):
                await self._client.set(key, payload, ex=ttl_seconds)
        except Exception:
            return

    async def get_m6_operations_projection(
        self,
        *,
        window: M6OperationsWindow,
    ) -> M6OperationsView | None:
        """Read only a safe aggregate projection; cache loss is a PostgreSQL rebuild."""

        if type(window) is not M6OperationsWindow:
            return None
        await self.open()
        if self._client is None:
            return None
        try:
            async with asyncio.timeout(DURABLE_CACHE_TIMEOUT_SECONDS):
                raw = await self._client.get(_m6_operations_cache_key(window=window))
            return _decode_m6_operations_projection(raw, window=window)
        except Exception:
            return None

    async def set_m6_operations_projection(
        self,
        *,
        value: M6OperationsView,
        ttl_seconds: int,
    ) -> None:
        """Cache a bounded aggregate only; the durable truth remains PostgreSQL."""

        if (
            type(value) is not M6OperationsView
            or type(ttl_seconds) is not int
            or isinstance(ttl_seconds, bool)
            or ttl_seconds != 300
        ):
            return
        await self.open()
        if self._client is None:
            return
        try:
            payload = _encode_m6_operations_projection(value)
            if len(payload) > DURABLE_CACHE_VALUE_MAX_BYTES:
                return
            async with asyncio.timeout(DURABLE_CACHE_TIMEOUT_SECONDS):
                await self._client.set(
                    _m6_operations_cache_key(window=value.window),
                    payload,
                    ex=ttl_seconds,
                )
        except Exception:
            return


def _valid_key(*, key: object, namespace: object) -> bool:
    return (
        type(key) is str
        and type(namespace) is str
        and namespace in {"retrieval", "context"}
        and len(key) == len(f"durable:{namespace}:v3:") + 64
        and key.startswith(f"durable:{namespace}:v3:")
        and all(character in "0123456789abcdef" for character in key.rsplit(":", 1)[-1])
    )


def _decode(raw: object, *, namespace: str) -> DurableCacheValue | None:
    if type(raw) is not bytes or not raw or len(raw) > DURABLE_CACHE_VALUE_MAX_BYTES:
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
        if value.namespace != namespace or value.version != DURABLE_SCHEMA_VERSION:
            return None
        return value
    except Exception:
        return None


def _encode_user_context_projection(value: UserContextProjection) -> bytes:
    summary = value.summary
    payload: dict[str, object] = {
        "version": _USER_CONTEXT_PROJECTION_VERSION,
        "thread_id": value.thread_id,
        "latest_ordinal": value.latest_ordinal,
        "summary": (
            None
            if summary is None
            else {
                "thread_id": summary.thread_id,
                "revision": summary.revision,
                "covered_through_ordinal": summary.covered_through_ordinal,
                "summary": summary.summary,
            }
        ),
        "recent_turns": [
            {
                "thread_id": turn.thread_id,
                "ordinal": turn.ordinal,
                "role": turn.role.value,
                "display_content": turn.display_content,
                "terminal_run_id": turn.terminal_run_id,
            }
            for turn in value.recent_turns
        ],
    }
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _m6_operations_cache_key(*, window: M6OperationsWindow) -> str:
    """Use only the fixed window as material; no user or run value reaches Redis."""

    if type(window) is not M6OperationsWindow:
        raise TypeError("M6 operations cache window is invalid")
    digest = sha256(window.value.encode("ascii")).hexdigest()
    return f"durable:operations:v1:{digest}"


def _encode_m6_operations_projection(value: M6OperationsView) -> bytes:
    """Serialize the explicit no-identity operations allowlist."""

    if type(value) is not M6OperationsView:
        raise TypeError("M6 operations cache value is invalid")
    payload: dict[str, object] = {
        "alerts": [{"code": alert.code.value} for alert in value.alerts],
        "breakers": [
            {"operation": breaker.operation.value, "state": breaker.state.value}
            for breaker in value.breakers
        ],
        "cost_totals": [
            {"currency": total.currency, "micro_units": total.micro_units}
            for total in value.cost_totals
        ],
        "counts": {
            "aborted": value.aborted_count,
            "completed": value.completed_count,
            "failed": value.failed_count,
            "no_match": value.no_match_count,
            "operation": value.operation_count,
            "operation_failure": value.operation_failure_count,
            "receipt_reported": value.receipt_reported_count,
            "receipt_unavailable": value.receipt_unavailable_count,
        },
        "version": _M6_OPERATIONS_PROJECTION_VERSION,
        "window": value.window.value,
    }
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _decode_m6_operations_projection(
    raw: object,
    *,
    window: M6OperationsWindow,
) -> M6OperationsView | None:
    if type(raw) is not bytes or not raw or len(raw) > DURABLE_CACHE_VALUE_MAX_BYTES:
        return None
    try:
        payload = json.loads(raw)
        if type(payload) is not dict or set(payload) != {
            "alerts",
            "breakers",
            "cost_totals",
            "counts",
            "version",
            "window",
        }:
            return None
        values = cast("dict[str, object]", payload)
        if (
            values["version"] != _M6_OPERATIONS_PROJECTION_VERSION
            or values["window"] != window.value
        ):
            return None
        counts = values["counts"]
        cost_totals = values["cost_totals"]
        breakers = values["breakers"]
        alerts = values["alerts"]
        if (
            type(counts) is not dict
            or set(counts)
            != {
                "aborted",
                "completed",
                "failed",
                "no_match",
                "operation",
                "operation_failure",
                "receipt_reported",
                "receipt_unavailable",
            }
            or any(type(value) is not int or isinstance(value, bool) for value in counts.values())
            or type(cost_totals) is not list
            or type(breakers) is not list
            or type(alerts) is not list
            or not all(
                type(item) is dict
                and set(item) == {"currency", "micro_units"}
                and type(item["currency"]) is str
                and type(item["micro_units"]) is int
                and not isinstance(item["micro_units"], bool)
                for item in cost_totals
            )
            or not all(
                type(item) is dict
                and set(item) == {"operation", "state"}
                and type(item["operation"]) is str
                and type(item["state"]) is str
                for item in breakers
            )
            or not all(
                type(item) is dict and set(item) == {"code"} and type(item["code"]) is str
                for item in alerts
            )
        ):
            return None
        parsed_cost_totals = cast("list[dict[str, object]]", cost_totals)
        parsed_breakers = cast("list[dict[str, object]]", breakers)
        parsed_alerts = cast("list[dict[str, object]]", alerts)
        return M6OperationsView(
            window=window,
            completed_count=counts["completed"],
            no_match_count=counts["no_match"],
            failed_count=counts["failed"],
            aborted_count=counts["aborted"],
            operation_count=counts["operation"],
            operation_failure_count=counts["operation_failure"],
            receipt_reported_count=counts["receipt_reported"],
            receipt_unavailable_count=counts["receipt_unavailable"],
            cost_totals=tuple(
                M6CostTotal(
                    currency=cast("str", item["currency"]),
                    micro_units=cast("int", item["micro_units"]),
                )
                for item in parsed_cost_totals
            ),
            breakers=tuple(
                M6BreakerView(
                    operation=M6Operation(cast("str", item["operation"])),
                    state=M6BreakerState(cast("str", item["state"])),
                )
                for item in parsed_breakers
            ),
            alerts=tuple(
                M6AlertView(code=M6AlertCode(cast("str", item["code"]))) for item in parsed_alerts
            ),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _decode_user_context_projection(raw: object) -> UserContextProjection | None:
    if type(raw) is not bytes or not raw or len(raw) > DURABLE_CACHE_VALUE_MAX_BYTES:
        return None
    try:
        payload = json.loads(raw)
        if type(payload) is not dict:
            return None
        decoded = cast("dict[str, object]", payload)
        if decoded.get("version") != _USER_CONTEXT_PROJECTION_VERSION:
            return None
        thread_id = decoded.get("thread_id")
        latest_ordinal = decoded.get("latest_ordinal")
        summary_raw = decoded.get("summary")
        turns_raw = decoded.get("recent_turns")
        if (
            type(thread_id) is not str
            or type(latest_ordinal) is not int
            or isinstance(latest_ordinal, bool)
            or type(turns_raw) is not list
        ):
            return None
        summary: ThreadSummary | None
        if summary_raw is None:
            summary = None
        elif type(summary_raw) is dict:
            summary_data = cast("dict[str, object]", summary_raw)
            summary = ThreadSummary(
                thread_id=cast("str", summary_data["thread_id"]),
                revision=cast("int", summary_data["revision"]),
                covered_through_ordinal=cast("int", summary_data["covered_through_ordinal"]),
                summary=cast("str", summary_data["summary"]),
            )
        else:
            return None
        turns = tuple(
            ConversationTurn(
                thread_id=cast("str", turn["thread_id"]),
                ordinal=cast("int", turn["ordinal"]),
                role=ConversationRole(cast("str", turn["role"])),
                display_content=cast("str", turn["display_content"]),
                terminal_run_id=cast("str | None", turn["terminal_run_id"]),
            )
            for turn in turns_raw
            if type(turn) is dict
        )
        if len(turns) != len(turns_raw):
            return None
        return UserContextProjection(
            thread_id=thread_id,
            summary=summary,
            recent_turns=turns,
            latest_ordinal=latest_ordinal,
        )
    except (KeyError, TypeError, ValueError):
        return None


__all__ = ["DurableRedisCache", "validate_redis_url"]
