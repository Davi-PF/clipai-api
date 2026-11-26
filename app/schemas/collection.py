"""Pydantic v2 request and response schemas for collections (playlists)."""

import re
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.clip import ClipPrivacy, TechnicalMetadataSchema
from app.schemas.user import enrich_user_payload

_HEX_COLOR_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _validate_accent_color(value: str | None) -> str | None:
    """Validate optional hex accent color (#RRGGBB)."""
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if not _HEX_COLOR_PATTERN.fullmatch(cleaned):
        msg = "A cor de destaque deve estar no formato hexadecimal #RRGGBB (ex.: #2EB886)."
        raise ValueError(msg)
    return cleaned.upper()


class CollectionCreateRequest(BaseModel):
    """Payload for POST /api/v1/collections."""

    title: str = Field(
        min_length=3,
        max_length=100,
        validation_alias=AliasChoices("title", "name"),
    )
    description: str | None = Field(default=None, max_length=500)
    privacy: ClipPrivacy = "public"
    accent_color: str | None = Field(default=None, max_length=7)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        """Strip whitespace and verify minimum length."""
        cleaned = value.strip()
        if len(cleaned) < 3:
            msg = "O título da coleção deve ter pelo menos 3 caracteres."
            raise ValueError(msg)
        return cleaned

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str | None) -> str | None:
        """Normalize blank descriptions to None."""
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("accent_color")
    @classmethod
    def validate_color(cls, value: str | None) -> str | None:
        """Validate optional hex color."""
        return _validate_accent_color(value)


class CollectionUpdateRequest(BaseModel):
    """Payload for PATCH /api/v1/collections/{collection_id}."""

    title: str | None = Field(
        default=None,
        min_length=3,
        max_length=100,
        validation_alias=AliasChoices("title", "name"),
    )
    description: str | None = Field(default=None, max_length=500)
    privacy: ClipPrivacy | None = None
    accent_color: str | None = Field(default=None, max_length=7)

    @field_validator("title")
    @classmethod
    def validate_optional_title(cls, value: str | None) -> str | None:
        """Strip whitespace and validate minimum length when provided."""
        if value is None:
            return None
        cleaned = value.strip()
        if len(cleaned) < 3:
            msg = "O título da coleção deve ter pelo menos 3 caracteres."
            raise ValueError(msg)
        return cleaned

    @field_validator("accent_color")
    @classmethod
    def validate_optional_color(cls, value: str | None) -> str | None:
        """Validate optional hex color."""
        return _validate_accent_color(value)


class CollectionOwnerSummary(BaseModel):
    """Public owner summary embedded in collection responses."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    username: str
    display_name: str
    avatar_url: str | None = None
    discord_linked: bool = False
    discord_username: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _resolve_owner_fields(cls, data: Any) -> dict[str, Any]:
        return enrich_user_payload(data)


class CollectionSummaryResponse(BaseModel):
    """Collection card returned by listing, create, and update endpoints."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    description: str | None
    privacy: ClipPrivacy
    accent_color: str | None
    clips_count: int
    preview_thumbnails: list[str]
    owner: CollectionOwnerSummary
    created_at: datetime
    updated_at: datetime


class CollectionClipCardResponse(BaseModel):
    """Compact clip representation embedded inside a collection detail response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    slug: str
    title: str
    video_url: str
    thumbnail_url: str | None
    file_size_bytes: int | None = None
    duration_seconds: float
    technical_metadata: TechnicalMetadataSchema | None = None
    views_count: int
    likes_count: int
    is_liked: bool = False
    is_favorited: bool = False
    is_liked_by_me: bool = False
    is_favorited_by_me: bool = False


class CollectionClipItemResponse(BaseModel):
    """Ordered clip entry inside GET /api/v1/collections/{collection_id}."""

    model_config = ConfigDict(from_attributes=True)

    position: int
    added_at: datetime
    clip: CollectionClipCardResponse


class CollectionDetailResponse(BaseModel):
    """Full collection metadata with paginated ordered clip items."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    description: str | None
    privacy: ClipPrivacy
    accent_color: str | None
    clips_count: int
    owner: CollectionOwnerSummary
    clips: list[CollectionClipItemResponse]
    page: int
    page_size: int
    total_pages: int
    created_at: datetime
    updated_at: datetime


class CollectionAddClipRequest(BaseModel):
    """Payload for POST /api/v1/collections/{collection_id}/clips."""

    clip_id: UUID
    position: int | None = Field(default=None, ge=0)


class CollectionAddClipResponse(BaseModel):
    """Response for POST /api/v1/collections/{collection_id}/clips."""

    model_config = ConfigDict(from_attributes=True)

    collection_id: UUID
    clip_id: UUID
    position: int
    added_at: datetime
