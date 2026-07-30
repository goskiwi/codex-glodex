"""Offline, artifact-inventory, and privacy evidence for M1e."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
from collections.abc import Callable, MutableMapping
from pathlib import Path, PurePosixPath

import pytest
from pytest_socket import SocketBlockedError

from scripts.build_esci_benchmark import BENCHMARK_ID, build_benchmark
from tests.m1e.conftest import SyntheticEsciSource

pytestmark = pytest.mark.nfr

PROJECT_ROOT = Path(__file__).parents[3]
BENCHMARK_ROOT = PROJECT_ROOT / "data" / "benchmarks" / "esci-small-us-v1"
_MAX_ARTIFACT_BYTES = 20 * 1024 * 1024
_APPROVED_ARTIFACT_FILES = frozenset(
    {
        "ATTRIBUTION.md",
        "LICENSE",
        "NOTICE",
        "judgements.jsonl",
        "manifest.json",
        "products.jsonl",
        "queries.jsonl",
    }
)
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
    ("private-key", re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")),
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
import contextlib
import io
import json
import os
import socket
import sys

credential_names = {
    "AMAZON_ACCESS_KEY",
    "AMAZON_SECRET_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "TAVILY_API_KEY",
    "EBAY_APP_ID",
    "EBAY_CERT_ID",
    "GLODEX_CONFIG",
}
credential_accesses = []

class GuardedEnvironment(dict):
    def _check(self, key):
        if isinstance(key, str) and key.upper() in credential_names:
            credential_accesses.append(key)
            raise AssertionError("benchmark CLI read a credential or config environment variable")

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
    raise AssertionError("benchmark CLI attempted network access")

socket.socket.connect = blocked_network
socket.create_connection = blocked_network

from glodex import cli

artifact_root = sys.argv[1]
output = io.StringIO()
with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
    exit_code = cli.main(("benchmark-esci", "--artifact-root", artifact_root))
lines = output.getvalue().splitlines()
response = json.loads(lines[0]) if len(lines) == 1 else None
print(
    json.dumps(
        {
            "credential_accesses": credential_accesses,
            "exit_code": exit_code,
            "line_count": len(lines),
            "network_calls": network_calls,
            "output_has_artifact_root": artifact_root in output.getvalue(),
            "response_is_object": type(response) is dict,
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


def _forbidden_m1e_path(path: str) -> bool:
    pure_path = PurePosixPath(path)
    folded_parts = tuple(part.casefold() for part in pure_path.parts)
    name = pure_path.name.casefold()

    if pure_path.suffix.casefold() in {".parquet", ".csv"}:
        return True
    if any(part in {"esci-data", "shopping_queries_dataset"} for part in folded_parts):
        return True
    if any(part in {"cache", "caches", "raw", "source", "sources"} for part in folded_parts):
        return True
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return True
    return any(
        fragment in name
        for fragment in (
            "cookie",
            "download-log",
            "download_log",
            "raw-response",
            "raw_response",
            "source-path",
            "source_path",
            "token",
        )
    )


def _secret_violations(paths: frozenset[str]) -> tuple[str, ...]:
    violations: list[str] = []
    for relative_path in sorted(paths):
        path = PROJECT_ROOT / relative_path
        if (
            not path.is_file()
            or path.is_symlink()
            or path.suffix.casefold() not in _TEXT_SUFFIXES
            or relative_path.startswith("data/benchmarks/esci-small-us-v1/")
        ):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern_name, pattern in _SECRET_PATTERNS:
            for match in pattern.finditer(text):
                candidate = match.group(0).casefold()
                if relative_path == _SYNTHETIC_SECRET_FIXTURE or any(
                    marker in candidate for marker in ("example", "fake", "secret", "sentinel")
                ):
                    continue
                violations.append(f"{relative_path}: {pattern_name}")
    return tuple(violations)


def _run_fresh_benchmark(artifact_root: Path) -> dict[str, object]:
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("GLODEX_")
    }
    environment.update(
        {
            "AMAZON_ACCESS_KEY": "amazon-access-sentinel",
            "AMAZON_SECRET_KEY": "amazon-secret-sentinel",
            "AWS_ACCESS_KEY_ID": "aws-access-sentinel",
            "AWS_SECRET_ACCESS_KEY": "aws-secret-sentinel",
            "DEEPSEEK_API_KEY": "deepseek-sentinel",
            "DASHSCOPE_API_KEY": "dashscope-sentinel",
            "TAVILY_API_KEY": "tavily-sentinel",
            "EBAY_APP_ID": "ebay-app-sentinel",
            "EBAY_CERT_ID": "ebay-cert-sentinel",
            "GLODEX_CONFIG": "/private/untrusted/glodex.toml",
        }
    )
    completed = subprocess.run(
        [sys.executable, "-c", _FRESH_PROCESS_PROBE, str(artifact_root)],
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    payload = json.loads(completed.stdout)
    assert type(payload) is dict
    return payload


@pytest.mark.spec("GLO-M1E-NFR-001")
def test_default_pytest_process_has_no_socket_or_provider_environment(
    pytestconfig: pytest.Config,
    external_environment_variable_predicate: Callable[[str], bool],
    external_environment_sanitizer: Callable[[MutableMapping[str, str]], frozenset[str]],
) -> None:
    assert pytestconfig.getoption("--disable-socket") is True
    assert not any(external_environment_variable_predicate(name) for name in os.environ)

    with (
        pytest.warns(UserWarning, match=r"tried to use socket\.socket"),
        pytest.raises(SocketBlockedError),
    ):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    environment = {
        "PATH": "/usr/bin",
        "HTTP_PROXY": "http://proxy.invalid",
        "AWS_ACCESS_KEY_ID": "must-be-removed",
        "DEEPSEEK_API_KEY": "must-also-be-removed",
    }
    removed = external_environment_sanitizer(environment)

    assert removed == frozenset(
        {
            "HTTP_PROXY",
            "AWS_ACCESS_KEY_ID",
            "DEEPSEEK_API_KEY",
        }
    )
    assert environment == {"PATH": "/usr/bin"}


@pytest.mark.spec("GLO-M1E-P0-004", "GLO-M1E-NFR-001", "GLO-M1E-NFR-003")
def test_benchmark_cli_reads_no_credentials_config_or_network(
    tmp_path: Path,
    valid_esci_source: Callable[..., SyntheticEsciSource],
) -> None:
    source = valid_esci_source()
    artifact_root = tmp_path / BENCHMARK_ID
    build_benchmark(source.root, source.revision, artifact_root)

    assert _run_fresh_benchmark(artifact_root) == {
        "credential_accesses": [],
        "exit_code": 0,
        "line_count": 1,
        "network_calls": [],
        "output_has_artifact_root": False,
        "response_is_object": True,
    }
    assert _run_fresh_benchmark(Path("/private/glodex-esci-operator-source")) == {
        "credential_accesses": [],
        "exit_code": 1,
        "line_count": 1,
        "network_calls": [],
        "output_has_artifact_root": False,
        "response_is_object": True,
    }


@pytest.mark.spec("GLO-M1E-P0-001", "GLO-M1E-P0-002", "GLO-M1E-NFR-002", "GLO-M1E-NFR-003")
def test_repository_inventory_excludes_raw_esci_sources_credentials_and_unapproved_artifacts() -> (
    None
):
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

    forbidden_paths = tuple(
        path for path in sorted(tracked | staged | prospective) if _forbidden_m1e_path(path)
    )
    artifact_paths = frozenset(
        path for path in prospective if path.startswith("data/benchmarks/esci-small-us-v1/")
    )
    expected_artifact_paths = frozenset(
        f"data/benchmarks/esci-small-us-v1/{name}" for name in _APPROVED_ARTIFACT_FILES
    )

    assert forbidden_paths == ()
    assert artifact_paths <= expected_artifact_paths
    assert _secret_violations(prospective) == ()


@pytest.mark.spec("GLO-M1E-P0-001", "GLO-M1E-P0-002", "GLO-M1E-NFR-002", "GLO-M1E-NFR-003")
def test_checked_in_benchmark_artifact_is_small_flat_and_uses_only_approved_files() -> None:
    if not BENCHMARK_ROOT.exists():
        return

    artifact_files = tuple(
        sorted(path for path in BENCHMARK_ROOT.rglob("*") if path.is_file() or path.is_symlink())
    )
    relative_files = tuple(path.relative_to(BENCHMARK_ROOT).as_posix() for path in artifact_files)

    assert not any(path.is_symlink() for path in artifact_files)
    assert set(relative_files) == _APPROVED_ARTIFACT_FILES
    assert sum(path.stat().st_size for path in artifact_files) <= _MAX_ARTIFACT_BYTES
