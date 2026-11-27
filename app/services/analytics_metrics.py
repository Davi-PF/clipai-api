"""Pure low-complexity aggregation helpers for Creator Studio analytics."""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID

from app.models.clip import Clip
from app.models.telemetry import ClipWatchSession
from app.repositories.telemetry_repository import ClipSessionAggregate, DailyWatchAggregate
from app.schemas.analytics import (
    AnalyticsPeriod,
    DailyViewsPoint,
    GameAnalyticsItem,
    RetentionCurvePoint,
)
from app.schemas.telemetry import VALID_RETENTION_BUCKETS

_PERIOD_DAYS: dict[str, int] = {
    "7d": 7,
    "30d": 30,
    "90d": 90,
}


@dataclass(frozen=True)
class SessionSummaryMetrics:
    """Aggregated retention and watch-time metrics for a set of watch sessions."""

    total_sessions: int
    total_watch_time_seconds: int
    avg_watch_seconds: float
    avg_retention_pct: float
    completion_rate_pct: float


def resolve_period_window(
    period: AnalyticsPeriod,
    *,
    now: datetime | None = None,
) -> tuple[datetime | None, date | None, date]:
    """Return (since_utc, start_date, end_date) for the requested analytics window."""
    now_utc = (now or datetime.now(UTC)).astimezone(UTC)
    end_date = now_utc.date()
    days = _PERIOD_DAYS.get(period)
    if days is None:
        return None, None, end_date
    start_date = end_date - timedelta(days=days - 1)
    since_utc = datetime.combine(start_date, time.min, tzinfo=UTC)
    return since_utc, start_date, end_date


def compute_session_metrics(sessions: list[ClipWatchSession]) -> SessionSummaryMetrics:
    """Compute watch time, average retention, and completion rate without zero division."""
    total = len(sessions)
    if total == 0:
        return SessionSummaryMetrics(
            total_sessions=0,
            total_watch_time_seconds=0,
            avg_watch_seconds=0.0,
            avg_retention_pct=0.0,
            completion_rate_pct=0.0,
        )

    total_watch_float = sum(float(item.watched_seconds) for item in sessions)
    avg_retention = sum(int(item.max_progress_pct) for item in sessions) / total
    completed_count = sum(1 for item in sessions if item.completed)

    return SessionSummaryMetrics(
        total_sessions=total,
        total_watch_time_seconds=round(total_watch_float),
        avg_watch_seconds=round(total_watch_float / total, 2),
        avg_retention_pct=round(avg_retention, 1),
        completion_rate_pct=round((completed_count / total) * 100.0, 1),
    )


def compute_metrics_from_aggregates(
    aggregates: Iterable[ClipSessionAggregate],
) -> SessionSummaryMetrics:
    """Compute exact summary KPIs from SQL-aggregated per-clip session counters."""
    total_sessions = 0
    total_watch_float = 0.0
    total_progress = 0
    total_completed = 0
    for item in aggregates:
        total_sessions += item.total_sessions
        total_watch_float += item.total_watch_seconds
        total_progress += item.sum_max_progress_pct
        total_completed += item.completed_count

    if total_sessions == 0:
        return SessionSummaryMetrics(
            total_sessions=0,
            total_watch_time_seconds=0,
            avg_watch_seconds=0.0,
            avg_retention_pct=0.0,
            completion_rate_pct=0.0,
        )

    return SessionSummaryMetrics(
        total_sessions=total_sessions,
        total_watch_time_seconds=round(total_watch_float),
        avg_watch_seconds=round(total_watch_float / total_sessions, 2),
        avg_retention_pct=round(total_progress / total_sessions, 1),
        completion_rate_pct=round((total_completed / total_sessions) * 100.0, 1),
    )


def _fill_daily_series(
    daily_sessions: Mapping[date, int],
    daily_watch: Mapping[date, float],
    *,
    start_date: date | None,
    end_date: date,
) -> list[DailyViewsPoint]:
    effective_start = start_date
    if effective_start is None:
        if not daily_sessions:
            return []
        effective_start = min(daily_sessions.keys())

    if effective_start > end_date:
        return []

    points: list[DailyViewsPoint] = []
    cursor = effective_start
    while cursor <= end_date:
        points.append(
            DailyViewsPoint(
                date=cursor.isoformat(),
                sessions=daily_sessions.get(cursor, 0),
                watch_time_seconds=round(daily_watch.get(cursor, 0.0)),
            )
        )
        cursor += timedelta(days=1)
    return points


