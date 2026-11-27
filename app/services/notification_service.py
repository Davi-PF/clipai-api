"""Business logic service for user notification inbox."""

from uuid import UUID

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError
from app.models.user import User
from app.repositories.notification_repository import NotificationRepository
from app.schemas.notification import (
    MarkAllNotificationsReadResponse,
    NotificationItemResponse,
    NotificationListResponse,
)


class NotificationService:
    """Orchestrates listing and read-state transitions for user notifications."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._notification_repo = NotificationRepository(session)

    async def list_notifications(
        self,
        current_user: User,
        *,
        unread_only: bool = False,
        page: int = 1,
        page_size: int = 20,
    ) -> NotificationListResponse:
        """Return paginated notifications with actor data and unread_count."""
        notifications, total, unread_count = await self._notification_repo.list_for_user(
            current_user.id,
            unread_only=unread_only,
            page=page,
            page_size=page_size,
        )
        return NotificationListResponse.create(
            items=[NotificationItemResponse.model_validate(item) for item in notifications],
            page=page,
            page_size=page_size,
            total=total,
            unread_count=unread_count,
        )

    async def mark_as_read(
        self,
        notification_id: UUID,
        current_user: User,
    ) -> NotificationItemResponse:
        """Mark a single notification owned by the current user as read."""
        notification = await self._notification_repo.get_by_id(notification_id)
        if notification is None:
            raise AppError(
                status_code=status.HTTP_404_NOT_FOUND,
                code="RESOURCE_NOT_FOUND",
                message="A notificação solicitada não foi encontrada.",
            )
        if notification.user_id != current_user.id:
            raise AppError(
                status_code=status.HTTP_403_FORBIDDEN,
                code="FORBIDDEN",
                message="Você não tem permissão para alterar esta notificação.",
            )

        if not notification.is_read:
            await self._notification_repo.mark_as_read(notification)
            await self._session.commit()

        return NotificationItemResponse.model_validate(notification)

    async def mark_all_as_read(
        self,
        current_user: User,
    ) -> MarkAllNotificationsReadResponse:
        """Mark all unread notifications belonging to the current user as read."""
        updated_count = await self._notification_repo.mark_all_as_read(current_user.id)
        await self._session.commit()
        return MarkAllNotificationsReadResponse(
            updated_count=updated_count,
            message="Todas as notificações foram marcadas como lidas.",
        )
