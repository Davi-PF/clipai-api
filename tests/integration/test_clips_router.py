"""End-to-end integration tests for /api/v1/games, /api/v1/tags, and /api/v1/clips/*."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
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
from app.models.clip import Clip, Tag
from app.models.game import Game
from app.models.user import User
from app.repositories.tag_repository import normalize_tag_slug
from app.routers.clips import get_storage_service as get_clips_storage_service
from app.routers.games import get_storage_service as get_games_storage_service
from app.services.storage_service import StorageService, StoredObjectMetadata


class _InMemoryClipStore:
    """In-memory backing store for clip/game/tag/user repositories in integration tests."""

    def __init__(self) -> None:
        self.users: dict[UUID, User] = {}
        self.games: dict[UUID, Game] = {}
        self.tags: dict[str, Tag] = {}
        self.clips: dict[UUID, Clip] = {}


class _FakeUserRepo:
    def __init__(self, store: _InMemoryClipStore, _session: AsyncSession) -> None:
        self._store = store

    async def get_by_id(self, user_id: UUID) -> User | None:
        return self._store.users.get(user_id)


class _FakeGameRepo:
    def __init__(self, store: _InMemoryClipStore, _session: AsyncSession) -> None:
        self._store = store

    async def get_by_id(self, game_id: UUID) -> Game | None:
        return self._store.games.get(game_id)

    async def list_games(
        self,
        *,
        q: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Game], int]:
        games = sorted(self._store.games.values(), key=lambda g: g.name)
        if q and q.strip():
            term = q.strip().lower()
            games = [g for g in games if term in g.name.lower()]
        total = len(games)
        start = (page - 1) * page_size
        return games[start : start + page_size], total


class _FakeTagRepo:
    def __init__(self, store: _InMemoryClipStore, _session: AsyncSession) -> None:
        self._store = store

    async def list_tags(self, *, q: str | None = None, limit: int = 20) -> list[Tag]:
        tags = sorted(self._store.tags.values(), key=lambda t: t.name)
        if q and q.strip():
            term = q.strip().lower()
            tags = [t for t in tags if term in t.name.lower() or term in t.slug]
        return tags[:limit]

    async def get_or_create_many(self, raw_tags: list[str]) -> list[Tag]:
        result: list[Tag] = []
        seen: set[str] = set()
        for raw in raw_tags:
            slug = normalize_tag_slug(raw)
            if not slug or slug in seen:
                continue
            seen.add(slug)
            tag = self._store.tags.get(slug)
            if tag is None:
                tag = Tag(id=uuid4(), name=slug, slug=slug, created_at=datetime.now(UTC))
                self._store.tags[slug] = tag
            result.append(tag)
        return result


class _FakeClipRepo:
    def __init__(self, store: _InMemoryClipStore, _session: AsyncSession) -> None:
        self._store = store

    async def get_by_id(self, clip_id: UUID) -> Clip | None:
        return self._store.clips.get(clip_id)

    async def get_by_slug(self, slug: str) -> Clip | None:
        return next((c for c in self._store.clips.values() if c.slug == slug), None)

    async def get_by_identifier(self, identifier: str) -> Clip | None:
        try:
            clip_id = UUID(identifier)
        except ValueError:
            return await self.get_by_slug(identifier)
        return await self.get_by_id(clip_id)

    async def count_pending_by_user(self, user_id: UUID) -> int:
        return sum(
            1
            for c in self._store.clips.values()
            if c.user_id == user_id and c.status == "pending_upload"
        )

    async def create(
        self,
        *,
        clip_id: UUID,
        slug: str,
        user_id: UUID,
        game_id: UUID | None,
        title: str,
        description: str | None,
        privacy: str,
        video_object_key: str,
        thumbnail_object_key: str | None,
        file_size_bytes: int,
        duration_seconds: Decimal,
        mime_type: str,
        technical_metadata: dict[str, Any],
        tags: list[Tag],
    ) -> Clip:
        now = datetime.now(UTC)
        clip = Clip(
            id=clip_id,
            slug=slug,
            user_id=user_id,
            game_id=game_id,
            title=title,
            description=description,
            status="pending_upload",
            privacy=privacy,
            video_object_key=video_object_key,
            thumbnail_object_key=thumbnail_object_key,
            file_size_bytes=file_size_bytes,
            duration_seconds=duration_seconds,
            mime_type=mime_type,
            technical_metadata=technical_metadata,
            views_count=0,
            likes_count=0,
            favorites_count=0,
            author=self._store.users[user_id],
            game=self._store.games.get(game_id) if game_id else None,
            tags=list(tags),
            created_at=now,
            updated_at=now,
        )
        self._store.clips[clip.id] = clip
        return clip

    async def replace_tags(self, clip: Clip, tags: list[Tag]) -> None:
        clip.tags = list(tags)

    async def delete(self, clip: Clip) -> None:
        self._store.clips.pop(clip.id, None)

    async def get_user_liked_clip_ids(
        self,
        _user_id: UUID,
        _clip_ids: list[UUID],
    ) -> set[UUID]:
        return set()

    async def get_user_favorited_clip_ids(
        self,
        _user_id: UUID,
        _clip_ids: list[UUID],
    ) -> set[UUID]:
        return set()

    async def list_clips(
        self,
        *,
        q: str | None = None,
        game_id: UUID | None = None,
        game_slug: str | None = None,
        tag_slug: str | None = None,
        author_username: str | None = None,
        mine: bool = False,
        favorited_by_user_id: UUID | None = None,
        collection_id: UUID | None = None,
        current_user_id: UUID | None = None,
        sort: str = "recent",
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Clip], int]:
        _ = (favorited_by_user_id, collection_id)
        clips = [c for c in self._store.clips.values() if c.status == "ready"]
        clips = self._filter_visibility(clips, mine=mine, current_user_id=current_user_id)
        clips = self._filter_metadata(
            clips,
            q=q,
            game_id=game_id,
            game_slug=game_slug,
            tag_slug=tag_slug,
            author_username=author_username,
        )
        if sort == "popular":
            clips.sort(key=lambda c: (c.likes_count, c.views_count, c.created_at), reverse=True)
        else:
            clips.sort(key=lambda c: c.created_at, reverse=True)
        total = len(clips)
        start = (page - 1) * page_size
        return clips[start : start + page_size], total

    @staticmethod
    def _filter_visibility(
        clips: list[Clip],
        *,
        mine: bool,
        current_user_id: UUID | None,
    ) -> list[Clip]:
        if mine and current_user_id is not None:
            return [c for c in clips if c.user_id == current_user_id]
        if current_user_id is None:
            return [c for c in clips if c.privacy == "public"]
        return [
            c
            for c in clips
            if c.privacy in ("public", "friends")
            or (c.privacy == "unlisted" and c.user_id == current_user_id)
        ]

    @staticmethod
    def _filter_metadata(
        clips: list[Clip],
        *,
        q: str | None,
        game_id: UUID | None,
        game_slug: str | None,
        tag_slug: str | None,
        author_username: str | None,
    ) -> list[Clip]:
        if q and q.strip():
            term = q.strip().lower()
            clips = [
                c
                for c in clips
                if term in c.title.lower()
                or (c.description and term in c.description.lower())
                or (c.game and term in c.game.name.lower())
            ]
        if game_id is not None:
            clips = [c for c in clips if c.game_id == game_id]
        if game_slug and game_slug.strip():
            slug = game_slug.strip().lower()
            clips = [c for c in clips if c.game and c.game.slug == slug]
        if tag_slug and tag_slug.strip():
            ts = tag_slug.strip().lower()
            clips = [c for c in clips if any(t.slug == ts for t in c.tags)]
        if author_username and author_username.strip():
            au = author_username.strip().lower()
            clips = [c for c in clips if c.author.username.lower() == au]
        return clips


@pytest.fixture
def clips_test_env(
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """Provide wired FastAPI app with in-memory clip store and mocked StorageService."""
    store = _InMemoryClipStore()
    now = datetime.now(UTC)

    cs2 = Game(id=uuid4(), name="CS2", slug="cs2", created_at=now)
    valorant = Game(id=uuid4(), name="Valorant", slug="valorant", created_at=now)
    store.games[cs2.id] = cs2
    store.games[valorant.id] = valorant

    owner = User(
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
    other_user = User(
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
    unverified_user = User(
        id=uuid4(),
        username="novato",
        email="novato@clipai.com.br",
        password_hash="hashed",
        is_email_verified=False,
        is_active=True,
        is_2fa_enabled=False,
        created_at=now,
        updated_at=now,
    )
    for user in (owner, other_user, unverified_user):
        store.users[user.id] = user

    monkeypatch.setattr("app.core.dependencies.UserRepository", lambda s: _FakeUserRepo(store, s))
    monkeypatch.setattr(
        "app.services.clip_service.GameRepository", lambda s: _FakeGameRepo(store, s)
    )
    monkeypatch.setattr("app.services.clip_service.TagRepository", lambda s: _FakeTagRepo(store, s))
    monkeypatch.setattr(
        "app.services.clip_service.ClipRepository", lambda s: _FakeClipRepo(store, s)
    )

    storage_mock = MagicMock(spec=StorageService)
    storage_mock.build_video_key.side_effect = StorageService.build_video_key
    storage_mock.build_thumbnail_key.side_effect = StorageService.build_thumbnail_key
    storage_mock.build_public_url.side_effect = lambda key: f"https://cdn.clipai.test/{key}"
    storage_mock.generate_presigned_upload_url = AsyncMock(
        side_effect=lambda key, _ct, expires_in=600: (
            f"https://r2.test/upload/{key}?exp={expires_in}"
        )
    )

    async def _fake_verify_object(object_key: str) -> StoredObjectMetadata:
        if "thumb." in object_key:
            return StoredObjectMetadata(content_length=180_000, content_type="image/webp")
        return StoredObjectMetadata(content_length=12_000_000, content_type="video/mp4")

    storage_mock.verify_object_exists = AsyncMock(side_effect=_fake_verify_object)
    storage_mock.delete_objects = AsyncMock()

    application = create_app(settings=test_settings)
    db_session = AsyncMock(spec=AsyncSession)

    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    application.dependency_overrides[get_db] = _override_get_db
    application.dependency_overrides[get_clips_storage_service] = lambda: storage_mock
    application.dependency_overrides[get_games_storage_service] = lambda: storage_mock

    return {
        "app": application,
        "store": store,
        "storage": storage_mock,
        "owner": owner,
        "owner_token": create_access_token(owner.id, test_settings),
        "other": other_user,
        "other_token": create_access_token(other_user.id, test_settings),
        "unverified_token": create_access_token(unverified_user.id, test_settings),
        "cs2": cs2,
        "valorant": valorant,
    }


async def test_games_tags_and_upload_confirm_lifecycle(clips_test_env: dict[str, Any]) -> None:
    """Verify games/tags discovery, presigned upload generation, and R2 HeadObject confirm."""
    app = clips_test_env["app"]
    cs2: Game = clips_test_env["cs2"]
    owner_headers = {"Authorization": f"Bearer {clips_test_env['owner_token']}"}
    unverified_headers = {"Authorization": f"Bearer {clips_test_env['unverified_token']}"}
    other_headers = {"Authorization": f"Bearer {clips_test_env['other_token']}"}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        games_resp = await client.get("/api/v1/games", params={"q": "cs"})
        assert games_resp.status_code == status.HTTP_200_OK
        assert len(games_resp.json()["items"]) == 1
        assert games_resp.json()["items"][0]["slug"] == "cs2"

        # Unverified user blocked from uploading
        blocked = await client.post(
            "/api/v1/clips/upload-url",
            headers=unverified_headers,
            json={
                "title": "Tentativa sem verificacao",
                "video_mime_type": "video/mp4",
                "video_size_bytes": 1000,
                "duration_seconds": 15,
            },
        )
        assert blocked.status_code == status.HTTP_403_FORBIDDEN
        assert blocked.json()["error"]["code"] == "EMAIL_NOT_VERIFIED"

        # Invalid MIME rejected with 415
        bad_mime = await client.post(
            "/api/v1/clips/upload-url",
            headers=owner_headers,
            json={
                "title": "Formato errado",
                "video_mime_type": "video/x-msvideo",
                "video_size_bytes": 1000,
                "duration_seconds": 15,
            },
        )
        assert bad_mime.status_code == status.HTTP_415_UNSUPPORTED_MEDIA_TYPE

        # Valid upload-url request creates pending_upload and signs R2 URLs
        upload_resp = await client.post(
            "/api/v1/clips/upload-url",
            headers=owner_headers,
            json={
                "title": "Clutch 1v4 de AWP na Mirage",
                "description": "Round decisivo",
                "game_id": str(cs2.id),
                "privacy": "public",
                "tags": ["Clutch", "AWP Mirage", "clutch"],
                "video_mime_type": "video/mp4",
                "video_size_bytes": 12_000_000,
                "duration_seconds": 34.5,
                "thumbnail_mime_type": "image/webp",
                "thumbnail_size_bytes": 180_000,
            },
        )
        assert upload_resp.status_code == status.HTTP_201_CREATED
        upload_data = upload_resp.json()
        clip_id = upload_data["clip_id"]
        slug = upload_data["slug"]
        assert upload_data["status"] == "pending_upload"
        assert upload_data["video_upload"]["headers"] == {"Content-Type": "video/mp4"}
        assert upload_data["thumbnail_upload"]["headers"] == {"Content-Type": "image/webp"}

        # Pending clip is hidden from third parties
        hidden = await client.get(f"/api/v1/clips/{slug}", headers=other_headers)
        assert hidden.status_code == status.HTTP_404_NOT_FOUND

        # Non-owner cannot confirm
        forbid_confirm = await client.post(
            f"/api/v1/clips/{clip_id}/confirm-upload",
            headers=other_headers,
        )
        assert forbid_confirm.status_code == status.HTTP_403_FORBIDDEN

        # Owner confirms -> status transitions to ready
        confirm_resp = await client.post(
            f"/api/v1/clips/{clip_id}/confirm-upload",
            headers=owner_headers,
        )
        assert confirm_resp.status_code == status.HTTP_200_OK
        confirmed = confirm_resp.json()
        assert confirmed["status"] == "ready"
        assert confirmed["slug"] == slug
        assert confirmed["tags"] == ["clutch", "awp-mirage"]
        assert confirmed["video_url"].startswith("https://cdn.clipai.test/clips/")

        # Tags endpoint now lists created tags
        tags_resp = await client.get("/api/v1/tags", params={"q": "awp"})
        assert tags_resp.status_code == status.HTTP_200_OK
        assert [t["slug"] for t in tags_resp.json()["items"]] == ["awp-mirage"]


async def test_search_filters_privacy_edit_and_delete(clips_test_env: dict[str, Any]) -> None:
    """Verify search, filters, privacy visibility, edit, delete, and R2 cleanup."""
    app = clips_test_env["app"]
    store: _InMemoryClipStore = clips_test_env["store"]
    storage: MagicMock = clips_test_env["storage"]
    cs2: Game = clips_test_env["cs2"]
    valorant: Game = clips_test_env["valorant"]
    owner_headers = {"Authorization": f"Bearer {clips_test_env['owner_token']}"}
    other_headers = {"Authorization": f"Bearer {clips_test_env['other_token']}"}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        created: dict[str, dict[str, Any]] = {}
        for title, game, privacy, tags in (
            ("Ace Mirage Publico", cs2, "public", ["ace", "cs2"]),
            ("Clutch Bind Amigos", valorant, "friends", ["clutch"]),
            ("Treino Smoke Oculto", cs2, "unlisted", ["smoke"]),
        ):
            up = await client.post(
                "/api/v1/clips/upload-url",
                headers=owner_headers,
                json={
                    "title": title,
                    "game_id": str(game.id),
                    "privacy": privacy,
                    "tags": tags,
                    "video_mime_type": "video/webm",
                    "video_size_bytes": 5_000_000,
                    "duration_seconds": 20.0,
                },
            )
            assert up.status_code == status.HTTP_201_CREATED
            cid = up.json()["clip_id"]
            conf = await client.post(f"/api/v1/clips/{cid}/confirm", headers=owner_headers)
            assert conf.status_code == status.HTTP_200_OK
            created[privacy] = conf.json()

        # Anonymous sees only public
        anon_list = await client.get("/api/v1/clips")
        assert anon_list.status_code == status.HTTP_200_OK
        assert [c["privacy"] for c in anon_list.json()["items"]] == ["public"]

        # Other logged user sees public + friends, NOT owner's unlisted
        other_list = await client.get("/api/v1/clips", headers=other_headers)
        assert {c["privacy"] for c in other_list.json()["items"]} == {"public", "friends"}

        # Owner sees all 3 with mine=true
        mine_list = await client.get("/api/v1/clips", params={"mine": True}, headers=owner_headers)
        assert mine_list.json()["total"] == 3

        # Search by q, game slug, tag, and author
        filtered = await client.get(
            "/api/v1/clips",
            params={"q": "mirage", "game": "cs2", "tag": "ace", "author": "fallen_br"},
        )
        assert filtered.json()["total"] == 1
        assert filtered.json()["items"][0]["slug"] == created["public"]["slug"]

        # Detail privacy: unlisted accessible anonymously, friends blocked anonymously
        unlisted_slug = created["unlisted"]["slug"]
        friends_slug = created["friends"]["slug"]
        assert (await client.get(f"/api/v1/clips/{unlisted_slug}")).status_code == 200
        assert (await client.get(f"/api/v1/clips/{friends_slug}")).status_code == 401
        assert (
            await client.get(f"/api/v1/clips/{friends_slug}", headers=other_headers)
        ).status_code == 200

        # Edit clip (owner ok, non-owner 403)
        pub_id = created["public"]["id"]
        forbid_patch = await client.patch(
            f"/api/v1/clips/{pub_id}",
            headers=other_headers,
            json={"title": "Tentativa"},
        )
        assert forbid_patch.status_code == status.HTTP_403_FORBIDDEN

        patch_resp = await client.patch(
            f"/api/v1/clips/{pub_id}",
            headers=owner_headers,
            json={"title": "Ace Mirage Atualizado", "tags": ["highlight"], "privacy": "unlisted"},
        )
        assert patch_resp.status_code == status.HTTP_200_OK
        assert patch_resp.json()["title"] == "Ace Mirage Atualizado"
        assert patch_resp.json()["tags"] == ["highlight"]
        assert patch_resp.json()["privacy"] == "unlisted"

        # Delete clip removes from DB and calls R2 delete_objects
        forbid_del = await client.delete(f"/api/v1/clips/{pub_id}", headers=other_headers)
        assert forbid_del.status_code == status.HTTP_403_FORBIDDEN

        del_resp = await client.delete(f"/api/v1/clips/{pub_id}", headers=owner_headers)
        assert del_resp.status_code == status.HTTP_204_NO_CONTENT
        assert UUID(pub_id) not in store.clips
        storage.delete_objects.assert_awaited()


async def test_confirm_head_object_failure_and_upload_rate_limit(
    clips_test_env: dict[str, Any],
) -> None:
    """Mark clip failed on HeadObject error and enforce 10/hour upload-url rate limit."""
    app = clips_test_env["app"]
    store: _InMemoryClipStore = clips_test_env["store"]
    storage: MagicMock = clips_test_env["storage"]
    owner_headers = {"Authorization": f"Bearer {clips_test_env['owner_token']}"}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        up = await client.post(
            "/api/v1/clips/upload-url",
            headers=owner_headers,
            json={
                "title": "Falha no R2",
                "video_mime_type": "video/mp4",
                "video_size_bytes": 2_000_000,
                "duration_seconds": 12.0,
            },
        )
        clip_id = up.json()["clip_id"]

        storage.verify_object_exists.side_effect = _upload_verification_error()
        fail_confirm = await client.post(
            f"/api/v1/clips/{clip_id}/confirm-upload",
            headers=owner_headers,
        )
        assert fail_confirm.status_code == status.HTTP_400_BAD_REQUEST
        assert fail_confirm.json()["error"]["code"] == "UPLOAD_VERIFICATION_FAILED"
        assert store.clips[UUID(clip_id)].status == "failed"

        # Reset storage mock and hit 10/hour upload-url limit (1 used above + 9 more)
        storage.verify_object_exists.side_effect = lambda key: (
            StoredObjectMetadata(content_length=180_000, content_type="image/webp")
            if "thumb." in key
            else StoredObjectMetadata(content_length=1_000_000, content_type="video/mp4")
        )
        for idx in range(9):
            resp = await client.post(
                "/api/v1/clips/upload-url",
                headers=owner_headers,
                json={
                    "title": f"Clipe {idx}",
                    "video_mime_type": "video/mp4",
                    "video_size_bytes": 1_000_000,
                    "duration_seconds": 10.0,
                },
            )
            assert resp.status_code == status.HTTP_201_CREATED
            cid = resp.json()["clip_id"]
            await client.post(f"/api/v1/clips/{cid}/confirm-upload", headers=owner_headers)

        limited = await client.post(
            "/api/v1/clips/upload-url",
            headers=owner_headers,
            json={
                "title": "Excedeu limite",
                "video_mime_type": "video/mp4",
                "video_size_bytes": 1_000_000,
                "duration_seconds": 10.0,
            },
        )
        assert limited.status_code == status.HTTP_429_TOO_MANY_REQUESTS
        assert limited.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"


async def test_cancel_upload_removes_pending_clip_and_r2_objects_and_guards_ready(
    clips_test_env: dict[str, Any],
) -> None:
    """Verify POST /clips/{id}/cancel-upload cleans DB + R2 and rejects ready/non-owner."""
    app = clips_test_env["app"]
    store: _InMemoryClipStore = clips_test_env["store"]
    storage: MagicMock = clips_test_env["storage"]
    cs2: Game = clips_test_env["cs2"]
    owner_headers = {"Authorization": f"Bearer {clips_test_env['owner_token']}"}
    other_headers = {"Authorization": f"Bearer {clips_test_env['other_token']}"}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        up = await client.post(
            "/api/v1/clips/upload-url",
            headers=owner_headers,
            json={
                "title": "Envio cancelado no meio",
                "game_id": str(cs2.id),
                "video_mime_type": "video/mp4",
                "video_size_bytes": 5_000_000,
                "duration_seconds": 15.0,
                "thumbnail_mime_type": "image/webp",
                "thumbnail_size_bytes": 120_000,
            },
        )
        assert up.status_code == status.HTTP_201_CREATED
        pending_id = up.json()["clip_id"]
        pending_clip = store.clips[UUID(pending_id)]
        expected_keys = [pending_clip.video_object_key, pending_clip.thumbnail_object_key]

        forbid_cancel = await client.post(
            f"/api/v1/clips/{pending_id}/cancel-upload",
            headers=other_headers,
        )
        assert forbid_cancel.status_code == status.HTTP_403_FORBIDDEN

        cancel_resp = await client.post(
            f"/api/v1/clips/{pending_id}/cancel-upload",
            headers=owner_headers,
        )
        assert cancel_resp.status_code == status.HTTP_204_NO_CONTENT
        assert UUID(pending_id) not in store.clips
        storage.delete_objects.assert_awaited_with(expected_keys)

        missing_cancel = await client.post(
            f"/api/v1/clips/{pending_id}/cancel-upload",
            headers=owner_headers,
        )
        assert missing_cancel.status_code == status.HTTP_404_NOT_FOUND

        # Ready clips cannot be deleted via cancel-upload
        up_ready = await client.post(
            "/api/v1/clips/upload-url",
            headers=owner_headers,
            json={
                "title": "Envio finalizado",
                "game_id": str(cs2.id),
                "video_mime_type": "video/mp4",
                "video_size_bytes": 5_000_000,
                "duration_seconds": 15.0,
            },
        )
        ready_id = up_ready.json()["clip_id"]
        confirm_resp = await client.post(
            f"/api/v1/clips/{ready_id}/confirm-upload",
            headers=owner_headers,
        )
        assert confirm_resp.status_code == status.HTTP_200_OK

        cancel_ready = await client.post(
            f"/api/v1/clips/{ready_id}/cancel-upload",
            headers=owner_headers,
        )
        assert cancel_ready.status_code == status.HTTP_400_BAD_REQUEST
        assert cancel_ready.json()["error"]["code"] == "BAD_REQUEST"
        assert UUID(ready_id) in store.clips


def _upload_verification_error() -> Exception:
    from app.core.exceptions import AppError

    return AppError(
        status_code=status.HTTP_400_BAD_REQUEST,
        code="UPLOAD_VERIFICATION_FAILED",
        message="Não foi possível confirmar o envio do arquivo no servidor de mídia.",
    )
