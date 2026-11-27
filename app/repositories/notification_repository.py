"""SQLAlchemy 2.0 async repository for user notifications."""

from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.notification import Notification


class NotificationRepository:
    """Data access layer for the notifications table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        user_id: UUID,
        actor_id: UUID,
        notification_type: str,
        entity_id: UUID | None = None,
    ) -> Notification:
        """Create a new notification and flush to assign database defaults."""
        notification = Notification(
            user_id=user_id,
            actor_id=actor_id,
            type=notification_type,
            entity_id=entity_id,
            is_read=False,
        )
        self._session.add(notification)
        await self._session.flush()
        return notification

    async def get_by_id(self, notification_id: UUID) -> Notification | None:
        """Fetch a notification by UUID with its actor eager-loaded."""
        stmt = (
            select(Notification)
            .options(selectinload(Notification.actor))
            .where(Notification.id == notification_id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_for_user(
        self,
        user_id: UUID,
        *,
        unread_only: bool = False,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Notification], int, int]:
        """List paginated notifications for a user along with total and unread_count."""
        filters: list[Any] = [Notification.user_id == user_id]
        if unread_only:
            filters.append(Notification.is_read.is_(False))

        total_stmt = select(func.count(Notification.id)).where(*filters)
        total = int((await self._session.execute(total_stmt)).scalar_one())

        if unread_only:
            unread_count = total
        else:
            unread_stmt = select(func.count(Notification.id)).where(
                Notification.user_id == user_id,
                Notification.is_read.is_(False),
            )
            unread_count = int((await self._session.execute(unread_stmt)).scalar_one())

        offset = (page - 1) * page_size
        items_stmt = (
            select(Notification)
            .options(selectinload(Notification.actor))
            .where(*filters)
            .order_by(Notification.created_at.desc())
            .offset(offset)
            .limit(page_size)
        )
        items = list((await self._session.execute(items_stmt)).scalars().all())
        return items, total, unread_count

    async def mark_as_read(self, notification: Notification) -> Notification:
        """Mark a single notification as read and flush."""
        notification.is_read = True
        self._session.add(notification)
        await self._session.flush()
        return notification

    async def mark_all_as_read(self, user_id: UUID) -> int:
        """Mark all unread notifications for a user as read in a single query."""
        stmt = (
            update(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.is_read.is_(False),
            )
            .values(is_read=True)
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return int(getattr(result, "rowcount", 0) or 0)

    async def mark_entity_notifications_read(
        self,
        *,
        entity_id: UUID,
        user_id: UUID | None = None,
        notification_type: str | None = None,
    ) -> int:
        """Mark unread notifications linked to an entity (e.g. friendship) as read."""
        filters: list[Any] = [
            Notification.entity_id == entity_id,
            Notification.is_read.is_(False),
        ]
        if user_id is not None:
            filters.append(Notification.user_id == user_id)
        if notification_type is not None:
            filters.append(Notification.type == notification_type)

        stmt = update(Notification).where(*filters).values(is_read=True)
        result = await self._session.execute(stmt)
        await self._session.flush()
        return int(getattr(result, "rowcount", 0) or 0)
