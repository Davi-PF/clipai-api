"""Pydantic v2 request and response schemas for user accounts and avatar uploads."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

from app.core.config import get_settings
from app.core.privacy import CollectionsVisibility

AvatarSource = Literal["custom", "discord", "none"]


def _get_field(source: Any, name: str, default: Any = None) -> Any:
    """Read an attribute from an ORM entity or key from a dict."""
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


def resolve_display_name(source: Any) -> str:
    """Resolve display_name with fallback to username when null or blank."""
    display_name = _get_field(source, "display_name")
    if isinstance(display_name, str) and display_name.strip():
        return display_name.strip()
    username = _get_field(source, "username")
    return str(username).strip() if username else ""


def resolve_user_avatar_url(
    source: Any,
    *,
    public_base_url: str | None = None,
) -> str | None:
    """Resolve effective avatar URL according to avatar_source."""
    raw_source = _get_field(source, "avatar_source")
    avatar_source = str(raw_source).strip().lower() if raw_source else "none"
    avatar_key = _get_field(source, "avatar_key")
    discord_avatar_url = _get_field(source, "discord_avatar_url")
    raw_avatar_url = _get_field(source, "avatar_url")

    if avatar_source == "custom":
        if isinstance(avatar_key, str) and avatar_key.strip():
            base = (public_base_url or get_settings().r2_public_base_url).rstrip("/")
            return f"{base}/{avatar_key.strip().lstrip('/')}"
        return str(raw_avatar_url) if raw_avatar_url else None

    if avatar_source == "discord":
        if isinstance(discord_avatar_url, str) and discord_avatar_url.strip():
            return discord_avatar_url.strip()
        return None

    # avatar_source == "none"
    if not avatar_key and not discord_avatar_url and isinstance(raw_avatar_url, str):
        cleaned = raw_avatar_url.strip()
        return cleaned or None
    return None


def enrich_user_payload(data: Any) -> dict[str, Any]:
    """Normalize an ORM User instance or dict with resolved profile, Discord, and 2FA fields."""
    if isinstance(data, dict):
        base = dict(data)
    else:
        base = {
            "id": getattr(data, "id", None),
            "username": getattr(data, "username", None),
            "email": getattr(data, "email", None),
            "display_name": getattr(data, "display_name", None),
            "bio": getattr(data, "bio", None),
            "collections_visibility": getattr(data, "collections_visibility", None),
            "avatar_url": getattr(data, "avatar_url", None),
            "avatar_key": getattr(data, "avatar_key", None),
            "avatar_source": getattr(data, "avatar_source", None),
            "discord_id": getattr(data, "discord_id", None),
            "discord_username": getattr(data, "discord_username", None),
            "discord_avatar_url": getattr(data, "discord_avatar_url", None),
            "discord_linked_at": getattr(data, "discord_linked_at", None),
            "is_email_verified": getattr(data, "is_email_verified", False),
            "is_2fa_enabled": getattr(data, "is_2fa_enabled", False),
            "password_hash": getattr(data, "password_hash", None),
            "created_at": getattr(data, "created_at", None),
        }

    discord_id = base.get("discord_id")
    discord_linked = (
        bool(base["discord_linked"])
        if "discord_linked" in base and discord_id is None
        else bool(discord_id)
    )
    has_password = (
        bool(base["has_password"])
        if "has_password" in base and "password_hash" not in base
        else bool(base.get("password_hash"))
    )
    avatar_source = base.get("avatar_source") or "none"

    base["display_name"] = resolve_display_name(data)
    base["avatar_source"] = avatar_source
    base["collections_visibility"] = base.get("collections_visibility") or "public"
    base["avatar_url"] = (
        base["avatar_url"]
        if isinstance(data, dict)
        and "avatar_url" in data
        and "avatar_key" not in data
        and "avatar_source" not in data
        else resolve_user_avatar_url(data)
    )
    base["has_custom_avatar"] = bool(base.get("avatar_key"))
    base["discord_linked"] = discord_linked
    base["discord_username"] = base.get("discord_username") if discord_linked else None
    base["is_2fa_enabled"] = bool(base.get("is_2fa_enabled", False))
    base["has_password"] = has_password
    return base


class UserAuthSummary(BaseModel):
    """User summary embedded in POST /api/v1/auth/login and OAuth/2FA responses."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    username: str
    email: EmailStr
    display_name: str
    avatar_url: str | None = None
    avatar_source: AvatarSource = "none"
    discord_linked: bool = False
    discord_username: str | None = None
    is_email_verified: bool
    is_2fa_enabled: bool = False
    has_password: bool = True
    collections_visibility: CollectionsVisibility = "public"

    @model_validator(mode="before")
    @classmethod
    def _resolve_fields(cls, data: Any) -> dict[str, Any]:
        return enrich_user_payload(data)


