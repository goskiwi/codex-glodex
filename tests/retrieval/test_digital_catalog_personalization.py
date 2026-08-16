from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

from glodex.agent.contracts import ItemSearchInput, Platform
from glodex.application.rerank_contracts import RerankRequest, RerankResult
from glodex.retrieval.digital_catalog import (
    DigitalCatalogItemSource,
    DigitalFirstItemSource,
    _DigitalProduct,
)


@dataclass(frozen=True)
class _Result:
    candidates: tuple[object, ...]


@dataclass
class _Source:
    result: _Result
    calls: list[dict[str, object]] = field(default_factory=list)

    async def search(self, request: ItemSearchInput, **kwargs: object) -> Any:
        self.calls.append({"request": request, **kwargs})
        return self.result


def _request() -> ItemSearchInput:
    return ItemSearchInput(
        query="旅行背包",
        platform=Platform.SHOPEE,
        category="bags.backpack",
        min_landed_cost_cny=None,
        max_landed_cost_cny=Decimal("500"),
        top_k=5,
    )


def _source(*, recent: _Source, historical: _Source) -> DigitalFirstItemSource:
    return DigitalFirstItemSource(recent=cast(Any, recent), historical=cast(Any, historical))


def test_preference_vector_keeps_reviewed_recent_catalog_first() -> None:
    personalized = _Result(candidates=(object(),))
    historical = _Source(personalized)
    recent = _Source(_Result(candidates=(object(),)))

    result = asyncio.run(
        _source(recent=recent, historical=historical).search(
            _request(),
            query_vector=(1.0, 0.0),
            preference_vector=(0.0, 1.0),
        )
    )

    assert result is recent.result
    assert historical.calls == []
    assert len(recent.calls) == 1
    assert recent.calls[0]["preference_vector"] == (0.0, 1.0)


def test_recent_miss_falls_back_to_personalized_historical_catalog() -> None:
    recent_result = _Result(candidates=(object(),))
    historical = _Source(recent_result)
    recent = _Source(_Result(candidates=()))

    result = asyncio.run(
        _source(recent=recent, historical=historical).search(
            _request(),
            query_vector=(1.0, 0.0),
            preference_vector=(0.0, 1.0),
        )
    )

    assert result is historical.result
    assert len(historical.calls) == 1
    assert len(recent.calls) == 1


def test_no_preference_keeps_recent_first_behavior() -> None:
    recent_result = _Result(candidates=(object(),))
    historical = _Source(_Result(candidates=(object(),)))
    recent = _Source(recent_result)

    result = asyncio.run(
        _source(recent=recent, historical=historical).search(
            _request(),
            query_vector=(1.0, 0.0),
            preference_vector=None,
        )
    )

    assert result is recent_result
    assert len(recent.calls) == 1
    assert historical.calls == []


def test_recent_catalog_ranks_matching_evidence_before_budget_proximity() -> None:
    request = ItemSearchInput(
        query="支持 USB 和 XLR 的播客麦克风",
        platform=Platform.AMAZON,
        category="microphone",
        min_landed_cost_cny=None,
        max_landed_cost_cny=Decimal("1500"),
        top_k=3,
    )
    common = {
        "category": "microphone",
        "source_uri": "https://example.com/spec",
        "captured_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    matching = _DigitalProduct(
        product_id="matching",
        title="Podcast microphone",
        price_cny=Decimal("600"),
        attributes={"connection": "USB-C 与 XLR", "usage": "播客录音"},
        **common,
    )
    expensive_nonmatch = _DigitalProduct(
        product_id="nonmatch",
        title="USB microphone",
        price_cny=Decimal("1499"),
        attributes={"connection": "USB-C", "usage": "游戏语音"},
        **common,
    )

    assert DigitalCatalogItemSource._rank_key(
        matching, request
    ) < DigitalCatalogItemSource._rank_key(expensive_nonmatch, request)


def test_recent_catalog_cross_encoder_reranks_the_fixed_coarse_pool() -> None:
    requests: list[RerankRequest] = []

    class _Reranker:
        async def rerank(self, request: RerankRequest) -> RerankResult:
            requests.append(request)
            return RerankResult(
                identities=tuple(document.identity for document in reversed(request.documents))
            )

    source = object.__new__(DigitalCatalogItemSource)
    source._reranker = _Reranker()  # type: ignore[assignment]
    common = {
        "category": "laptop",
        "price_cny": Decimal("7000"),
        "source_uri": "https://example.com/spec",
        "captured_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    light = _DigitalProduct(
        product_id="light",
        title="Light laptop",
        attributes={"weight": "1.2 kg", "gpu": "integrated"},
        **common,
    )
    creator = _DigitalProduct(
        product_id="creator",
        title="Creator laptop",
        attributes={"weight": "1.5 kg", "gpu": "RTX 4060"},
        **common,
    )

    result = asyncio.run(source._rerank("旅行 剪视频 轻便 笔记本", [light, creator]))

    assert [product.product_id for product in result] == ["creator", "light"]
    assert len(requests) == 1
    assert requests[0].query == "旅行 剪视频 轻便 笔记本"
    assert "weight: 1.5 kg" in requests[0].documents[1].text
    assert "gpu: RTX 4060" in requests[0].documents[1].text
