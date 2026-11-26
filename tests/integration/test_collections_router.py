"""End-to-end integration tests for likes, favorites, views, and collections CRUD."""

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
from app.models.clip import Clip
from app.models.collection import Collection, CollectionClip
from app.models.game import Game
from app.models.user import User
from app.routers.clips import get_storage_service as get_clips_storage_service
from app.services.storage_service import StorageService


class _InMemorySocialStore:
    """Stateful in-memory store for users, clips, likes, favorites, and collections."""

    def __init__(self) -> None:
        self.users: dict[UUID, User] = {}
        self.clips: dict[UUID, Clip] = {}
        self.likes: set[tuple[UUID, UUID]] = set()
        self.favorites: set[tuple[UUID, UUID]] = set()
        self.collections: dict[UUID, Collection] = {}
        self.collection_clips: dict[tuple[UUID, UUID], CollectionClip] = {}


class _FakeUserRepo:
    def __init__(self, store: _InMemorySocialStore, _session: AsyncSession) -> None:
        self._store = store

    async def get_by_id(self, user_id: UUID) -> User | None:
        return self._store.users.get(user_id)


class _FakeClipRepo:
    def __init__(self, store: _InMemorySocialStore, _session: AsyncSession) -> None:
        self._store = store

    async def get_by_id(self, clip_id: UUID) -> Clip | None:
        return self._store.clips.get(clip_id)

    async def get_by_identifier(self, identifier: str) -> Clip | None:
        try:
            return self._store.clips.get(UUID(identifier))
        except ValueError:
            return next((c for c in self._store.clips.values() if c.slug == identifier), None)

    async def delete(self, clip: Clip) -> None:
        self._store.clips.pop(clip.id, None)
        # Simulate ON DELETE CASCADE in PostgreSQL
        self._store.likes = {pair for pair in self._store.likes if pair[1] != clip.id}
        self._store.favorites = {pair for pair in self._store.favorites if pair[1] != clip.id}
        self._store.collection_clips = {
            key: link for key, link in self._store.collection_clips.items() if key[1] != clip.id
        }

    async def like_clip(self, *, user_id: UUID, clip_id: UUID) -> int:
        clip = self._store.clips[clip_id]
        if (user_id, clip_id) not in self._store.likes:
            self._store.likes.add((user_id, clip_id))
            clip.likes_count += 1
        return clip.likes_count

    async def unlike_clip(self, *, user_id: UUID, clip_id: UUID) -> int:
        clip = self._store.clips[clip_id]
        if (user_id, clip_id) in self._store.likes:
            self._store.likes.remove((user_id, clip_id))
            clip.likes_count = max(clip.likes_count - 1, 0)
        return clip.likes_count

    async def favorite_clip(self, *, user_id: UUID, clip_id: UUID) -> int:
        clip = self._store.clips[clip_id]
        if (user_id, clip_id) not in self._store.favorites:
            self._store.favorites.add((user_id, clip_id))
            clip.favorites_count += 1
        return clip.favorites_count

    async def unfavorite_clip(self, *, user_id: UUID, clip_id: UUID) -> int:
        clip = self._store.clips[clip_id]
        if (user_id, clip_id) in self._store.favorites:
            self._store.favorites.remove((user_id, clip_id))
            clip.favorites_count = max(clip.favorites_count - 1, 0)
        return clip.favorites_count

    async def increment_views(self, clip_id: UUID, *, viewer_id: UUID | None = None) -> int:
        _ = viewer_id
        clip = self._store.clips[clip_id]
        clip.views_count += 1
        return clip.views_count

    async def get_user_liked_clip_ids(self, user_id: UUID, clip_ids: list[UUID]) -> set[UUID]:
        return {cid for cid in clip_ids if (user_id, cid) in self._store.likes}

    async def get_user_favorited_clip_ids(self, user_id: UUID, clip_ids: list[UUID]) -> set[UUID]:
        return {cid for cid in clip_ids if (user_id, cid) in self._store.favorites}

    async def get_top_highlight_clip_id(
        self,
        *,
        current_user_id: UUID | None = None,
        period: str = "all",
    ) -> UUID | None:
        _ = (current_user_id, period)
        eligible = [
            c
            for c in self._store.clips.values()
            if c.status == "ready" and (c.favorites_count > 0 or c.likes_count > 0)
        ]
        if not eligible:
            return None
        eligible.sort(
            key=lambda c: (
                c.favorites_count * 2 + c.likes_count,
                c.favorites_count,
                c.likes_count,
                c.views_count,
                c.created_at,
            ),
            reverse=True,
        )
        return eligible[0].id

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
        period: str = "all",
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Clip], int]:
        _ = (q, game_id, game_slug, tag_slug, author_username, period)
        clips = [c for c in self._store.clips.values() if c.status == "ready"]
        if mine and current_user_id is not None:
            clips = [c for c in clips if c.user_id == current_user_id]
        elif current_user_id is None:
            clips = [c for c in clips if c.privacy == "public"]
        else:
            clips = [
                c
                for c in clips
                if c.privacy in ("public", "friends")
                or (c.privacy == "unlisted" and c.user_id == current_user_id)
            ]

        if favorited_by_user_id is not None:
            clips = [c for c in clips if (favorited_by_user_id, c.id) in self._store.favorites]
        if collection_id is not None:
            coll_clip_ids = {
                cid for (col_id, cid) in self._store.collection_clips if col_id == collection_id
            }
            clips = [c for c in clips if c.id in coll_clip_ids]

        if sort in ("popular", "featured"):
            clips.sort(
                key=lambda c: (
                    c.likes_count * 3 + c.favorites_count * 2 + c.views_count,
                    c.likes_count,
                    c.views_count,
                    c.created_at,
                ),
                reverse=True,
            )
        else:
            clips.sort(key=lambda c: c.created_at, reverse=True)

        total = len(clips)
        start = (page - 1) * page_size
        return clips[start : start + page_size], total


