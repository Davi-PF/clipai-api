"""SQLAlchemy 2.0 async repository for ClipWatchSession persistence and analytics queries."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.telemetry import ClipWatchSession


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
