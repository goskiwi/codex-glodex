"""Cross-process proof that the M0 business projection is deterministic."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from tests.golden_support import GOLDEN_SCENARIOS, GoldenProjection

pytestmark = [
    pytest.mark.nfr,
    pytest.mark.spec(
        "GLO-P0-011",
        "GLO-P0-012",
        "AC-008",
        "AC-012",
        "GLO-NFR-004",
        "GLO-NFR-007",
    ),
]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROCESS_RUNS = 20
PROCESS_TIMEOUT_SECONDS = 30

_EXTERNAL_ENVIRONMENT_NAMES = frozenset(
    {
        "ALL_PROXY",
        "ANTHROPIC_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "COHERE_API_KEY",
        "DATABASE_URL",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "HF_TOKEN",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "HUGGINGFACEHUB_API_TOKEN",
        "MISTRAL_API_KEY",
        "NO_PROXY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "PGDATABASE",
        "PGHOST",
        "PGPASSWORD",
        "PGPORT",
        "PGSERVICE",
        "PGSSLMODE",
        "PGUSER",
        "REDIS_URL",
        "SQLALCHEMY_DATABASE_URI",
    }
)
_EXTERNAL_ENVIRONMENT_PREFIXES = (
    "ANTHROPIC_",
    "AWS_",
    "AZURE_",
    "COHERE_",
    "DATABASE_",
    "DB_",
    "GCP_",
    "GEMINI_",
    "GOOGLE_CLOUD_",
    "GROQ_",
    "HUGGINGFACE_",
    "MARIADB_",
    "MISTRAL_",
    "MODEL_",
    "MONGO_",
    "MONGODB_",
    "MYSQL_",
    "OLLAMA_",
    "OPENAI_",
    "POSTGRES_",
    "REDIS_",
)

_CHILD_PROGRAM = """
import asyncio
import http.client
import json
import socket
import sqlite3
import urllib.request


def blocked_external_call(*args, **kwargs):
    del args, kwargs
    raise AssertionError("determinism child attempted an external service call")


socket.socket.connect = blocked_external_call
socket.create_connection = blocked_external_call
http.client.HTTPConnection.connect = blocked_external_call
http.client.HTTPSConnection.connect = blocked_external_call
urllib.request.urlopen = blocked_external_call
sqlite3.connect = blocked_external_call

from tests.golden_support import build_all_golden_projections

projections = asyncio.run(build_all_golden_projections())
print(json.dumps(projections, ensure_ascii=False, sort_keys=True))
"""


def test_golden_business_projections_are_identical_across_twenty_processes() -> None:
    expected_scenarios = {scenario.name for scenario in GOLDEN_SCENARIOS}
    observed: list[tuple[int, dict[str, GoldenProjection]]] = []

    for hash_seed in range(PROCESS_RUNS):
        projection = _run_child(hash_seed)
        assert set(projection) == expected_scenarios, (
            f"PYTHONHASHSEED={hash_seed} returned unexpected scenarios: {sorted(projection)}"
        )
        observed.append((hash_seed, projection))

    baseline_seed, baseline = observed[0]
    for hash_seed, projection in observed[1:]:
        assert projection == baseline, (
            "Golden business projection changed across independent processes: "
            f"baseline PYTHONHASHSEED={baseline_seed}, "
            f"mismatching PYTHONHASHSEED={hash_seed}"
        )

    assert len(observed) == PROCESS_RUNS
    assert all(
        sum(scenario.name in projection for _, projection in observed) >= 2
        for scenario in GOLDEN_SCENARIOS
    )
    assert baseline["completed"]["status"] == "COMPLETED"
    assert baseline["no-match"]["status"] == "NO_MATCH"
    assert baseline["ranker-degraded"]["status"] == "COMPLETED"


def _run_child(hash_seed: int) -> dict[str, GoldenProjection]:
    environment = _offline_environment(hash_seed)
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _CHILD_PROGRAM],
            cwd=PROJECT_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=PROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(
            f"PYTHONHASHSEED={hash_seed} exceeded {PROCESS_TIMEOUT_SECONDS}s; "
            f"stdout={error.stdout!r}; stderr={error.stderr!r}",
            pytrace=False,
        )

    if completed.returncode != 0:
        pytest.fail(
            f"PYTHONHASHSEED={hash_seed} exited {completed.returncode}; "
            f"stdout={completed.stdout!r}; stderr={completed.stderr!r}",
            pytrace=False,
        )

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        pytest.fail(
            f"PYTHONHASHSEED={hash_seed} produced invalid JSON ({error}); "
            f"stdout={completed.stdout!r}; stderr={completed.stderr!r}",
            pytrace=False,
        )
    if not isinstance(payload, dict):
        pytest.fail(
            f"PYTHONHASHSEED={hash_seed} produced a non-object projection: {payload!r}",
            pytrace=False,
        )
    return cast(dict[str, GoldenProjection], payload)


def _offline_environment(hash_seed: int) -> dict[str, str]:
    environment = {
        name: value for name, value in os.environ.items() if not _is_external_environment_name(name)
    }
    environment["PYTHONHASHSEED"] = str(hash_seed)
    return environment


def _is_external_environment_name(name: str) -> bool:
    normalized = name.upper()
    return normalized in _EXTERNAL_ENVIRONMENT_NAMES or normalized.startswith(
        _EXTERNAL_ENVIRONMENT_PREFIXES
    )
