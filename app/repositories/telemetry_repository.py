"""SQLAlchemy 2.0 async repository for ClipWatchSession persistence and analytics queries."""

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.telemetry import ClipWatchSession


@dataclass(frozen=True, slots=True)
class ClipSessionAggregate:
    """SQL-aggregated watch session counters for a single clip."""

    clip_id: UUID
    total_sessions: int
    total_watch_seconds: float
    sum_max_progress_pct: int
    completed_count: int


@dataclass(frozen=True, slots=True)
class DailyWatchAggregate:
    """SQL-aggregated daily session count and watch duration (UTC day)."""

    day: date
    sessions: int
    total_watch_seconds: float


class TelemetryRepository:
    """Data access layer for clip_watch_sessions."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_clip_and_token(
        self,
        clip_id: UUID,
        session_token: str,
    ) -> ClipWatchSession | None:
        """Fetch an existing watch session by its unique (clip_id, session_token) pair."""
        stmt = select(ClipWatchSession).where(
            ClipWatchSession.clip_id == clip_id,
            ClipWatchSession.session_token == session_token,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def upsert_watch_session(
        self,
        *,
        clip_id: UUID,
        viewer_id: UUID | None,
        session_token: str,
        watched_seconds: float,
        max_progress_pct: int,
        reached_buckets: list[int],
    ) -> ClipWatchSession:
        """Idempotently create or merge a watch session by (clip_id, session_token)."""
        existing = await self.get_by_clip_and_token(clip_id, session_token)
        if existing is not None:
            merged_buckets = sorted(set(existing.reached_buckets or []) | set(reached_buckets))
            merged_watched = max(float(existing.watched_seconds), float(watched_seconds))
            merged_progress = max(int(existing.max_progress_pct), int(max_progress_pct))

            existing.reached_buckets = merged_buckets
            existing.watched_seconds = merged_watched
            existing.max_progress_pct = merged_progress
            existing.completed = merged_progress >= 90
            if existing.viewer_id is None and viewer_id is not None:
                existing.viewer_id = viewer_id

            await self._session.flush()
            return existing

        record = ClipWatchSession(
            clip_id=clip_id,
            viewer_id=viewer_id,
            session_token=session_token,
            watched_seconds=float(watched_seconds),
            max_progress_pct=int(max_progress_pct),
            reached_buckets=sorted(set(reached_buckets)),
            completed=int(max_progress_pct) >= 90,
        )
        self._session.add(record)
        await self._session.flush()
        return record

    async def list_sessions_for_clips(
        self,
        clip_ids: list[UUID],
        *,
        since: datetime | None = None,
    ) -> list[ClipWatchSession]:
        """Batch-fetch watch sessions for a set of clips within an optional time window."""
        if not clip_ids:
            return []
        stmt = select(ClipWatchSession).where(ClipWatchSession.clip_id.in_(clip_ids))
        if since is not None:
            stmt = stmt.where(ClipWatchSession.created_at >= since)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def aggregate_stats_by_clip(
        self,
        clip_ids: list[UUID],
        *,
        since: datetime | None = None,
    ) -> dict[UUID, ClipSessionAggregate]:
        """Aggregate watch sessions per clip directly in PostgreSQL (avoids loading raw rows)."""
        if not clip_ids:
            return {}
        completed_expr = case((ClipWatchSession.completed.is_(True), 1), else_=0)
        stmt = (
            select(
                ClipWatchSession.clip_id,
                func.count(ClipWatchSession.id),
                func.coalesce(func.sum(ClipWatchSession.watched_seconds), 0.0),
                func.coalesce(func.sum(ClipWatchSession.max_progress_pct), 0),
                func.coalesce(func.sum(completed_expr), 0),
            )
            .where(ClipWatchSession.clip_id.in_(clip_ids))
            .group_by(ClipWatchSession.clip_id)
        )
        if since is not None:
            stmt = stmt.where(ClipWatchSession.created_at >= since)

        rows = (await self._session.execute(stmt)).all()
        return {
            clip_id: ClipSessionAggregate(
                clip_id=clip_id,
                total_sessions=int(total_sessions or 0),
                total_watch_seconds=float(total_watch_seconds or 0.0),
                sum_max_progress_pct=int(sum_max_progress_pct or 0),
                completed_count=int(completed_count or 0),
            )
            for (
                clip_id,
                total_sessions,
                total_watch_seconds,
                sum_max_progress_pct,
                completed_count,
            ) in rows
        }

    async def aggregate_daily_watch_series(
        self,
        clip_ids: list[UUID],
        *,
        since: datetime | None = None,
    ) -> list[DailyWatchAggregate]:
        """Aggregate daily session counts and watch time in UTC directly in PostgreSQL."""
        if not clip_ids:
            return []
        day_expr = func.date(func.timezone("UTC", ClipWatchSession.created_at))
        stmt = (
            select(
                day_expr.label("day"),
                func.count(ClipWatchSession.id),
                func.coalesce(func.sum(ClipWatchSession.watched_seconds), 0.0),
            )
            .where(ClipWatchSession.clip_id.in_(clip_ids))
            .group_by(day_expr)
            .order_by(day_expr.asc())
        )
        if since is not None:
            stmt = stmt.where(ClipWatchSession.created_at >= since)

        rows = (await self._session.execute(stmt)).all()
        return [
            DailyWatchAggregate(
                day=day_value.date() if isinstance(day_value, datetime) else day_value,
                sessions=int(sessions_count or 0),
                total_watch_seconds=float(watch_seconds or 0.0),
            )
            for day_value, sessions_count, watch_seconds in rows
        ]
