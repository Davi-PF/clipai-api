"""Pydantic v2 schemas for clip upload, detail, listing, and editing."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.user import enrich_user_payload

ClipPrivacy = Literal["public", "friends", "unlisted"]
ClipStatus = Literal["pending_upload", "ready", "failed"]
ClipSortOption = Literal["recent", "popular", "featured", "most_viewed"]
ClipPeriod = Literal["7d", "30d", "all"]


class TechnicalMetadataSchema(BaseModel):
    """Validated JSONB schema for clips.technical_metadata."""

    model_config = ConfigDict(from_attributes=True)

    codec: str = Field(default="unknown", max_length=32)
    container: str = Field(default="mp4", max_length=16)
    width: int = Field(default=1920, ge=160, le=3840)
    height: int = Field(default=1080, ge=160, le=2160)
    resolution_label: str = Field(default="1080p", max_length=16)
    fps: float = Field(default=60.0, ge=1.0, le=240.0)
    bitrate_kbps: int = Field(default=0, ge=0, le=200_000)
    audio_codec: str | None = Field(default=None, max_length=32)
    audio_channels: int | None = Field(default=None, ge=1, le=16)
    aspect_ratio: str = Field(default="16:9", max_length=16)
    mime_type: str | None = Field(default=None, max_length=30)


class UploadUrlRequest(BaseModel):
    """Payload for POST /api/v1/clips/upload-url."""

    title: str = Field(min_length=3, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    game_id: UUID | None = None
    privacy: ClipPrivacy = "public"
    tags: list[str] = Field(default_factory=list, max_length=10)
    video_mime_type: str = Field(min_length=1, max_length=30)
    video_size_bytes: int = Field(gt=0)
    duration_seconds: float = Field(ge=1.0, le=120.0)
    thumbnail_mime_type: str | None = Field(default=None, max_length=30)
    thumbnail_size_bytes: int | None = Field(default=None, gt=0)
    technical_metadata: TechnicalMetadataSchema = Field(default_factory=TechnicalMetadataSchema)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        """Strip whitespace and ensure minimum title length."""
        cleaned = value.strip()
        if len(cleaned) < 3:
            msg = "O título do clipe deve ter pelo menos 3 caracteres."
            raise ValueError(msg)
        return cleaned

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str | None) -> str | None:
        """Strip whitespace or convert blank descriptions to None."""
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("video_mime_type", "thumbnail_mime_type")
    @classmethod
    def normalize_mime_type(cls, value: str | None) -> str | None:
        """Normalize MIME type strings to lowercase."""
        return value.strip().lower() if value else None


class PresignedUploadTarget(BaseModel):
    """Direct-to-R2 presigned PUT instruction for browser clients."""

    model_config = ConfigDict(from_attributes=True)

    url: str
    method: Literal["PUT"] = "PUT"
    headers: dict[str, str]


class UploadUrlResponse(BaseModel):
    """Response for POST /api/v1/clips/upload-url."""

    model_config = ConfigDict(from_attributes=True)

    clip_id: UUID
    slug: str
    status: ClipStatus
    expires_in: int
    video_upload: PresignedUploadTarget
    thumbnail_upload: PresignedUploadTarget | None = None


class ClipAuthorSummary(BaseModel):
    """Public author summary embedded in clip payloads."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    username: str
    display_name: str
    avatar_url: str | None = None
    discord_linked: bool = False
    discord_username: str | None = None
    is_2fa_enabled: bool = False
    has_password: bool = True

    @model_validator(mode="before")
    @classmethod
    def _resolve_author_fields(cls, data: Any) -> dict[str, Any]:
        return enrich_user_payload(data)


class ClipGameSummary(BaseModel):
    """Game summary embedded in clip payloads."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    slug: str


class ClipSummaryResponse(BaseModel):
    """Clip card item returned in paginated listings."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    slug: str
    title: str
    privacy: ClipPrivacy
    video_url: str
    thumbnail_url: str | None
    duration_seconds: float
    views_count: int
    likes_count: int
    favorites_count: int
    is_liked: bool = False
    is_favorited: bool = False
    is_liked_by_me: bool = False
    is_favorited_by_me: bool = False
    is_top_highlight: bool = False
    author: ClipAuthorSummary
    game: ClipGameSummary | None
    tags: list[str]
    created_at: datetime


class ClipDetailResponse(BaseModel):
    """Full clip detail returned by GET /api/v1/clips/{slug} and confirm/update."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    slug: str
    title: str
    description: str | None
    status: ClipStatus
    privacy: ClipPrivacy
    video_url: str
    thumbnail_url: str | None
    file_size_bytes: int
    duration_seconds: float
    mime_type: str
    technical_metadata: TechnicalMetadataSchema
    views_count: int
    likes_count: int
    favorites_count: int
    is_liked: bool = False
    is_favorited: bool = False
    is_liked_by_me: bool = False
    is_favorited_by_me: bool = False
    author: ClipAuthorSummary
    game: ClipGameSummary | None
    tags: list[str]
    created_at: datetime
    updated_at: datetime


class ClipLikeResponse(BaseModel):
    """Response for POST/DELETE /api/v1/clips/{clip_id}/like."""

    model_config = ConfigDict(from_attributes=True)

    clip_id: UUID
    liked: bool
    is_liked_by_me: bool
    likes_count: int


class ClipFavoriteResponse(BaseModel):
    """Response for POST/DELETE /api/v1/clips/{clip_id}/favorite."""

    model_config = ConfigDict(from_attributes=True)

    clip_id: UUID
    favorited: bool
    is_favorited_by_me: bool
    favorites_count: int


class ClipViewResponse(BaseModel):
    """Response for POST /api/v1/clips/{clip_id}/views (or /view)."""

    model_config = ConfigDict(from_attributes=True)

    clip_id: UUID
    views_count: int


class ClipUpdateRequest(BaseModel):
    """Payload for PATCH /api/v1/clips/{clip_id}."""

    title: str | None = Field(default=None, min_length=3, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    game_id: UUID | None = None
    privacy: ClipPrivacy | None = None
    tags: list[str] | None = Field(default=None, max_length=10)

    @field_validator("title")
    @classmethod
    def validate_optional_title(cls, value: str | None) -> str | None:
        """Strip whitespace and validate minimum title length when provided."""
        if value is None:
            return None
        cleaned = value.strip()
        if len(cleaned) < 3:
            msg = "O título do clipe deve ter pelo menos 3 caracteres."
            raise ValueError(msg)
        return cleaned
