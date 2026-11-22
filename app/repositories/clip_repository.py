"""SQLAlchemy 2.0 async repository for Clip persistence, search, and filtering."""

from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Select, and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.clip import Clip, ClipTag, Tag
from app.models.game import Game
from app.models.user import User


class ClipRepository:
    """Data access layer for the clips and clip_tags tables."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _base_load_options() -> tuple[Any, ...]:
        return (
            selectinload(Clip.author),
            selectinload(Clip.game),
            selectinload(Clip.tags),
        )

    async def get_by_id(self, clip_id: UUID) -> Clip | None:
        """Fetch a clip by UUID with author, game, and tags loaded."""
        stmt = select(Clip).options(*self._base_load_options()).where(Clip.id == clip_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_slug(self, slug: str) -> Clip | None:
        """Fetch a clip by URL slug with author, game, and tags loaded."""
        stmt = select(Clip).options(*self._base_load_options()).where(Clip.slug == slug)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_identifier(self, identifier: str) -> Clip | None:
        """Resolve a clip either by UUID string or URL slug."""
        try:
            clip_id = UUID(identifier)
        except ValueError:
            return await self.get_by_slug(identifier)
        return await self.get_by_id(clip_id)

    async def count_pending_by_user(self, user_id: UUID) -> int:
        """Count pending_upload clips owned by the given user."""
        stmt = select(func.count(Clip.id)).where(
            Clip.user_id == user_id,
            Clip.status == "pending_upload",
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def create(
        self,
        *,
        clip_id: UUID,
        slug: str,
        user_id: UUID,
        game_id: UUID | None,
        title: str,
        description: str | None,
        privacy: str,
        video_object_key: str,
        thumbnail_object_key: str | None,
        file_size_bytes: int,
        duration_seconds: Decimal,
        mime_type: str,
        technical_metadata: dict[str, Any],
        tags: list[Tag],
    ) -> Clip:
        """Insert a new pending_upload clip and associate normalized tags."""
        clip = Clip(
            id=clip_id,
            slug=slug,
            user_id=user_id,
            game_id=game_id,
            title=title,
            description=description,
            status="pending_upload",
            privacy=privacy,
            video_object_key=video_object_key,
            thumbnail_object_key=thumbnail_object_key,
            file_size_bytes=file_size_bytes,
            duration_seconds=duration_seconds,
            mime_type=mime_type,
            technical_metadata=technical_metadata,
        )
        self._session.add(clip)
        await self._session.flush()

        for tag in tags:
            self._session.add(ClipTag(clip_id=clip.id, tag_id=tag.id))
        await self._session.flush()
        return clip

    async def replace_tags(self, clip: Clip, tags: list[Tag]) -> None:
        """Replace clip tag associations atomically within the active transaction."""
        await self._session.execute(delete(ClipTag).where(ClipTag.clip_id == clip.id))
        for tag in tags:
            self._session.add(ClipTag(clip_id=clip.id, tag_id=tag.id))
        await self._session.flush()

    async def delete(self, clip: Clip) -> None:
        """Delete a clip record from PostgreSQL."""
        await self._session.delete(clip)
        await self._session.flush()

    async def list_clips(
        self,
        *,
        q: str | None = None,
        game_id: UUID | None = None,
        game_slug: str | None = None,
        tag_slug: str | None = None,
        author_username: str | None = None,
        mine: bool = False,
        current_user_id: UUID | None = None,
        sort: str = "recent",
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Clip], int]:
        """List ready clips applying privacy visibility, search, and filters."""
        stmt: Select[tuple[Clip]] = select(Clip).options(*self._base_load_options())
        count_stmt: Select[tuple[int]] = select(func.count(Clip.id))

        filters = self._build_list_filters(
            q=q,
            game_id=game_id,
            game_slug=game_slug,
            tag_slug=tag_slug,
            author_username=author_username,
            mine=mine,
            current_user_id=current_user_id,
        )
        if filters:
            stmt = stmt.where(*filters)
            count_stmt = count_stmt.where(*filters)

        total_result = await self._session.execute(count_stmt)
        total = int(total_result.scalar_one())

        if sort == "popular":
            stmt = stmt.order_by(
                Clip.likes_count.desc(),
                Clip.views_count.desc(),
                Clip.created_at.desc(),
            )
        else:
            stmt = stmt.order_by(Clip.created_at.desc())

        offset = (page - 1) * page_size
        items_result = await self._session.execute(stmt.offset(offset).limit(page_size))
        return list(items_result.scalars().all()), total

    @staticmethod
    def _build_list_filters(
        *,
        q: str | None,
        game_id: UUID | None,
        game_slug: str | None,
        tag_slug: str | None,
        author_username: str | None,
        mine: bool,
        current_user_id: UUID | None,
    ) -> list[Any]:
        """Construct SQLAlchemy filter clauses for clip listing."""
        filters: list[Any] = [Clip.status == "ready"]

        if mine and current_user_id is not None:
            filters.append(Clip.user_id == current_user_id)
        elif current_user_id is None:
            filters.append(Clip.privacy == "public")
        else:
            filters.append(
                or_(
                    Clip.privacy.in_(["public", "friends"]),
                    and_(Clip.privacy == "unlisted", Clip.user_id == current_user_id),
                )
            )

        if q and q.strip():
            pattern = f"%{q.strip()}%"
            filters.append(
                or_(
                    Clip.title.ilike(pattern),
                    Clip.description.ilike(pattern),
                    Clip.game.has(Game.name.ilike(pattern)),
                )
            )
        if game_id is not None:
            filters.append(Clip.game_id == game_id)
        if game_slug and game_slug.strip():
            filters.append(Clip.game.has(Game.slug == game_slug.strip().lower()))
        if tag_slug and tag_slug.strip():
            filters.append(Clip.tags.any(Tag.slug == tag_slug.strip().lower()))
        if author_username and author_username.strip():
            filters.append(
                Clip.author.has(func.lower(User.username) == author_username.strip().lower())
            )

        return filters
