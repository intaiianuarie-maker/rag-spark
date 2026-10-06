"""Pydantic request/response schemas for the auth API.

Response shapes are the contract consumed by the M1-002a Next.js proxy
(see docs/engineering/frontend-m1-prep.md "Boss answers"):
- tokens are returned in the JSON body (no Set-Cookie from FastAPI);
- ``POST /auth/login`` and ``POST /auth/refresh`` return :class:`TokenPair`;
- ``POST /auth/register`` returns :class:`UserPublic` (never the hash);
- ``POST /auth/logout`` returns 204.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128
EMAIL_MAX_LENGTH = 320


def _normalize_email(value: str) -> str:
    email = value.strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise ValueError("invalid email address")
    local, _, domain = email.partition("@")
    if not local or "." not in domain:
        raise ValueError("invalid email address")
    return email


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=EMAIL_MAX_LENGTH)
    password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)

    @field_validator("email")
    @classmethod
    def _validate_email(cls, value: str) -> str:
        return _normalize_email(value)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=EMAIL_MAX_LENGTH)
    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)

    @field_validator("email")
    @classmethod
    def _validate_email(cls, value: str) -> str:
        return _normalize_email(value)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=4096)


class LogoutRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=4096)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Access token lifetime in seconds.")


class UserPublic(BaseModel):
    id: str
    email: str
    created_at: datetime


class AuthErrorBody(BaseModel):
    error: str
    message: str
