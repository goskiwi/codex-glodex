"""CategoryInsight fixed-set gate contracts."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = PROJECT_ROOT / "scripts/evaluate_category_insight_recall.py"
DATASET = PROJECT_ROOT / "data/eval/category-insight-recall.jsonl"
SPEC = importlib.util.spec_from_file_location("category_insight_recall_eval", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
evaluation = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = evaluation
SPEC.loader.exec_module(evaluation)


def test_fixed_dataset_has_the_required_route_and_rejection_buckets() -> None:
    cases = evaluation.load_cases(DATASET)

    assert len(cases) == 50
    assert sum(not case.relevant_card_ids for case in cases) == 10
    assert {case.bucket for case in cases} == {
        "ambiguous",
        "exact_alias",
        "negative",
        "semantic",
    }


def test_positive_metrics_reward_recall_and_rank() -> None:
    recall, mrr, ndcg = evaluation._positive_metrics(
        ("wrong", "target", "other"),
        ("target",),
    )

    assert recall == 1.0
    assert mrr == 0.5
    assert 0 < ndcg < 1


def test_dataset_rejects_a_missing_case(tmp_path: Path) -> None:
    path = tmp_path / "short.jsonl"
    path.write_text(DATASET.read_text(encoding="utf-8").splitlines()[0] + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="fixed bucket distribution"):
        evaluation.load_cases(path)
