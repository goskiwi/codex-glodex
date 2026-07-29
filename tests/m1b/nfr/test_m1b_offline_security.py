from __future__ import annotations

import os
import re
import socket
import subprocess
from collections.abc import Callable, MutableMapping
from pathlib import Path, PurePosixPath

import pytest
from pytest_socket import SocketBlockedError

pytestmark = [
    pytest.mark.nfr,
    pytest.mark.spec(
        "GLO-M1B-P0-001",
        "GLO-M1B-P0-006",
        "M1B-AC-001",
        "M1B-AC-006",
        "GLO-M1B-NFR-001",
        "GLO-M1B-NFR-004",
        "GLO-M1B-NFR-006",
    ),
]

PROJECT_ROOT = Path(__file__).parents[3]
_APPROVED_FIXTURES = frozenset(
    {
        "tests/m1b/fixtures/ebay_search_empty.json",
        "tests/m1b/fixtures/ebay_search_success.json",
    }
)
_DATA_SUFFIXES = frozenset(
    {
        ".env",
        ".har",
        ".json",
        ".jsonl",
        ".toml",
        ".txt",
        ".yaml",
        ".yml",
    }
)
_CAPTURE_ID = re.compile(r"capture-[0-9a-f]{32}\Z")
_SENSITIVE_DATA_PATTERNS = (
    re.compile(r'"access_token"\s*:', re.IGNORECASE),
    re.compile(r"\bauthorization\s*[:=]\s*[\"']?bearer\s+\S+", re.IGNORECASE),
    re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    re.compile(r"glodex\.capture-receipt\.v1"),
    re.compile(r"glodex-m1b-ebay-capture"),
)


def _git_index_paths() -> tuple[str, ...]:
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
    )
    return tuple(
        sorted(
            entry.decode("utf-8", errors="strict")
            for entry in completed.stdout.split(b"\0")
            if entry
        )
    )


def _git_index_blob(path: str) -> str:
    completed = subprocess.run(
        ["git", "show", f":{path}"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
    )
    return completed.stdout.decode("utf-8", errors="replace")


def _forbidden_git_path(path: str) -> bool:
    pure_path = PurePosixPath(path)
    parts = pure_path.parts
    folded_parts = tuple(part.casefold() for part in parts)
    name = pure_path.name.casefold()

    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return True
    if parts and parts[0] == "项目架构":
        return True
    if parts and folded_parts[0] in {"output", "uploaded"}:
        return True
    if pure_path.suffix.casefold() == ".har":
        return True
    if any(part in {"cassette", "cassettes", "recording", "recordings"} for part in folded_parts):
        return True
    if any(_CAPTURE_ID.fullmatch(part) for part in parts):
        return True
    return pure_path.suffix.casefold() in _DATA_SUFFIXES and (
        "live" in name or "receipt" in name or "raw-response" in name
    )


def test_default_pytest_process_has_no_socket_or_provider_environment(
    pytestconfig: pytest.Config,
    external_environment_variable_predicate: Callable[[str], bool],
    external_environment_sanitizer: Callable[[MutableMapping[str, str]], frozenset[str]],
) -> None:
    assert pytestconfig.getoption("--disable-socket") is True
    assert not any(external_environment_variable_predicate(name) for name in os.environ)
    assert not any(name.upper().startswith("EBAY_") for name in os.environ)

    with (
        pytest.warns(UserWarning, match=r"tried to use socket\.socket"),
        pytest.raises(SocketBlockedError),
    ):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    environment = {
        "PATH": "/usr/bin",
        "HTTP_PROXY": "http://proxy.invalid",
        "EBAY_APP_ID": "must-be-removed",
        "ebay_cert_id": "must-also-be-removed",
    }
    removed = external_environment_sanitizer(environment)

    assert removed == frozenset({"HTTP_PROXY", "EBAY_APP_ID", "ebay_cert_id"})
    assert environment == {"PATH": "/usr/bin"}


def test_git_inventory_contains_no_live_or_sensitive_capture_artifact() -> None:
    indexed_paths = _git_index_paths()
    forbidden_paths = tuple(path for path in indexed_paths if _forbidden_git_path(path))

    indexed_fixture_paths = frozenset(
        path for path in indexed_paths if path.startswith("tests/m1b/fixtures/")
    )
    working_fixture_paths = frozenset(
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in (PROJECT_ROOT / "tests" / "m1b" / "fixtures").glob("*")
        if path.is_file()
    )

    sensitive_blobs: list[str] = []
    for path in indexed_paths:
        if PurePosixPath(path).suffix.casefold() not in _DATA_SUFFIXES:
            continue
        text = _git_index_blob(path)
        if any(pattern.search(text) for pattern in _SENSITIVE_DATA_PATTERNS):
            sensitive_blobs.append(path)

    assert forbidden_paths == ()
    assert indexed_fixture_paths <= _APPROVED_FIXTURES
    assert working_fixture_paths == _APPROVED_FIXTURES
    assert sensitive_blobs == []

    for candidate in (".env", "项目架构/architecture-reference.png"):
        ignored = subprocess.run(
            ["git", "check-ignore", "--no-index", "--quiet", "--", candidate],
            cwd=PROJECT_ROOT,
            check=False,
        )
        assert ignored.returncode == 0
