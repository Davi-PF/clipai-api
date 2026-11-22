"""Pydantic v2 response schemas for user accounts."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr


class UserAuthSummary(BaseModel):
    """User summary embedded in POST /api/v1/auth/login responses."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    username: str
    email: EmailStr
    display_name: str | None
    avatar_url: str | None
    is_email_verified: bool
    is_2fa_enabled: bool


class UserRegisteredResponse(BaseModel):
    """User summary embedded in POST /api/v1/auth/register responses."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    username: str
    email: EmailStr
    display_name: str | None
    is_email_verified: bool
    is_2fa_enabled: bool
    created_at: datetime


class UserMeResponse(BaseModel):
    """Full profile response for GET /api/v1/users/me and GET /api/v1/auth/me."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    username: str
    email: EmailStr
    display_name: str | None
    avatar_url: str | None
    bio: str | None
    is_email_verified: bool
    is_2fa_enabled: bool
    created_at: datetime
