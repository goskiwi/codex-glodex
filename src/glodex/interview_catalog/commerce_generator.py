"""Deterministic Category Card-driven commerce fact generation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from glodex.interview_catalog.category_policy import CategoryCard

_CENT = Decimal("0.01")
_LOW_TYPICAL_FACTOR = Decimal("0.35")
_HIGH_TYPICAL_FACTOR = Decimal("3.00")


@dataclass(frozen=True, slots=True)
class GeneratedCommerce:
    item_price: Decimal
    stock_status: str
    shipping: Decimal
    delivery_days_min: int
    delivery_days_max: int


def generate_commerce(
    *,
    document_id: str,
    title: str,
    card: CategoryCard,
    seed: str,
) -> GeneratedCommerce:
    """Generate stable facts whose ranges come only from the assigned Card."""

    price_quantile = _fraction(seed, document_id, "price")
    # ``hard_min`` and ``hard_max`` are validation guardrails, not endpoints of
    # a uniform price distribution.  Interpolating all the way to hard_max made
    # an ordinary bag with a high hash quantile cost nearly CNY 30,000.  Keep
    # generated interview prices in a bounded, median-centred typical band and
    # use the hard bounds only as final clamps.
    if price_quantile < Decimal("0.5"):
        factor = _LOW_TYPICAL_FACTOR + (
            Decimal(1) - _LOW_TYPICAL_FACTOR
        ) * price_quantile * Decimal(2)
    else:
        factor = Decimal(1) + (_HIGH_TYPICAL_FACTOR - Decimal(1)) * (
            price_quantile - Decimal("0.5")
        ) * Decimal(2)
    price = card.pricing.p50 * factor
    normalized_title = title.casefold()
    if any(token in normalized_title for token in ("pro", "premium", "ultra", "旗舰")):
        price *= Decimal("1.12")
    elif any(token in normalized_title for token in ("mini", "basic", "budget", "入门")):
        price *= Decimal("0.90")
    price = _money(min(max(price, card.pricing.hard_min), card.pricing.hard_max))

    stock_status = (
        "IN_STOCK"
        if _fraction(seed, document_id, "stock") < card.inventory.in_stock_probability
        else "OUT_OF_STOCK"
    )
    if _fraction(seed, document_id, "free-shipping") < card.shipping.free_probability:
        shipping = Decimal(0)
    else:
        shipping = card.shipping.minimum + (
            card.shipping.maximum - card.shipping.minimum
        ) * _fraction(seed, document_id, "shipping")
    shipping = _money(
        min(shipping, card.shipping.maximum, price * card.shipping.maximum_price_ratio)
    )
    return GeneratedCommerce(
        item_price=price,
        stock_status=stock_status,
        shipping=shipping,
        delivery_days_min=card.shipping.delivery_days_min,
        delivery_days_max=card.shipping.delivery_days_max,
    )


def _fraction(seed: str, document_id: str, field: str) -> Decimal:
    material = f"{seed}\x1f{document_id}\x1f{field}".encode()
    value = int.from_bytes(hashlib.sha256(material).digest()[:8], "big")
    return Decimal(value) / Decimal(2**64)


def _money(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


__all__ = ["GeneratedCommerce", "generate_commerce"]
