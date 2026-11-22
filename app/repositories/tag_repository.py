"""SQLAlchemy 2.0 async repository for Tag queries and normalization."""

import re
import unicodedata

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.clip import ClipTag, Tag

_NON_ALNUM_PATTERN = re.compile(r"[^a-z0-9]+")


def normalize_tag_slug(raw_tag: str) -> str:
    """Normalize arbitrary tag text into a lowercase URL-safe slug (max 30 chars)."""
    ascii_text = (
        unicodedata.normalize("NFKD", raw_tag.strip().lower())
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    slug = _NON_ALNUM_PATTERN.sub("-", ascii_text).strip("-")
    return slug[:30].strip("-")


class TagRepository:
    """Data access layer for the tags table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_tags(self, *, q: str | None = None, limit: int = 20) -> list[Tag]:
        """List existing tags ordered by clip usage count and name."""
        usage_count = func.count(ClipTag.clip_id)
        stmt = (
            select(Tag)
            .outerjoin(ClipTag, ClipTag.tag_id == Tag.id)
            .group_by(Tag.id)
            .order_by(usage_count.desc(), Tag.name.asc())
            .limit(limit)
        )
        if q and q.strip():
            pattern = f"%{q.strip().lower()}%"
            stmt = stmt.where(func.lower(Tag.name).like(pattern) | Tag.slug.like(pattern))

        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_or_create_many(self, raw_tags: list[str]) -> list[Tag]:
        """Normalize raw tag labels and return persisted Tag instances."""
        unique_slugs: list[str] = []
        seen: set[str] = set()
        for raw_tag in raw_tags:
            slug = normalize_tag_slug(raw_tag)
            if slug and slug not in seen:
                seen.add(slug)
                unique_slugs.append(slug)

        if not unique_slugs:
            return []

        stmt = select(Tag).where(Tag.slug.in_(unique_slugs))
        existing_result = await self._session.execute(stmt)
        by_slug: dict[str, Tag] = {tag.slug: tag for tag in existing_result.scalars().all()}

        ordered_tags: list[Tag] = []
        for slug in unique_slugs:
            tag = by_slug.get(slug)
            if tag is None:
                tag = Tag(name=slug, slug=slug)
                self._session.add(tag)
                by_slug[slug] = tag
            ordered_tags.append(tag)

        await self._session.flush()
        return ordered_tags
