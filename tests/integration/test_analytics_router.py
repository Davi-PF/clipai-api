"""End-to-end integration tests for Sprint 5: Telemetry Collection and Creator Studio Analytics."""

from collections import defaultdict
from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import status
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.database import get_db
from app.core.security import create_access_token
from app.main import create_app
from app.models.clip import Clip
from app.models.game import Game
from app.models.telemetry import ClipWatchSession
from app.models.user import User
from app.repositories.telemetry_repository import ClipSessionAggregate, DailyWatchAggregate
from app.routers.clips import get_storage_service
from app.services.storage_service import StorageService


class _AnalyticsStore:
    """In-memory backing store for users, games, clips, and watch sessions."""

    def __init__(self) -> None:
        self.users: dict[UUID, User] = {}
        self.clips: dict[UUID, Clip] = {}
        self.sessions: dict[tuple[UUID, str], ClipWatchSession] = {}
        self.query_counts: dict[str, int] = defaultdict(int)


class _FakeUserRepo:
    def __init__(self, store: _AnalyticsStore, _session: AsyncSession) -> None:
        self._store = store

    async def get_by_id(self, user_id: UUID) -> User | None:
        return self._store.users.get(user_id)


class _FakeClipRepo:
    def __init__(self, store: _AnalyticsStore, _session: AsyncSession) -> None:
        self._store = store

    async def get_by_id(
        self,
        clip_id: UUID,
        *,
        load_relations: bool = True,
    ) -> Clip | None:
        _ = load_relations
        self._store.query_counts["clip_get_by_id"] += 1
        return self._store.clips.get(clip_id)

    async def like_clip(self, *, user_id: UUID, clip_id: UUID) -> int:
        _ = user_id
        clip = self._store.clips[clip_id]
        clip.likes_count += 1
        return clip.likes_count

    async def list_creator_clips(
        self,
        user_id: UUID,
        *,
        q: str | None = None,
        privacy: str | None = None,
        game_id: UUID | None = None,
    ) -> list[Clip]:
        self._store.query_counts["list_creator_clips"] += 1
        clips = [
            c for c in self._store.clips.values() if c.user_id == user_id and c.status == "ready"
        ]
        if q and q.strip():
            term = q.strip().lower()
            clips = [c for c in clips if term in c.title.lower()]
        if privacy:
            clips = [c for c in clips if c.privacy == privacy]
        if game_id is not None:
            clips = [c for c in clips if c.game_id == game_id]
        clips.sort(key=lambda c: c.created_at, reverse=True)
        return clips


