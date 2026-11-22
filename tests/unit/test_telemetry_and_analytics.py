"""Unit tests for video telemetry schemas, repository, service, and analytics math helpers."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError
from app.models.clip import Clip
from app.models.game import Game
from app.models.telemetry import ClipWatchSession
from app.models.user import User
from app.repositories.clip_repository import ClipRepository
from app.repositories.telemetry_repository import TelemetryRepository
from app.schemas.telemetry import ClipTelemetryRequest
from app.services.analytics_metrics import (
    build_game_breakdown,
    build_retention_curve,
    build_views_by_day,
    compute_session_metrics,
    find_biggest_drop_bucket,
    resolve_period_window,
)
from app.services.analytics_service import AnalyticsService
from app.services.storage_service import StorageService
from app.services.telemetry_service import TelemetryService


def _make_user(username: str = "fallen_br") -> User:
    now = datetime.now(UTC)
    return User(
        id=uuid4(),
        username=username,
        email=f"{username}@clipai.com.br",
        password_hash="hashed",
        display_name=username,
        is_email_verified=True,
        is_active=True,
        is_2fa_enabled=False,
        created_at=now,
        updated_at=now,
    )


def _make_clip(
    owner: User,
    *,
    title: str = "Clutch 1v4",
    status: str = "ready",
    privacy: str = "public",
    duration: str = "30.00",
    views: int = 10,
    likes: int = 2,
    game: Game | None = None,
) -> Clip:
    now = datetime.now(UTC)
    clip_id = uuid4()
    return Clip(
        id=clip_id,
        slug=f"slug-{clip_id.hex[:8]}",
        user_id=owner.id,
        game_id=game.id if game else None,
        title=title,
        description=None,
        status=status,
        privacy=privacy,
        video_object_key=f"clips/{owner.id}/{clip_id}/video.mp4",
        thumbnail_object_key=f"clips/{owner.id}/{clip_id}/thumb.webp",
        file_size_bytes=5_000_000,
        duration_seconds=Decimal(duration),
        mime_type="video/mp4",
        technical_metadata={},
        views_count=views,
        likes_count=likes,
        favorites_count=1,
        author=owner,
        game=game,
        tags=[],
        created_at=now,
        updated_at=now,
    )


def _make_watch_session(
    clip_id,
    *,
    watched_seconds: float,
    max_progress_pct: int,
    reached_buckets: list[int],
    created_at: datetime | None = None,
    session_token: str | None = None,
) -> ClipWatchSession:
    ts = created_at or datetime.now(UTC)
    return ClipWatchSession(
        id=uuid4(),
        clip_id=clip_id,
        viewer_id=None,
        session_token=session_token or str(uuid4()),
        watched_seconds=watched_seconds,
        max_progress_pct=max_progress_pct,
        reached_buckets=reached_buckets,
        completed=max_progress_pct >= 90,
        created_at=ts,
        updated_at=ts,
    )


def test_telemetry_schema_validation_and_bucket_normalization() -> None:
    """Validate UUID session_token and multiples-of-10 reached_buckets in PT-BR."""
    raw_uuid = str(uuid4()).upper()
    req = ClipTelemetryRequest(
        session_token=f"  {raw_uuid}  ",
        watched_seconds=18.5,
        max_progress_pct=60,
        reached_buckets=[30, 0, 10, 30, 20],
    )
    assert req.session_token == raw_uuid.lower()
    assert req.reached_buckets == [0, 10, 20, 30]

    with pytest.raises(ValidationError, match="UUID válido"):
        ClipTelemetryRequest(
            session_token="not-a-uuid",
            watched_seconds=10.0,
            max_progress_pct=20,
            reached_buckets=[0, 10],
        )

    for invalid_buckets in ([15], [-10], [110], [0, 25, 50]):
        with pytest.raises(ValidationError, match="múltiplos de 10"):
            ClipTelemetryRequest(
                session_token=str(uuid4()),
                watched_seconds=10.0,
                max_progress_pct=20,
                reached_buckets=invalid_buckets,
            )


async def test_telemetry_repository_upsert_merges_buckets_and_max_metrics() -> None:
    """Create new watch session and idempotently merge subsequent updates for same token."""
    session = AsyncMock(spec=AsyncSession)
    session.add = MagicMock()
    repo = TelemetryRepository(session)
    clip_id = uuid4()
    viewer_id = uuid4()
    token = str(uuid4())

    # 1. Insert new session (not yet completed)
    empty_res = MagicMock()
    empty_res.scalar_one_or_none.return_value = None
    session.execute.return_value = empty_res

    created = await repo.upsert_watch_session(
        clip_id=clip_id,
        viewer_id=None,
        session_token=token,
        watched_seconds=12.0,
        max_progress_pct=40,
        reached_buckets=[0, 10, 20, 40],
    )
    assert created.completed is False
    assert created.reached_buckets == [0, 10, 20, 40]
    session.add.assert_called_once_with(created)

    # 2. Merge existing session: union buckets, keep max watched_seconds & progress, flip completed
    found_res = MagicMock()
    found_res.scalar_one_or_none.return_value = created
    session.execute.return_value = found_res

    merged = await repo.upsert_watch_session(
        clip_id=clip_id,
        viewer_id=viewer_id,
        session_token=token,
        watched_seconds=8.0,
        max_progress_pct=95,
        reached_buckets=[30, 50, 90],
    )
    assert merged is created
    assert merged.watched_seconds == 12.0
    assert merged.max_progress_pct == 95
    assert merged.reached_buckets == [0, 10, 20, 30, 40, 50, 90]
    assert merged.completed is True
    assert merged.viewer_id == viewer_id

    # 3. list_sessions_for_clips empty shortcut & filtered query
    assert await repo.list_sessions_for_clips([]) == []
    list_res = MagicMock()
    list_res.scalars.return_value.all.return_value = [created]
    session.execute.return_value = list_res
    fetched = await repo.list_sessions_for_clips([clip_id], since=datetime.now(UTC))
    assert fetched == [created]
    session.commit.assert_not_awaited()


async def test_clip_repository_list_creator_clips_applies_filters() -> None:
    """Verify ClipRepository.list_creator_clips executes filtered creator query."""
    session = AsyncMock(spec=AsyncSession)
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = []
    session.execute.return_value = result_mock

    repo = ClipRepository(session)
    items = await repo.list_creator_clips(
        uuid4(),
        q="clutch",
        privacy="unlisted",
        game_id=uuid4(),
    )
    assert items == []
    session.execute.assert_awaited_once()


async def test_telemetry_service_guards_privacy_status_and_caps_watch_time() -> None:
    """Enforce ready status, friends privacy, and duration * 3 cap on watched_seconds."""
    session = AsyncMock(spec=AsyncSession)
    service = TelemetryService(session)
    owner = _make_user()

    service._clip_repo = AsyncMock()
    service._telemetry_repo = AsyncMock()

    # Missing or non-ready clip -> 404
    service._clip_repo.get_by_id.return_value = None
    payload = ClipTelemetryRequest(
        session_token=str(uuid4()),
        watched_seconds=200.0,
        max_progress_pct=100,
        reached_buckets=[0, 50, 100],
    )
    with pytest.raises(AppError) as missing_exc:
        await service.record_telemetry(uuid4(), payload, current_user=None)
    assert missing_exc.value.status_code == 404

    pending = _make_clip(owner, status="pending_upload")
    service._clip_repo.get_by_id.return_value = pending
    with pytest.raises(AppError) as pending_exc:
        await service.record_telemetry(pending.id, payload, current_user=None)
    assert pending_exc.value.status_code == 404

    # Friends clip blocks anonymous -> 401
    friends_clip = _make_clip(owner, privacy="friends", duration="20.00")
    service._clip_repo.get_by_id.return_value = friends_clip
    with pytest.raises(AppError) as anon_exc:
        await service.record_telemetry(friends_clip.id, payload, current_user=None)
    assert anon_exc.value.status_code == 401

    # Capped watched_seconds = duration (20s) * 3 = 60.0s
    saved = _make_watch_session(
        friends_clip.id,
        watched_seconds=60.0,
        max_progress_pct=100,
        reached_buckets=[0, 50, 100],
        session_token=payload.session_token,
    )
    service._telemetry_repo.upsert_watch_session.return_value = saved
    resp = await service.record_telemetry(friends_clip.id, payload, current_user=owner)

    assert resp.watched_seconds == 60.0
    assert resp.completed is True
    assert service._telemetry_repo.upsert_watch_session.await_args.kwargs["watched_seconds"] == 60.0
    session.commit.assert_awaited_once()


def test_analytics_metrics_curve_drop_off_zero_sessions_and_gap_filling() -> None:
    """Verify pure analytics helpers handle 0 sessions, drop-off detection, and daily gaps."""
    now = datetime(2026, 11, 22, 15, 0, tzinfo=UTC)

    since_7d, start_7d, end_7d = resolve_period_window("7d", now=now)
    assert since_7d is not None
    assert start_7d is not None
    assert (end_7d - start_7d).days == 6

    since_all, start_all, end_all = resolve_period_window("all", now=now)
    assert since_all is None
    assert start_all is None
    assert end_all == now.date()

    # Zero sessions -> zeroed KPIs, 11 zeroed curve points, no drop bucket
    empty_metrics = compute_session_metrics([])
    assert empty_metrics.total_sessions == 0
    assert empty_metrics.avg_retention_pct == 0.0
    assert empty_metrics.completion_rate_pct == 0.0

    empty_curve = build_retention_curve([])
    assert len(empty_curve) == 11
    assert all(point.retention_pct == 0.0 and point.sessions == 0 for point in empty_curve)
    assert find_biggest_drop_bucket(empty_curve, total_sessions=0) is None
    assert build_views_by_day([], start_date=None, end_date=end_all) == []

    # 4 sessions: 100% reach 0-20%, only 1 reaches 30-100% -> biggest drop is 75% at bucket 30
    clip_id = uuid4()
    day_1 = now - timedelta(days=2)
    sessions = [
        _make_watch_session(
            clip_id,
            watched_seconds=30.0,
            max_progress_pct=100,
            reached_buckets=[0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100],
            created_at=day_1,
        ),
        _make_watch_session(
            clip_id,
            watched_seconds=6.0,
            max_progress_pct=20,
            reached_buckets=[0, 10, 20],
            created_at=day_1,
        ),
        _make_watch_session(
            clip_id,
            watched_seconds=7.0,
            max_progress_pct=20,
            reached_buckets=[0, 10, 20],
            created_at=now,
        ),
        _make_watch_session(
            clip_id,
            watched_seconds=5.0,
            max_progress_pct=20,
            reached_buckets=[0, 10, 20],
            created_at=now,
        ),
    ]

    metrics = compute_session_metrics(sessions)
    assert metrics.total_sessions == 4
    assert metrics.total_watch_time_seconds == 48
    assert metrics.avg_watch_seconds == 12.0
    assert metrics.avg_retention_pct == 40.0
    assert metrics.completion_rate_pct == 25.0

    curve = build_retention_curve(sessions)
    assert curve[0].bucket_pct == 0 and curve[0].retention_pct == 100.0
    assert curve[2].bucket_pct == 20 and curve[2].retention_pct == 100.0
    assert curve[3].bucket_pct == 30 and curve[3].retention_pct == 25.0
    assert find_biggest_drop_bucket(curve, total_sessions=4) == 30

    # Daily series fills middle gap day with 0
    series_7d = build_views_by_day(sessions, start_date=start_7d, end_date=end_7d)
    assert len(series_7d) == 7
    by_date = {point.date: point for point in series_7d}
    assert by_date["2026-11-20"].sessions == 2
    assert by_date["2026-11-21"].sessions == 0
    assert by_date["2026-11-22"].sessions == 2

    # Game breakdown handles clips without game ("Sem jogo")
    owner = _make_user()
    cs2 = Game(id=uuid4(), name="CS2", slug="cs2")
    clip_cs2 = _make_clip(owner, game=cs2, views=50)
    clip_none = _make_clip(owner, game=None, views=10)
    breakdown = build_game_breakdown(
        [clip_cs2, clip_none],
        {clip_cs2.id: sessions[:2], clip_none.id: []},
    )
    assert [item.game_name for item in breakdown] == ["CS2", "Sem jogo"]
    assert breakdown[0].avg_retention_pct == 60.0
    assert breakdown[1].avg_retention_pct == 0.0


async def test_analytics_service_enforces_clip_ownership() -> None:
    """Reject retention requests for missing clips (404) or another creator's clip (403)."""
    session = AsyncMock(spec=AsyncSession)
    storage = MagicMock(spec=StorageService)
    storage.build_public_url.side_effect = lambda key: f"https://cdn.clipai.test/{key}"
    service = AnalyticsService(session=session, storage_service=storage)
    service._clip_repo = AsyncMock()
    service._telemetry_repo = AsyncMock()

    owner = _make_user("owner")
    other = _make_user("other")
    clip = _make_clip(owner)

    service._clip_repo.get_by_id.return_value = None
    with pytest.raises(AppError) as not_found:
        await service.get_clip_retention(clip.id, owner)
    assert not_found.value.status_code == 404

    service._clip_repo.get_by_id.return_value = clip
    with pytest.raises(AppError) as forbidden:
        await service.get_clip_retention(clip.id, other)
    assert forbidden.value.status_code == 403
    assert "permissão" in forbidden.value.message.lower()
