"""Local-only account and session primitives for owner-scoped user-memory data."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from re import compile
from typing import Final

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_USERNAME: Final = compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,31}\Z")
_USER_ID: Final = compile(r"user-[0-9a-f]{32}\Z")
_SESSION_TOKEN_BYTES: Final = 32
_SESSION_LIFETIME: Final = timedelta(days=7)
_PASSWORD_HASHER: Final = PasswordHasher()


class LocalIdentityError(ValueError):
    """Stable local-only identity validation error without secret detail."""


@dataclass(frozen=True, slots=True)
class LocalUser:
    """Safe local account projection; it intentionally excludes password material."""

    user_id: str
    username: str

    def __post_init__(self) -> None:
        validate_user_id(self.user_id)
        validate_username(self.username)


@dataclass(frozen=True, slots=True)
class LocalSession:
    """Authenticated session projection; the raw cookie token is never stored here."""

    user: LocalUser
    expires_at: datetime

    def __post_init__(self) -> None:
        if self.expires_at.tzinfo is None:
            raise LocalIdentityError("session expiry is invalid")


def validate_username(value: object) -> str:
    """Validate one non-email local login name."""

    if type(value) is not str or _USERNAME.fullmatch(value) is None:
        raise LocalIdentityError("username is invalid")
    return value


def validate_user_id(value: object) -> str:
    """Validate a server-generated opaque user identity."""

    if type(value) is not str or _USER_ID.fullmatch(value) is None:
        raise LocalIdentityError("user ID is invalid")
    return value


def hash_password(password: object) -> str:
    """Return an Argon2id hash for any JSON string password.

    The local-only account flow deliberately has no length or complexity policy.
    """

    if type(password) is not str:
        raise LocalIdentityError("password is invalid")
    return _PASSWORD_HASHER.hash(password)


def password_matches(*, password: object, password_hash: object) -> bool:
    """Verify a candidate without exposing malformed/hash mismatch distinctions."""

    if type(password) is not str or type(password_hash) is not str:
        return False
    try:
        return bool(_PASSWORD_HASHER.verify(password_hash, password))
    except (InvalidHashError, VerificationError):
        return False


def new_user_id() -> str:
    """Create one Durable-owned opaque identity."""

    return f"user-{secrets.token_hex(16)}"


def new_session_token() -> str:
    """Create one cookie-safe bearer value; persist only ``session_token_hash``."""

    return secrets.token_urlsafe(_SESSION_TOKEN_BYTES)


def session_token_hash(token: object) -> str:
    """Hash a bounded cookie value before storage or comparison."""

    if (
        type(token) is not str
        or not 32 <= len(token) <= 128
        or not token.isascii()
        or "\0" in token
    ):
        raise LocalIdentityError("session token is invalid")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def session_expiry(*, now: datetime | None = None) -> datetime:
    """Return the fixed local session expiry in UTC."""

    if now is not None and (type(now) is not datetime or now.tzinfo is None):
        raise LocalIdentityError("session issue time is invalid")
    issued_at = datetime.now(UTC) if now is None else now.astimezone(UTC)
    return issued_at + _SESSION_LIFETIME


__all__ = [
    "LocalIdentityError",
    "LocalSession",
    "LocalUser",
    "hash_password",
    "new_session_token",
    "new_user_id",
    "password_matches",
    "session_expiry",
    "session_token_hash",
    "validate_user_id",
    "validate_username",
]
