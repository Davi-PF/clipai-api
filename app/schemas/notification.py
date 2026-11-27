"""Pydantic v2 response schemas for user notifications."""

from datetime import datetime
from math import ceil
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from app.schemas.user import enrich_user_payload

NotificationType = Literal["friend_request", "friend_accepted"]


class NotificationActorSummary(BaseModel):
    """Minimal user summary representing who triggered a notification."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    username: str
    display_name: str
    avatar_url: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _resolve_fields(cls, data: Any) -> dict[str, Any]:
        return enrich_user_payload(data)


class NotificationItemResponse(BaseModel):
    """Single notification item returned by notification endpoints."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    type: NotificationType
    entity_id: UUID | None = None
    is_read: bool
    created_at: datetime
    actor: NotificationActorSummary


class NotificationListResponse(BaseModel):
    """Paginated notification inbox response with unread counter."""

    model_config = ConfigDict(from_attributes=True)

    items: list[NotificationItemResponse]
    page: int
    page_size: int
    total: int
    total_pages: int
    unread_count: int

    @classmethod
    def create(
        cls,
        *,
        items: list[NotificationItemResponse],
        page: int,
        page_size: int,
        total: int,
        unread_count: int,
    ) -> "NotificationListResponse":
        """Build paginated notification envelope with computed total_pages."""
        total_pages = ceil(total / page_size) if total > 0 else 0
        return cls(
            items=items,
            page=page,
            page_size=page_size,
            total=total,
            total_pages=total_pages,
            unread_count=unread_count,
        )


class MarkAllNotificationsReadResponse(BaseModel):
    """Response for POST /api/v1/notifications/read-all."""

    model_config = ConfigDict(from_attributes=True)

    updated_count: int
    message: str
