"""Pydantic v2 response schemas for games and tags."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class GameResponse(BaseModel):
    """Game catalog item returned by GET /api/v1/games."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    slug: str
    cover_url: str | None = None


class TagResponse(BaseModel):
    """Tag item returned by GET /api/v1/tags."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    slug: str


class TagListResponse(BaseModel):
    """List wrapper returned by GET /api/v1/tags."""

    model_config = ConfigDict(from_attributes=True)

    items: list[TagResponse]
