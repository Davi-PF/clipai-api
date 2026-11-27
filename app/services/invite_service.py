"""Business logic service for invite links and automatic friendships."""

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AppError
from app.models.user import User
from app.repositories.friendship_repository import FriendshipRepository
from app.repositories.notification_repository import NotificationRepository
from app.repositories.user_repository import UserRepository
from app.schemas.friendship import FriendUserSummary
from app.schemas.invite import (
    InviteAcceptResponse,
    InvitePreviewResponse,
    MyInviteResponse,
)
from app.schemas.user import resolve_display_name, resolve_user_avatar_url


class InviteService:
    """Orchestrates invite code resolution, public previews, and instant friendship acceptance."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._user_repo = UserRepository(session)
        self._friendship_repo = FriendshipRepository(session)
        self._notification_repo = NotificationRepository(session)

    def get_my_invite(self, current_user: User) -> MyInviteResponse:
        """Return the authenticated user's invite code and full frontend share URL."""
        base_url = self._settings.frontend_url.rstrip("/")
        return MyInviteResponse(
            invite_code=current_user.invite_code,
            invite_url=f"{base_url}/invite/{current_user.invite_code}",
        )

    async def get_invite_preview(self, code: str) -> InvitePreviewResponse:
        """Return safe public inviter metadata for a valid invite code."""
        inviter = await self._resolve_active_inviter(code)
        discord_linked = bool(inviter.discord_id)
        return InvitePreviewResponse(
            invite_code=inviter.invite_code,
            username=inviter.username,
            display_name=resolve_display_name(inviter),
            avatar_url=resolve_user_avatar_url(
                inviter,
                public_base_url=self._settings.r2_public_base_url,
            ),
            discord_username=inviter.discord_username if discord_linked else None,
        )

    async def accept_invite(
        self,
        code: str,
        current_user: User,
    ) -> InviteAcceptResponse:
        """Idempotently create or upgrade an accepted friendship via an invite link."""
        inviter = await self._resolve_active_inviter(code)
        if inviter.id == current_user.id:
            raise AppError(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="BAD_REQUEST",
                message="Você não pode aceitar o seu próprio link de convite.",
            )

        existing = await self._friendship_repo.get_between_users(inviter.id, current_user.id)
        if existing is not None and existing.status == "accepted":
            return InviteAcceptResponse(
                friendship_id=existing.id,
                already_friends=True,
                message="Vocês já são amigos.",
                inviter=FriendUserSummary.model_validate(inviter),
            )

        if existing is not None:
            existing.status = "accepted"
            friendship = await self._friendship_repo.save(existing)
            await self._notification_repo.mark_entity_notifications_read(
                entity_id=existing.id,
                notification_type="friend_request",
            )
        else:
            friendship = await self._friendship_repo.create(
                requester_id=inviter.id,
                addressee_id=current_user.id,
                status="accepted",
            )

        await self._notification_repo.create(
            user_id=inviter.id,
            actor_id=current_user.id,
            notification_type="friend_accepted",
            entity_id=friendship.id,
        )
        await self._session.commit()

        return InviteAcceptResponse(
            friendship_id=friendship.id,
            already_friends=False,
            message="Convite aceito! Vocês agora são amigos.",
            inviter=FriendUserSummary.model_validate(inviter),
        )

    async def _resolve_active_inviter(self, code: str) -> User:
        inviter = await self._user_repo.get_by_invite_code(code)
        if inviter is None or not inviter.is_active:
            raise AppError(
                status_code=status.HTTP_404_NOT_FOUND,
                code="RESOURCE_NOT_FOUND",
                message="Link de convite inválido ou não encontrado.",
            )
        return inviter
