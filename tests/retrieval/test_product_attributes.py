"""Contract tests for the current product-attribute projection."""

from __future__ import annotations

from decimal import Decimal

import pytest

from glodex.agent.contracts import (
    Candidate,
    CandidateAttribute,
    PickedItem,
    Platform,
    PreferenceCandidate,
    SemanticAssertionCandidate,
)
from glodex.retrieval.product_attributes import (
    project_product_attributes,
    validate_product_attributes,
)


def test_projection_exposes_only_declared_attribute_lines() -> None:
    result = project_product_attributes(
        {
            "attribute_names": ["brand", "screen_size"],
            "search_text": (
                "iPad Air 11-inch\n"
                "brand: Apple\n"
                "screen_size:   11 in\n"
                "Title: iPad Air 11-inch"
            ),
        }
    )

    assert result == {"brand": "Apple", "screen_size": "11 in"}


def test_projection_uses_declared_scalar_and_unambiguous_storage() -> None:
    result = project_product_attributes(
        {
            "brand": "Example",
            "attribute_names": ["brand"],
            "search_text": "16GB SSD gaming laptop computer",
        }
    )

    assert result == {"brand": "Example", "storage": "16 GB"}


def test_projection_preserves_all_attributes_and_deduplicates_repeated_values() -> None:
    names = [f"attribute_{index}" for index in range(30)]
    result = project_product_attributes(
        {
            "attribute_names": names,
            "search_text": "\n".join(
                ["attribute_0: first", "attribute_0: first"]
                + [f"{name}: value-{index}" for index, name in enumerate(names[1:], start=1)]
            ),
        }
    )

    assert len(result) == len(names)
    assert result["attribute_0"] == "first"
    assert result[names[-1]] == "value-29"


def test_product_attribute_collections_have_no_count_limit() -> None:
    attributes = tuple(
        CandidateAttribute(name=f"attribute_{index}", value=f"value-{index}")
        for index in range(30)
    )
    candidate = Candidate(
        candidate_id="candidate-1",
        item_id="item-1",
        platform=Platform.AMAZON,
        title="Complete product",
        price=Decimal("1"),
        currency="USD",
        attributes=attributes,
        source_ref="source-1",
        record_ref="record-1",
    )

    assert candidate.attributes == attributes
    for contract in (Candidate, SemanticAssertionCandidate, PreferenceCandidate, PickedItem):
        attribute_schema = contract.model_json_schema()["properties"]["attributes"]
        assert "maxItems" not in attribute_schema


@pytest.mark.parametrize(
    "value",
    [
        [],
        {"": "value"},
        {"name": " value"},
        {"name": 1},
    ],
)
def test_runtime_attribute_validation_rejects_non_contract_values(value: object) -> None:
    with pytest.raises(ValueError):
        validate_product_attributes(value)


@pytest.mark.parametrize("name", [" attribute", "attribute ", "\0attribute", "a" * 65])
def test_projection_rejects_invalid_declared_names(name: str) -> None:
    with pytest.raises(ValueError):
        project_product_attributes(
            {
                "attribute_names": [name],
                "search_text": "attribute: value",
            }
        )
