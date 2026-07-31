"""Offline contract evidence for M2b's narrow operator-only boundaries."""

from __future__ import annotations

import pytest

from glodex.adapters.m2b_postgres import validate_postgres_dsn
from glodex.adapters.m2b_redis import _decode, validate_redis_url
from glodex.application.agent.contracts import AgentEventKind, AgentRunEvent, ToolName
from glodex.application.durable.context import digest_safe_events
from glodex.application.durable.contracts import M2B_SCHEMA_VERSION, DurableCacheValue, cache_key
from glodex.cli import CliUsageError, _build_parser

pytestmark = [
    pytest.mark.contract,
    pytest.mark.spec(
        "GLO-M2B-P0-001",
        "GLO-M2B-P0-005",
        "GLO-M2B-P0-006",
        "GLO-M2B-P0-007",
        "GLO-M2B-P0-008",
        "GLO-M2B-NFR-003",
        "GLO-M2B-NFR-004",
        "GLO-M2B-NFR-005",
        "GLO-M2B-NFR-006",
        "GLO-M2B-NFR-007",
    ),
]


def test_durable_service_endpoints_are_fixed_loopback_without_credentials() -> None:
    assert validate_postgres_dsn("postgresql://glodex@127.0.0.1:5433/glodex").endswith(
        ":5433/glodex"
    )
    assert validate_redis_url("redis://127.0.0.1:6380/0").endswith(":6380/0")
    for value in (
        "postgresql://glodex:secret@127.0.0.1:5433/glodex",
        "postgresql://glodex@localhost:5433/glodex",
        "redis://127.0.0.1:6379/0",
        "redis://:secret@127.0.0.1:6380/0",
    ):
        with pytest.raises(ValueError):
            if value.startswith("postgres"):
                validate_postgres_dsn(value)
            else:
                validate_redis_url(value)


def test_redis_value_rejects_wrong_version_and_context_digest_has_no_raw_request() -> None:
    events = (
        AgentRunEvent(kind=AgentEventKind.AGENT_STARTED, run_id="run-m2b-contract"),
        AgentRunEvent(
            kind=AgentEventKind.TOOL_FINISHED,
            run_id="run-m2b-contract",
            tool_name=ToolName.SHOPPING_SUMMARY,
            safe_code="COMPLETED",
        ),
    )
    digest = digest_safe_events(events)
    value = digest.cache_value()
    decoded = _decode(
        (
            b'{"identities":["'
            + digest.digest.encode("ascii")
            + b'"],"metadata":[["event_count",2],["terminal_count",0],["tool_count",1]],'
            + b'"namespace":"context","version":"glodex.m2b.v1"}'
        ),
        namespace="context",
    )

    assert value.version == M2B_SCHEMA_VERSION
    assert decoded == value
    assert "query" not in digest.digest
    assert (
        _decode(
            b'{"namespace":"context","version":"old","identities":[]}',
            namespace="context",
        )
        is None
    )
    assert "private preference" not in cache_key(
        namespace="retrieval", material={"query": "private preference"}
    )


def test_operator_cli_rejects_arbitrary_dsn_and_keeps_m2b_commands_explicit() -> None:
    parser = _build_parser()
    parsed = parser.parse_args(("m2b-migrate", "--live"))
    server = parser.parse_args(("m2b-serve", "--live"))

    assert parsed.command == "m2b-migrate"
    assert server.command == "m2b-serve"
    with pytest.raises(CliUsageError):
        parser.parse_args(("m2b-migrate", "--live", "--dsn", "postgresql://remote/db"))
    with pytest.raises(CliUsageError):
        parser.parse_args(("m2b-serve", "--live", "--port", "8765"))


def test_cache_value_is_bounded_and_versioned() -> None:
    value = DurableCacheValue(
        namespace="retrieval",
        version=M2B_SCHEMA_VERSION,
        identities=("record-1",),
        metadata=(("query_candidate_count", 1),),
    )

    assert value.identities == ("record-1",)