class _FakeTelemetryRepo:
    def __init__(self, store: _AnalyticsStore, _session: AsyncSession) -> None:
        self._store = store

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
        key = (clip_id, session_token)
        existing = self._store.sessions.get(key)
        now = datetime.now(UTC)
        if existing is not None:
            existing.reached_buckets = sorted(
                set(existing.reached_buckets or []) | set(reached_buckets)
            )
            existing.watched_seconds = max(float(existing.watched_seconds), float(watched_seconds))
            existing.max_progress_pct = max(int(existing.max_progress_pct), int(max_progress_pct))
            existing.completed = existing.max_progress_pct >= 90
            if existing.viewer_id is None and viewer_id is not None:
                existing.viewer_id = viewer_id
            existing.updated_at = now
            return existing

        record = ClipWatchSession(
            id=uuid4(),
            clip_id=clip_id,
            viewer_id=viewer_id,
            session_token=session_token,
            watched_seconds=float(watched_seconds),
            max_progress_pct=int(max_progress_pct),
            reached_buckets=sorted(set(reached_buckets)),
            completed=int(max_progress_pct) >= 90,
            created_at=now,
            updated_at=now,
        )
        self._store.sessions[key] = record
        return record

    async def list_sessions_for_clips(
        self,
        clip_ids: list[UUID],
        *,
        since: datetime | None = None,
    ) -> list[ClipWatchSession]:
        self._store.query_counts["list_sessions_for_clips"] += 1
        id_set = set(clip_ids)
        results: list[ClipWatchSession] = []
        for item in self._store.sessions.values():
            if item.clip_id not in id_set:
                continue
            if since is not None and item.created_at < since:
                continue
            results.append(item)
        return results

    async def aggregate_stats_by_clip(
        self,
        clip_ids: list[UUID],
        *,
        since: datetime | None = None,
    ) -> dict[UUID, ClipSessionAggregate]:
        self._store.query_counts["aggregate_stats_by_clip"] += 1
        sessions = await self.list_sessions_for_clips(clip_ids, since=since)
        grouped: dict[UUID, list[ClipWatchSession]] = defaultdict(list)
        for item in sessions:
            grouped[item.clip_id].append(item)
        return {
            cid: ClipSessionAggregate(
                clip_id=cid,
                total_sessions=len(items),
                total_watch_seconds=sum(float(s.watched_seconds) for s in items),
                sum_max_progress_pct=sum(int(s.max_progress_pct) for s in items),
                completed_count=sum(1 for s in items if s.completed),
            )
            for cid, items in grouped.items()
        }

    async def aggregate_daily_watch_series(
        self,
        clip_ids: list[UUID],
        *,
        since: datetime | None = None,
    ) -> list[DailyWatchAggregate]:
        self._store.query_counts["aggregate_daily_watch_series"] += 1
        sessions = await self.list_sessions_for_clips(clip_ids, since=since)
        by_day: dict[date, list[ClipWatchSession]] = defaultdict(list)
        for item in sessions:
            by_day[item.created_at.astimezone(UTC).date()].append(item)
        return [
            DailyWatchAggregate(
                day=day_key,
                sessions=len(items),
                total_watch_seconds=sum(float(s.watched_seconds) for s in items),
            )
            for day_key, items in sorted(by_day.items())
        ]


