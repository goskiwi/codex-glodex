"""Unit tests for the runtime evidence completion layer (product_evidence-v1)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from glodex.facts.evidence import (
    EvidenceCandidate,
    EvidenceCompleter,
    EvidenceError,
    EvidenceResult,
    MemoryEvidenceStore,
    build_evidence_row,
    build_search_query,
    build_tavily_search,
    proof_search_terms,
)


def _result(url: str = "https://example.com/x", title: str = "Review") -> EvidenceResult:
    return EvidenceResult(
        url=url,
        title=title,
        snippet="ROG G14 real battery about 8h review",
        published_date="2026-07-01",
    )


async def _fake_search(query: str, limit: int) -> tuple[EvidenceResult, ...]:
    if "no-hits" in query:
        return ()
    return (_result(),)


class TestEvidenceBasics:
    def test_need_mapping(self) -> None:
        assert proof_search_terms("battery_life_realworld") == "续航测试 电池实测"
        assert proof_search_terms("gaming_fps") == "游戏 帧率 性能测试"
        assert proof_search_terms("no-such-proof-key") is None

    def test_query_builder(self) -> None:
        query = build_search_query(brand="ASUS", model="ROG G14", keyword="续航测试")
        assert query == "ASUS ROG G14 续航测试"
        assert len(query) <= 200

    def test_evidence_row(self) -> None:
        row = build_evidence_row(
            canonical_product_id="global:asus|rog-g14",
            brand="ASUS",
            model="ROG G14",
            fact_key="battery_life_realworld",
            query="ASUS ROG G14 续航测试",
            results=(_result(),),
            captured_at=datetime(2026, 8, 1, tzinfo=UTC),
        )
        assert row["status"] == "EVIDENCED"
        assert row["schema_version"] == "glodex.product-evidence.v1"
        assert row["claims"][0]["source_url"] == "https://example.com/x"

    def test_store_roundtrip(self, tmp_path) -> None:
        async def run() -> None:
            store = MemoryEvidenceStore()
            row = build_evidence_row(
                canonical_product_id="global:asus|rog-g14",
                brand="ASUS",
                model="ROG G14",
                fact_key="gaming_fps",
                query="ASUS ROG G14 游戏",
                results=(_result(),),
                captured_at=datetime(2026, 8, 1, tzinfo=UTC),
            )
            await store.put(row)
            assert len(await store.get("global:asus|rog-g14", "gaming_fps")) == 1

        asyncio.run(run())


class TestEvidenceCompleter:
    def test_completes_and_caches(self, tmp_path) -> None:
        store = MemoryEvidenceStore()
        completer = EvidenceCompleter(store=store, search=_fake_search)
        candidate = EvidenceCandidate(
            canonical_product_id="global:asus|rog-g14",
            brand="ASUS",
            model="ROG G14",
            category="laptop",
            facts={"gpu": "RTX 4060"},
        )

        async def run() -> None:
            first = await completer.complete([candidate], ["gaming_fps"])
            assert "global:asus|rog-g14" in first
            assert "gaming_fps" in first["global:asus|rog-g14"]
            # Second call reuses the cache: no new search hits.
            second = await completer.complete([candidate], ["gaming_fps"])
            assert second["global:asus|rog-g14"]["gaming_fps"] == first[
                "global:asus|rog-g14"
            ]["gaming_fps"]
            assert store.size == 1

        asyncio.run(run())

    def test_skips_identity_unclear(self) -> None:
        store = MemoryEvidenceStore()
        completer = EvidenceCompleter(store=store, search=_fake_search)
        candidate = EvidenceCandidate(
            canonical_product_id="family:es:x",
            brand="Alpha",
            model="",
            category="general",
            facts={},
        )

        async def run() -> None:
            result = await completer.complete([candidate], ["gaming_fps"])
            assert result == {}

        asyncio.run(run())

    def test_skips_no_hits(self) -> None:
        store = MemoryEvidenceStore()
        completer = EvidenceCompleter(store=store, search=_fake_search)
        candidate = EvidenceCandidate(
            canonical_product_id="global:a|b",
            brand="A",
            model="no-hits-model",
            category="laptop",
            facts={},
        )

        async def run() -> None:
            result = await completer.complete([candidate], ["gaming_fps"])
            assert result == {}

        asyncio.run(run())

    def test_rejects_bad_inputs(self) -> None:
        with pytest.raises(EvidenceError):
            EvidenceCompleter(store=object(), search=_fake_search)  # type: ignore[arg-type]

        completer = EvidenceCompleter(store=MemoryEvidenceStore(), search=_fake_search)

        async def run() -> None:
            with pytest.raises(EvidenceError):
                await completer.complete([], ["gaming_fps"], max_per_product=0)

        asyncio.run(run())

    def test_tavily_builder_requires_credentials(self) -> None:
        with pytest.raises(EvidenceError):
            build_tavily_search(api_key="")
