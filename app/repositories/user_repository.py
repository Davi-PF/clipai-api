"""SQLAlchemy 2.0 async repository for User persistence."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.clip import Clip
from app.models.collection import Collection
from app.models.user import User, generate_invite_code


class UserRepository:
    """Data access layer for the users table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, user_id: UUID) -> User | None:
        """Fetch a user by primary key UUID."""
        stmt = select(User).where(User.id == user_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> User | None:
        """Fetch a user by case-insensitive e-mail address."""
        normalized_email = email.strip().lower()
        stmt = select(User).where(func.lower(User.email) == normalized_email)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_username(self, username: str) -> User | None:
        """Fetch a user by case-insensitive username."""
        normalized_username = username.strip().lower()
        stmt = select(User).where(func.lower(User.username) == normalized_username)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_discord_id(self, discord_id: str) -> User | None:
        """Fetch a user by unique Discord account identifier."""
        stmt = select(User).where(User.discord_id == discord_id.strip())
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_invite_code(self, invite_code: str) -> User | None:
        """Fetch a user by exact unique invite code."""
        cleaned = invite_code.strip()
        if not cleaned:
            return None
        stmt = select(User).where(User.invite_code == cleaned)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def search_users(
        self,
        query: str,
        *,
        exclude_user_id: UUID,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[User], int]:
        """Search active users by username, display_name, or discord_username (case-insensitive)."""
        cleaned = query.strip()
        handle = cleaned.lstrip("@").strip()
        if not cleaned and not handle:
            return [], 0

        patterns = [f"%{cleaned}%"]
        if handle and handle != cleaned:
            patterns.append(f"%{handle}%")

        match_clauses = []
        for pattern in patterns:
            match_clauses.extend(
                [
                    User.username.ilike(pattern),
                    User.display_name.ilike(pattern),
                    User.discord_username.ilike(pattern),
                ]
            )

        filters = (
            User.is_active.is_(True),
            User.id != exclude_user_id,
            or_(*match_clauses),
        )
        count_stmt = select(func.count(User.id)).where(*filters)
        total = int((await self._session.execute(count_stmt)).scalar_one())

        offset = (page - 1) * page_size
        items_stmt = (
            select(User)
            .where(*filters)
            .order_by(func.lower(User.username).asc())
            .offset(offset)
            .limit(page_size)
        )
        items = list((await self._session.execute(items_stmt)).scalars().all())
        return items, total

    async def create(
        self,
        *,
        username: str,
        email: str,
        password_hash: str | None = None,
        display_name: str | None = None,
        is_email_verified: bool = False,
        avatar_source: str = "none",
        discord_id: str | None = None,
        discord_username: str | None = None,
        discord_avatar_url: str | None = None,
        discord_linked_at: datetime | None = None,
        invite_code: str | None = None,
    ) -> User:
        """Persist a new user and flush to assign database defaults."""
        avatar_url = discord_avatar_url if avatar_source == "discord" else None
        user = User(
            username=username.strip(),
            email=email.strip().lower(),
            password_hash=password_hash,
            display_name=display_name.strip() if display_name else None,
            is_email_verified=is_email_verified,
            avatar_source=avatar_source,
            avatar_url=avatar_url,
            discord_id=discord_id,
            discord_username=discord_username,
            discord_avatar_url=discord_avatar_url,
            discord_linked_at=discord_linked_at,
            invite_code=invite_code or generate_invite_code(),
        )
        self._session.add(user)
        await self._session.flush()
        return user

    async def save(self, user: User) -> User:
        """Flush in-memory mutations on a User entity."""
        self._session.add(user)
        await self._session.flush()
        return user

    async def mark_email_verified(self, user: User) -> User:
        """Mark user e-mail as verified."""
        user.is_email_verified = True
        await self._session.flush()
        return user

    async def update_password_hash(self, user: User, password_hash: str) -> User:
        """Update stored Argon2id password hash."""
        user.password_hash = password_hash
        await self._session.flush()
        return user

    async def get_public_stats(
        self,
        user_id: UUID,
        *,
        allowed_collection_privacies: tuple[str, ...] = ("public",),
    ) -> dict[str, int]:
        """Aggregate public ready clips stats and visible profile collections count."""
        clips_stmt = select(
            func.count(Clip.id),
            func.coalesce(func.sum(Clip.views_count), 0),
            func.coalesce(func.sum(Clip.likes_count), 0),
        ).where(
            Clip.user_id == user_id,
            Clip.status == "ready",
            Clip.privacy == "public",
        )
        clips_row = (await self._session.execute(clips_stmt)).one()

        if allowed_collection_privacies:
            collections_stmt = select(func.count(Collection.id)).where(
                Collection.user_id == user_id,
                Collection.show_on_profile.is_(True),
                Collection.privacy.in_(allowed_collection_privacies),
            )
            collections_count = int((await self._session.execute(collections_stmt)).scalar_one())
        else:
            collections_count = 0

        return {
            "clips_count": int(clips_row[0] or 0),
            "total_views": int(clips_row[1] or 0),
            "total_likes": int(clips_row[2] or 0),
            "collections_count": collections_count,
        }
