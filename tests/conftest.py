"""Shared pytest safety fixtures.

The environment is sanitized while this conftest is imported so application
modules cannot observe ambient credentials during test collection.
"""

from __future__ import annotations

import os
from collections.abc import Callable, MutableMapping

import pytest

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


def _is_external_service_environment_variable(name: str) -> bool:
    normalized_name = name.upper()
    return normalized_name in _EXTERNAL_ENVIRONMENT_NAMES or normalized_name.startswith(
        _EXTERNAL_ENVIRONMENT_PREFIXES
    )


def _clear_external_service_environment(
    environment: MutableMapping[str, str],
) -> frozenset[str]:
    removed_names = {
        name for name in tuple(environment) if _is_external_service_environment_variable(name)
    }
    for name in removed_names:
        environment.pop(name, None)
    return frozenset(removed_names)


_clear_external_service_environment(os.environ)


@pytest.fixture(scope="session")
def external_environment_variable_predicate() -> Callable[[str], bool]:
    """Return the predicate used by the collection-time environment guard."""

    return _is_external_service_environment_variable


@pytest.fixture(scope="session")
def external_environment_sanitizer() -> Callable[[MutableMapping[str, str]], frozenset[str]]:
    """Return the sanitizer so its fail-closed behavior can be tested directly."""

    return _clear_external_service_environment
