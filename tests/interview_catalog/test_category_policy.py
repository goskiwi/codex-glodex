from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from glodex.interview_catalog.category_policy import (
    COMMERCE_GENERATION_SEED,
    COMMERCE_RULESET_VERSION,
    CategoryPolicyError,
    load_category_cards,
)

CARD_ARTIFACT = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "current-product"
    / "category-cards-v2"
    / "category_cards.json"
)


def _card(card_id: str = "electronics.laptop") -> dict[str, object]:
    return {
        "card_id": card_id,
        "parent_id": card_id.split(".", 1)[0],
        "name": "Laptop",
        "entity_kind": "PRIMARY_PRODUCT",
        "category_aliases": ["Electronics / Computers / Laptops"],
        "positive_keywords": ["laptop", "notebook"],
        "negative_keywords": ["case", "sleeve", "charger"],
        "prototype_text": "laptop notebook computer",
        "pricing": {"hard_min": "1500", "p50": "5500", "hard_max": "35000"},
        "inventory": {
            "in_stock_probability": "0.98",
            "out_of_stock_probability": "0.02",
        },
        "shipping": {
            "minimum": "0",
            "maximum": "120",
            "free_probability": "0.45",
            "maximum_price_ratio": "0.50",
            "delivery_days_min": 2,
            "delivery_days_max": 10,
        },
    }


def _write_cards(tmp_path: Path, cards: list[dict[str, object]]) -> Path:
    path = tmp_path / "category_cards.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "glodex.category-cards.v2",
                "ruleset_version": "current-commerce-v1",
                "cards": cards,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_inventory_policy_has_only_two_states(tmp_path: Path) -> None:
    policies = load_category_cards(_write_cards(tmp_path, [_card()]), expected_count=1)

    inventory = policies.require("electronics.laptop").inventory

    assert inventory.in_stock_probability == Decimal("0.98")
    assert inventory.out_of_stock_probability == Decimal("0.02")
    assert not hasattr(inventory, "unknown_probability")


def test_inventory_probabilities_must_sum_to_one(tmp_path: Path) -> None:
    card = _card()
    card["inventory"] = {
        "in_stock_probability": "0.98",
        "out_of_stock_probability": "0.03",
    }

    with pytest.raises(CategoryPolicyError, match="sum to one"):
        load_category_cards(_write_cards(tmp_path, [card]), expected_count=1)


def test_unknown_inventory_probability_is_rejected(tmp_path: Path) -> None:
    card = _card()
    inventory = dict(card["inventory"])
    inventory["unknown_probability"] = "0.01"
    card["inventory"] = inventory

    with pytest.raises(CategoryPolicyError, match="inventory fields"):
        load_category_cards(_write_cards(tmp_path, [card]), expected_count=1)


def test_pricing_bounds_are_ordered(tmp_path: Path) -> None:
    card = _card()
    card["pricing"] = {"hard_min": "6000", "p50": "5500", "hard_max": "35000"}

    with pytest.raises(CategoryPolicyError, match="price bounds"):
        load_category_cards(_write_cards(tmp_path, [card]), expected_count=1)


def test_committed_artifact_has_128_reasonable_two_state_cards() -> None:
    policies = load_category_cards(CARD_ARTIFACT)

    assert len(policies.cards) == 128
    assert policies.ruleset_version == COMMERCE_RULESET_VERSION == "semantic-category-clean-v4"
    assert COMMERCE_GENERATION_SEED == "semantic-category-clean-v3"
    assert "gaming models" in policies.require("electronics.phone").prototype_text
    assert policies.require("electronics.laptop").pricing.hard_min >= Decimal("1500")
    assert policies.require("home.drinkware.cup").pricing.hard_max <= Decimal("800")
    assert policies.require("electronics.laptop-accessory").entity_kind == "ACCESSORY"
    assert policies.require("electronics.laptop-part").entity_kind == "REPLACEMENT_PART"
    assert all(
        card.inventory.in_stock_probability + card.inventory.out_of_stock_probability
        == Decimal("1")
        for card in policies.cards
    )
    assert all(not hasattr(card.inventory, "unknown_probability") for card in policies.cards)
