"""Business logic service for Creator Studio analytics and clip retention curves."""

from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import UserScopedTTLCache, analytics_cache
from app.core.exceptions import AppError
from app.models.clip import Clip
from app.models.user import User
from app.repositories.clip_repository import ClipRepository
from app.repositories.telemetry_repository import ClipSessionAggregate, TelemetryRepository
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
    build_game_breakdown_from_aggregates,
    build_retention_curve,
    build_views_by_day,
    build_views_by_day_from_aggregates,
    compute_metrics_from_aggregates,
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
        *,
        cache: UserScopedTTLCache | None = None,
        cache_ttl_seconds: float | None = None,
    ) -> None:
        self._session = session
        self._storage = storage_service
        self._clip_repo = ClipRepository(session)
        self._telemetry_repo = TelemetryRepository(session)
        self._cache = cache or analytics_cache
        self._cache_ttl_seconds = cache_ttl_seconds

    async def get_overview(
        self,
        current_user: User,
        *,
        period: AnalyticsPeriod = "30d",
        now: datetime | None = None,
    ) -> AnalyticsOverviewResponse:
        """Compute consolidated creator KPIs, daily sessions, game breakdown, and top 5 clips."""
        if now is not None:
            return await self._compute_overview(current_user, period=period, now=now)

        return await self._cache.get_or_set(
            current_user.id,
            "analytics:overview",
            lambda: self._compute_overview(current_user, period=period, now=None),
            params={"period": period},
            ttl_seconds=self._cache_ttl_seconds,
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
        if now is not None:
            return await self._compute_creator_clips(
                current_user,
                q=q,
                privacy=privacy,
                game_id=game_id,
                period=period,
                sort=sort,
                page=page,
                page_size=page_size,
                now=now,
            )

        return await self._cache.get_or_set(
            current_user.id,
            "analytics:clips",
            lambda: self._compute_creator_clips(
                current_user,
                q=q,
                privacy=privacy,
                game_id=game_id,
                period=period,
                sort=sort,
                page=page,
                page_size=page_size,
                now=None,
            ),
            params={
                "q": q,
                "privacy": privacy,
                "game_id": game_id,
                "period": period,
                "sort": sort,
                "page": page,
                "page_size": page_size,
            },
            ttl_seconds=self._cache_ttl_seconds,
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
        if now is not None:
            return await self._compute_clip_retention(
                clip_id,
                current_user,
                period=period,
                now=now,
            )

        return await self._cache.get_or_set(
            current_user.id,
            "analytics:retention",
            lambda: self._compute_clip_retention(
                clip_id,
                current_user,
                period=period,
                now=None,
            ),
            params={"clip_id": clip_id, "period": period},
            ttl_seconds=self._cache_ttl_seconds,
        )

    async def _compute_overview(
        self,
        current_user: User,
        *,
        period: AnalyticsPeriod,
        now: datetime | None,
    ) -> AnalyticsOverviewResponse:
        since_utc, start_date, end_date = resolve_period_window(period, now=now)
        clips = await self._clip_repo.list_creator_clips(current_user.id)
        clip_ids = [clip.id for clip in clips]
        stats_by_clip = await self._telemetry_repo.aggregate_stats_by_clip(
            clip_ids,
            since=since_utc,
        )
        daily_rows = await self._telemetry_repo.aggregate_daily_watch_series(
            clip_ids,
            since=since_utc,
        )
        overall_metrics = compute_metrics_from_aggregates(stats_by_clip.values())

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
            views_by_day=build_views_by_day_from_aggregates(
                daily_rows,
                start_date=start_date,
                end_date=end_date,
            ),
            by_game=build_game_breakdown_from_aggregates(clips, stats_by_clip),
            top_clips=self._build_top_clips(clips, stats_by_clip),
        )

    async def _compute_creator_clips(
        self,
        current_user: User,
        *,
        q: str | None,
        privacy: ClipPrivacy | None,
        game_id: UUID | None,
        period: AnalyticsPeriod,
        sort: CreatorClipSort,
        page: int,
        page_size: int,
        now: datetime | None,
    ) -> PaginatedResponse[CreatorClipAnalyticsItem]:
        since_utc, _start_date, _end_date = resolve_period_window(period, now=now)
        clips = await self._clip_repo.list_creator_clips(
            current_user.id,
            q=q,
            privacy=privacy,
            game_id=game_id,
        )
        stats_by_clip = await self._telemetry_repo.aggregate_stats_by_clip(
            [clip.id for clip in clips],
            since=since_utc,
        )

        enriched = [self._to_creator_clip_item(clip, stats_by_clip.get(clip.id)) for clip in clips]
        sorted_items = self._sort_creator_clips(enriched, sort=sort)

        total = len(sorted_items)
        offset = (page - 1) * page_size
        return PaginatedResponse.create(
            items=sorted_items[offset : offset + page_size],
            page=page,
            page_size=page_size,
            total=total,
        )

    async def _compute_clip_retention(
        self,
        clip_id: UUID,
        current_user: User,
        *,
        period: AnalyticsPeriod,
        now: datetime | None,
    ) -> ClipRetentionResponse:
        clip = await self._clip_repo.get_by_id(clip_id, load_relations=False)
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

    def _build_top_clips(
        self,
        clips: list[Clip],
        stats_by_clip: Mapping[UUID, ClipSessionAggregate],
    ) -> list[TopClipAnalyticsItem]:
        ranked: list[tuple[tuple[int, int, int, datetime], TopClipAnalyticsItem]] = []
        for clip in clips:
            clip_agg = stats_by_clip.get(clip.id)
            metrics = compute_metrics_from_aggregates((clip_agg,) if clip_agg is not None else ())
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
        clip_agg: ClipSessionAggregate | None,
    ) -> CreatorClipAnalyticsItem:
        metrics = compute_metrics_from_aggregates((clip_agg,) if clip_agg is not None else ())
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
