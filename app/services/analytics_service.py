"""Business logic service for Creator Studio analytics and clip retention curves."""

from collections import defaultdict
from datetime import datetime
from uuid import UUID

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError
from app.models.clip import Clip
from app.models.telemetry import ClipWatchSession
from app.models.user import User
from app.repositories.clip_repository import ClipRepository
from app.repositories.telemetry_repository import TelemetryRepository
from app.schemas.analytics import (
    AnalyticsOverviewResponse,
    AnalyticsPeriod,
    ClipRetentionResponse,
    CreatorClipAnalyticsItem,
    CreatorClipSort,
    TopClipAnalyticsItem,
)
from app.schemas.clip import ClipPrivacy
from app.schemas.common import PaginatedResponse
from app.services.analytics_metrics import (
    build_game_breakdown,
    build_retention_curve,
    build_views_by_day,
    compute_session_metrics,
    find_biggest_drop_bucket,
    resolve_period_window,
)
from app.services.storage_service import StorageService


class AnalyticsService:
    """Orchestrates creator-scoped overview, clip listing, and retention analytics."""

    def __init__(
        self,
        session: AsyncSession,
        storage_service: StorageService,
    ) -> None:
        self._session = session
        self._storage = storage_service
        self._clip_repo = ClipRepository(session)
        self._telemetry_repo = TelemetryRepository(session)

    async def get_overview(
        self,
        current_user: User,
        *,
        period: AnalyticsPeriod = "30d",
        now: datetime | None = None,
    ) -> AnalyticsOverviewResponse:
        """Compute consolidated creator KPIs, daily sessions, game breakdown, and top 5 clips."""
        since_utc, start_date, end_date = resolve_period_window(period, now=now)
        clips = await self._clip_repo.list_creator_clips(current_user.id)
        sessions = await self._telemetry_repo.list_sessions_for_clips(
            [clip.id for clip in clips],
            since=since_utc,
        )
        sessions_by_clip = self._group_sessions_by_clip(sessions)
        overall_metrics = compute_session_metrics(sessions)

        return AnalyticsOverviewResponse(
            period=period,
            total_clips=len(clips),
            total_views=sum(clip.views_count for clip in clips),
            total_likes=sum(clip.likes_count for clip in clips),
            total_favorites=sum(clip.favorites_count for clip in clips),
            total_watch_time_seconds=overall_metrics.total_watch_time_seconds,
            avg_retention_pct=overall_metrics.avg_retention_pct,
            completion_rate_pct=overall_metrics.completion_rate_pct,
            total_sessions=overall_metrics.total_sessions,
            views_by_day=build_views_by_day(
                sessions,
                start_date=start_date,
                end_date=end_date,
            ),
            by_game=build_game_breakdown(clips, sessions_by_clip),
            top_clips=self._build_top_clips(clips, sessions_by_clip),
        )

    async def list_creator_clips(
        self,
        current_user: User,
        *,
        q: str | None = None,
        privacy: ClipPrivacy | None = None,
        game_id: UUID | None = None,
        period: AnalyticsPeriod = "30d",
        sort: CreatorClipSort = "recent",
        page: int = 1,
        page_size: int = 20,
        now: datetime | None = None,
    ) -> PaginatedResponse[CreatorClipAnalyticsItem]:
        """Return paginated creator clips (all privacies) enriched with period retention metrics."""
        since_utc, _start_date, _end_date = resolve_period_window(period, now=now)
        clips = await self._clip_repo.list_creator_clips(
            current_user.id,
            q=q,
            privacy=privacy,
            game_id=game_id,
        )
        sessions = await self._telemetry_repo.list_sessions_for_clips(
            [clip.id for clip in clips],
            since=since_utc,
        )
        sessions_by_clip = self._group_sessions_by_clip(sessions)

        enriched = [
            self._to_creator_clip_item(clip, sessions_by_clip.get(clip.id, [])) for clip in clips
        ]
        sorted_items = self._sort_creator_clips(enriched, sort=sort)

        total = len(sorted_items)
        offset = (page - 1) * page_size
        return PaginatedResponse.create(
            items=sorted_items[offset : offset + page_size],
            page=page,
            page_size=page_size,
            total=total,
        )

    async def get_clip_retention(
        self,
        clip_id: UUID,
        current_user: User,
        *,
        period: AnalyticsPeriod = "30d",
        now: datetime | None = None,
    ) -> ClipRetentionResponse:
        """Return 0..100% retention curve, drop-off bucket, and daily series for an owned clip."""
        clip = await self._clip_repo.get_by_id(clip_id)
        if clip is None:
            raise AppError(
                status_code=status.HTTP_404_NOT_FOUND,
                code="RESOURCE_NOT_FOUND",
                message="O clipe solicitado não foi encontrado.",
            )
        if clip.user_id != current_user.id:
            raise AppError(
                status_code=status.HTTP_403_FORBIDDEN,
                code="FORBIDDEN",
                message="Você não tem permissão para visualizar as métricas deste clipe.",
            )

        since_utc, start_date, end_date = resolve_period_window(period, now=now)
        sessions = await self._telemetry_repo.list_sessions_for_clips([clip.id], since=since_utc)
        metrics = compute_session_metrics(sessions)
        curve = build_retention_curve(sessions)

        return ClipRetentionResponse(
            clip_id=clip.id,
            slug=clip.slug,
            title=clip.title,
            duration_seconds=float(clip.duration_seconds),
            period=period,
            total_sessions=metrics.total_sessions,
            avg_watch_seconds=metrics.avg_watch_seconds,
            avg_retention_pct=metrics.avg_retention_pct,
            completion_rate_pct=metrics.completion_rate_pct,
            biggest_drop_bucket_pct=find_biggest_drop_bucket(
                curve,
                total_sessions=metrics.total_sessions,
            ),
            views_by_day=build_views_by_day(
                sessions,
                start_date=start_date,
                end_date=end_date,
            ),
            curve=curve,
        )

    @staticmethod
    def _group_sessions_by_clip(
        sessions: list[ClipWatchSession],
    ) -> dict[UUID, list[ClipWatchSession]]:
        grouped: dict[UUID, list[ClipWatchSession]] = defaultdict(list)
        for item in sessions:
            grouped[item.clip_id].append(item)
        return grouped

    def _build_top_clips(
        self,
        clips: list[Clip],
        sessions_by_clip: dict[UUID, list[ClipWatchSession]],
    ) -> list[TopClipAnalyticsItem]:
        ranked: list[tuple[tuple[int, int, int, datetime], TopClipAnalyticsItem]] = []
        for clip in clips:
            clip_sessions = sessions_by_clip.get(clip.id, [])
            metrics = compute_session_metrics(clip_sessions)
            item = TopClipAnalyticsItem(
                id=clip.id,
                slug=clip.slug,
                title=clip.title,
                thumbnail_url=(
                    self._storage.build_public_url(clip.thumbnail_object_key)
                    if clip.thumbnail_object_key
                    else None
                ),
                game_name=clip.game.name if clip.game is not None else None,
                views_count=clip.views_count,
                likes_count=clip.likes_count,
                avg_retention_pct=metrics.avg_retention_pct,
                completion_rate_pct=metrics.completion_rate_pct,
            )
            rank_key = (metrics.total_sessions, clip.views_count, clip.likes_count, clip.created_at)
            ranked.append((rank_key, item))

        ranked.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _key, item in ranked[:5]]

    def _to_creator_clip_item(
        self,
        clip: Clip,
        clip_sessions: list[ClipWatchSession],
    ) -> CreatorClipAnalyticsItem:
        metrics = compute_session_metrics(clip_sessions)
        return CreatorClipAnalyticsItem(
            id=clip.id,
            slug=clip.slug,
            title=clip.title,
            privacy=clip.privacy,  # type: ignore[arg-type]
            status=clip.status,  # type: ignore[arg-type]
            thumbnail_url=(
                self._storage.build_public_url(clip.thumbnail_object_key)
                if clip.thumbnail_object_key
                else None
            ),
            duration_seconds=float(clip.duration_seconds),
            game_id=clip.game_id,
            game_name=clip.game.name if clip.game is not None else None,
            views_count=clip.views_count,
            likes_count=clip.likes_count,
            favorites_count=clip.favorites_count,
            sessions_count=metrics.total_sessions,
            avg_retention_pct=metrics.avg_retention_pct,
            completion_rate_pct=metrics.completion_rate_pct,
            created_at=clip.created_at,
        )

    @staticmethod
    def _sort_creator_clips(
        items: list[CreatorClipAnalyticsItem],
        *,
        sort: CreatorClipSort,
    ) -> list[CreatorClipAnalyticsItem]:
        if sort == "views":
            return sorted(
                items,
                key=lambda item: (item.views_count, item.sessions_count, item.created_at),
                reverse=True,
            )
        if sort == "retention":
            return sorted(
                items,
                key=lambda item: (item.avg_retention_pct, item.sessions_count, item.created_at),
                reverse=True,
            )
        if sort == "likes":
            return sorted(
                items,
                key=lambda item: (item.likes_count, item.views_count, item.created_at),
                reverse=True,
            )
        return sorted(items, key=lambda item: item.created_at, reverse=True)