@pytest.fixture
def analytics_test_env(
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """Provide FastAPI app wired with in-memory clips and watch sessions."""
    store = _AnalyticsStore()
    now = datetime.now(UTC)

    creator = User(
        id=uuid4(),
        username="fallen_br",
        email="fallen@clipai.com.br",
        password_hash="hashed",
        display_name="FalleN",
        is_email_verified=True,
        is_active=True,
        is_2fa_enabled=False,
        created_at=now,
        updated_at=now,
    )
    other = User(
        id=uuid4(),
        username="coldzera",
        email="cold@clipai.com.br",
        password_hash="hashed",
        display_name="coldzera",
        is_email_verified=True,
        is_active=True,
        is_2fa_enabled=False,
        created_at=now,
        updated_at=now,
    )
    store.users[creator.id] = creator
    store.users[other.id] = other

    cs2 = Game(id=uuid4(), name="CS2", slug="cs2", created_at=now)
    valorant = Game(id=uuid4(), name="Valorant", slug="valorant", created_at=now)

    def _add_clip(
        owner: User,
        *,
        title: str,
        privacy: str = "public",
        status_val: str = "ready",
        game: Game | None = cs2,
        views: int = 100,
        likes: int = 20,
        favorites: int = 5,
        duration: str = "30.00",
        days_ago: int = 1,
    ) -> Clip:
        cid = uuid4()
        ts = now - timedelta(days=days_ago)
        clip = Clip(
            id=cid,
            slug=f"c-{cid.hex[:8]}",
            user_id=owner.id,
            game_id=game.id if game else None,
            title=title,
            description=None,
            status=status_val,
            privacy=privacy,
            video_object_key=f"clips/{owner.id}/{cid}/video.mp4",
            thumbnail_object_key=f"clips/{owner.id}/{cid}/thumb.webp",
            file_size_bytes=4_000_000,
            duration_seconds=Decimal(duration),
            mime_type="video/mp4",
            technical_metadata={},
            views_count=views,
            likes_count=likes,
            favorites_count=favorites,
            author=owner,
            game=game,
            tags=[],
            created_at=ts,
            updated_at=ts,
        )
        store.clips[cid] = clip
        return clip

    clip_public = _add_clip(
        creator,
        title="Ace na Mirage",
        privacy="public",
        game=cs2,
        views=120,
        likes=30,
        favorites=10,
        days_ago=3,
    )
    clip_friends = _add_clip(
        creator,
        title="Clutch na Bind",
        privacy="friends",
        game=valorant,
        views=50,
        likes=45,
        favorites=4,
        days_ago=2,
    )
    clip_unlisted = _add_clip(
        creator,
        title="Treino de Smoke",
        privacy="unlisted",
        game=cs2,
        views=10,
        likes=2,
        favorites=1,
        days_ago=1,
    )
    clip_pending = _add_clip(
        creator,
        title="Ainda Enviando",
        status_val="pending_upload",
        views=0,
        likes=0,
        favorites=0,
    )
    clip_other = _add_clip(
        other,
        title="Clipe de Terceiro",
        privacy="public",
        game=cs2,
        views=999,
        likes=100,
    )

    monkeypatch.setattr("app.core.dependencies.UserRepository", lambda s: _FakeUserRepo(store, s))
    monkeypatch.setattr(
        "app.services.telemetry_service.ClipRepository", lambda s: _FakeClipRepo(store, s)
    )
    monkeypatch.setattr(
        "app.services.telemetry_service.TelemetryRepository",
        lambda s: _FakeTelemetryRepo(store, s),
    )
    monkeypatch.setattr(
        "app.services.analytics_service.ClipRepository", lambda s: _FakeClipRepo(store, s)
    )
    monkeypatch.setattr(
        "app.services.clip_service.ClipRepository", lambda s: _FakeClipRepo(store, s)
    )
    monkeypatch.setattr(
        "app.services.analytics_service.TelemetryRepository",
        lambda s: _FakeTelemetryRepo(store, s),
    )

    storage_mock = MagicMock(spec=StorageService)
    storage_mock.build_public_url.side_effect = lambda key: f"https://cdn.clipai.test/{key}"

    application = create_app(settings=test_settings)
    db_session = AsyncMock(spec=AsyncSession)

    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    application.dependency_overrides[get_db] = _override_get_db
    application.dependency_overrides[get_storage_service] = lambda: storage_mock

    return {
        "app": application,
        "store": store,
        "creator_headers": {
            "Authorization": f"Bearer {create_access_token(creator.id, test_settings)}"
        },
        "other_headers": {
            "Authorization": f"Bearer {create_access_token(other.id, test_settings)}"
        },
        "clip_public": clip_public,
        "clip_friends": clip_friends,
        "clip_unlisted": clip_unlisted,
        "clip_pending": clip_pending,
        "clip_other": clip_other,
        "cs2": cs2,
        "valorant": valorant,
    }


async def test_telemetry_upsert_validation_privacy_and_rate_limit(
    analytics_test_env: dict[str, Any],
) -> None:
    """Verify idempotent bucket merge, invalid bucket rejection, privacy guard, and capping."""
    app = analytics_test_env["app"]
    creator_headers = analytics_test_env["creator_headers"]
    clip_public: Clip = analytics_test_env["clip_public"]
    clip_friends: Clip = analytics_test_env["clip_friends"]
    clip_pending: Clip = analytics_test_env["clip_pending"]

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        # Invalid bucket -> 422 VALIDATION_ERROR
        bad_bucket = await client.post(
            f"/api/v1/clips/{clip_public.id}/telemetry",
            json={
                "session_token": str(uuid4()),
                "watched_seconds": 10,
                "max_progress_pct": 25,
                "reached_buckets": [0, 15, 25],
            },
        )
        assert bad_bucket.status_code == 422
        assert bad_bucket.json()["error"]["code"] == "VALIDATION_ERROR"

        # Pending clip -> 404
        pending_resp = await client.post(
            f"/api/v1/clips/{clip_pending.id}/telemetry",
            json={
                "session_token": str(uuid4()),
                "watched_seconds": 5,
                "max_progress_pct": 10,
                "reached_buckets": [0, 10],
            },
        )
        assert pending_resp.status_code == status.HTTP_404_NOT_FOUND

        # Anonymous ping on friends clip -> 401
        friends_anon = await client.post(
            f"/api/v1/clips/{clip_friends.id}/telemetry",
            json={
                "session_token": str(uuid4()),
                "watched_seconds": 5,
                "max_progress_pct": 10,
                "reached_buckets": [0, 10],
            },
        )
        assert friends_anon.status_code == status.HTTP_401_UNAUTHORIZED

        # First ping creates session; second ping merges buckets & caps watch_seconds to 30*3=90
        token = str(uuid4())
        ping_1 = await client.post(
            f"/api/v1/clips/{clip_public.id}/telemetry",
            json={
                "session_token": token,
                "watched_seconds": 12.0,
                "max_progress_pct": 40,
                "reached_buckets": [0, 10, 20, 40],
            },
        )
        assert ping_1.status_code == status.HTTP_200_OK
        assert ping_1.json()["completed"] is False
        assert ping_1.json()["reached_buckets"] == [0, 10, 20, 40]

        ping_2 = await client.post(
            f"/api/v1/clips/{clip_public.id}/telemetry",
            headers=creator_headers,
            json={
                "session_token": token,
                "watched_seconds": 999.0,
                "max_progress_pct": 92,
                "reached_buckets": [30, 50, 90],
            },
        )
        assert ping_2.status_code == status.HTTP_200_OK
        merged = ping_2.json()
        assert merged["watched_seconds"] == 90.0
        assert merged["max_progress_pct"] == 92
        assert merged["completed"] is True
        assert merged["reached_buckets"] == [0, 10, 20, 30, 40, 50, 90]


async def test_creator_analytics_overview_clips_filters_sorting_and_retention(
    analytics_test_env: dict[str, Any],
) -> None:
    """Verify overview KPIs, continuous daily chart, creator clips sorting, and retention curve."""
    app = analytics_test_env["app"]
    creator_headers = analytics_test_env["creator_headers"]
    other_headers = analytics_test_env["other_headers"]
    clip_public: Clip = analytics_test_env["clip_public"]
    clip_friends: Clip = analytics_test_env["clip_friends"]
    clip_unlisted: Clip = analytics_test_env["clip_unlisted"]
    clip_other: Clip = analytics_test_env["clip_other"]
    valorant: Game = analytics_test_env["valorant"]

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        # Anonymous blocked on all analytics routes
        assert (await client.get("/api/v1/analytics/overview")).status_code == 401
        assert (await client.get("/api/v1/analytics/clips")).status_code == 401
        assert (
            await client.get(f"/api/v1/analytics/clips/{clip_public.id}/retention")
        ).status_code == 401

        # Seed telemetry:
        # clip_public: 2 sessions -> [0..100] (completed) and [0,10,20] (drop at 30%)
        await client.post(
            f"/api/v1/clips/{clip_public.id}/telemetry",
            json={
                "session_token": str(uuid4()),
                "watched_seconds": 30.0,
                "max_progress_pct": 100,
                "reached_buckets": [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100],
            },
        )
        await client.post(
            f"/api/v1/clips/{clip_public.id}/telemetry",
            json={
                "session_token": str(uuid4()),
                "watched_seconds": 6.0,
                "max_progress_pct": 20,
                "reached_buckets": [0, 10, 20],
            },
        )
        # clip_friends: 1 completed session (100% avg retention)
        await client.post(
            f"/api/v1/clips/{clip_friends.id}/telemetry",
            headers=creator_headers,
            json={
                "session_token": str(uuid4()),
                "watched_seconds": 28.0,
                "max_progress_pct": 100,
                "reached_buckets": [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100],
            },
        )

        # 1. Overview (period=7d)
        overview_resp = await client.get(
            "/api/v1/analytics/overview",
            params={"period": "7d"},
            headers=creator_headers,
        )
        assert overview_resp.status_code == status.HTTP_200_OK
        overview = overview_resp.json()
        assert overview["total_clips"] == 3  # excludes pending_upload and other user's clip
        assert overview["total_views"] == 180
        assert overview["total_likes"] == 77
        assert overview["total_favorites"] == 15
        assert overview["total_sessions"] == 3
        assert overview["total_watch_time_seconds"] == 64
        assert overview["avg_retention_pct"] == 73.3
        assert overview["completion_rate_pct"] == 66.7
        assert len(overview["views_by_day"]) == 7
        assert len(overview["by_game"]) == 2
        assert len(overview["top_clips"]) == 3

        # 2. Creator clips list: includes public, friends, unlisted + filters & sort modes
        all_mine = await client.get("/api/v1/analytics/clips", headers=creator_headers)
        assert all_mine.status_code == status.HTTP_200_OK
        assert all_mine.json()["total"] == 3
        assert {item["privacy"] for item in all_mine.json()["items"]} == {
            "public",
            "friends",
            "unlisted",
        }

        by_views = await client.get(
            "/api/v1/analytics/clips",
            params={"sort": "views"},
            headers=creator_headers,
        )
        assert [c["id"] for c in by_views.json()["items"]] == [
            str(clip_public.id),
            str(clip_friends.id),
            str(clip_unlisted.id),
        ]

        by_retention = await client.get(
            "/api/v1/analytics/clips",
            params={"sort": "retention"},
            headers=creator_headers,
        )
        assert by_retention.json()["items"][0]["id"] == str(clip_friends.id)

        by_likes = await client.get(
            "/api/v1/analytics/clips",
            params={"sort": "likes"},
            headers=creator_headers,
        )
        assert by_likes.json()["items"][0]["id"] == str(clip_friends.id)

        filtered = await client.get(
            "/api/v1/analytics/clips",
            params={"q": "bind", "privacy": "friends", "game_id": str(valorant.id)},
            headers=creator_headers,
        )
        assert filtered.json()["total"] == 1
        assert filtered.json()["items"][0]["id"] == str(clip_friends.id)

        # 3. Retention curve for owned clip_public
        ret_resp = await client.get(
            f"/api/v1/analytics/clips/{clip_public.id}/retention",
            params={"period": "30d"},
            headers=creator_headers,
        )
        assert ret_resp.status_code == status.HTTP_200_OK
        ret = ret_resp.json()
        assert ret["total_sessions"] == 2
        assert ret["avg_watch_seconds"] == 18.0
        assert ret["avg_retention_pct"] == 60.0
        assert ret["completion_rate_pct"] == 50.0
        assert ret["biggest_drop_bucket_pct"] == 30
        assert len(ret["curve"]) == 11
        assert ret["curve"][0] == {"bucket_pct": 0, "retention_pct": 100.0, "sessions": 2}
        assert ret["curve"][3] == {"bucket_pct": 30, "retention_pct": 50.0, "sessions": 1}
        assert len(ret["views_by_day"]) == 30

        # 4. Clip with 0 sessions returns clean zeroed curve without zero-division error
        zero_ret = await client.get(
            f"/api/v1/analytics/clips/{clip_unlisted.id}/retention",
            params={"period": "30d"},
            headers=creator_headers,
        )
        assert zero_ret.status_code == status.HTTP_200_OK
        assert zero_ret.json()["total_sessions"] == 0
        assert zero_ret.json()["biggest_drop_bucket_pct"] is None
        assert len(zero_ret.json()["curve"]) == 11
        assert all(pt["retention_pct"] == 0.0 for pt in zero_ret.json()["curve"])

        # 5. Owner isolation -> 403 FORBIDDEN in PT-BR for third-party clip
        forbid_resp = await client.get(
            f"/api/v1/analytics/clips/{clip_other.id}/retention",
            headers=creator_headers,
        )
        assert forbid_resp.status_code == status.HTTP_403_FORBIDDEN
        assert forbid_resp.json()["error"]["code"] == "FORBIDDEN"

        forbid_other = await client.get(
            f"/api/v1/analytics/clips/{clip_public.id}/retention",
            headers=other_headers,
        )
        assert forbid_other.status_code == status.HTTP_403_FORBIDDEN


async def test_analytics_cache_hits_and_invalidates_on_clip_and_telemetry_mutations(
    analytics_test_env: dict[str, Any],
) -> None:
    """Verify repeated analytics calls hit TTL cache and mutations invalidate per user."""
    app = analytics_test_env["app"]
    store: _AnalyticsStore = analytics_test_env["store"]
    creator_headers = analytics_test_env["creator_headers"]
    other_headers = analytics_test_env["other_headers"]
    clip_public: Clip = analytics_test_env["clip_public"]

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        # First call computes from repository
        first = await client.get(
            "/api/v1/analytics/overview",
            params={"period": "30d"},
            headers=creator_headers,
        )
        assert first.status_code == status.HTTP_200_OK
        assert store.query_counts["list_creator_clips"] == 1
        assert store.query_counts["aggregate_stats_by_clip"] == 1
        assert store.query_counts["aggregate_daily_watch_series"] == 1

        # Second identical call hits in-memory cache -> zero extra DB queries
        second = await client.get(
            "/api/v1/analytics/overview",
            params={"period": "30d"},
            headers=creator_headers,
        )
        assert second.status_code == status.HTTP_200_OK
        assert second.json() == first.json()
        assert store.query_counts["list_creator_clips"] == 1

        # Different query param misses cache independently
        period_7d = await client.get(
            "/api/v1/analytics/overview",
            params={"period": "7d"},
            headers=creator_headers,
        )
        assert period_7d.status_code == status.HTTP_200_OK
        assert store.query_counts["list_creator_clips"] == 2

        # Another user's like on creator's clip invalidates creator's analytics cache
        like_resp = await client.post(
            f"/api/v1/clips/{clip_public.id}/like",
            headers=other_headers,
        )
        assert like_resp.status_code == status.HTTP_200_OK

        refreshed_after_like = await client.get(
            "/api/v1/analytics/overview",
            params={"period": "30d"},
            headers=creator_headers,
        )
        assert refreshed_after_like.status_code == status.HTTP_200_OK
        assert refreshed_after_like.json()["total_likes"] == first.json()["total_likes"] + 1
        assert store.query_counts["list_creator_clips"] == 3

        # Retention curve caches on repeat, then invalidates when new telemetry arrives
        ret_1 = await client.get(
            f"/api/v1/analytics/clips/{clip_public.id}/retention",
            params={"period": "30d"},
            headers=creator_headers,
        )
        assert ret_1.status_code == status.HTTP_200_OK
        count_after_ret_1 = store.query_counts["clip_get_by_id"]

        ret_2 = await client.get(
            f"/api/v1/analytics/clips/{clip_public.id}/retention",
            params={"period": "30d"},
            headers=creator_headers,
        )
        assert ret_2.status_code == status.HTTP_200_OK
        assert store.query_counts["clip_get_by_id"] == count_after_ret_1

        tel_resp = await client.post(
            f"/api/v1/clips/{clip_public.id}/telemetry",
            json={
                "session_token": str(uuid4()),
                "watched_seconds": 25.0,
                "max_progress_pct": 100,
                "reached_buckets": [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100],
            },
        )
        assert tel_resp.status_code == status.HTTP_200_OK

        ret_3 = await client.get(
            f"/api/v1/analytics/clips/{clip_public.id}/retention",
            params={"period": "30d"},
            headers=creator_headers,
        )
        assert ret_3.status_code == status.HTTP_200_OK
        assert ret_3.json()["total_sessions"] == 1
        assert store.query_counts["clip_get_by_id"] > count_after_ret_1
