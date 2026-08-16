"""Offline contract evidence for Durable's narrow operator-only boundaries."""

from __future__ import annotations

import json

import pytest
import uvicorn

import glodex.cli as cli
import glodex.composition.agent_api as agent_api_composition
from glodex.agent.contracts import AgentEventKind, AgentRunEvent, ToolName
from glodex.cli import CliUsageError, _build_parser, main
from glodex.infrastructure.postgres import validate_postgres_dsn
from glodex.infrastructure.redis import _decode, validate_redis_url
from glodex.runtime.context import digest_safe_events
from glodex.runtime.contracts import DURABLE_SCHEMA_VERSION, DurableCacheValue, cache_key

pytestmark = [
    pytest.mark.contract,
    pytest.mark.spec(
        "GLO-DURABLE-P0-001",
        "GLO-DURABLE-P0-005",
        "GLO-DURABLE-P0-006",
        "GLO-DURABLE-P0-007",
        "GLO-DURABLE-P0-008",
        "GLO-DURABLE-NFR-003",
        "GLO-DURABLE-NFR-004",
        "GLO-DURABLE-NFR-005",
        "GLO-DURABLE-NFR-006",
        "GLO-DURABLE-NFR-007",
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
        AgentRunEvent(kind=AgentEventKind.AGENT_STARTED, run_id="run-durable-contract"),
        AgentRunEvent(
            kind=AgentEventKind.TOOL_FINISHED,
            run_id="run-durable-contract",
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
            + b'"namespace":"context","version":"glodex.durable.v3"}'
        ),
        namespace="context",
    )

    assert value.version == DURABLE_SCHEMA_VERSION
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


def test_operator_cli_uses_semantic_commands_without_legacy_aliases() -> None:
    parser = _build_parser()
    storage = parser.parse_args(("storage", "migrate", "--live"))
    agent_api = parser.parse_args(("agent-api", "serve", "--live"))
    web_console = parser.parse_args(("web-console", "serve", "--live"))
    product_index = parser.parse_args(("product-index", "verify", "--live"))

    assert storage.command == "storage"
    assert storage.storage_action == "migrate"
    assert agent_api.command == "agent-api"
    assert agent_api.agent_api_action == "serve"
    assert web_console.command == "web-console"
    assert web_console.web_console_action == "serve"
    assert product_index.command == "product-index"
    assert product_index.action == "verify"
    assert not hasattr(product_index, "snapshot")
    with pytest.raises(CliUsageError):
        parser.parse_args(("durable-migrate", "--live"))
    with pytest.raises(CliUsageError):
        parser.parse_args(("durable-serve", "--live"))
    with pytest.raises(CliUsageError):
        parser.parse_args(("web_console-serve", "--live"))


def test_agent_api_fails_closed_when_live_preflight_is_unavailable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(("agent-api", "serve", "--live")) == 1
    assert json.loads(capsys.readouterr().out) == {
        "code": "AGENT_API_PREFLIGHT_FAILED",
        "status": "FAILED",
    }


def test_agent_api_binds_fixed_loopback_only_after_composition(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = object()
    calls: list[tuple[object, str, int]] = []

    async def build_agent_api(*, config: object) -> object:
        assert config is not None
        return app

    def run(server_app: object, *, host: str, port: int, **options: object) -> None:
        assert options == {"access_log": False, "log_config": None}
        calls.append((server_app, host, port))

    monkeypatch.setattr(cli, "load_config", lambda **_kwargs: object())
    monkeypatch.setattr(agent_api_composition, "build_agent_api", build_agent_api)
    monkeypatch.setattr(uvicorn, "run", run)

    assert main(("agent-api", "serve", "--live")) == 0
    assert calls == [(app, "127.0.0.1", 8766)]
    assert json.loads(capsys.readouterr().out) == {
        "host": "127.0.0.1",
        "port": 8766,
        "status": "STARTING",
    }


def test_cache_value_is_bounded_and_versioned() -> None:
    value = DurableCacheValue(
        namespace="retrieval",
        version=DURABLE_SCHEMA_VERSION,
        identities=("record-1",),
        metadata=(("query_candidate_count", 1),),
    )

    assert value.identities == ("record-1",)
