"""Run the explicit real DashScope qwen3-rerank smoke over bounded M1d projections."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if not __package__:
    sys.path.insert(0, str(PROJECT_ROOT))

from glodex.adapters.agent_indexes import load_agent_indexes  # noqa: E402
from glodex.adapters.dashscope_rerank import (  # noqa: E402
    M2aRerankError,
    RerankDocument,
    RerankRequest,
    build_dashscope_reranker,
)


async def run_smoke() -> int:
    """Call the fixed reranker once for Products and once for Cards, or fail safely."""

    try:
        indexes = await load_agent_indexes(
            snapshot_root=PROJECT_ROOT / "data" / "snapshots",
            agent_root=PROJECT_ROOT / "data" / "agent",
        )
        reranker = build_dashscope_reranker()
        products = tuple(
            RerankDocument(
                identity=product.product_id,
                text="\n".join((product.title, product.category)),
            )
            for product in indexes.batch.products[:2]
        )
        cards = tuple(
            RerankDocument(identity=card.card_id, text=card.index_text[:2_000])
            for card in indexes.cards[:2]
        )
        product_result = await reranker.rerank(RerankRequest(query="phone", documents=products))
        card_result = await reranker.rerank(RerankRequest(query="phone", documents=cards))
        if set(product_result.identities) != {item.identity for item in products}:
            raise M2aRerankError("M2A_RERANK_DEGRADED")
        if set(card_result.identities) != {item.identity for item in cards}:
            raise M2aRerankError("M2A_RERANK_DEGRADED")
    except Exception:
        print(json.dumps({"code": "M2A_RERANK_DEGRADED", "status": "FAILED"}))
        return 1
    print(
        json.dumps({"card_count": 2, "model": "qwen3-rerank", "product_count": 2, "status": "OK"})
    )
    return 0


def main() -> int:
    return asyncio.run(run_smoke())


if __name__ == "__main__":
    raise SystemExit(main())
