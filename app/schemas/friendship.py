"""Pydantic v2 schemas for friendships and friend requests."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from app.models.friendship import Friendship
from app.schemas.user import (
    FriendshipStatus,
    SearchFriendshipStatus,
    enrich_user_payload,
)

FriendshipRecordStatus = Literal["pending", "accepted"]
FriendRequestDirection = Literal["received", "sent"]

__all__ = [
    "FriendItemResponse",
    "FriendRequestDirection",
    "FriendRequestItemResponse",
    "FriendRequestsResponse",
    "FriendUserSummary",
    "FriendshipActionResponse",
    "FriendshipRecordStatus",
    "FriendshipStatus",
    "SearchFriendshipStatus",
    "resolve_friendship_status",
]


def resolve_friendship_status(
    viewer_id: UUID | None,
    target_id: UUID,
    friendship: Friendship | None,
) -> tuple[FriendshipStatus, UUID | None]:
    """Compute viewer-relative friendship status and friendship_id for a target user."""
    if viewer_id is not None and viewer_id == target_id:
        return "self", None
    if viewer_id is None or friendship is None:
        return "none", None
    if friendship.status == "accepted":
        return "accepted", friendship.id
    if friendship.requester_id == viewer_id:
        return "pending_sent", friendship.id
    return "pending_received", friendship.id


class FriendUserSummary(BaseModel):
    """Compact user representation embedded in friendship responses."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    username: str
    display_name: str
    avatar_url: str | None = None
    discord_linked: bool = False
    discord_username: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _resolve_fields(cls, data: Any) -> dict[str, Any]:
        return enrich_user_payload(data)


class FriendItemResponse(BaseModel):
    """Accepted friend entry returned by GET /api/v1/friends."""

    model_config = ConfigDict(from_attributes=True)

    friendship_id: UUID
    since: datetime
    user: FriendUserSummary


class FriendRequestItemResponse(BaseModel):
    """Pending friend request entry returned by GET /api/v1/friends/requests."""

    model_config = ConfigDict(from_attributes=True)

    friendship_id: UUID
    direction: FriendRequestDirection
    created_at: datetime
    user: FriendUserSummary


class FriendRequestsResponse(BaseModel):
    """Split view of pending received and sent friend requests."""

    model_config = ConfigDict(from_attributes=True)

    received: list[FriendRequestItemResponse]
    sent: list[FriendRequestItemResponse]


class FriendshipActionResponse(BaseModel):
    """Response for sending or accepting a friend request."""

    model_config = ConfigDict(from_attributes=True)

    friendship_id: UUID
    status: FriendshipRecordStatus
    auto_accepted: bool = False
    message: str
    user: FriendUserSummary
