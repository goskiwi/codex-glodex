from __future__ import annotations

from decimal import Decimal
from types import MappingProxyType

from glodex.interview_catalog.category_classifier import CategoryClassifier
from glodex.interview_catalog.category_policy import (
    CategoryCard,
    CategoryPolicySet,
    InventoryPolicy,
    PricingPolicy,
    ShippingPolicy,
)


def _card(
    card_id: str,
    *,
    aliases: tuple[str, ...] = (),
    positive: tuple[str, ...] = (),
    negative: tuple[str, ...] = (),
    entity_kind: str = "PRIMARY_PRODUCT",
) -> CategoryCard:
    return CategoryCard(
        card_id=card_id,
        parent_id=card_id.split(".", 1)[0],
        name=card_id,
        entity_kind=entity_kind,
        category_aliases=aliases,
        positive_keywords=positive,
        negative_keywords=negative,
        prototype_text=card_id,
        pricing=PricingPolicy(Decimal("1"), Decimal("10"), Decimal("100")),
        inventory=InventoryPolicy(Decimal("0.98"), Decimal("0.02")),
        shipping=ShippingPolicy(
            Decimal("0"), Decimal("20"), Decimal("0.5"), Decimal("0.5"), 2, 8
        ),
    )


def _classifier() -> CategoryClassifier:
    cards = (
        _card(
            "electronics.laptop",
            aliases=("Computers / Laptops",),
            positive=("laptop", "notebook"),
            negative=("case", "sleeve", "charger"),
        ),
        _card(
            "electronics.laptop-accessory",
            positive=("laptop case", "laptop sleeve", "charger"),
            entity_kind="ACCESSORY",
        ),
        _card("home.drinkware.cup", positive=("cup", "mug")),
        _card("general.general-merchandise", positive=("general",)),
    )
    policies = CategoryPolicySet(
        ruleset_version="current-commerce-v1",
        cards=cards,
        by_id=MappingProxyType({card.card_id: card for card in cards}),
    )
    vectors = {
        "electronics.laptop": (1.0, 0.0, 0.0),
        "electronics.laptop-accessory": (0.0, 1.0, 0.0),
        "home.drinkware.cup": (0.0, 0.0, 1.0),
        "general.general-merchandise": (0.0, 0.0, 0.0),
    }
    return CategoryClassifier(policies, card_vectors=vectors)


def test_exact_source_category_is_the_only_non_semantic_assignment() -> None:
    result = _classifier().classify(
        category="Computers / Laptops",
    )

    assert result.card_id == "electronics.laptop"
    assert result.method == "SOURCE_CATEGORY"


def test_title_keywords_cannot_override_the_semantic_vector() -> None:
    classifier = _classifier()

    laptop = classifier.classify(
        category=None,
        vector_match=("electronics.laptop", 0.91, 0.40),
    )
    sleeve = classifier.classify(
        category=None,
        vector_match=("electronics.laptop-accessory", 0.92, 0.40),
    )

    assert laptop.card_id == "electronics.laptop"
    assert sleeve.card_id == "electronics.laptop-accessory"
    assert laptop.method == sleeve.method == "SEMANTIC_VECTOR"


def test_item_vector_is_used_when_text_does_not_resolve_a_card() -> None:
    result = _classifier().classify(
        category=None,
        item_vector=(0.0, 0.1, 0.99),
    )

    assert result.card_id == "home.drinkware.cup"
    assert result.method == "SEMANTIC_VECTOR"


def test_precomputed_batch_vector_match_avoids_per_document_dot_products() -> None:
    result = _classifier().classify(
        category=None,
        vector_match=("home.drinkware.cup", 0.97, 0.20),
    )

    assert result.card_id == "home.drinkware.cup"
    assert result.method == "SEMANTIC_VECTOR"
    assert result.score == 0.97


def test_weak_or_ambiguous_semantic_match_is_rejected() -> None:
    classifier = _classifier()

    weak = classifier.classify(
        category=None,
        vector_match=("electronics.laptop", 0.24, 0.10),
    )
    ambiguous = classifier.classify(
        category=None,
        vector_match=("electronics.laptop", 0.70, 0.695),
    )

    assert weak.card_id == ambiguous.card_id == "general.general-merchandise"
    assert weak.method == ambiguous.method == "FALLBACK"


def test_general_leaf_is_the_deterministic_last_resort() -> None:
    result = _classifier().classify(category=None)

    assert result.card_id == "general.general-merchandise"
    assert result.method == "FALLBACK"
