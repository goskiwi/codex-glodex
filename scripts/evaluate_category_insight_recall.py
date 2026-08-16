#!/usr/bin/env python3
"""Gate CategoryInsight ranking and rejection over the fixed 50-query set."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GATEWAY_PATH = PROJECT_ROOT / "opensearch/category-knowledge/category_knowledge_gateway.py"
EXPECTED_BUCKETS = {
    "ambiguous": 10,
    "exact_alias": 10,
    "negative": 10,
    "semantic": 20,
}
RERANK_REQUIRED_BUCKETS = frozenset({"ambiguous", "semantic"})


@dataclass(frozen=True, slots=True)
class Case:
    query: str
    bucket: str
    relevant_card_ids: tuple[str, ...]


def _load_gateway() -> ModuleType:
    spec = importlib.util.spec_from_file_location("category_knowledge_eval_gateway", GATEWAY_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("CategoryInsight gateway module is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_cases(path: Path) -> tuple[Case, ...]:
    cases: list[Case] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if type(value) is not dict or set(value) != {"query", "bucket", "relevant_card_ids"}:
            raise ValueError("CategoryInsight evaluation case schema is invalid")
        query = value["query"]
        bucket = value["bucket"]
        relevant = value["relevant_card_ids"]
        if (
            type(query) is not str
            or not query.strip()
            or type(bucket) is not str
            or type(relevant) is not list
            or len(relevant) != len(set(relevant))
            or any(type(card_id) is not str or not card_id for card_id in relevant)
        ):
            raise ValueError("CategoryInsight evaluation case is invalid")
        cases.append(Case(query=query, bucket=bucket, relevant_card_ids=tuple(relevant)))
    if len(cases) != 50 or Counter(case.bucket for case in cases) != EXPECTED_BUCKETS:
        raise ValueError("CategoryInsight evaluation set has an invalid fixed bucket distribution")
    if any(case.relevant_card_ids for case in cases if case.bucket == "negative") or any(
        not case.relevant_card_ids for case in cases if case.bucket != "negative"
    ):
        raise ValueError("CategoryInsight positive and negative labels are inconsistent")
    return tuple(cases)


def _positive_metrics(
    ranked: tuple[str, ...],
    relevant: tuple[str, ...],
) -> tuple[float, float, float]:
    top = ranked[:8]
    rel = set(relevant)
    recall = len(rel.intersection(top)) / len(rel)
    first = next((index for index, card_id in enumerate(top, 1) if card_id in rel), None)
    mrr = 0.0 if first is None else 1.0 / first
    dcg = sum(
        (1.0 if card_id in rel else 0.0) / math.log2(index + 1)
        for index, card_id in enumerate(top, 1)
    )
    ideal = sum(1.0 / math.log2(index + 1) for index in range(1, min(len(rel), 8) + 1))
    return recall, mrr, 0.0 if ideal == 0 else dcg / ideal


def evaluate(binding: Path, cases: tuple[Case, ...]) -> dict[str, object]:
    gateway = _load_gateway()
    runtime = gateway.Runtime.load(binding)
    recall_sum = mrr_sum = ndcg_sum = 0.0
    coarse_recall_sum = coarse_mrr_sum = coarse_ndcg_sum = 0.0
    positive_count = negative_count = false_accepts = false_rejects = correct_top1 = 0
    rerank_required_count = rerank_invocations = rerank_successes = exact_alias_bypasses = 0
    failures: list[dict[str, object]] = []
    for case in cases:
        ranking = runtime.rank_categories(case.query, "quick")
        accepted = ranking.selected_card_id is not None
        ranked = tuple(candidate.card_id for candidate in ranking.candidates)
        coarse_ranked = ranking.coarse_candidates
        exact_alias_bypasses += int(ranking.route == "EXACT_ALIAS")
        if case.bucket in RERANK_REQUIRED_BUCKETS:
            rerank_required_count += 1
            rerank_invocations += int(ranking.route in {"RERANK", "RERANK_UNAVAILABLE"})
            rerank_successes += int(ranking.route == "RERANK")
        if case.bucket == "negative":
            negative_count += 1
            false_accepts += int(accepted)
            if accepted:
                failures.append(
                    {
                        "query": case.query,
                        "reason": "FALSE_ACCEPT",
                        "selected_card_id": ranking.selected_card_id,
                    }
                )
            continue
        positive_count += 1
        recall, mrr, ndcg = _positive_metrics(ranked, case.relevant_card_ids)
        coarse_recall, coarse_mrr, coarse_ndcg = _positive_metrics(
            coarse_ranked, case.relevant_card_ids
        )
        recall_sum += recall
        mrr_sum += mrr
        ndcg_sum += ndcg
        coarse_recall_sum += coarse_recall
        coarse_mrr_sum += coarse_mrr
        coarse_ndcg_sum += coarse_ndcg
        false_rejects += int(not accepted)
        correct_top1 += int(ranking.selected_card_id in set(case.relevant_card_ids))
        if ranking.selected_card_id not in set(case.relevant_card_ids):
            failures.append(
                {
                    "query": case.query,
                    "reason": "MISSED_OR_REJECTED",
                    "selected_card_id": ranking.selected_card_id,
                    "ranked_card_ids": list(ranked),
                }
            )
    metrics = {
        "recall_at_8": recall_sum / positive_count,
        "mrr": mrr_sum / positive_count,
        "ndcg_at_8": ndcg_sum / positive_count,
        "coarse_recall_at_8": coarse_recall_sum / positive_count,
        "coarse_mrr": coarse_mrr_sum / positive_count,
        "coarse_ndcg_at_8": coarse_ndcg_sum / positive_count,
        "top1_accuracy": correct_top1 / positive_count,
        "false_reject_rate": false_rejects / positive_count,
        "negative_false_accept_rate": false_accepts / negative_count,
        "rerank_invocation_rate": rerank_invocations / rerank_required_count,
        "rerank_success_rate": rerank_successes / rerank_required_count,
        "exact_alias_bypass_rate": exact_alias_bypasses / len(cases),
    }
    passed = (
        metrics["recall_at_8"] >= 0.75
        and metrics["mrr"] >= 0.65
        and metrics["ndcg_at_8"] >= 0.70
        and metrics["top1_accuracy"] >= 0.75
        and metrics["false_reject_rate"] <= 0.25
        and metrics["negative_false_accept_rate"] <= 0.10
        and metrics["rerank_invocation_rate"] >= 0.80
        and metrics["rerank_success_rate"] >= 0.80
        and metrics["recall_at_8"] >= metrics["coarse_recall_at_8"]
        and metrics["mrr"] >= metrics["coarse_mrr"]
        and metrics["ndcg_at_8"] >= metrics["coarse_ndcg_at_8"]
    )
    return {
        "schema_version": "glodex.category-insight-recall-eval.v1",
        "passed": passed,
        "case_count": len(cases),
        "metrics": metrics,
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=PROJECT_ROOT / "data/eval/category-insight-recall.jsonl",
    )
    parser.add_argument(
        "--binding",
        type=Path,
        default=PROJECT_ROOT / "opensearch/category-knowledge/category_knowledge_binding.json",
    )
    args = parser.parse_args()
    report = evaluate(args.binding, load_cases(args.dataset))
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
