"""Strict Category Card policies used by current-product enrichment."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

CATEGORY_CARDS_SCHEMA = "glodex.category-cards.v2"
COMMERCE_RULESET_VERSION = "semantic-category-clean-v4"
# Category semantics can evolve without randomly repricing every unchanged item.
# Keep the established deterministic commerce facts stable across the v4
# classifier-only correction.
COMMERCE_GENERATION_SEED = "semantic-category-clean-v3"


class CategoryPolicyError(ValueError):
    """A stable error in a Category Card artifact."""


@dataclass(frozen=True, slots=True)
class PricingPolicy:
    hard_min: Decimal
    p50: Decimal
    hard_max: Decimal

    def __post_init__(self) -> None:
        if not all(_valid_decimal(value) and value >= 0 for value in self.values):
            raise CategoryPolicyError("price bounds must be finite non-negative decimals")
        if not self.hard_min < self.p50 < self.hard_max:
            raise CategoryPolicyError("price bounds must satisfy hard_min < p50 < hard_max")

    @property
    def values(self) -> tuple[Decimal, Decimal, Decimal]:
        return self.hard_min, self.p50, self.hard_max


@dataclass(frozen=True, slots=True)
class InventoryPolicy:
    in_stock_probability: Decimal
    out_of_stock_probability: Decimal

    def __post_init__(self) -> None:
        probabilities = (self.in_stock_probability, self.out_of_stock_probability)
        if not all(
            _valid_decimal(value) and Decimal(0) <= value <= Decimal(1) for value in probabilities
        ):
            raise CategoryPolicyError("inventory probabilities must be between zero and one")
        if sum(probabilities, Decimal(0)) != Decimal(1):
            raise CategoryPolicyError("inventory probabilities must sum to one")


@dataclass(frozen=True, slots=True)
class ShippingPolicy:
    minimum: Decimal
    maximum: Decimal
    free_probability: Decimal
    maximum_price_ratio: Decimal
    delivery_days_min: int
    delivery_days_max: int

    def __post_init__(self) -> None:
        if not all(_valid_decimal(value) for value in self.decimal_values):
            raise CategoryPolicyError("shipping values must be finite decimals")
        if self.minimum < 0 or self.minimum > self.maximum:
            raise CategoryPolicyError("shipping bounds are invalid")
        if not Decimal(0) <= self.free_probability <= Decimal(1):
            raise CategoryPolicyError("free shipping probability is invalid")
        if not Decimal(0) < self.maximum_price_ratio <= Decimal(1):
            raise CategoryPolicyError("shipping price ratio is invalid")
        if (
            type(self.delivery_days_min) is not int
            or type(self.delivery_days_max) is not int
            or self.delivery_days_min < 0
            or self.delivery_days_min > self.delivery_days_max
        ):
            raise CategoryPolicyError("delivery day bounds are invalid")

    @property
    def decimal_values(self) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        return self.minimum, self.maximum, self.free_probability, self.maximum_price_ratio


@dataclass(frozen=True, slots=True)
class CategoryCard:
    card_id: str
    parent_id: str
    name: str
    entity_kind: str
    category_aliases: tuple[str, ...]
    positive_keywords: tuple[str, ...]
    negative_keywords: tuple[str, ...]
    prototype_text: str
    pricing: PricingPolicy
    inventory: InventoryPolicy
    shipping: ShippingPolicy

    def __post_init__(self) -> None:
        _identifier(self.card_id, "card ID")
        _identifier(self.parent_id, "parent ID", allow_dot=False)
        _text(self.name, "card name")
        _text(self.prototype_text, "prototype text")
        if not self.card_id.startswith(self.parent_id + "."):
            raise CategoryPolicyError("card ID must be a child of parent ID")
        if self.entity_kind not in {
            "PRIMARY_PRODUCT",
            "ACCESSORY",
            "REPLACEMENT_PART",
            "DECORATION",
        }:
            raise CategoryPolicyError("entity kind is invalid")
        for field_name, values in (
            ("category aliases", self.category_aliases),
            ("positive keywords", self.positive_keywords),
            ("negative keywords", self.negative_keywords),
        ):
            if type(values) is not tuple:
                raise CategoryPolicyError(field_name + " must be a tuple")
            normalized = tuple(_text(value, field_name).casefold() for value in values)
            if len(normalized) != len(set(normalized)):
                raise CategoryPolicyError(field_name + " must be unique")


@dataclass(frozen=True, slots=True)
class CategoryPolicySet:
    ruleset_version: str
    cards: tuple[CategoryCard, ...]
    by_id: Mapping[str, CategoryCard]

    def require(self, card_id: str) -> CategoryCard:
        try:
            return self.by_id[card_id]
        except KeyError as error:
            raise CategoryPolicyError("unknown Category Card: " + card_id) from error


def load_category_cards(path: Path | str, *, expected_count: int = 128) -> CategoryPolicySet:
    """Load one exact JSON artifact and reject implicit or unknown fields."""

    artifact_path = Path(path)
    try:
        root = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CategoryPolicyError("Category Card artifact is unreadable") from error
    root = _object(root, "Category Card artifact")
    _exact_keys(root, {"schema_version", "ruleset_version", "cards"}, "artifact")
    if root["schema_version"] != CATEGORY_CARDS_SCHEMA:
        raise CategoryPolicyError("Category Card schema is unsupported")
    ruleset_version = _text(root["ruleset_version"], "ruleset version")
    raw_cards = root["cards"]
    if type(raw_cards) is not list:
        raise CategoryPolicyError("cards must be a list")
    if len(raw_cards) != expected_count:
        raise CategoryPolicyError(f"Category Card count must be exactly {expected_count}")
    cards = tuple(_parse_card(value) for value in raw_cards)
    by_id = {card.card_id: card for card in cards}
    if len(by_id) != len(cards):
        raise CategoryPolicyError("Category Card IDs must be unique")
    return CategoryPolicySet(
        ruleset_version=ruleset_version,
        cards=cards,
        by_id=MappingProxyType(by_id),
    )


def _parse_card(value: object) -> CategoryCard:
    raw = _object(value, "card")
    _exact_keys(
        raw,
        {
            "card_id",
            "parent_id",
            "name",
            "entity_kind",
            "category_aliases",
            "positive_keywords",
            "negative_keywords",
            "prototype_text",
            "pricing",
            "inventory",
            "shipping",
        },
        "card",
    )
    pricing = _object(raw["pricing"], "pricing")
    _exact_keys(pricing, {"hard_min", "p50", "hard_max"}, "pricing fields")
    inventory = _object(raw["inventory"], "inventory")
    _exact_keys(
        inventory,
        {"in_stock_probability", "out_of_stock_probability"},
        "inventory fields",
    )
    shipping = _object(raw["shipping"], "shipping")
    _exact_keys(
        shipping,
        {
            "minimum",
            "maximum",
            "free_probability",
            "maximum_price_ratio",
            "delivery_days_min",
            "delivery_days_max",
        },
        "shipping fields",
    )
    return CategoryCard(
        card_id=_text(raw["card_id"], "card ID"),
        parent_id=_text(raw["parent_id"], "parent ID"),
        name=_text(raw["name"], "card name"),
        entity_kind=_text(raw["entity_kind"], "entity kind"),
        category_aliases=_strings(raw["category_aliases"], "category aliases"),
        positive_keywords=_strings(raw["positive_keywords"], "positive keywords"),
        negative_keywords=_strings(raw["negative_keywords"], "negative keywords"),
        prototype_text=_text(raw["prototype_text"], "prototype text"),
        pricing=PricingPolicy(
            _decimal(pricing["hard_min"], "hard_min"),
            _decimal(pricing["p50"], "p50"),
            _decimal(pricing["hard_max"], "hard_max"),
        ),
        inventory=InventoryPolicy(
            _decimal(inventory["in_stock_probability"], "in_stock_probability"),
            _decimal(inventory["out_of_stock_probability"], "out_of_stock_probability"),
        ),
        shipping=ShippingPolicy(
            minimum=_decimal(shipping["minimum"], "shipping minimum"),
            maximum=_decimal(shipping["maximum"], "shipping maximum"),
            free_probability=_decimal(shipping["free_probability"], "free probability"),
            maximum_price_ratio=_decimal(shipping["maximum_price_ratio"], "maximum price ratio"),
            delivery_days_min=_int(shipping["delivery_days_min"], "delivery_days_min"),
            delivery_days_max=_int(shipping["delivery_days_max"], "delivery_days_max"),
        ),
    )


def _object(value: object, name: str) -> dict[str, object]:
    if type(value) is not dict:
        raise CategoryPolicyError(name + " must be an object")
    return value


def _exact_keys(value: Mapping[str, object], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise CategoryPolicyError(name + " has invalid fields")


def _strings(value: object, name: str) -> tuple[str, ...]:
    if type(value) is not list:
        raise CategoryPolicyError(name + " must be a list")
    return tuple(_text(item, name) for item in value)


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value.strip() or len(value.strip()) > 4_000:
        raise CategoryPolicyError(name + " is invalid")
    return value.strip()


def _identifier(value: str, name: str, *, allow_dot: bool = True) -> None:
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789-")
    if allow_dot:
        allowed.add(".")
    if value != value.casefold() or any(character not in allowed for character in value):
        raise CategoryPolicyError(name + " is invalid")


def _decimal(value: object, name: str) -> Decimal:
    if type(value) not in (str, int):
        raise CategoryPolicyError(name + " must be a decimal string")
    try:
        result = Decimal(str(value))
    except Exception as error:
        raise CategoryPolicyError(name + " is invalid") from error
    if not _valid_decimal(result):
        raise CategoryPolicyError(name + " is invalid")
    return result


def _valid_decimal(value: object) -> bool:
    return type(value) is Decimal and value.is_finite()


def _int(value: object, name: str) -> int:
    if type(value) is not int or isinstance(value, bool):
        raise CategoryPolicyError(name + " must be an integer")
    return value


__all__ = [
    "CATEGORY_CARDS_SCHEMA",
    "COMMERCE_GENERATION_SEED",
    "COMMERCE_RULESET_VERSION",
    "CategoryCard",
    "CategoryPolicyError",
    "CategoryPolicySet",
    "InventoryPolicy",
    "PricingPolicy",
    "ShippingPolicy",
    "load_category_cards",
]