class UserRegisteredResponse(BaseModel):
    """User summary embedded in POST /api/v1/auth/register responses."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    username: str
    email: EmailStr
    display_name: str
    avatar_url: str | None = None
    discord_linked: bool = False
    discord_username: str | None = None
    is_email_verified: bool
    is_2fa_enabled: bool = False
    has_password: bool = True
    created_at: datetime

    @model_validator(mode="before")
    @classmethod
    def _resolve_fields(cls, data: Any) -> dict[str, Any]:
        return enrich_user_payload(data)


class UserMeResponse(BaseModel):
    """Full profile response for GET /api/v1/users/me and GET /api/v1/auth/me."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    username: str
    email: EmailStr
    display_name: str
    avatar_url: str | None = None
    avatar_source: AvatarSource = "none"
    has_custom_avatar: bool = False
    bio: str | None = None
    collections_visibility: CollectionsVisibility = "public"
    discord_linked: bool = False
    discord_username: str | None = None
    discord_avatar_url: str | None = None
    discord_linked_at: datetime | None = None
    is_email_verified: bool
    is_2fa_enabled: bool = False
    has_password: bool = True
    created_at: datetime

    @model_validator(mode="before")
    @classmethod
    def _resolve_fields(cls, data: Any) -> dict[str, Any]:
        return enrich_user_payload(data)


class UserUpdateRequest(BaseModel):
    """Payload for PATCH /api/v1/users/me."""

    display_name: str | None = Field(default=None, max_length=50)
    bio: str | None = Field(default=None, max_length=240)
    avatar_source: AvatarSource | None = None
    collections_visibility: CollectionsVisibility | None = None

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str | None) -> str | None:
        """Strip whitespace and convert empty string to None (falls back to username)."""
        if value is None:
            return None
        cleaned = value.strip()
        if len(cleaned) > 50:
            msg = "O nome de exibição deve ter no máximo 50 caracteres."
            raise ValueError(msg)
        return cleaned or None

    @field_validator("bio")
    @classmethod
    def normalize_bio(cls, value: str | None) -> str | None:
        """Strip whitespace and validate max 240 characters."""
        if value is None:
            return None
        cleaned = value.strip()
        if len(cleaned) > 240:
            msg = "A biografia deve ter no máximo 240 caracteres."
            raise ValueError(msg)
        return cleaned or None


class AvatarUploadUrlRequest(BaseModel):
    """Payload for POST /api/v1/users/me/avatar/upload-url."""

    mime_type: str = Field(
        min_length=1,
        max_length=30,
        validation_alias=AliasChoices("mime_type", "content_type", "avatar_mime_type"),
    )
    size_bytes: int = Field(
        gt=0,
        validation_alias=AliasChoices("size_bytes", "file_size_bytes", "avatar_size_bytes"),
    )

    @field_validator("mime_type")
    @classmethod
    def normalize_mime_type(cls, value: str) -> str:
        """Normalize MIME type string to lowercase."""
        return value.strip().lower()


class AvatarPresignedTarget(BaseModel):
    """Presigned PUT instruction for browser avatar upload."""

    model_config = ConfigDict(from_attributes=True)

    url: str
    method: Literal["PUT"] = "PUT"
    headers: dict[str, str]


class AvatarUploadUrlResponse(BaseModel):
    """Response for POST /api/v1/users/me/avatar/upload-url."""

    model_config = ConfigDict(from_attributes=True)

    avatar_key: str
    object_key: str
    upload_url: str
    expires_in: int
    avatar_upload: AvatarPresignedTarget


class AvatarConfirmRequest(BaseModel):
    """Payload for POST /api/v1/users/me/avatar/confirm."""

    avatar_key: str = Field(
        min_length=1,
        max_length=255,
        validation_alias=AliasChoices("avatar_key", "object_key"),
    )

    @field_validator("avatar_key")
    @classmethod
    def strip_key(cls, value: str) -> str:
        """Trim whitespace from R2 object key."""
        return value.strip()


class UserPublicStats(BaseModel):
    """Public aggregated statistics for a user profile."""

    model_config = ConfigDict(from_attributes=True)

    clips_count: int = 0
    total_views: int = 0
    total_likes: int = 0
    collections_count: int = 0


class UserPublicResponse(BaseModel):
    """Public profile returned by GET /api/v1/users/{username}."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    username: str
    display_name: str
    bio: str | None = None
    avatar_url: str | None = None
    avatar_source: AvatarSource = "none"
    discord_linked: bool = False
    discord_username: str | None = None
    collections_visibility: CollectionsVisibility = "public"
    stats: UserPublicStats = Field(default_factory=UserPublicStats)
    clips_count: int = 0
    total_views: int = 0
    total_likes: int = 0
    collections_count: int = 0
    created_at: datetime
