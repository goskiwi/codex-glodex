"""Narrow local-account HTTP projections without bearer-token exposure."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator

from glodex.api.contracts import ApiDTO

LocalUsername = Annotated[str, StringConstraints(min_length=3, max_length=32)]
LocalPassword = Annotated[str, StringConstraints(min_length=1, max_length=128)]


class LocalCredentials(ApiDTO):
    """Credentials accepted only by the Durable loopback authentication routes."""

    username: LocalUsername
    password: LocalPassword

    @field_validator("username", mode="before")
    @classmethod
    def _trim_username(cls, value: object) -> object:
        return value.strip() if type(value) is str else value

    @field_validator("password")
    @classmethod
    def _reject_null_password(cls, value: str) -> str:
        if "\0" in value:
            raise ValueError("password contains a null character")
        return value


class LocalAuthStatus(ApiDTO):
    """Safe current-session response; raw tokens and user IDs never leave Durable."""

    schema_version: Literal["glodex.local-auth.v1"] = Field(
        default="glodex.local-auth.v1", serialization_alias="schemaVersion"
    )
    username: LocalUsername
    expires_at: str = Field(serialization_alias="expiresAt")


__all__ = ["LocalAuthStatus", "LocalCredentials"]