def build_views_by_day(
    sessions: list[ClipWatchSession],
    *,
    start_date: date | None,
    end_date: date,
) -> list[DailyViewsPoint]:
    """Build a gap-free daily time series filling inactive days with zeroes."""
    daily_sessions: dict[date, int] = defaultdict(int)
    daily_watch: dict[date, float] = defaultdict(float)

    for item in sessions:
        day = item.created_at.astimezone(UTC).date()
        daily_sessions[day] += 1
        daily_watch[day] += float(item.watched_seconds)

    return _fill_daily_series(
        daily_sessions,
        daily_watch,
        start_date=start_date,
        end_date=end_date,
    )


def build_views_by_day_from_aggregates(
    daily_rows: list[DailyWatchAggregate],
    *,
    start_date: date | None,
    end_date: date,
) -> list[DailyViewsPoint]:
    """Build a gap-free daily time series from SQL-aggregated UTC day buckets."""
    daily_sessions = {row.day: row.sessions for row in daily_rows}
    daily_watch = {row.day: row.total_watch_seconds for row in daily_rows}
    return _fill_daily_series(
        daily_sessions,
        daily_watch,
        start_date=start_date,
        end_date=end_date,
    )


def build_retention_curve(sessions: list[ClipWatchSession]) -> list[RetentionCurvePoint]:
    """Build the 11 retention buckets (0..100%) safely handling empty session sets."""
    total = len(sessions)
    if total == 0:
        return [
            RetentionCurvePoint(bucket_pct=bucket, retention_pct=0.0, sessions=0)
            for bucket in VALID_RETENTION_BUCKETS
        ]

    bucket_counts: dict[int, int] = dict.fromkeys(VALID_RETENTION_BUCKETS, 0)
    for item in sessions:
        for bucket in set(item.reached_buckets or []):
            if bucket in bucket_counts:
                bucket_counts[bucket] += 1

    return [
        RetentionCurvePoint(
            bucket_pct=bucket,
            retention_pct=round((bucket_counts[bucket] / total) * 100.0, 1),
            sessions=bucket_counts[bucket],
        )
        for bucket in VALID_RETENTION_BUCKETS
    ]


def find_biggest_drop_bucket(
    curve: list[RetentionCurvePoint],
    *,
    total_sessions: int,
) -> int | None:
    """Identify the bucket with the largest consecutive retention drop-off."""
    if total_sessions == 0 or len(curve) < 2:
        return None

    biggest_drop = 0.0
    drop_bucket: int | None = None
    for index in range(1, len(curve)):
        drop = curve[index - 1].retention_pct - curve[index].retention_pct
        if drop > biggest_drop:
            biggest_drop = drop
            drop_bucket = curve[index].bucket_pct
    return drop_bucket


def _group_clips_by_game(clips: list[Clip]) -> dict[tuple[UUID | None, str], list[Clip]]:
    grouped_clips: dict[tuple[UUID | None, str], list[Clip]] = defaultdict(list)
    for clip in clips:
        game_id = clip.game_id
        game_name = clip.game.name if clip.game is not None else "Sem jogo"
        grouped_clips[(game_id, game_name)].append(clip)
    return grouped_clips


def _sort_game_breakdown(items: list[GameAnalyticsItem]) -> list[GameAnalyticsItem]:
    items.sort(key=lambda item: (-item.total_views, -item.clips_count, item.game_name.lower()))
    return items


def build_game_breakdown(
    clips: list[Clip],
    sessions_by_clip: dict[UUID, list[ClipWatchSession]],
) -> list[GameAnalyticsItem]:
    """Aggregate clip counts, total views, and average retention grouped by game."""
    items: list[GameAnalyticsItem] = []
    for (game_id, game_name), game_clips in _group_clips_by_game(clips).items():
        game_sessions = [
            session for clip in game_clips for session in sessions_by_clip.get(clip.id, [])
        ]
        metrics = compute_session_metrics(game_sessions)
        items.append(
            GameAnalyticsItem(
                game_id=game_id,
                game_name=game_name,
                clips_count=len(game_clips),
                total_views=sum(clip.views_count for clip in game_clips),
                avg_retention_pct=metrics.avg_retention_pct,
            )
        )
    return _sort_game_breakdown(items)


def build_game_breakdown_from_aggregates(
    clips: list[Clip],
    stats_by_clip: Mapping[UUID, ClipSessionAggregate],
) -> list[GameAnalyticsItem]:
    """Aggregate clip counts, total views, and average retention per game from SQL aggregates."""
    items: list[GameAnalyticsItem] = []
    for (game_id, game_name), game_clips in _group_clips_by_game(clips).items():
        game_aggs = [stats_by_clip[clip.id] for clip in game_clips if clip.id in stats_by_clip]
        metrics = compute_metrics_from_aggregates(game_aggs)
        items.append(
            GameAnalyticsItem(
                game_id=game_id,
                game_name=game_name,
                clips_count=len(game_clips),
                total_views=sum(clip.views_count for clip in game_clips),
                avg_retention_pct=metrics.avg_retention_pct,
            )
        )
    return _sort_game_breakdown(items)
