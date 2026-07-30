"""Fresh-process offline and repository-inventory evidence for M1d."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

pytestmark = pytest.mark.nfr

PROJECT_ROOT = Path(__file__).parents[3]
ARCHITECTURE_IMAGE_ROOT = PROJECT_ROOT / "项目架构"
_PROVIDER_CREDENTIALS = (
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "TAVILY_API_KEY",
    "EBAY_APP_ID",
    "EBAY_CERT_ID",
)
_CAPTURE_ID = re.compile(r"capture-[0-9a-f]{32}\Z")
_TEXT_SUFFIXES = frozenset(
    {
        ".cfg",
        ".ini",
        ".json",
        ".jsonl",
        ".md",
        ".py",
        ".sh",
        ".toml",
        ".txt",
        ".yaml",
        ".yml",
    }
)
_SECRET_PATTERNS = (
    (
        "private-key",
        re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    ),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-classic-token", re.compile(r"\bghp_[A-Za-z0-9]{30,}\b")),
    (
        "github-fine-grained-token",
        re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b"),
    ),
    ("openai-style-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    (
        "bearer-token",
        re.compile(
            r"\bauthorization\s*[:=]\s*[\"']?bearer\s+[A-Za-z0-9._-]{16,}",
            re.IGNORECASE,
        ),
    ),
)
_SYNTHETIC_SECRET_FIXTURE = "tests/nfr/test_security_boundaries.py"

_FRESH_PROCESS_PROBE = r"""
import asyncio
import contextlib
import http.client
import io
import json
import os
import socket
import sys
import urllib.request

credential_names = {
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "TAVILY_API_KEY",
    "EBAY_APP_ID",
    "EBAY_CERT_ID",
}
credential_accesses = []

class GuardedEnvironment(dict):
    def _check(self, key):
        if isinstance(key, str) and key.upper() in credential_names:
            credential_accesses.append(key)
            raise AssertionError("default process read an Agent credential")

    def __getitem__(self, key):
        self._check(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        self._check(key)
        return super().get(key, default)

    def __contains__(self, key):
        self._check(key)
        return super().__contains__(key)

os.environ = GuardedEnvironment(dict(os.environ))
network_calls = []

def blocked_network(*args, **kwargs):
    del args, kwargs
    network_calls.append("network")
    raise AssertionError("default process attempted network access")

async def blocked_async_network(*args, **kwargs):
    blocked_network(*args, **kwargs)

socket.socket.connect = blocked_network
socket.create_connection = blocked_network
asyncio.open_connection = blocked_async_network
http.client.HTTPConnection.connect = blocked_network
http.client.HTTPSConnection.connect = blocked_network
urllib.request.urlopen = blocked_network

from glodex import cli

output = io.StringIO()
with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
    exit_code = cli.main(("demo",))
response = json.loads(output.getvalue().splitlines()[0])
agent_modules = (
    "glodex.agent_bootstrap",
    "glodex.adapters.agent_live_http",
    "glodex.adapters.deepseek_agent",
    "glodex.application.agent.runtime",
)
print(
    json.dumps(
        {
            "exit_code": exit_code,
            "status": response["status"],
            "credential_accesses": credential_accesses,
            "network_calls": network_calls,
            "agent_modules": {
                name: name in sys.modules
                for name in agent_modules
            },
        },
        sort_keys=True,
    )
)
"""


def _git_paths(*arguments: str) -> tuple[str, ...]:
    completed = subprocess.run(
        ["git", *arguments],
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


def _forbidden_artifact(path: str) -> bool:
    pure_path = PurePosixPath(path)
    folded_parts = tuple(part.casefold() for part in pure_path.parts)
    name = pure_path.name.casefold()
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return True
    if folded_parts and folded_parts[0] in {"output", "uploaded", "项目架构"}:
        return True
    if pure_path.suffix.casefold() == ".har":
        return True
    if any(part in {"cassette", "cassettes", "recording", "recordings"} for part in folded_parts):
        return True
    if any(_CAPTURE_ID.fullmatch(part) for part in pure_path.parts):
        return True
    return any(
        fragment in name
        for fragment in (
            "live-response",
            "live_response",
            "provider-body",
            "raw-response",
            "receipt",
        )
    )


def _secret_violations(paths: frozenset[str]) -> tuple[str, ...]:
    violations: list[str] = []
    for relative_path in sorted(paths):
        path = PROJECT_ROOT / relative_path
        if not path.is_file() or path.is_symlink() or path.suffix.casefold() not in _TEXT_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern_name, pattern in _SECRET_PATTERNS:
            for match in pattern.finditer(text):
                candidate = match.group(0).casefold()
                if relative_path == _SYNTHETIC_SECRET_FIXTURE or any(
                    marker in candidate
                    for marker in ("example", "fake", "secret", "sentinel", "synthetic")
                ):
                    continue
                violations.append(f"{relative_path}: {pattern_name}")
    return tuple(violations)


@pytest.mark.spec(
    "GLO-M1D-P0-001",
    "GLO-M1D-NFR-001",
    "GLO-M1D-NFR-005",
)
def test_fresh_default_cli_reads_no_agent_credentials_or_network() -> None:
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("GLODEX_") and name not in _PROVIDER_CREDENTIALS
    }
    environment.update({name: f"{name.casefold()}-sentinel" for name in _PROVIDER_CREDENTIALS})
    completed = subprocess.run(
        [sys.executable, "-c", _FRESH_PROCESS_PROBE],
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert json.loads(completed.stdout) == {
        "agent_modules": {
            "glodex.adapters.agent_live_http": False,
            "glodex.adapters.deepseek_agent": False,
            "glodex.agent_bootstrap": False,
            "glodex.application.agent.runtime": False,
        },
        "credential_accesses": [],
        "exit_code": 0,
        "network_calls": [],
        "status": "COMPLETED",
    }


@pytest.mark.spec(
    "GLO-M1D-P0-006",
    "GLO-M1D-NFR-005",
    "GLO-M1D-NFR-006",
)
def test_repository_inventory_excludes_secrets_live_artifacts_and_reference_pngs() -> None:
    tracked = frozenset(_git_paths("ls-files", "-z", "--cached"))
    staged = frozenset(
        _git_paths(
            "diff",
            "--cached",
            "--name-only",
            "--diff-filter=ACMR",
            "-z",
        )
    )
    prospective = tracked | frozenset(
        _git_paths("ls-files", "-z", "--others", "--exclude-standard")
    )
    images = tuple(sorted(ARCHITECTURE_IMAGE_ROOT.rglob("*.png")))
    image_paths = frozenset(path.relative_to(PROJECT_ROOT).as_posix() for path in images)

    assert len(image_paths) == 26
    assert image_paths.isdisjoint(tracked)
    assert image_paths.isdisjoint(staged)
    assert not tuple(
        path for path in sorted(tracked | staged | prospective) if _forbidden_artifact(path)
    )
    assert _secret_violations(prospective) == ()

    ignored_candidates = (
        ".env",
        "output/m1d-live/receipt.json",
        next(iter(sorted(image_paths))),
    )
    for candidate in ignored_candidates:
        ignored = subprocess.run(
            ["git", "check-ignore", "--no-index", "--quiet", "--", candidate],
            cwd=PROJECT_ROOT,
            check=False,
        )
        assert ignored.returncode == 0
