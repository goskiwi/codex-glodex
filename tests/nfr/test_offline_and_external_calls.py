"""Dynamic proof that the complete M0 path remains local and offline."""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import socket
import sqlite3
import subprocess
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from glodex.adapters.deterministic_ranker import DeterministicQueryRanker
from glodex.adapters.local_snapshot import LocalSnapshotCatalog
from glodex.adapters.rule_intent import RuleIntentInterpreter
from glodex.bootstrap import build_service, submit_search
from glodex.cli import DEMO_QUERY, main
from glodex.config import load_config
from glodex.contracts import RunStatus

pytestmark = [
    pytest.mark.nfr,
    pytest.mark.spec("GLO-P0-012", "AC-012", "GLO-NFR-006", "GLO-NFR-011"),
]

_VOLATILE_FIELDS = frozenset({"duration_ms", "occurred_at", "run_id"})


def _blocked_call(
    name: str,
    calls: dict[str, int],
) -> Callable[..., Any]:
    def blocked(*_args: object, **_kwargs: object) -> Any:
        calls[name] += 1
        raise AssertionError(f"external boundary was called: {name}")

    return blocked


def _business_semantics(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            key: _business_semantics(item)
            for key, item in value.items()
            if key not in _VOLATILE_FIELDS
        }
    if isinstance(value, list):
        return [_business_semantics(item) for item in value]
    return value


def test_default_composition_uses_only_local_stateless_adapters() -> None:
    service = build_service(load_config(environ={}))

    assert type(service.intent_interpreter) is RuleIntentInterpreter
    assert type(service.catalog_gateway) is LocalSnapshotCatalog
    assert type(service.query_ranker) is DeterministicQueryRanker


def test_search_service_and_cli_complete_without_any_external_call(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    external_environment_variable_predicate: Callable[[str], bool],
) -> None:
    monkeypatch.setenv("GLODEX_CONFIG", str(tmp_path / "poisoned-config.toml"))
    for name in tuple(os.environ):
        if name.startswith("GLODEX_") or external_environment_variable_predicate(name):
            monkeypatch.delenv(name, raising=False)

    assert "GLODEX_CONFIG" not in os.environ
    assert not any(name.startswith("GLODEX_") for name in os.environ)

    external_calls = {
        "asyncio": 0,
        "fixture-uri": 0,
        "http": 0,
        "https": 0,
        "socket": 0,
        "sqlite": 0,
        "subprocess": 0,
        "urlopen": 0,
    }
    real_path_open = Path.open

    def guarded_path_open(path: Path, *args: object, **kwargs: object) -> Any:
        if str(path).startswith("fixture:"):
            external_calls["fixture-uri"] += 1
            raise AssertionError("fixture evidence URI was treated as a file")
        return real_path_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_path_open)
    monkeypatch.setattr(
        socket.socket,
        "connect",
        _blocked_call("socket", external_calls),
    )
    monkeypatch.setattr(
        socket,
        "create_connection",
        _blocked_call("socket", external_calls),
    )
    monkeypatch.setattr(
        asyncio,
        "open_connection",
        _blocked_call("asyncio", external_calls),
    )
    monkeypatch.setattr(
        http.client.HTTPConnection,
        "connect",
        _blocked_call("http", external_calls),
    )
    monkeypatch.setattr(
        http.client.HTTPSConnection,
        "connect",
        _blocked_call("https", external_calls),
    )
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        _blocked_call("urlopen", external_calls),
    )
    monkeypatch.setattr(
        sqlite3,
        "connect",
        _blocked_call("sqlite", external_calls),
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        _blocked_call("subprocess", external_calls),
    )

    config = load_config(environ={})
    response = asyncio.run(
        submit_search(
            {
                "query": DEMO_QUERY,
                "locale": "zh-CN",
                "display_currency": "USD",
                "top_k": 3,
                "snapshot_version": "m0-v1",
            },
            build_service(config),
        )
    )
    assert response.status is RunStatus.COMPLETED
    assert response.results

    assert main(["demo"]) == 0
    first = capsys.readouterr()
    first_payload = json.loads(first.out)
    assert main(["demo"]) == 0
    second = capsys.readouterr()
    second_payload = json.loads(second.out)

    assert first.err == second.err == ""
    assert first_payload["status"] == second_payload["status"] == "COMPLETED"
    assert first_payload["results"]
    assert _business_semantics(first_payload) == _business_semantics(second_payload)
    assert "fixture://" not in first.out + second.out
    assert external_calls == {
        "asyncio": 0,
        "fixture-uri": 0,
        "http": 0,
        "https": 0,
        "socket": 0,
        "sqlite": 0,
        "subprocess": 0,
        "urlopen": 0,
    }
