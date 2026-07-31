"""GPU-free M2c retrieval contract evidence."""

from __future__ import annotations

import pytest

from glodex.adapters.m2c_indexes import M2C_PIPELINE_ID
from glodex.adapters.m2c_retrieval import (
    card_hybrid_body,
    parse_ranked_ids,
    product_hybrid_body,
    product_user_ann_body,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec(
        "GLO-M2C-P0-005",
        "GLO-M2C-P0-006",
        "GLO-M2C-NFR-001",
        "GLO-M2C-NFR-003",
    ),
]


def _vector() -> tuple[float, ...]:
    return (1.0, *(0.0 for _ in range(1_023)))


def test_m2c_bodies_are_model_alias_bound_and_keep_the_fixed_limits() -> None:
    product = product_hybrid_body(query="phone", query_vector=_vector(), platform="amazon")
    user = product_user_ann_body(vector=_vector(), platform="amazon")
    card = card_hybrid_body(category="phone", query="phone", query_vector=_vector())

    assert product["search_pipeline"] == M2C_PIPELINE_ID
    assert product["size"] == 30
    assert product["query"]["hybrid"]["queries"][1]["knn"]["item_vector"]["k"] == 30
    assert user["size"] == 10
    assert user["query"]["knn"]["item_vector"]["k"] == 10
    assert card["search_pipeline"] == M2C_PIPELINE_ID
    assert card["size"] == 30


def test_m2c_ranked_identity_parser_rejects_injection_and_stabilizes_ties() -> None:
    valid = {
        "hits": {
            "hits": [
                {"_id": "b", "_score": 0.5, "_source": {"record_key": "b"}},
                {"_id": "a", "_score": 0.5, "_source": {"record_key": "a"}},
            ]
        }
    }
    assert parse_ranked_ids(valid, identity_field="record_key", maximum=30) == ("a", "b")

    with pytest.raises(ValueError):
        parse_ranked_ids(
            {"hits": {"hits": [{"_id": "a", "_score": 1.0, "_source": {"record_key": "x"}}]}},
            identity_field="record_key",
            maximum=30,
        )
