"""Executable proof that the M0 test process is offline and credential-free."""

from __future__ import annotations

import os
import socket
from collections.abc import Callable, MutableMapping

import pytest
from pytest_socket import SocketBlockedError

pytestmark = [
    pytest.mark.architecture,
    pytest.mark.spec("GLO-P0-012", "AC-012", "GLO-NFR-006", "GLO-NFR-009"),
]


def test_pytest_configuration_disables_real_sockets(pytestconfig: pytest.Config) -> None:
    addopts = pytestconfig.getini("addopts")

    assert "--disable-socket" in addopts
    with (
        pytest.warns(UserWarning, match=r"tried to use socket\.socket"),
        pytest.raises(SocketBlockedError),
    ):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)


def test_external_service_environment_is_empty(
    external_environment_variable_predicate: Callable[[str], bool],
) -> None:
    leaked_names = sorted(
        name for name in os.environ if external_environment_variable_predicate(name)
    )

    assert leaked_names == []


def test_environment_sanitizer_removes_proxies_credentials_and_service_endpoints(
    external_environment_sanitizer: Callable[[MutableMapping[str, str]], frozenset[str]],
) -> None:
    environment = {
        "PATH": "/usr/bin",
        "http_proxy": "http://proxy.invalid",
        "OPENAI_API_KEY": "secret",
        "AWS_ACCESS_KEY_ID": "secret",
        "DATABASE_URL": "postgresql://database.invalid/glodex",
        "REDIS_URL": "redis://cache.invalid",
        "MODEL_ENDPOINT": "https://model.invalid",
    }

    removed_names = external_environment_sanitizer(environment)

    assert removed_names == frozenset(
        {
            "http_proxy",
            "OPENAI_API_KEY",
            "AWS_ACCESS_KEY_ID",
            "DATABASE_URL",
            "REDIS_URL",
            "MODEL_ENDPOINT",
        }
    )
    assert environment == {"PATH": "/usr/bin"}
