"""Shared exact validators for immutable domain values."""

from __future__ import annotations

from datetime import datetime, timedelta


def require_text(value: object, name: str, *, maximum: int) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if len(value) > maximum:
        raise ValueError(f"{name} exceeds {maximum} code points")
    return value


def require_currency(value: object) -> str:
    if (
        type(value) is not str
        or len(value) != 3
        or not value.isascii()
        or not value.isalpha()
        or not value.isupper()
    ):
        raise ValueError("currency must be an uppercase three-letter code")
    return value


def require_utc(value: object, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
    return value


__all__ = ["require_currency", "require_text", "require_utc"]
