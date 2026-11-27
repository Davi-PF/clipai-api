"""Business logic service for friendships and friend requests."""

from uuid import UUID

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError
from app.models.friendship import Friendship
from app.models.user import User
from app.repositories.friendship_repository import FriendshipRepository
from app.repositories.notification_repository import NotificationRepository
from app.repositories.user_repository import UserRepository
from app.schemas.common import PaginatedResponse
from app.schemas.friendship import (
    FriendItemResponse,
    FriendRequestItemResponse,
    FriendRequestsResponse,
    FriendshipActionResponse,
    FriendUserSummary,
)


class FriendshipService:
    """Orchestrates friend listing, requests, acceptance, auto-accept, and removal."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._friendship_repo = FriendshipRepository(session)
        self._user_repo = UserRepository(session)
        self._notification_repo = NotificationRepository(session)

    async def list_friends(
        self,
        current_user: User,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> PaginatedResponse[FriendItemResponse]:
        """Return paginated list of accepted friends for the authenticated user."""
        friendships, total = await self._friendship_repo.list_accepted_friends(
            current_user.id,
            page=page,
            page_size=page_size,
        )
        items = [
            FriendItemResponse(
                friendship_id=friendship.id,
                since=friendship.updated_at,
                user=FriendUserSummary.model_validate(
                    self._other_user(friendship, current_user.id)
                ),
            )
            for friendship in friendships
        ]
        return PaginatedResponse.create(items=items, page=page, page_size=page_size, total=total)

    async def list_requests(self, current_user: User) -> FriendRequestsResponse:
        """Return pending friend requests split into received and sent lists."""
        received_rows, sent_rows = await self._friendship_repo.list_pending_requests(
            current_user.id
        )
        received = [
            FriendRequestItemResponse(
                friendship_id=row.id,
                direction="received",
                created_at=row.created_at,
                user=FriendUserSummary.model_validate(row.requester),
            )
            for row in received_rows
        ]
        sent = [
            FriendRequestItemResponse(
                friendship_id=row.id,
                direction="sent",
                created_at=row.created_at,
                user=FriendUserSummary.model_validate(row.addressee),
            )
            for row in sent_rows
        ]
        return FriendRequestsResponse(received=received, sent=sent)

    async def send_request(
        self,
        target_user_id: UUID,
        current_user: User,
    ) -> FriendshipActionResponse:
        """Send a friend request, or auto-accept if the target already requested the user."""
        if target_user_id == current_user.id:
            raise AppError(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="BAD_REQUEST",
                message="Você não pode enviar um pedido de amizade para si mesmo.",
            )

        target_user = await self._user_repo.get_by_id(target_user_id)
        if target_user is None or not target_user.is_active:
            raise AppError(
                status_code=status.HTTP_404_NOT_FOUND,
                code="RESOURCE_NOT_FOUND",
                message="O usuário solicitado não foi encontrado.",
            )

        existing = await self._friendship_repo.get_between_users(current_user.id, target_user.id)
        if existing is not None:
            return await self._handle_existing_on_request(
                existing,
                current_user=current_user,
                target_user=target_user,
            )

        friendship = await self._friendship_repo.create(
            requester_id=current_user.id,
            addressee_id=target_user.id,
            status="pending",
        )
        await self._notification_repo.create(
            user_id=target_user.id,
            actor_id=current_user.id,
            notification_type="friend_request",
            entity_id=friendship.id,
        )
        await self._session.commit()

        return FriendshipActionResponse(
            friendship_id=friendship.id,
            status="pending",
            auto_accepted=False,
            message="Pedido de amizade enviado com sucesso.",
            user=FriendUserSummary.model_validate(target_user),
        )

    async def accept_request(
        self,
        friendship_id: UUID,
        current_user: User,
    ) -> FriendshipActionResponse:
        """Accept a pending friend request (addressee only) and notify requester."""
        friendship = await self._friendship_repo.get_by_id(friendship_id)
        if friendship is None:
            raise AppError(
                status_code=status.HTTP_404_NOT_FOUND,
                code="RESOURCE_NOT_FOUND",
                message="Pedido de amizade não encontrado.",
            )
        if friendship.addressee_id != current_user.id:
            raise AppError(
                status_code=status.HTTP_403_FORBIDDEN,
                code="FORBIDDEN",
                message="Apenas o destinatário pode aceitar este pedido de amizade.",
            )
        if friendship.status != "pending":
            raise AppError(
                status_code=status.HTTP_409_CONFLICT,
                code="CONFLICT",
                message="Este pedido de amizade já foi aceito.",
            )

        friendship.status = "accepted"
        await self._friendship_repo.save(friendship)
        await self._notification_repo.mark_entity_notifications_read(
            entity_id=friendship.id,
            user_id=current_user.id,
            notification_type="friend_request",
        )
        await self._notification_repo.create(
            user_id=friendship.requester_id,
            actor_id=current_user.id,
            notification_type="friend_accepted",
            entity_id=friendship.id,
        )
        await self._session.commit()

        return FriendshipActionResponse(
            friendship_id=friendship.id,
            status="accepted",
            auto_accepted=False,
            message="Pedido de amizade aceito com sucesso.",
            user=FriendUserSummary.model_validate(friendship.requester),
        )

    async def remove_or_cancel(
        self,
        friendship_id: UUID,
        current_user: User,
    ) -> None:
        """Decline received request, cancel sent request, or remove existing friendship."""
        friendship = await self._friendship_repo.get_by_id(friendship_id)
        if friendship is None:
            raise AppError(
                status_code=status.HTTP_404_NOT_FOUND,
                code="RESOURCE_NOT_FOUND",
                message="Amizade ou pedido não encontrado.",
            )
        if current_user.id not in (friendship.requester_id, friendship.addressee_id):
            raise AppError(
                status_code=status.HTTP_403_FORBIDDEN,
                code="FORBIDDEN",
                message="Você não tem permissão para remover esta amizade ou pedido.",
            )

        await self._notification_repo.mark_entity_notifications_read(
            entity_id=friendship.id,
            notification_type="friend_request",
        )
        await self._friendship_repo.delete(friendship)
        await self._session.commit()

    async def _handle_existing_on_request(
        self,
        existing: Friendship,
        *,
        current_user: User,
        target_user: User,
    ) -> FriendshipActionResponse:
        """Reject duplicate/existing friendship or auto-accept reverse pending request."""
        if existing.status == "accepted":
            raise AppError(
                status_code=status.HTTP_409_CONFLICT,
                code="CONFLICT",
                message="Vocês já são amigos.",
            )
        if existing.requester_id == current_user.id:
            raise AppError(
                status_code=status.HTTP_409_CONFLICT,
                code="CONFLICT",
                message="Você já enviou um pedido de amizade para este usuário.",
            )

        # Target user already sent a pending request -> auto-accept!
        existing.status = "accepted"
        await self._friendship_repo.save(existing)
        await self._notification_repo.mark_entity_notifications_read(
            entity_id=existing.id,
            user_id=current_user.id,
            notification_type="friend_request",
        )
        await self._notification_repo.create(
            user_id=target_user.id,
            actor_id=current_user.id,
            notification_type="friend_accepted",
            entity_id=existing.id,
        )
        await self._session.commit()

        return FriendshipActionResponse(
            friendship_id=existing.id,
            status="accepted",
            auto_accepted=True,
            message="Pedido de amizade aceito automaticamente! Vocês agora são amigos.",
            user=FriendUserSummary.model_validate(target_user),
        )

    @staticmethod
    def _other_user(friendship: Friendship, current_user_id: UUID) -> User:
        return (
            friendship.addressee
            if friendship.requester_id == current_user_id
            else friendship.requester
        )
