"""Friendships HTTP router (/api/v1/friends)."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.common import PaginatedResponse
from app.schemas.friendship import (
    FriendItemResponse,
    FriendRequestsResponse,
    FriendshipActionResponse,
)
from app.services.friendship_service import FriendshipService

router = APIRouter(prefix="/friends", tags=["Friends"])


def get_friendship_service(
    session: AsyncSession = Depends(get_db),
) -> FriendshipService:
    """Provide FriendshipService dependency."""
    return FriendshipService(session=session)


@router.get(
    "",
    response_model=PaginatedResponse[FriendItemResponse],
    summary="Lista paginada de amigos aceitos do usuário autenticado",
)
async def list_friends(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    friendship_service: FriendshipService = Depends(get_friendship_service),
) -> PaginatedResponse[FriendItemResponse]:
    """List accepted friends of the authenticated user."""
    return await friendship_service.list_friends(
        current_user,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/requests",
    response_model=FriendRequestsResponse,
    summary="Lista pedidos de amizade pendentes recebidos e enviados",
)
async def list_friend_requests(
    current_user: User = Depends(get_current_user),
    friendship_service: FriendshipService = Depends(get_friendship_service),
) -> FriendRequestsResponse:
    """List pending received and sent friend requests."""
    return await friendship_service.list_requests(current_user)


@router.post(
    "/request/{user_id}",
    response_model=FriendshipActionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Envia pedido de amizade (ou aceita automaticamente pedido reverso pendente)",
)
async def send_friend_request(
    user_id: UUID,
    current_user: User = Depends(get_current_user),
    friendship_service: FriendshipService = Depends(get_friendship_service),
) -> FriendshipActionResponse:
    """Send a friend request to another user."""
    return await friendship_service.send_request(user_id, current_user)


@router.post(
    "/{friendship_id}/accept",
    response_model=FriendshipActionResponse,
    summary="Aceita um pedido de amizade pendente recebido",
)
async def accept_friend_request(
    friendship_id: UUID,
    current_user: User = Depends(get_current_user),
    friendship_service: FriendshipService = Depends(get_friendship_service),
) -> FriendshipActionResponse:
    """Accept an incoming pending friend request."""
    return await friendship_service.accept_request(friendship_id, current_user)


@router.delete(
    "/{friendship_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Recusa pedido recebido, cancela pedido enviado ou desfaz amizade",
)
async def remove_or_cancel_friendship(
    friendship_id: UUID,
    current_user: User = Depends(get_current_user),
    friendship_service: FriendshipService = Depends(get_friendship_service),
) -> None:
    """Decline, cancel, or remove a friendship record."""
    await friendship_service.remove_or_cancel(friendship_id, current_user)
