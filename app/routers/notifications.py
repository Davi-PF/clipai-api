"""Notifications HTTP router (/api/v1/notifications)."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.notification import (
    MarkAllNotificationsReadResponse,
    NotificationItemResponse,
    NotificationListResponse,
)
from app.services.notification_service import NotificationService

router = APIRouter(prefix="/notifications", tags=["Notifications"])


def get_notification_service(
    session: AsyncSession = Depends(get_db),
) -> NotificationService:
    """Provide NotificationService dependency."""
    return NotificationService(session=session)


@router.get(
    "",
    response_model=NotificationListResponse,
    summary="Lista paginada de notificações do usuário autenticado com contagem de não lidas",
)
async def list_notifications(
    unread_only: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    notification_service: NotificationService = Depends(get_notification_service),
) -> NotificationListResponse:
    """List paginated notifications and unread counter for the authenticated user."""
    return await notification_service.list_notifications(
        current_user,
        unread_only=unread_only,
        page=page,
        page_size=page_size,
    )


@router.patch(
    "/{notification_id}/read",
    response_model=NotificationItemResponse,
    summary="Marca uma notificação específica como lida",
)
async def mark_notification_as_read(
    notification_id: UUID,
    current_user: User = Depends(get_current_user),
    notification_service: NotificationService = Depends(get_notification_service),
) -> NotificationItemResponse:
    """Mark a single notification as read (recipient only)."""
    return await notification_service.mark_as_read(notification_id, current_user)


@router.post(
    "/read-all",
    response_model=MarkAllNotificationsReadResponse,
    summary="Marca todas as notificações do usuário autenticado como lidas",
)
async def mark_all_notifications_as_read(
    current_user: User = Depends(get_current_user),
    notification_service: NotificationService = Depends(get_notification_service),
) -> MarkAllNotificationsReadResponse:
    """Mark all unread notifications of the authenticated user as read."""
    return await notification_service.mark_all_as_read(current_user)
