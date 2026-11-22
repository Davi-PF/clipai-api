"""Pydantic v2 response schemas for Creator Studio analytics endpoints."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.schemas.clip import ClipPrivacy, ClipStatus

AnalyticsPeriod = Literal["7d", "30d", "90d", "all"]
CreatorClipSort = Literal["views", "retention", "likes", "recent"]


class DailyViewsPoint(BaseModel):
    """Single day entry in a continuous daily watch-session time series."""

    model_config = ConfigDict(from_attributes=True)

    date: str
    sessions: int
    watch_time_seconds: int


class GameAnalyticsItem(BaseModel):
    """Aggregated creator performance grouped by game."""

    model_config = ConfigDict(from_attributes=True)

    game_id: UUID | None = None
    game_name: str
    clips_count: int
    total_views: int
    avg_retention_pct: float


class TopClipAnalyticsItem(BaseModel):
    """Top-performing creator clip summary for overview highlights."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    slug: str
    title: str
    thumbnail_url: str | None = None
    game_name: str | None = None
    views_count: int
    likes_count: int
    avg_retention_pct: float
    completion_rate_pct: float


class AnalyticsOverviewResponse(BaseModel):
    """Consolidated KPIs, daily chart, game breakdown, and top 5 clips."""

    model_config = ConfigDict(from_attributes=True)

    period: AnalyticsPeriod
    total_clips: int
    total_views: int
    total_likes: int
    total_favorites: int
    total_watch_time_seconds: int
    avg_retention_pct: float
    completion_rate_pct: float
    total_sessions: int
    views_by_day: list[DailyViewsPoint]
    by_game: list[GameAnalyticsItem]
    top_clips: list[TopClipAnalyticsItem]


class CreatorClipAnalyticsItem(BaseModel):
    """Creator clip metadata enriched with period retention metrics."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    slug: str
    title: str
    privacy: ClipPrivacy
    status: ClipStatus
    thumbnail_url: str | None = None
    duration_seconds: float
    game_id: UUID | None = None
    game_name: str | None = None
    views_count: int
    likes_count: int
    favorites_count: int
    sessions_count: int
    avg_retention_pct: float
    completion_rate_pct: float
    created_at: datetime


class RetentionCurvePoint(BaseModel):
    """Retention percentage and active sessions at a 10% video progress bucket."""

    model_config = ConfigDict(from_attributes=True)

    bucket_pct: int
    retention_pct: float
    sessions: int


class ClipRetentionResponse(BaseModel):
    """Detailed clip retention curve (0..100%), drop-off point, and daily series."""

    model_config = ConfigDict(from_attributes=True)

    clip_id: UUID
    slug: str
    title: str
    duration_seconds: float
    period: AnalyticsPeriod
    total_sessions: int
    avg_watch_seconds: float
    avg_retention_pct: float
    completion_rate_pct: float
    biggest_drop_bucket_pct: int | None = None
    views_by_day: list[DailyViewsPoint]
    curve: list[RetentionCurvePoint]