class _FakeCollectionRepo:
    def __init__(self, store: _InMemorySocialStore, _session: AsyncSession) -> None:
        self._store = store

    async def get_by_id(self, collection_id: UUID) -> Collection | None:
        return self._store.collections.get(collection_id)

    async def create(
        self,
        *,
        user_id: UUID,
        title: str,
        description: str | None,
        privacy: str,
        accent_color: str | None,
    ) -> Collection:
        now = datetime.now(UTC)
        coll = Collection(
            id=uuid4(),
            user_id=user_id,
            title=title,
            description=description,
            privacy=privacy,
            accent_color=accent_color,
            owner=self._store.users[user_id],
            created_at=now,
            updated_at=now,
        )
        self._store.collections[coll.id] = coll
        return coll

    async def delete(self, collection: Collection) -> None:
        self._store.collections.pop(collection.id, None)
        self._store.collection_clips = {
            key: link
            for key, link in self._store.collection_clips.items()
            if key[0] != collection.id
        }

    async def list_collections(
        self,
        *,
        mine: bool = False,
        author_username: str | None = None,
        current_user_id: UUID | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Collection], int]:
        items = list(self._store.collections.values())
        if mine and current_user_id is not None:
            items = [c for c in items if c.user_id == current_user_id]
        elif current_user_id is None:
            items = [c for c in items if c.privacy == "public"]
        else:
            items = [
                c
                for c in items
                if c.privacy in ("public", "friends")
                or (c.privacy == "unlisted" and c.user_id == current_user_id)
            ]
        if author_username and author_username.strip():
            au = author_username.strip().lower()
            items = [c for c in items if c.owner.username.lower() == au]
        items.sort(key=lambda c: c.created_at, reverse=True)
        total = len(items)
        start = (page - 1) * page_size
        return items[start : start + page_size], total

    async def get_collections_preview_metadata(
        self,
        collection_ids: list[UUID],
        *,
        current_user_id: UUID | None,
        preview_limit: int = 4,
    ) -> dict[UUID, tuple[int, list[str]]]:
        result: dict[UUID, tuple[int, list[str]]] = {}
        for coll_id in collection_ids:
            links = self._visible_links(coll_id, current_user_id)
            thumbs = [
                lnk.clip.thumbnail_object_key
                for lnk in sorted(links, key=lambda item: item.added_at, reverse=True)
                if lnk.clip.thumbnail_object_key
            ][:preview_limit]
            result[coll_id] = (len(links), thumbs)
        return result

    async def list_collection_clips(
        self,
        collection_id: UUID,
        *,
        current_user_id: UUID | None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[CollectionClip], int]:
        links = self._visible_links(collection_id, current_user_id)
        links.sort(key=lambda item: (item.position, item.added_at))
        total = len(links)
        start = (page - 1) * page_size
        return links[start : start + page_size], total

    def _visible_links(
        self,
        collection_id: UUID,
        current_user_id: UUID | None,
    ) -> list[CollectionClip]:
        result: list[CollectionClip] = []
        for (cid, _clip_id), link in self._store.collection_clips.items():
            if cid != collection_id or link.clip.status != "ready":
                continue
            if current_user_id is None and link.clip.privacy == "friends":
                continue
            result.append(link)
        return result

    async def get_clip_link(self, collection_id: UUID, clip_id: UUID) -> CollectionClip | None:
        return self._store.collection_clips.get((collection_id, clip_id))

    async def get_next_position(self, collection_id: UUID) -> int:
        positions = [
            link.position
            for (cid, _), link in self._store.collection_clips.items()
            if cid == collection_id
        ]
        return max(positions) + 1 if positions else 0

    async def add_clip(
        self,
        *,
        collection_id: UUID,
        clip_id: UUID,
        position: int,
    ) -> CollectionClip:
        link = CollectionClip(
            collection_id=collection_id,
            clip_id=clip_id,
            position=position,
            added_at=datetime.now(UTC),
            clip=self._store.clips[clip_id],
        )
        self._store.collection_clips[(collection_id, clip_id)] = link
        return link

    async def remove_clip(self, *, collection_id: UUID, clip_id: UUID) -> None:
        self._store.collection_clips.pop((collection_id, clip_id), None)


