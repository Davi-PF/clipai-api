"""SQLAlchemy 2.0 async repository for Game queries."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.game import Game


class GameRepository:
    """Data access layer for the games table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, game_id: UUID) -> Game | None:
        """Fetch a game by primary key UUID."""
        stmt = select(Game).where(Game.id == game_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_slug(self, slug: str) -> Game | None:
        """Fetch a game by normalized slug."""
        stmt = select(Game).where(Game.slug == slug.strip().lower())
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_games(
        self,
        *,
        q: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Game], int]:
        """List games paginated with optional name search."""
        base_stmt = select(Game)
        count_stmt = select(func.count(Game.id))

        if q and q.strip():
            pattern = f"%{q.strip()}%"
            base_stmt = base_stmt.where(Game.name.ilike(pattern))
            count_stmt = count_stmt.where(Game.name.ilike(pattern))

        total_result = await self._session.execute(count_stmt)
        total = int(total_result.scalar_one())

        offset = (page - 1) * page_size
        items_stmt = base_stmt.order_by(Game.name.asc()).offset(offset).limit(page_size)
        items_result = await self._session.execute(items_stmt)
        return list(items_result.scalars().all()), total
