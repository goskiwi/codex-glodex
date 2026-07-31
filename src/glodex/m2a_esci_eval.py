"""Explicit live M2a rerank comparison over the label-isolated ESCI candidate pool."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from glodex.adapters.dashscope_rerank import DashScopeReranker, RerankDocument, RerankRequest
from glodex.esci_benchmark import (
    CandidatePool,
    EvaluationResult,
    RankedCandidate,
    evaluate_rankings,
    evaluation_summary,
    load_candidate_pool,
    rank_candidate_pool,
)

_MAX_DOCUMENT_CHARACTERS: Final = 2_000


@dataclass(frozen=True, slots=True)
class M2aEsciComparison:
    """Aggregate-only coarse vs qwen3-rerank result; no labels/text leave this module."""

    coarse: EvaluationResult
    reranked: EvaluationResult


async def evaluate_m2a_rerank(
    *,
    artifact_root: Path,
    reranker: DashScopeReranker,
) -> M2aEsciComparison:
    """Rerank each fixed candidate-only query before labels are loaded for either metric."""

    if not isinstance(artifact_root, Path) or type(reranker) is not DashScopeReranker:
        raise TypeError("M2a ESCI evaluation requires an exact root and reranker")
    pool = load_candidate_pool(artifact_root)
    coarse_rankings = rank_candidate_pool(pool)
    reranked_rankings = await _rerank_candidate_pool(pool, reranker=reranker)
    # ``evaluate_rankings`` first loads labels only after all provider calls finish.
    return M2aEsciComparison(
        coarse=evaluate_rankings(pool, coarse_rankings),
        reranked=evaluate_rankings(pool, reranked_rankings),
    )


def comparison_summary(result: M2aEsciComparison) -> dict[str, object]:
    if type(result) is not M2aEsciComparison:
        raise TypeError("M2a ESCI comparison must be exact")
    coarse = evaluation_summary(result.coarse)
    reranked = evaluation_summary(result.reranked)
    return {
        "benchmark_id": coarse["benchmark_id"],
        "coarse_metrics": coarse["metrics"],
        "counts": coarse["counts"],
        "model": "qwen3-rerank",
        "reranked_metrics": reranked["metrics"],
        "status": "COMPLETED",
    }


async def _rerank_candidate_pool(
    pool: CandidatePool,
    *,
    reranker: DashScopeReranker,
) -> dict[str, tuple[RankedCandidate, ...]]:
    rankings: dict[str, tuple[RankedCandidate, ...]] = {}
    for query in pool.queries:
        candidate_ids = pool.candidates_by_query[query.query_id]
        documents = tuple(
            RerankDocument(identity=product_id, text=_product_text(pool, product_id))
            for product_id in candidate_ids
        )
        result = await reranker.rerank(RerankRequest(query=query.query[:512], documents=documents))
        if set(result.identities) != set(candidate_ids):
            raise ValueError("reranker changed ESCI candidate membership")
        rankings[query.query_id] = tuple(
            RankedCandidate(product_id=product_id, score=float(len(result.identities) - rank))
            for rank, product_id in enumerate(result.identities)
        )
    return rankings


def _product_text(pool: CandidatePool, product_id: str) -> str:
    product = pool.products[product_id]
    return "\n".join(
        item
        for item in (product.product_title, product.product_brand, product.product_bullet_point)
        if item
    )[:_MAX_DOCUMENT_CHARACTERS]


__all__ = ["M2aEsciComparison", "comparison_summary", "evaluate_m2a_rerank"]