@pytest.fixture
def social_test_env(
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """Provide FastAPI app wired with in-memory clips, interactions, and collections."""
    store = _InMemorySocialStore()
    now = datetime.now(UTC)

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
    other = User(
        id=uuid4(),
        username="fer_br",
        email="fer@clipai.com.br",
        password_hash="hashed",
        display_name="fer",
        is_email_verified=True,
        is_active=True,
        is_2fa_enabled=False,
        created_at=now,
        updated_at=now,
    )
    for user in (owner, other):
        store.users[user.id] = user

    cs2 = Game(id=uuid4(), name="CS2", slug="cs2", created_at=now)
    clip_a_id = uuid4()
    clip_b_id = uuid4()
    clip_a = Clip(
        id=clip_a_id,
        slug="clip-alpha-1",
        user_id=owner.id,
        game_id=cs2.id,
        title="Ace na Mirage",
        description="Clutch",
        status="ready",
        privacy="public",
        video_object_key=f"clips/{owner.id}/{clip_a_id}/video.mp4",
        thumbnail_object_key=f"clips/{owner.id}/{clip_a_id}/thumb.webp",
        file_size_bytes=4_000_000,
        duration_seconds=Decimal("22.50"),
        mime_type="video/mp4",
        technical_metadata={},
        views_count=10,
        likes_count=0,
        favorites_count=0,
        author=owner,
        game=cs2,
        tags=[],
        created_at=now,
        updated_at=now,
    )
    clip_b = Clip(
        id=clip_b_id,
        slug="clip-beta-2",
        user_id=owner.id,
        game_id=cs2.id,
        title="Flick de AWP",
        description="Highlight",
        status="ready",
        privacy="public",
        video_object_key=f"clips/{owner.id}/{clip_b_id}/video.mp4",
        thumbnail_object_key=f"clips/{owner.id}/{clip_b_id}/thumb.webp",
        file_size_bytes=6_000_000,
        duration_seconds=Decimal("18.00"),
        mime_type="video/mp4",
        technical_metadata={},
        views_count=1,
        likes_count=0,
        favorites_count=0,
        author=owner,
        game=cs2,
        tags=[],
        created_at=now,
        updated_at=now,
    )
    store.clips[clip_a.id] = clip_a
    store.clips[clip_b.id] = clip_b

    monkeypatch.setattr("app.core.dependencies.UserRepository", lambda s: _FakeUserRepo(store, s))
    monkeypatch.setattr(
        "app.services.clip_service.ClipRepository", lambda s: _FakeClipRepo(store, s)
    )
    monkeypatch.setattr(
        "app.services.clip_service.CollectionRepository",
        lambda s: _FakeCollectionRepo(store, s),
    )
    monkeypatch.setattr(
        "app.services.collection_service.ClipRepository",
        lambda s: _FakeClipRepo(store, s),
    )
    monkeypatch.setattr(
        "app.services.collection_service.CollectionRepository",
        lambda s: _FakeCollectionRepo(store, s),
    )

    storage_mock = MagicMock(spec=StorageService)
    storage_mock.build_public_url.side_effect = lambda key: f"https://cdn.clipai.test/{key}"
    storage_mock.delete_objects = AsyncMock()

    application = create_app(settings=test_settings)
    db_session = AsyncMock(spec=AsyncSession)

    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    application.dependency_overrides[get_db] = _override_get_db
    application.dependency_overrides[get_clips_storage_service] = lambda: storage_mock

    return {
        "app": application,
        "store": store,
        "owner": owner,
        "other": other,
        "owner_headers": {
            "Authorization": f"Bearer {create_access_token(owner.id, test_settings)}"
        },
        "other_headers": {
            "Authorization": f"Bearer {create_access_token(other.id, test_settings)}"
        },
        "clip_a": clip_a,
        "clip_b": clip_b,
    }


async def test_likes_favorites_views_enrichment_and_popular_sorting(
    social_test_env: dict[str, Any],
) -> None:
    """Verify idempotent likes/favorites, view rate limiting, enrichment, and sorting."""
    app = social_test_env["app"]
    owner_headers = social_test_env["owner_headers"]
    other_headers = social_test_env["other_headers"]
    clip_a: Clip = social_test_env["clip_a"]
    clip_b: Clip = social_test_env["clip_b"]

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        # Idempotent like (POST twice -> count remains 1)
        like_1 = await client.post(f"/api/v1/clips/{clip_b.id}/like", headers=owner_headers)
        like_2 = await client.post(f"/api/v1/clips/{clip_b.id}/like", headers=owner_headers)
        assert like_1.status_code == status.HTTP_200_OK
        assert like_1.json()["liked"] is True
        assert like_1.json()["likes_count"] == 1
        assert like_2.json()["likes_count"] == 1

        # Second user likes clip_b too -> count becomes 2
        like_other = await client.post(f"/api/v1/clips/{clip_b.id}/like", headers=other_headers)
        assert like_other.json()["likes_count"] == 2

        # Idempotent favorite + filter favorited=true & /users/me/favorites
        fav_1 = await client.post(f"/api/v1/clips/{clip_b.id}/favorite", headers=owner_headers)
        fav_2 = await client.post(f"/api/v1/clips/{clip_b.id}/favorite", headers=owner_headers)
        assert fav_1.status_code == status.HTTP_200_OK
        assert fav_1.json()["favorited"] is True
        assert fav_2.json()["favorites_count"] == 1

        fav_list = await client.get(
            "/api/v1/clips",
            params={"favorited": True},
            headers=owner_headers,
        )
        assert fav_list.json()["total"] == 1
        assert fav_list.json()["items"][0]["id"] == str(clip_b.id)
        assert fav_list.json()["items"][0]["is_liked"] is True
        assert fav_list.json()["items"][0]["is_favorited"] is True

        me_favs = await client.get("/api/v1/users/me/favorites", headers=owner_headers)
        assert me_favs.status_code == status.HTTP_200_OK
        assert me_favs.json()["total"] == 1

        # Anonymous sees is_liked=False and is_favorited=False on detail
        anon_detail = await client.get(f"/api/v1/clips/{clip_b.slug}")
        assert anon_detail.json()["is_liked"] is False
        assert anon_detail.json()["is_favorited"] is False

        # View increment + anti-F5 rate limit
        view_1 = await client.post(f"/api/v1/clips/{clip_b.id}/view")
        assert view_1.status_code == status.HTTP_200_OK
        assert view_1.json()["views_count"] == 2

        view_f5 = await client.post(f"/api/v1/clips/{clip_b.id}/views")
        assert view_f5.status_code == status.HTTP_429_TOO_MANY_REQUESTS
        assert view_f5.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"

        # Different authenticated user can still register a view
        view_other = await client.post(f"/api/v1/clips/{clip_b.id}/views", headers=other_headers)
        assert view_other.status_code == status.HTTP_200_OK
        assert view_other.json()["views_count"] == 3

        # Popular / featured sort places clip_a (10 views) vs clip_b (2*3 + 1*2 + 3 = 11 score)
        featured = await client.get("/api/v1/clips", params={"sort": "featured"})
        assert [c["id"] for c in featured.json()["items"]] == [str(clip_b.id), str(clip_a.id)]

        # Unlike and unfavorite idempotently
        unlike_1 = await client.delete(f"/api/v1/clips/{clip_b.id}/like", headers=owner_headers)
        unlike_2 = await client.delete(f"/api/v1/clips/{clip_b.id}/like", headers=owner_headers)
        assert unlike_1.json()["liked"] is False
        assert unlike_1.json()["likes_count"] == 1
        assert unlike_2.json()["likes_count"] == 1

        unfav = await client.delete(f"/api/v1/clips/{clip_b.id}/favorite", headers=owner_headers)
        assert unfav.json()["favorited"] is False
        assert unfav.json()["favorites_count"] == 0


async def test_collections_crud_clip_membership_and_cascade_on_clip_delete(
    social_test_env: dict[str, Any],
) -> None:
    """Verify full collections lifecycle, permissions, clip membership, and cascade delete."""
    app = social_test_env["app"]
    owner_headers = social_test_env["owner_headers"]
    other_headers = social_test_env["other_headers"]
    clip_a: Clip = social_test_env["clip_a"]
    clip_b: Clip = social_test_env["clip_b"]

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        # Create collection using 'name' alias + accent_color
        create_resp = await client.post(
            "/api/v1/collections",
            headers=owner_headers,
            json={
                "name": "Melhores Clutches CS2",
                "description": "Highlights 1vX",
                "privacy": "public",
                "accent_color": "#2EB886",
            },
        )
        assert create_resp.status_code == status.HTTP_201_CREATED
        coll = create_resp.json()
        coll_id = coll["id"]
        assert coll["title"] == "Melhores Clutches CS2"
        assert coll["accent_color"] == "#2EB886"
        assert coll["clips_count"] == 0

        # Non-owner cannot add clip, edit, or delete collection
        assert (
            await client.post(
                f"/api/v1/collections/{coll_id}/clips",
                headers=other_headers,
                json={"clip_id": str(clip_a.id)},
            )
        ).status_code == status.HTTP_403_FORBIDDEN

        assert (
            await client.patch(
                f"/api/v1/collections/{coll_id}",
                headers=other_headers,
                json={"title": "Invasao"},
            )
        ).status_code == status.HTTP_403_FORBIDDEN

        assert (
            await client.delete(f"/api/v1/collections/{coll_id}", headers=other_headers)
        ).status_code == status.HTTP_403_FORBIDDEN

        # Owner adds two clips (idempotent second add for clip_a)
        add_a1 = await client.post(
            f"/api/v1/collections/{coll_id}/clips",
            headers=owner_headers,
            json={"clip_id": str(clip_a.id), "position": 0},
        )
        add_a2 = await client.post(
            f"/api/v1/collections/{coll_id}/clips",
            headers=owner_headers,
            json={"clip_id": str(clip_a.id)},
        )
        add_b = await client.post(
            f"/api/v1/collections/{coll_id}/clips",
            headers=owner_headers,
            json={"clip_id": str(clip_b.id)},
        )
        assert add_a1.status_code == status.HTTP_201_CREATED
        assert add_a2.status_code == status.HTTP_201_CREATED
        assert add_b.json()["position"] == 1

        # List collections returns clips_count=2 and preview_thumbnails
        list_resp = await client.get(
            "/api/v1/collections",
            params={"mine": True},
            headers=owner_headers,
        )
        assert list_resp.status_code == status.HTTP_200_OK
        assert list_resp.json()["items"][0]["clips_count"] == 2
        assert len(list_resp.json()["items"][0]["preview_thumbnails"]) == 2

        user_colls = await client.get("/api/v1/users/fallen_br/collections")
        assert user_colls.status_code == status.HTTP_200_OK
        assert user_colls.json()["total"] == 1

        # Filter /api/v1/clips by collection_id
        coll_clips = await client.get("/api/v1/clips", params={"collection_id": coll_id})
        assert coll_clips.json()["total"] == 2

        # Collection detail returns ordered paginated clips
        detail = await client.get(f"/api/v1/collections/{coll_id}", headers=owner_headers)
        assert detail.status_code == status.HTTP_200_OK
        assert [item["clip"]["id"] for item in detail.json()["clips"]] == [
            str(clip_a.id),
            str(clip_b.id),
        ]
        assert detail.json()["clips"][0]["clip"]["file_size_bytes"] == 4_000_000
        assert detail.json()["clips"][0]["clip"]["technical_metadata"] is not None

        # Non-owner and anonymous receive None for technical_metadata and file_size_bytes
        other_coll_detail = await client.get(
            f"/api/v1/collections/{coll_id}",
            headers=other_headers,
        )
        assert other_coll_detail.status_code == status.HTTP_200_OK
        assert other_coll_detail.json()["clips"][0]["clip"]["file_size_bytes"] is None
        assert other_coll_detail.json()["clips"][0]["clip"]["technical_metadata"] is None
        assert other_coll_detail.json()["clips"][0]["clip"]["duration_seconds"] == 22.5

        anon_coll_detail = await client.get(f"/api/v1/collections/{coll_id}")
        assert anon_coll_detail.status_code == status.HTTP_200_OK
        assert anon_coll_detail.json()["clips"][0]["clip"]["file_size_bytes"] is None
        assert anon_coll_detail.json()["clips"][0]["clip"]["technical_metadata"] is None
        assert anon_coll_detail.json()["clips"][0]["clip"]["duration_seconds"] == 22.5

        # Owner edits collection
        patch_resp = await client.patch(
            f"/api/v1/collections/{coll_id}",
            headers=owner_headers,
            json={"title": "Top Clutches", "accent_color": "#E8B04B", "privacy": "unlisted"},
        )
        assert patch_resp.status_code == status.HTTP_200_OK
        assert patch_resp.json()["title"] == "Top Clutches"
        assert patch_resp.json()["accent_color"] == "#E8B04B"

        # Deleting clip_a cascades removal from collection_clips
        del_clip = await client.delete(f"/api/v1/clips/{clip_a.id}", headers=owner_headers)
        assert del_clip.status_code == status.HTTP_204_NO_CONTENT
        detail_after_cascade = await client.get(
            f"/api/v1/collections/{coll_id}",
            headers=owner_headers,
        )
        assert [item["clip"]["id"] for item in detail_after_cascade.json()["clips"]] == [
            str(clip_b.id)
        ]

        # Owner removes clip_b from collection, then deletes collection
        rem_b = await client.delete(
            f"/api/v1/collections/{coll_id}/clips/{clip_b.id}",
            headers=owner_headers,
        )
        assert rem_b.status_code == status.HTTP_204_NO_CONTENT

        del_coll = await client.delete(f"/api/v1/collections/{coll_id}", headers=owner_headers)
        assert del_coll.status_code == status.HTTP_204_NO_CONTENT
        assert (await client.get(f"/api/v1/collections/{coll_id}")).status_code == 404
