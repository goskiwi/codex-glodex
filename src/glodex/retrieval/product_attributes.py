"""Product-attribute projection for current retrieval results."""

from __future__ import annotations

import re
from collections.abc import Mapping

_MAX_ATTRIBUTE_NAME_CHARACTERS = 64
_MAX_ATTRIBUTE_VALUE_CHARACTERS = 256
_STORAGE_AFTER_SIZE = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?)\s*(TB|GB)\s*(?:NVME\s*)?(?:SSD|HDD)\b",
    re.IGNORECASE,
)
_STORAGE_BEFORE_SIZE = re.compile(
    r"\b(?:SSD|HDD)\s*(\d+(?:\.\d+)?)\s*(TB|GB)(?![\w.])",
    re.IGNORECASE,
)


def validate_product_attributes(value: object) -> dict[str, str]:
    """Validate the exact runtime attribute shape without coercion."""

    if type(value) is not dict:
        raise ValueError("product attributes must be an object")
    result: dict[str, str] = {}
    for name, attribute_value in value.items():
        if (
            type(name) is not str
            or not name
            or name != name.strip()
            or "\0" in name
            or len(name) > _MAX_ATTRIBUTE_NAME_CHARACTERS
        ):
            raise ValueError("product attribute name is invalid")
        if (
            type(attribute_value) is not str
            or not attribute_value
            or attribute_value != attribute_value.strip()
            or "\0" in attribute_value
            or len(attribute_value) > _MAX_ATTRIBUTE_VALUE_CHARACTERS
        ):
            raise ValueError("product attribute value is invalid")
        result[name] = attribute_value
    return result


def project_product_attributes(source: Mapping[str, object]) -> dict[str, str]:
    """Project declared ``attribute_names`` from the catalog ``search_text``.

    The current catalog owns both fields. Declared scalar facts and ``name: value``
    lines are copied directly. The one extra text projection is an unambiguous
    storage-capacity pattern adjacent to ``SSD`` or ``HDD``.
    """

    if not isinstance(source, Mapping):
        raise ValueError("product attribute source must be a mapping")
    names = source.get("attribute_names")
    search_text = source.get("search_text")
    if type(names) is not list or type(search_text) is not str:
        raise ValueError("product attribute source is invalid")
    if any(
        type(name) is not str
        or not name
        or name != name.strip()
        or "\0" in name
        or len(name) > _MAX_ATTRIBUTE_NAME_CHARACTERS
        for name in names
    ):
        raise ValueError("product attribute names are invalid")
    if len(names) != len(set(names)):
        raise ValueError("product attribute names must be unique")

    lines = search_text.splitlines()
    projected: dict[str, str] = {}
    for name in names:
        prefix = name + ":"
        values: list[str] = []
        scalar = source.get(name)
        if type(scalar) is str:
            candidate = " ".join(scalar.split())
            if candidate and "\0" not in candidate:
                values.append(candidate)
        for line in lines:
            if not line.startswith(prefix):
                continue
            candidate = " ".join(line[len(prefix) :].split())
            if candidate and "\0" not in candidate and candidate not in values:
                values.append(candidate)
        selected: list[str] = []
        for candidate in values:
            combined = " | ".join((*selected, candidate))
            if len(combined) > _MAX_ATTRIBUTE_VALUE_CHARACTERS:
                break
            selected.append(candidate)
        if selected:
            projected[name] = " | ".join(selected)
    storage = _storage_from_text(search_text)
    if storage is not None and "storage" not in projected:
        projected["storage"] = storage
    return validate_product_attributes(projected)


def _storage_from_text(value: str) -> str | None:
    for pattern in (_STORAGE_AFTER_SIZE, _STORAGE_BEFORE_SIZE):
        match = pattern.search(value)
        if match is not None:
            amount, unit = match.groups()
            return f"{amount} {unit.upper()}"
    return None


__all__ = [
    "project_product_attributes",
    "validate_product_attributes",
]
