"""M5b default evidence is source/fake-only and keeps private data bounded."""

from __future__ import annotations

from pathlib import Path

import pytest

from glodex.api.memory_contracts import MemoryEntryView, MemoryManagementView

pytestmark = [
    pytest.mark.nfr,
    pytest.mark.spec("GLO-M5B-NFR-001", "GLO-M5B-NFR-003", "GLO-M5B-NFR-004"),
]

_ROOT = Path(__file__).parents[3]


def test_default_m5b_tests_do_not_load_credentials_or_live_services() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (_ROOT / "tests/m5b").rglob("*.py")
        if path != Path(__file__)
    ).casefold()

    for forbidden in (
        "llm_api_key",
        "os.environ",
        "127.0.0.1:8766",
        "127.0.0.1:8767",
        "postgresql://",
        "redis://",
    ):
        assert forbidden not in source


def test_public_memory_contract_excludes_private_reflect_material() -> None:
    public_fields = set(MemoryEntryView.model_fields) | set(MemoryManagementView.model_fields)

    for forbidden in (
        "source_ordinal",
        "source_span",
        "confidence",
        "canonical_key",
        "embedding",
        "score",
        "provider_body",
    ):
        assert forbidden not in public_fields
