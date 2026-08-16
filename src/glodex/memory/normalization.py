"""Deterministic normalization for blacklist memory that may affect publication."""

from __future__ import annotations

import re
from typing import Final

from glodex.memory.models import MemoryCandidate, UserMemoryCategory

_RULE: Final = re.compile(
    r"(?P<field>item_id|brand|material|platform):(?P<value>[^:\x00]{1,128})\Z"
)
_MATERIAL_ALIASES: Final = {
    "塑料": "plastic",
    "plastic": "plastic",
    "金属": "metal",
    "metal": "metal",
    "帆布": "canvas",
    "canvas": "canvas",
    "硅胶": "silicone",
    "silicone": "silicone",
    "皮革": "leather",
    "皮质": "leather",
    "leather": "leather",
}
_PLATFORM_ALIASES: Final = {
    "亚马逊": "amazon",
    "amazon": "amazon",
    "虾皮": "shopee",
    "shopee": "shopee",
    "速卖通": "aliexpress",
    "aliexpress": "aliexpress",
    "易贝": "ebay",
    "ebay": "ebay",
    "阿里巴巴": "alibaba",
    "alibaba": "alibaba",
    "沃尔玛": "walmart",
    "walmart": "walmart",
    "shein": "shein",
}


def normalize_blacklist_rule(content: str) -> str:
    """Validate and canonicalize a manually authored hard blacklist rule."""

    if type(content) is not str:
        raise ValueError("blacklist rule must be text")
    match = _RULE.fullmatch(content.strip())
    if match is None:
        raise ValueError("blacklist rule must use field:value")
    field = match.group("field")
    value = match.group("value").strip().casefold()
    if field == "material":
        value = _MATERIAL_ALIASES.get(value, value)
    elif field == "platform":
        try:
            value = _PLATFORM_ALIASES[value]
        except KeyError:
            raise ValueError("blacklist platform is unsupported") from None
    if not value:
        raise ValueError("blacklist rule value is empty")
    return f"{field}:{value}"


def persisted_memory_content(candidate: MemoryCandidate) -> str:
    """Convert one already-validated Reflect quote into its persisted representation."""

    if type(candidate) is not MemoryCandidate:
        raise TypeError("memory candidate must be exact")
    if candidate.category is not UserMemoryCategory.BLACKLIST:
        return candidate.content

    normalized = candidate.content.casefold()
    direct = _RULE.fullmatch(normalized)
    if direct is not None:
        return normalize_blacklist_rule(normalized)

    material_hits = {value for alias, value in _MATERIAL_ALIASES.items() if alias in normalized}
    platform_hits = {value for alias, value in _PLATFORM_ALIASES.items() if alias in normalized}
    if len(material_hits) == 1 and not platform_hits:
        return f"material:{material_hits.pop()}"
    if len(platform_hits) == 1 and not material_hits:
        return f"platform:{platform_hits.pop()}"

    # Ambiguous quotes remain soft context and can never become a hard predicate.
    return candidate.content


def normalize_candidate_fact(*, field: str, value: str) -> str:
    """Normalize a trusted candidate fact with the same vocabulary as hard rules."""

    normalized = value.strip().casefold()
    if field == "material":
        return _MATERIAL_ALIASES.get(normalized, normalized)
    if field == "platform":
        return _PLATFORM_ALIASES.get(normalized, normalized)
    return normalized


__all__ = [
    "normalize_blacklist_rule",
    "normalize_candidate_fact",
    "persisted_memory_content",
]
