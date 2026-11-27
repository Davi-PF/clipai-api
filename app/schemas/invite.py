"""Pydantic v2 response schemas for invite links."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.schemas.friendship import FriendUserSummary


class MyInviteResponse(BaseModel):
    """Response for GET /api/v1/invites/me."""

    model_config = ConfigDict(from_attributes=True)

    invite_code: str
    invite_url: str


class InvitePreviewResponse(BaseModel):
    """Safe public preview returned by GET /api/v1/invites/{code}."""

    model_config = ConfigDict(from_attributes=True)

    invite_code: str
    username: str
    display_name: str
    avatar_url: str | None = None
    discord_username: str | None = None


class InviteAcceptResponse(BaseModel):
    """Response for POST /api/v1/invites/{code}/accept."""

    model_config = ConfigDict(from_attributes=True)

    friendship_id: UUID
    status: Literal["accepted"] = "accepted"
    already_friends: bool = False
    message: str
    inviter: FriendUserSummary
