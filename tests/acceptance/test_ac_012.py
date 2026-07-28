from __future__ import annotations

import asyncio
import json
import os
import socket
from collections.abc import Callable, Mapping
from typing import Any

import pytest

from glodex import cli

pytestmark = pytest.mark.acceptance

_VOLATILE_FIELDS = frozenset(
    {
        "duration_ms",
        "occurred_at",
        "run_id",
    }
)


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


def _run_demo(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    assert cli.main(["demo"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert isinstance(payload, dict)
    return payload


@pytest.mark.spec("AC-012")
def test_ac_012_default_cli_is_offline_and_repeatable(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    external_environment_variable_predicate: Callable[[str], bool],
) -> None:
    for name in tuple(os.environ):
        if name.startswith("GLODEX_") or external_environment_variable_predicate(name):
            monkeypatch.delenv(name, raising=False)

    external_calls: list[tuple[str, object]] = []

    def reject_socket_connect(
        connection: socket.socket,
        address: object,
    ) -> None:
        del connection
        external_calls.append(("socket.connect", address))
        raise AssertionError("the default CLI must not open an external connection")

    def reject_create_connection(
        address: object,
        *args: object,
        **kwargs: object,
    ) -> socket.socket:
        del args
        del kwargs
        external_calls.append(("socket.create_connection", address))
        raise AssertionError("the default CLI must not open an external connection")

    async def reject_async_connection(
        host: object = None,
        port: object = None,
        *args: object,
        **kwargs: object,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        del args
        del kwargs
        external_calls.append(("asyncio.open_connection", (host, port)))
        raise AssertionError("the default CLI must not open an external connection")

    monkeypatch.setattr(socket.socket, "connect", reject_socket_connect)
    monkeypatch.setattr(socket, "create_connection", reject_create_connection)
    monkeypatch.setattr(asyncio, "open_connection", reject_async_connection)

    first = _run_demo(capsys)
    second = _run_demo(capsys)

    assert first["status"] == second["status"] == "COMPLETED"
    assert [item["product_id"] for item in first["results"]] == [
        item["product_id"] for item in second["results"]
    ]
    assert _business_semantics(first) == _business_semantics(second)
    assert external_calls == []
