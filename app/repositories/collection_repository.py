"""SQLAlchemy 2.0 async repository for Collection and CollectionClip persistence."""

from collections import defaultdict
from typing import Any
from uuid import UUID

from sqlalchemy import Select, and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import contains_eager, joinedload

from app.models.clip import Clip
from app.models.collection import Collection, CollectionClip
from app.models.user import User


class CollectionRepository:
    """Data access layer for collections and collection_clips tables."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, collection_id: UUID) -> Collection | None:
        """Fetch a collection by UUID with owner joined in a single query."""
        stmt = (
            select(Collection)
            .options(joinedload(Collection.owner))
            .where(Collection.id == collection_id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        user_id: UUID,
        title: str,
        description: str | None,
        privacy: str,
        accent_color: str | None,
        show_on_profile: bool = False,
    ) -> Collection:
        """Create a new collection and flush to assign database defaults."""
        collection = Collection(
            user_id=user_id,
            title=title,
            description=description,
            privacy=privacy,
            show_on_profile=show_on_profile,
            accent_color=accent_color,
        )
        self._session.add(collection)
        await self._session.flush()
        return collection

    async def delete(self, collection: Collection) -> None:
        """Delete a collection (cascades collection_clips links only)."""
        await self._session.delete(collection)
        await self._session.flush()

    async def list_collections(
        self,
        *,
        mine: bool = False,
        author_username: str | None = None,
        current_user_id: UUID | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Collection], int]:
        """List collections paginated respecting privacy visibility."""
        stmt: Select[tuple[Collection]] = select(Collection).options(joinedload(Collection.owner))
        count_stmt: Select[tuple[int]] = select(func.count(Collection.id))

        filters = self._build_collection_filters(
            mine=mine,
            author_username=author_username,
            current_user_id=current_user_id,
        )
        if filters:
            stmt = stmt.where(*filters)
            count_stmt = count_stmt.where(*filters)

        total = int((await self._session.execute(count_stmt)).scalar_one())
        if total == 0:
            return [], 0
        offset = (page - 1) * page_size
        items_stmt = stmt.order_by(Collection.created_at.desc()).offset(offset).limit(page_size)
        items = list((await self._session.execute(items_stmt)).scalars().all())
        return items, total

    async def list_profile_collections(
        self,
        *,
        owner_id: UUID,
        allowed_privacies: tuple[str, ...],
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Collection], int]:
        """List paginated profile-visible collections for a user respecting Global Ceiling."""
        if not allowed_privacies:
            return [], 0

        filters = (
            Collection.user_id == owner_id,
            Collection.show_on_profile.is_(True),
            Collection.privacy.in_(allowed_privacies),
        )
        count_stmt: Select[tuple[int]] = select(func.count(Collection.id)).where(*filters)
        total = int((await self._session.execute(count_stmt)).scalar_one())
        if total == 0:
            return [], 0

        offset = (page - 1) * page_size
        items_stmt: Select[tuple[Collection]] = (
            select(Collection)
            .options(joinedload(Collection.owner))
            .where(*filters)
            .order_by(Collection.created_at.desc())
            .offset(offset)
            .limit(page_size)
        )
        items = list((await self._session.execute(items_stmt)).scalars().all())
        return items, total

    async def get_collections_preview_metadata(
        self,
        collection_ids: list[UUID],
        *,
        current_user_id: UUID | None,
        preview_limit: int = 4,
    ) -> dict[UUID, tuple[int, list[str]]]:
        """Batch-fetch visible ready clip counts and preview thumbnail keys (zero N+1)."""
        if not collection_ids:
            return {}

        clip_visibility = self._clip_visibility_clause(current_user_id)
        stmt = (
            select(
                CollectionClip.collection_id,
                Clip.thumbnail_object_key,
            )
            .join(Clip, Clip.id == CollectionClip.clip_id)
            .where(
                CollectionClip.collection_id.in_(collection_ids),
                Clip.status == "ready",
                clip_visibility,
            )
            .order_by(CollectionClip.position.asc(), CollectionClip.added_at.asc())
        )
        rows = (await self._session.execute(stmt)).all()

        counts: dict[UUID, int] = defaultdict(int)
        thumbnails: dict[UUID, list[str]] = defaultdict(list)
        for coll_id, thumb_key in rows:
            counts[coll_id] += 1
            if thumb_key and len(thumbnails[coll_id]) < preview_limit:
                thumbnails[coll_id].append(thumb_key)

        return {coll_id: (counts[coll_id], thumbnails[coll_id]) for coll_id in collection_ids}

    async def list_collection_clips(
        self,
        collection_id: UUID,
        *,
        current_user_id: UUID | None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[CollectionClip], int]:
        """List paginated ready clips inside a collection respecting clip privacy."""
        clip_visibility = self._clip_visibility_clause(current_user_id)

        count_stmt = (
            select(func.count(CollectionClip.clip_id))
            .join(Clip, Clip.id == CollectionClip.clip_id)
            .where(
                CollectionClip.collection_id == collection_id,
                Clip.status == "ready",
                clip_visibility,
            )
        )
        total = int((await self._session.execute(count_stmt)).scalar_one())
        if total == 0:
            return [], 0

        offset = (page - 1) * page_size
        items_stmt = (
            select(CollectionClip)
            .join(Clip, Clip.id == CollectionClip.clip_id)
            .options(contains_eager(CollectionClip.clip))
            .where(
                CollectionClip.collection_id == collection_id,
                Clip.status == "ready",
                clip_visibility,
            )
            .order_by(CollectionClip.position.asc(), CollectionClip.added_at.asc())
            .offset(offset)
            .limit(page_size)
        )
        items = list((await self._session.execute(items_stmt)).scalars().all())
        return items, total

    async def get_clip_link(self, collection_id: UUID, clip_id: UUID) -> CollectionClip | None:
        """Fetch existing link between collection and clip."""
        stmt = select(CollectionClip).where(
            CollectionClip.collection_id == collection_id,
            CollectionClip.clip_id == clip_id,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_next_position(self, collection_id: UUID) -> int:
        """Return next sequential position index for appending a clip."""
        stmt = select(func.coalesce(func.max(CollectionClip.position) + 1, 0)).where(
            CollectionClip.collection_id == collection_id
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def add_clip(
        self,
        *,
        collection_id: UUID,
        clip_id: UUID,
        position: int,
    ) -> CollectionClip:
        """Insert a new collection-clip association."""
        link = CollectionClip(
            collection_id=collection_id,
            clip_id=clip_id,
            position=position,
        )
        self._session.add(link)
        await self._session.flush()
        return link

    async def remove_clip(self, *, collection_id: UUID, clip_id: UUID) -> None:
        """Remove a clip from a collection (idempotent)."""
        stmt = delete(CollectionClip).where(
            CollectionClip.collection_id == collection_id,
            CollectionClip.clip_id == clip_id,
        )
        await self._session.execute(stmt)
        await self._session.flush()

    @staticmethod
    def _build_collection_filters(
        *,
        mine: bool,
        author_username: str | None,
        current_user_id: UUID | None,
    ) -> list[Any]:
        filters: list[Any] = []
        if mine and current_user_id is not None:
            filters.append(Collection.user_id == current_user_id)
        elif current_user_id is None:
            filters.append(Collection.privacy == "public")
            filters.append(Collection.owner.has(User.collections_visibility == "public"))
        else:
            filters.append(
                or_(
                    Collection.user_id == current_user_id,
                    and_(
                        Collection.privacy.in_(["public", "friends"]),
                        Collection.owner.has(
                            User.collections_visibility.in_(["public", "friends"])
                        ),
                    ),
                )
            )

        if author_username and author_username.strip():
            filters.append(
                Collection.owner.has(func.lower(User.username) == author_username.strip().lower())
            )
        return filters

    @staticmethod
    def _clip_visibility_clause(current_user_id: UUID | None) -> Any:
        if current_user_id is None:
            return Clip.privacy.in_(["public", "unlisted"])
        return or_(
            Clip.privacy.in_(["public", "friends", "unlisted"]),
            Clip.user_id == current_user_id,
        )
