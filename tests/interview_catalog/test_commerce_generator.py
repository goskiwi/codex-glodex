from __future__ import annotations

from decimal import Decimal

from glodex.interview_catalog.category_policy import (
    CategoryCard,
    InventoryPolicy,
    PricingPolicy,
    ShippingPolicy,
)
from glodex.interview_catalog.commerce_generator import generate_commerce


def _card(card_id: str, minimum: str, p50: str, maximum: str) -> CategoryCard:
    return CategoryCard(
        card_id=card_id,
        parent_id=card_id.split(".", 1)[0],
        name=card_id,
        entity_kind="PRIMARY_PRODUCT",
        category_aliases=(),
        positive_keywords=(),
        negative_keywords=(),
        prototype_text=card_id,
        pricing=PricingPolicy(Decimal(minimum), Decimal(p50), Decimal(maximum)),
        inventory=InventoryPolicy(Decimal("0.98"), Decimal("0.02")),
        shipping=ShippingPolicy(
            minimum=Decimal("0"),
            maximum=Decimal("120"),
            free_probability=Decimal("0.45"),
            maximum_price_ratio=Decimal("0.50"),
            delivery_days_min=2,
            delivery_days_max=10,
        ),
    )


def test_prices_respect_the_assigned_category_card() -> None:
    laptop = generate_commerce(
        document_id="doc-laptop",
        title="16GB SSD gaming laptop",
        card=_card("electronics.laptop", "1500", "5500", "35000"),
        seed="interview-v1",
    )
    cup = generate_commerce(
        document_id="doc-cup",
        title="ceramic coffee cup",
        card=_card("home.drinkware.cup", "5", "80", "800"),
        seed="interview-v1",
    )

    assert Decimal("1500") <= laptop.item_price <= Decimal("35000")
    assert Decimal("5") <= cup.item_price <= Decimal("800")
    assert laptop.item_price != cup.item_price


def test_generation_is_stable_and_inventory_has_two_states() -> None:
    card = _card("electronics.laptop", "1500", "5500", "35000")

    first = generate_commerce(
        document_id="same-document",
        title="business laptop",
        card=card,
        seed="interview-v1",
    )
    second = generate_commerce(
        document_id="same-document",
        title="business laptop",
        card=card,
        seed="interview-v1",
    )

    assert first == second
    assert first.stock_status in {"IN_STOCK", "OUT_OF_STOCK"}
    assert first.shipping <= min(card.shipping.maximum, first.item_price / Decimal("2"))
    assert first.delivery_days_min <= first.delivery_days_max


def test_high_hash_quantile_does_not_turn_an_ordinary_backpack_into_a_luxury_item() -> None:
    card = _card("luggage.bag", "20", "300", "30000")

    backpack = generate_commerce(
        document_id="esci:us:B07MK6J5NT",
        title=(
            "KROSER Travel Laptop Backpack 17.3 Inch XL Heavy Duty Computer Backpack "
            "Water-Repellent Business College Daypack"
        ),
        card=card,
        seed="semantic-category-clean-v3",
    )

    assert Decimal("105") <= backpack.item_price <= Decimal("900")
    assert backpack.item_price < Decimal("1700")
