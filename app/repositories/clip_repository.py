"""SQLAlchemy 2.0 async repository for Clip persistence, interactions, and filtering."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Select, and_, delete, func, literal, or_, select, union_all, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.models.clip import Clip, ClipFavorite, ClipLike, ClipTag, ClipView, Tag
from app.models.collection import CollectionClip
from app.models.game import Game
from app.models.user import User

_PERIOD_DAYS: dict[str, int] = {"7d": 7, "30d": 30}


class ClipRepository:
    """Data access layer for clips, clip_tags, clip_likes, and clip_favorites."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _base_load_options() -> tuple[Any, ...]:
        return (
            joinedload(Clip.author),
            joinedload(Clip.game),
            selectinload(Clip.tags),
        )

    async def get_by_id(self, clip_id: UUID, *, load_relations: bool = True) -> Clip | None:
        """Fetch a clip by UUID, optionally eager-loading author, game, and tags."""
        stmt = select(Clip).where(Clip.id == clip_id)
        if load_relations:
            stmt = stmt.options(*self._base_load_options())
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

    async def like_clip(self, *, user_id: UUID, clip_id: UUID) -> int:
        """Idempotently add a like and atomically increment Clip.likes_count."""
        exists_stmt = select(ClipLike.user_id).where(
            ClipLike.user_id == user_id,
            ClipLike.clip_id == clip_id,
        )
        existing = (await self._session.execute(exists_stmt)).scalar_one_or_none()
        if existing is None:
            self._session.add(ClipLike(user_id=user_id, clip_id=clip_id))
            inc_stmt = (
                update(Clip)
                .where(Clip.id == clip_id)
                .values(likes_count=Clip.likes_count + 1)
                .returning(Clip.likes_count)
            )
            result = await self._session.execute(inc_stmt)
            await self._session.flush()
            return int(result.scalar_one())

        count_stmt = select(Clip.likes_count).where(Clip.id == clip_id)
        return int((await self._session.execute(count_stmt)).scalar_one())

    async def unlike_clip(self, *, user_id: UUID, clip_id: UUID) -> int:
        """Idempotently remove a like and atomically decrement Clip.likes_count."""
        del_stmt = (
            delete(ClipLike)
            .where(ClipLike.user_id == user_id, ClipLike.clip_id == clip_id)
            .returning(ClipLike.clip_id)
        )
        deleted = (await self._session.execute(del_stmt)).scalar_one_or_none()
        if deleted is not None:
            dec_stmt = (
                update(Clip)
                .where(Clip.id == clip_id)
                .values(likes_count=func.greatest(Clip.likes_count - 1, 0))
                .returning(Clip.likes_count)
            )
            result = await self._session.execute(dec_stmt)
            await self._session.flush()
            return int(result.scalar_one())

        count_stmt = select(Clip.likes_count).where(Clip.id == clip_id)
        return int((await self._session.execute(count_stmt)).scalar_one())

    async def favorite_clip(self, *, user_id: UUID, clip_id: UUID) -> int:
        """Idempotently add a favorite and atomically increment Clip.favorites_count."""
        exists_stmt = select(ClipFavorite.user_id).where(
            ClipFavorite.user_id == user_id,
            ClipFavorite.clip_id == clip_id,
        )
        existing = (await self._session.execute(exists_stmt)).scalar_one_or_none()
        if existing is None:
            self._session.add(ClipFavorite(user_id=user_id, clip_id=clip_id))
            inc_stmt = (
                update(Clip)
                .where(Clip.id == clip_id)
                .values(favorites_count=Clip.favorites_count + 1)
                .returning(Clip.favorites_count)
            )
            result = await self._session.execute(inc_stmt)
            await self._session.flush()
            return int(result.scalar_one())

        count_stmt = select(Clip.favorites_count).where(Clip.id == clip_id)
        return int((await self._session.execute(count_stmt)).scalar_one())

    async def unfavorite_clip(self, *, user_id: UUID, clip_id: UUID) -> int:
        """Idempotently remove a favorite and atomically decrement Clip.favorites_count."""
        del_stmt = (
            delete(ClipFavorite)
            .where(ClipFavorite.user_id == user_id, ClipFavorite.clip_id == clip_id)
            .returning(ClipFavorite.clip_id)
        )
        deleted = (await self._session.execute(del_stmt)).scalar_one_or_none()
        if deleted is not None:
            dec_stmt = (
                update(Clip)
                .where(Clip.id == clip_id)
                .values(favorites_count=func.greatest(Clip.favorites_count - 1, 0))
                .returning(Clip.favorites_count)
            )
            result = await self._session.execute(dec_stmt)
            await self._session.flush()
            return int(result.scalar_one())

        count_stmt = select(Clip.favorites_count).where(Clip.id == clip_id)
        return int((await self._session.execute(count_stmt)).scalar_one())

    async def increment_views(self, clip_id: UUID, *, viewer_id: UUID | None = None) -> int:
        """Record a temporal clip view event and atomically increment Clip.views_count."""
        self._session.add(ClipView(clip_id=clip_id, viewer_id=viewer_id))
        stmt = (
            update(Clip)
            .where(Clip.id == clip_id)
            .values(views_count=Clip.views_count + 1)
            .returning(Clip.views_count)
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return int(result.scalar_one())

    async def get_user_liked_clip_ids(
        self,
        user_id: UUID,
        clip_ids: list[UUID],
    ) -> set[UUID]:
        """Batch-fetch clip IDs liked by the given user (avoids N+1 queries)."""
        if not clip_ids:
            return set()
        stmt = select(ClipLike.clip_id).where(
            ClipLike.user_id == user_id,
            ClipLike.clip_id.in_(clip_ids),
        )
        result = await self._session.execute(stmt)
        return set(result.scalars().all())

    async def get_user_favorited_clip_ids(
        self,
        user_id: UUID,
        clip_ids: list[UUID],
    ) -> set[UUID]:
        """Batch-fetch clip IDs favorited by the given user (avoids N+1 queries)."""
        if not clip_ids:
            return set()
        stmt = select(ClipFavorite.clip_id).where(
            ClipFavorite.user_id == user_id,
            ClipFavorite.clip_id.in_(clip_ids),
        )
        result = await self._session.execute(stmt)
        return set(result.scalars().all())

    async def get_user_interaction_clip_ids(
        self,
        user_id: UUID,
        clip_ids: list[UUID],
    ) -> tuple[set[UUID], set[UUID]]:
        """Batch-fetch liked and favorited clip IDs in a single UNION ALL SQL query."""
        if not clip_ids:
            return set(), set()
        likes_stmt = select(
            ClipLike.clip_id.label("clip_id"),
            literal("like").label("kind"),
        ).where(
            ClipLike.user_id == user_id,
            ClipLike.clip_id.in_(clip_ids),
        )
        favs_stmt = select(
            ClipFavorite.clip_id.label("clip_id"),
            literal("favorite").label("kind"),
        ).where(
            ClipFavorite.user_id == user_id,
            ClipFavorite.clip_id.in_(clip_ids),
        )
        rows = (await self._session.execute(union_all(likes_stmt, favs_stmt))).all()
        liked_ids: set[UUID] = set()
        favorited_ids: set[UUID] = set()
        for clip_id, kind in rows:
            if kind == "like":
                liked_ids.add(clip_id)
            else:
                favorited_ids.add(clip_id)
        return liked_ids, favorited_ids

    async def list_clips(
        self,
        *,
        q: str | None = None,
        game_id: UUID | None = None,
        game_slug: str | None = None,
        tag_slug: str | None = None,
        author_username: str | None = None,
        mine: bool = False,
        favorited_by_user_id: UUID | None = None,
        collection_id: UUID | None = None,
        current_user_id: UUID | None = None,
        sort: str = "recent",
        period: str = "all",
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Clip], int]:
        """List ready clips applying privacy visibility, search, period, and ranking."""
        stmt: Select[tuple[Clip]] = select(Clip).options(*self._base_load_options())
        count_stmt: Select[tuple[int]] = select(func.count(Clip.id))

        filters = self._build_list_filters(
            q=q,
            game_id=game_id,
            game_slug=game_slug,
            tag_slug=tag_slug,
            author_username=author_username,
            mine=mine,
            favorited_by_user_id=favorited_by_user_id,
            collection_id=collection_id,
            current_user_id=current_user_id,
        )
        if filters:
            stmt = stmt.where(*filters)
            count_stmt = count_stmt.where(*filters)

        cutoff = self._resolve_period_cutoff(period)
        stmt, count_stmt = self._apply_sort_and_period(
            stmt=stmt,
            count_stmt=count_stmt,
            sort=sort,
            cutoff=cutoff,
        )

        total_result = await self._session.execute(count_stmt)
        total = int(total_result.scalar_one())

        offset = (page - 1) * page_size
        items_result = await self._session.execute(stmt.offset(offset).limit(page_size))
        return list(items_result.scalars().all()), total

    async def get_top_highlight_clip_id(
        self,
        *,
        current_user_id: UUID | None = None,
        period: str = "all",
    ) -> UUID | None:
        """Return the ID of the #1 ranked highlight clip visible to the requester."""
        stmt: Select[tuple[UUID]] = select(Clip.id)
        visibility_filters = self._build_visibility_filters(
            mine=False,
            favorited_by_user_id=None,
            collection_id=None,
            current_user_id=current_user_id,
        )
        stmt = stmt.where(*visibility_filters)
        cutoff = self._resolve_period_cutoff(period)
        stmt, _ = self._apply_featured_ranking(stmt=stmt, count_stmt=None, cutoff=cutoff)
        result = await self._session.execute(stmt.limit(1))
        return result.scalar_one_or_none()

    @staticmethod
    def _resolve_period_cutoff(period: str) -> datetime | None:
        days = _PERIOD_DAYS.get(period)
        if days is None:
            return None
        return datetime.now(UTC) - timedelta(days=days)

    @classmethod
    def _apply_sort_and_period(
        cls,
        *,
        stmt: Select[tuple[Clip]],
        count_stmt: Select[tuple[int]],
        sort: str,
        cutoff: datetime | None,
    ) -> tuple[Select[tuple[Clip]], Select[tuple[int]]]:
        if sort == "featured":
            ranked_stmt, ranked_count = cls._apply_featured_ranking(
                stmt=stmt,
                count_stmt=count_stmt,
                cutoff=cutoff,
            )
            return ranked_stmt, (ranked_count if ranked_count is not None else count_stmt)

        if sort == "most_viewed":
            return cls._apply_most_viewed_ranking(
                stmt=stmt,
                count_stmt=count_stmt,
                cutoff=cutoff,
            )

        if cutoff is not None:
            stmt = stmt.where(Clip.created_at >= cutoff)
            count_stmt = count_stmt.where(Clip.created_at >= cutoff)

        if sort == "popular":
            engagement = Clip.likes_count * 3 + Clip.favorites_count * 2 + Clip.views_count
            stmt = stmt.order_by(
                engagement.desc(),
                Clip.likes_count.desc(),
                Clip.views_count.desc(),
                Clip.created_at.desc(),
            )
        else:
            stmt = stmt.order_by(Clip.created_at.desc())

        return stmt, count_stmt

    @staticmethod
    def _apply_featured_ranking(
        *,
        stmt: Select[Any],
        count_stmt: Select[tuple[int]] | None,
        cutoff: datetime | None,
    ) -> tuple[Select[Any], Select[tuple[int]] | None]:
        """Rank clips by favorites * 2 + likes, requiring positive engagement."""
        if cutoff is None:
            eligibility = or_(Clip.favorites_count > 0, Clip.likes_count > 0)
            score = Clip.favorites_count * 2 + Clip.likes_count
            stmt = stmt.where(eligibility).order_by(
                score.desc(),
                Clip.favorites_count.desc(),
                Clip.likes_count.desc(),
                Clip.views_count.desc(),
                Clip.created_at.desc(),
            )
            if count_stmt is not None:
                count_stmt = count_stmt.where(eligibility)
            return stmt, count_stmt

        likes_sq = (
            select(ClipLike.clip_id.label("clip_id"), func.count().label("period_likes"))
            .where(ClipLike.created_at >= cutoff)
            .group_by(ClipLike.clip_id)
            .subquery()
        )
        favs_sq = (
            select(ClipFavorite.clip_id.label("clip_id"), func.count().label("period_favorites"))
            .where(ClipFavorite.created_at >= cutoff)
            .group_by(ClipFavorite.clip_id)
            .subquery()
        )
        period_likes = func.coalesce(likes_sq.c.period_likes, 0)
        period_favs = func.coalesce(favs_sq.c.period_favorites, 0)
        eligibility = (period_favs + period_likes) > 0
        score = period_favs * 2 + period_likes

        stmt = (
            stmt.outerjoin(likes_sq, Clip.id == likes_sq.c.clip_id)
            .outerjoin(favs_sq, Clip.id == favs_sq.c.clip_id)
            .where(eligibility)
            .order_by(
                score.desc(),
                period_favs.desc(),
                period_likes.desc(),
                Clip.views_count.desc(),
                Clip.created_at.desc(),
            )
        )
        if count_stmt is not None:
            count_stmt = (
                count_stmt.outerjoin(likes_sq, Clip.id == likes_sq.c.clip_id)
                .outerjoin(favs_sq, Clip.id == favs_sq.c.clip_id)
                .where(eligibility)
            )
        return stmt, count_stmt

    @staticmethod
    def _apply_most_viewed_ranking(
        *,
        stmt: Select[tuple[Clip]],
        count_stmt: Select[tuple[int]],
        cutoff: datetime | None,
    ) -> tuple[Select[tuple[Clip]], Select[tuple[int]]]:
        """Rank clips by views (all-time or within temporal window), requiring views > 0."""
        if cutoff is None:
            eligibility = Clip.views_count > 0
            stmt = stmt.where(eligibility).order_by(
                Clip.views_count.desc(),
                Clip.favorites_count.desc(),
                Clip.likes_count.desc(),
                Clip.created_at.desc(),
            )
            count_stmt = count_stmt.where(eligibility)
            return stmt, count_stmt

        views_sq = (
            select(ClipView.clip_id.label("clip_id"), func.count().label("period_views"))
            .where(ClipView.created_at >= cutoff)
            .group_by(ClipView.clip_id)
            .subquery()
        )
        period_views = func.coalesce(views_sq.c.period_views, 0)
        eligibility = period_views > 0

        stmt = (
            stmt.outerjoin(views_sq, Clip.id == views_sq.c.clip_id)
            .where(eligibility)
            .order_by(
                period_views.desc(),
                Clip.favorites_count.desc(),
                Clip.likes_count.desc(),
                Clip.created_at.desc(),
            )
        )
        count_stmt = count_stmt.outerjoin(views_sq, Clip.id == views_sq.c.clip_id).where(
            eligibility
        )
        return stmt, count_stmt

    async def list_creator_clips(
        self,
        user_id: UUID,
        *,
        q: str | None = None,
        privacy: str | None = None,
        game_id: UUID | None = None,
    ) -> list[Clip]:
        """Fetch all ready clips owned by a creator with game eager-loaded (no N+1)."""
        stmt = (
            select(Clip)
            .options(joinedload(Clip.game))
            .where(Clip.user_id == user_id, Clip.status == "ready")
        )
        if q and q.strip():
            stmt = stmt.where(Clip.title.ilike(f"%{q.strip()}%"))
        if privacy:
            stmt = stmt.where(Clip.privacy == privacy)
        if game_id is not None:
            stmt = stmt.where(Clip.game_id == game_id)

        stmt = stmt.order_by(Clip.created_at.desc())
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    def _build_list_filters(
        cls,
        *,
        q: str | None,
        game_id: UUID | None,
        game_slug: str | None,
        tag_slug: str | None,
        author_username: str | None,
        mine: bool,
        favorited_by_user_id: UUID | None,
        collection_id: UUID | None,
        current_user_id: UUID | None,
    ) -> list[Any]:
        """Construct SQLAlchemy filter clauses for clip listing."""
        filters = cls._build_visibility_filters(
            mine=mine,
            favorited_by_user_id=favorited_by_user_id,
            collection_id=collection_id,
            current_user_id=current_user_id,
        )
        filters.extend(
            cls._build_metadata_filters(
                q=q,
                game_id=game_id,
                game_slug=game_slug,
                tag_slug=tag_slug,
                author_username=author_username,
            )
        )
        return filters

    @staticmethod
    def _build_visibility_filters(
        *,
        mine: bool,
        favorited_by_user_id: UUID | None,
        collection_id: UUID | None,
        current_user_id: UUID | None,
    ) -> list[Any]:
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

        if favorited_by_user_id is not None:
            fav_subq = select(ClipFavorite.clip_id).where(
                ClipFavorite.user_id == favorited_by_user_id
            )
            filters.append(Clip.id.in_(fav_subq))

        if collection_id is not None:
            coll_subq = select(CollectionClip.clip_id).where(
                CollectionClip.collection_id == collection_id
            )
            filters.append(Clip.id.in_(coll_subq))
        return filters

    @staticmethod
    def _build_metadata_filters(
        *,
        q: str | None,
        game_id: UUID | None,
        game_slug: str | None,
        tag_slug: str | None,
        author_username: str | None,
    ) -> list[Any]:
        filters: list[Any] = []
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
