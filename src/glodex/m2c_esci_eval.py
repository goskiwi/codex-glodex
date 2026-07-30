"""Explicit live M2c BGE rerank comparison over the label-isolated ESCI pool."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from glodex.adapters.dashscope_rerank import RerankDocument, RerankRequest
from glodex.adapters.m2c_model_service import M2cReranker
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
_MAX_DOCUMENT_BYTES: Final = 1_200
_MAX_QUERY_CHARACTERS: Final = 512


@dataclass(frozen=True, slots=True)
class M2cEsciComparison:
    """Aggregate-only coarse vs BGE cross-encoder result."""

    coarse: EvaluationResult
    reranked: EvaluationResult
    manifest_prefix: str


async def evaluate_m2c_rerank(
    *,
    artifact_root: Path,
    reranker: M2cReranker,
) -> M2cEsciComparison:
    """Rerank the fixed candidate pool before this module reads any ESCI label."""

    if not isinstance(artifact_root, Path) or type(reranker) is not M2cReranker:
        raise TypeError("M2c ESCI evaluation requires an exact root and reranker")
    pool = load_candidate_pool(artifact_root)
    coarse_rankings = rank_candidate_pool(pool)
    reranked_rankings = await _rerank_candidate_pool(pool, reranker=reranker)
    # ``evaluate_rankings`` loads labels only after every GPU request has returned.
    return M2cEsciComparison(
        coarse=evaluate_rankings(pool, coarse_rankings),
        reranked=evaluate_rankings(pool, reranked_rankings),
        manifest_prefix=reranker.identity.manifest_digest[:16],
    )


def comparison_summary(result: M2cEsciComparison) -> dict[str, object]:
    """Render only aggregate evidence and a non-sensitive model identity prefix."""

    if type(result) is not M2cEsciComparison:
        raise TypeError("M2c ESCI comparison must be exact")
    coarse = evaluation_summary(result.coarse)
    reranked = evaluation_summary(result.reranked)
    return {
        "benchmark_id": coarse["benchmark_id"],
        "coarse_metrics": coarse["metrics"],
        "counts": coarse["counts"],
        "model": "glodex-bge-reranker-v1",
        "model_manifest": result.manifest_prefix,
        "reranked_metrics": reranked["metrics"],
        "status": "COMPLETED",
    }


async def _rerank_candidate_pool(
    pool: CandidatePool,
    *,
    reranker: M2cReranker,
) -> dict[str, tuple[RankedCandidate, ...]]:
    rankings: dict[str, tuple[RankedCandidate, ...]] = {}
    for query in pool.queries:
        candidate_ids = pool.candidates_by_query[query.query_id]
        documents = tuple(
            RerankDocument(identity=product_id, text=_bounded_text(_product_text(pool, product_id)))
            for product_id in candidate_ids
        )
        result = await reranker.rerank(
            RerankRequest(query=query.query[:_MAX_QUERY_CHARACTERS], documents=documents)
        )
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


def _bounded_text(value: str) -> str:
    """Keep a full 20-document ESCI request below the fixed M2c byte limit."""

    return value.encode("utf-8")[:_MAX_DOCUMENT_BYTES].decode("utf-8", "ignore")


__all__ = ["M2cEsciComparison", "comparison_summary", "evaluate_m2c_rerank"]
