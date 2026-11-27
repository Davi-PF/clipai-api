"""End-to-end integration tests for friendships, notifications, invites, and friends privacy."""

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
from app.models.friendship import Friendship
from app.models.game import Game
from app.models.notification import Notification
from app.models.user import User, generate_invite_code
from app.routers.auth import get_email_service
from app.routers.clips import get_storage_service as get_clips_storage_service
from app.services.email_service import EmailService
from app.services.storage_service import StorageService


class _InMemoryCommunityStore:
    """In-memory backing store wiring users, friendships, notifications, clips, and collections."""

    def __init__(self) -> None:
        self.users: dict[UUID, User] = {}
        self.friendships: dict[UUID, Friendship] = {}
        self.notifications: dict[UUID, Notification] = {}
        self.clips: dict[UUID, Clip] = {}
        self.collections: dict[UUID, Collection] = {}
        self.collection_clips: dict[tuple[UUID, UUID], CollectionClip] = {}

    def are_friends(self, user_a_id: UUID, user_b_id: UUID) -> bool:
        if user_a_id == user_b_id:
            return False
        return any(
            f.status == "accepted" and {f.requester_id, f.addressee_id} == {user_a_id, user_b_id}
            for f in self.friendships.values()
        )


class _FakeUserRepo:
    def __init__(self, store: _InMemoryCommunityStore, _session: AsyncSession) -> None:
        self._store = store

    async def get_by_id(self, user_id: UUID) -> User | None:
        return self._store.users.get(user_id)

    async def get_by_email(self, email: str) -> User | None:
        norm = email.strip().lower()
        return next((u for u in self._store.users.values() if u.email.lower() == norm), None)

    async def get_by_username(self, username: str) -> User | None:
        norm = username.strip().lower()
        return next((u for u in self._store.users.values() if u.username.lower() == norm), None)

    async def get_by_invite_code(self, invite_code: str) -> User | None:
        cleaned = invite_code.strip()
        return next((u for u in self._store.users.values() if u.invite_code == cleaned), None)

    async def search_users(
        self,
        query: str,
        *,
        exclude_user_id: UUID,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[User], int]:
        cleaned = query.strip().lower()
        handle = cleaned.lstrip("@").strip()
        terms = {t for t in (cleaned, handle) if t}
        matches = [
            u
            for u in self._store.users.values()
            if u.is_active
            and u.id != exclude_user_id
            and any(
                t in u.username.lower()
                or (u.display_name and t in u.display_name.lower())
                or (u.discord_username and t in u.discord_username.lower())
                for t in terms
            )
        ]
        matches.sort(key=lambda u: u.username.lower())
        start = (page - 1) * page_size
        return matches[start : start + page_size], len(matches)

    async def create(
        self,
        *,
        username: str,
        email: str,
        password_hash: str | None = None,
        display_name: str | None = None,
        is_email_verified: bool = False,
        avatar_source: str = "none",
        discord_id: str | None = None,
        discord_username: str | None = None,
        discord_avatar_url: str | None = None,
        discord_linked_at: datetime | None = None,
        invite_code: str | None = None,
    ) -> User:
        now = datetime.now(UTC)
        user = User(
            id=uuid4(),
            username=username.strip(),
            email=email.strip().lower(),
            password_hash=password_hash,
            display_name=display_name.strip() if display_name else None,
            is_email_verified=is_email_verified,
            is_active=True,
            is_2fa_enabled=False,
            avatar_source=avatar_source,
            avatar_url=discord_avatar_url if avatar_source == "discord" else None,
            discord_id=discord_id,
            discord_username=discord_username,
            discord_avatar_url=discord_avatar_url,
            discord_linked_at=discord_linked_at,
            collections_visibility="public",
            invite_code=invite_code or generate_invite_code(),
            created_at=now,
            updated_at=now,
        )
        self._store.users[user.id] = user
        return user

    async def get_public_stats(
        self,
        user_id: UUID,
        *,
        allowed_collection_privacies: tuple[str, ...] = ("public",),
    ) -> dict[str, int]:
        clips = [
            c
            for c in self._store.clips.values()
            if c.user_id == user_id and c.status == "ready" and c.privacy == "public"
        ]
        collections_count = sum(
            1
            for coll in self._store.collections.values()
            if coll.user_id == user_id
            and bool(coll.show_on_profile)
            and coll.privacy in allowed_collection_privacies
        )
        return {
            "clips_count": len(clips),
            "total_views": sum(c.views_count for c in clips),
            "total_likes": sum(c.likes_count for c in clips),
            "collections_count": collections_count,
        }


class _FakeFriendshipRepo:
    def __init__(self, store: _InMemoryCommunityStore, _session: AsyncSession) -> None:
        self._store = store

    async def get_by_id(self, friendship_id: UUID) -> Friendship | None:
        return self._store.friendships.get(friendship_id)

    async def get_between_users(self, user_a_id: UUID, user_b_id: UUID) -> Friendship | None:
        return next(
            (
                f
                for f in self._store.friendships.values()
                if {f.requester_id, f.addressee_id} == {user_a_id, user_b_id}
            ),
            None,
        )

    async def get_between_user_and_many(
        self,
        user_id: UUID,
        other_user_ids: list[UUID],
    ) -> dict[UUID, Friendship]:
        other_set = set(other_user_ids)
        mapping: dict[UUID, Friendship] = {}
        for f in self._store.friendships.values():
            if f.requester_id == user_id and f.addressee_id in other_set:
                mapping[f.addressee_id] = f
            elif f.addressee_id == user_id and f.requester_id in other_set:
                mapping[f.requester_id] = f
        return mapping

    async def are_friends(self, user_a_id: UUID, user_b_id: UUID) -> bool:
        return self._store.are_friends(user_a_id, user_b_id)

    async def list_accepted_friends(
        self,
        user_id: UUID,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Friendship], int]:
        rows = [
            f
            for f in self._store.friendships.values()
            if f.status == "accepted" and user_id in (f.requester_id, f.addressee_id)
        ]
        rows.sort(key=lambda f: f.updated_at, reverse=True)
        start = (page - 1) * page_size
        return rows[start : start + page_size], len(rows)

    async def list_pending_requests(
        self,
        user_id: UUID,
    ) -> tuple[list[Friendship], list[Friendship]]:
        received = [
            f
            for f in self._store.friendships.values()
            if f.status == "pending" and f.addressee_id == user_id
        ]
        sent = [
            f
            for f in self._store.friendships.values()
            if f.status == "pending" and f.requester_id == user_id
        ]
        return received, sent

    async def create(
        self,
        *,
        requester_id: UUID,
        addressee_id: UUID,
        status: str = "pending",
    ) -> Friendship:
        now = datetime.now(UTC)
        friendship = Friendship(
            id=uuid4(),
            requester_id=requester_id,
            addressee_id=addressee_id,
            status=status,
            requester=self._store.users[requester_id],
            addressee=self._store.users[addressee_id],
            created_at=now,
            updated_at=now,
        )
        self._store.friendships[friendship.id] = friendship
        return friendship

    async def save(self, friendship: Friendship) -> Friendship:
        friendship.updated_at = datetime.now(UTC)
        self._store.friendships[friendship.id] = friendship
        return friendship

    async def delete(self, friendship: Friendship) -> None:
        self._store.friendships.pop(friendship.id, None)


class _FakeNotificationRepo:
    def __init__(self, store: _InMemoryCommunityStore, _session: AsyncSession) -> None:
        self._store = store

    async def create(
        self,
        *,
        user_id: UUID,
        actor_id: UUID,
        notification_type: str,
        entity_id: UUID | None = None,
    ) -> Notification:
        notif = Notification(
            id=uuid4(),
            user_id=user_id,
            actor_id=actor_id,
            type=notification_type,
            entity_id=entity_id,
            is_read=False,
            created_at=datetime.now(UTC),
            actor=self._store.users[actor_id],
        )
        self._store.notifications[notif.id] = notif
        return notif

    async def get_by_id(self, notification_id: UUID) -> Notification | None:
        return self._store.notifications.get(notification_id)

    async def list_for_user(
        self,
        user_id: UUID,
        *,
        unread_only: bool = False,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Notification], int, int]:
        all_user = [n for n in self._store.notifications.values() if n.user_id == user_id]
        unread_count = sum(1 for n in all_user if not n.is_read)
        filtered = [n for n in all_user if not n.is_read] if unread_only else all_user
        filtered.sort(key=lambda n: n.created_at, reverse=True)
        start = (page - 1) * page_size
        return filtered[start : start + page_size], len(filtered), unread_count

    async def mark_as_read(self, notification: Notification) -> Notification:
        notification.is_read = True
        return notification

    async def mark_all_as_read(self, user_id: UUID) -> int:
        count = 0
        for notif in self._store.notifications.values():
            if notif.user_id == user_id and not notif.is_read:
                notif.is_read = True
                count += 1
        return count

    async def mark_entity_notifications_read(
        self,
        *,
        entity_id: UUID,
        user_id: UUID | None = None,
        notification_type: str | None = None,
    ) -> int:
        count = 0
        for notif in self._store.notifications.values():
            if notif.entity_id != entity_id or notif.is_read:
                continue
            if user_id is not None and notif.user_id != user_id:
                continue
            if notification_type is not None and notif.type != notification_type:
                continue
            notif.is_read = True
            count += 1
        return count


class _FakeClipRepo:
    def __init__(self, store: _InMemoryCommunityStore, _session: AsyncSession) -> None:
        self._store = store

    async def get_by_id(self, clip_id: UUID) -> Clip | None:
        return self._store.clips.get(clip_id)

    async def get_by_identifier(self, identifier: str) -> Clip | None:
        try:
            return self._store.clips.get(UUID(identifier))
        except ValueError:
            return next((c for c in self._store.clips.values() if c.slug == identifier), None)

    async def get_user_liked_clip_ids(self, _u: UUID, _ids: list[UUID]) -> set[UUID]:
        return set()

    async def get_user_favorited_clip_ids(self, _u: UUID, _ids: list[UUID]) -> set[UUID]:
        return set()

    async def get_top_highlight_clip_id(
        self,
        *,
        current_user_id: UUID | None = None,
        period: str = "all",
    ) -> UUID | None:
        _ = (current_user_id, period)
        return None

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
        _ = (q, game_id, game_slug, tag_slug, favorited_by_user_id, collection_id, sort, period)
        clips = [c for c in self._store.clips.values() if c.status == "ready"]
        if mine and current_user_id is not None:
            clips = [c for c in clips if c.user_id == current_user_id]
        elif current_user_id is None:
            clips = [c for c in clips if c.privacy == "public"]
        else:
            clips = [
                c
                for c in clips
                if c.privacy == "public"
                or c.user_id == current_user_id
                or (c.privacy == "friends" and self._store.are_friends(current_user_id, c.user_id))
            ]
        if author_username:
            au = author_username.strip().lower()
            clips = [c for c in clips if c.author.username.lower() == au]
        clips.sort(key=lambda c: c.created_at, reverse=True)
        start = (page - 1) * page_size
        return clips[start : start + page_size], len(clips)


class _FakeCollectionRepo:
    def __init__(self, store: _InMemoryCommunityStore, _session: AsyncSession) -> None:
        self._store = store

    async def get_by_id(self, collection_id: UUID) -> Collection | None:
        return self._store.collections.get(collection_id)

    async def list_profile_collections(
        self,
        *,
        owner_id: UUID,
        allowed_privacies: tuple[str, ...],
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Collection], int]:
        items = [
            c
            for c in self._store.collections.values()
            if c.user_id == owner_id and bool(c.show_on_profile) and c.privacy in allowed_privacies
        ]
        start = (page - 1) * page_size
        return items[start : start + page_size], len(items)

    async def get_collections_preview_metadata(
        self,
        collection_ids: list[UUID],
        *,
        current_user_id: UUID | None,
        preview_limit: int = 4,
    ) -> dict[UUID, tuple[int, list[str]]]:
        _ = (current_user_id, preview_limit)
        return {cid: (0, []) for cid in collection_ids}

    async def list_collection_clips(
        self,
        collection_id: UUID,
        *,
        current_user_id: UUID | None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[CollectionClip], int]:
        links = [
            link
            for (cid, _), link in self._store.collection_clips.items()
            if cid == collection_id
            and link.clip.status == "ready"
            and (
                link.clip.privacy != "friends"
                or (
                    current_user_id is not None
                    and (
                        current_user_id == link.clip.user_id
                        or self._store.are_friends(current_user_id, link.clip.user_id)
                    )
                )
            )
        ]
        start = (page - 1) * page_size
        return links[start : start + page_size], len(links)


@pytest.fixture
def community_env(
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """Provide FastAPI app wired with in-memory users, friendships, notifications, and media."""
    store = _InMemoryCommunityStore()
    now = datetime.now(UTC)

    alice = User(
        id=uuid4(),
        username="fallen_br",
        email="fallen@clipai.com.br",
        password_hash="hashed",
        display_name="FalleN",
        discord_id="1001",
        discord_username="fallen_disc",
        discord_avatar_url="https://cdn.discordapp.com/avatars/1001/a.png",
        avatar_source="discord",
        collections_visibility="public",
        invite_code="fallenInv123",
        is_email_verified=True,
        is_active=True,
        is_2fa_enabled=False,
        created_at=now,
        updated_at=now,
    )
    bob = User(
        id=uuid4(),
        username="fer_br",
        email="fer@clipai.com.br",
        password_hash="hashed",
        display_name="Fernando Alvarenga",
        discord_id="1002",
        discord_username="fer_god",
        discord_avatar_url="https://cdn.discordapp.com/avatars/1002/b.png",
        avatar_source="discord",
        collections_visibility="public",
        invite_code="ferInvite456",
        is_email_verified=True,
        is_active=True,
        is_2fa_enabled=False,
        created_at=now,
        updated_at=now,
    )
    charlie = User(
        id=uuid4(),
        username="coldzera",
        email="cold@clipai.com.br",
        password_hash="hashed",
        display_name="Marcelo David",
        collections_visibility="public",
        invite_code="coldInv78901",
        is_email_verified=True,
        is_active=True,
        is_2fa_enabled=False,
        created_at=now,
        updated_at=now,
    )
    for user in (alice, bob, charlie):
        store.users[user.id] = user

    cs2 = Game(id=uuid4(), name="CS2", slug="cs2", created_at=now)
    pub_clip = Clip(
        id=uuid4(),
        slug="pub-clip-slug",
        user_id=alice.id,
        game_id=cs2.id,
        title="Clipe Publico",
        status="ready",
        privacy="public",
        video_object_key=f"clips/{alice.id}/pub/video.mp4",
        thumbnail_object_key=f"clips/{alice.id}/pub/thumb.webp",
        file_size_bytes=2_000_000,
        duration_seconds=Decimal("20.00"),
        mime_type="video/mp4",
        technical_metadata={},
        views_count=5,
        likes_count=1,
        favorites_count=0,
        author=alice,
        game=cs2,
        tags=[],
        created_at=now,
        updated_at=now,
    )
    friends_clip = Clip(
        id=uuid4(),
        slug="friends-clip-slug",
        user_id=alice.id,
        game_id=cs2.id,
        title="Clipe Secreto de Amigos",
        status="ready",
        privacy="friends",
        video_object_key=f"clips/{alice.id}/friends/video.mp4",
        thumbnail_object_key=f"clips/{alice.id}/friends/thumb.webp",
        file_size_bytes=3_000_000,
        duration_seconds=Decimal("25.00"),
        mime_type="video/mp4",
        technical_metadata={},
        views_count=2,
        likes_count=0,
        favorites_count=0,
        author=alice,
        game=cs2,
        tags=[],
        created_at=now,
        updated_at=now,
    )
    store.clips[pub_clip.id] = pub_clip
    store.clips[friends_clip.id] = friends_clip

    friends_coll = Collection(
        id=uuid4(),
        user_id=alice.id,
        title="Colecao So Amigos",
        description="Apenas amigos aceitos",
        privacy="friends",
        show_on_profile=True,
        accent_color="#2EB886",
        owner=alice,
        created_at=now,
        updated_at=now,
    )
    pub_coll = Collection(
        id=uuid4(),
        user_id=alice.id,
        title="Colecao Publica",
        description="Aberta",
        privacy="public",
        show_on_profile=True,
        accent_color="#2EB886",
        owner=alice,
        created_at=now,
        updated_at=now,
    )
    store.collections[friends_coll.id] = friends_coll
    store.collections[pub_coll.id] = pub_coll
    store.collection_clips[(friends_coll.id, friends_clip.id)] = CollectionClip(
        collection_id=friends_coll.id,
        clip_id=friends_clip.id,
        position=0,
        added_at=now,
        clip=friends_clip,
    )

    for target in (
        "app.core.dependencies.UserRepository",
        "app.services.auth_service.UserRepository",
        "app.services.user_service.UserRepository",
        "app.services.friendship_service.UserRepository",
        "app.services.invite_service.UserRepository",
        "app.services.collection_service.UserRepository",
    ):
        monkeypatch.setattr(target, lambda s: _FakeUserRepo(store, s))

    for target in (
        "app.core.privacy.FriendshipRepository",
        "app.services.auth_service.FriendshipRepository",
        "app.services.user_service.FriendshipRepository",
        "app.services.friendship_service.FriendshipRepository",
        "app.services.invite_service.FriendshipRepository",
    ):
        monkeypatch.setattr(target, lambda s: _FakeFriendshipRepo(store, s))

    for target in (
        "app.services.auth_service.NotificationRepository",
        "app.services.friendship_service.NotificationRepository",
        "app.services.invite_service.NotificationRepository",
        "app.services.notification_service.NotificationRepository",
    ):
        monkeypatch.setattr(target, lambda s: _FakeNotificationRepo(store, s))

    for target in (
        "app.services.clip_service.ClipRepository",
        "app.services.collection_service.ClipRepository",
    ):
        monkeypatch.setattr(target, lambda s: _FakeClipRepo(store, s))

    for target in (
        "app.services.clip_service.CollectionRepository",
        "app.services.collection_service.CollectionRepository",
    ):
        monkeypatch.setattr(target, lambda s: _FakeCollectionRepo(store, s))

    storage_mock = MagicMock(spec=StorageService)
    storage_mock.build_public_url.side_effect = lambda key: f"https://cdn.clipai.test/{key}"
    email_mock = MagicMock(spec=EmailService)
    email_mock.send_verification_email = AsyncMock()

    application = create_app(settings=test_settings)
    db_session = AsyncMock(spec=AsyncSession)

    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    application.dependency_overrides[get_db] = _override_get_db
    application.dependency_overrides[get_clips_storage_service] = lambda: storage_mock
    application.dependency_overrides[get_email_service] = lambda: email_mock

    return {
        "app": application,
        "store": store,
        "alice": alice,
        "bob": bob,
        "charlie": charlie,
        "alice_headers": {
            "Authorization": f"Bearer {create_access_token(alice.id, test_settings)}"
        },
        "bob_headers": {"Authorization": f"Bearer {create_access_token(bob.id, test_settings)}"},
        "charlie_headers": {
            "Authorization": f"Bearer {create_access_token(charlie.id, test_settings)}"
        },
        "pub_clip": pub_clip,
        "friends_clip": friends_clip,
        "friends_coll": friends_coll,
        "pub_coll": pub_coll,
    }


async def test_friendship_lifecycle_discord_search_and_notifications(
    community_env: dict[str, Any],
) -> None:
    """Verify user search by @discord, request/accept/auto-accept/remove, and notification flow."""
    app = community_env["app"]
    alice: User = community_env["alice"]
    bob: User = community_env["bob"]
    charlie: User = community_env["charlie"]
    alice_h = community_env["alice_headers"]
    bob_h = community_env["bob_headers"]
    charlie_h = community_env["charlie_headers"]

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        # 1. Search by @discord handle excludes self and shows friendship_status="none"
        search_resp = await client.get(
            "/api/v1/users/search",
            params={"q": "@fer_god"},
            headers=alice_h,
        )
        assert search_resp.status_code == status.HTTP_200_OK
        assert search_resp.json()["total"] == 1
        found = search_resp.json()["items"][0]
        assert found["username"] == "fer_br"
        assert found["discord_username"] == "fer_god"
        assert found["friendship_status"] == "none"
        assert found["friendship_id"] is None

        # Searching own handle excludes self
        self_search = await client.get(
            "/api/v1/users/search",
            params={"q": "@fallen_disc"},
            headers=alice_h,
        )
        assert self_search.json()["total"] == 0

        # Profile friendship_status: self vs none
        assert (await client.get("/api/v1/users/fallen_br", headers=alice_h)).json()[
            "friendship_status"
        ] == "self"
        assert (await client.get("/api/v1/users/fer_br", headers=alice_h)).json()[
            "friendship_status"
        ] == "none"

        # 2. Send request: self blocked (400), Alice -> Bob succeeds (201), duplicate blocked (409)
        assert (
            await client.post(f"/api/v1/friends/request/{alice.id}", headers=alice_h)
        ).status_code == 400

        req_resp = await client.post(f"/api/v1/friends/request/{bob.id}", headers=alice_h)
        assert req_resp.status_code == status.HTTP_201_CREATED
        friendship_id = req_resp.json()["friendship_id"]
        assert req_resp.json()["status"] == "pending"

        assert (
            await client.post(f"/api/v1/friends/request/{bob.id}", headers=alice_h)
        ).status_code == 409

        # Requests endpoint & profile friendship_status reflect pending_sent / pending_received
        alice_reqs = (await client.get("/api/v1/friends/requests", headers=alice_h)).json()
        bob_reqs = (await client.get("/api/v1/friends/requests", headers=bob_h)).json()
        assert [r["friendship_id"] for r in alice_reqs["sent"]] == [friendship_id]
        assert [r["friendship_id"] for r in bob_reqs["received"]] == [friendship_id]

        assert (await client.get("/api/v1/users/fer_br", headers=alice_h)).json()[
            "friendship_status"
        ] == "pending_sent"
        bob_profile_view = (await client.get("/api/v1/users/fallen_br", headers=bob_h)).json()
        assert bob_profile_view["friendship_status"] == "pending_received"
        assert bob_profile_view["friendship_id"] == friendship_id

        # Bob received unread friend_request notification
        bob_notifs = (await client.get("/api/v1/notifications", headers=bob_h)).json()
        assert bob_notifs["unread_count"] == 1
        assert bob_notifs["items"][0]["type"] == "friend_request"
        assert bob_notifs["items"][0]["actor"]["username"] == "fallen_br"
        assert bob_notifs["items"][0]["entity_id"] == friendship_id

        # 3. Accept: requester cannot accept own request (403); addressee accepts (200)
        assert (
            await client.post(f"/api/v1/friends/{friendship_id}/accept", headers=alice_h)
        ).status_code == 403

        accept_resp = await client.post(f"/api/v1/friends/{friendship_id}/accept", headers=bob_h)
        assert accept_resp.status_code == status.HTTP_200_OK
        assert accept_resp.json()["status"] == "accepted"

        # Original notification for Bob is now read; Alice got unread friend_accepted notification
        bob_notifs_after = (await client.get("/api/v1/notifications", headers=bob_h)).json()
        assert bob_notifs_after["unread_count"] == 0
        assert bob_notifs_after["items"][0]["is_read"] is True

        alice_notifs = (await client.get("/api/v1/notifications", headers=alice_h)).json()
        assert alice_notifs["unread_count"] == 1
        assert alice_notifs["items"][0]["type"] == "friend_accepted"
        assert alice_notifs["items"][0]["actor"]["username"] == "fer_br"
        alice_notif_id = alice_notifs["items"][0]["id"]

        # Notification read ownership guard + PATCH read
        assert (
            await client.patch(f"/api/v1/notifications/{alice_notif_id}/read", headers=bob_h)
        ).status_code == 403
        read_one = await client.patch(
            f"/api/v1/notifications/{alice_notif_id}/read",
            headers=alice_h,
        )
        assert read_one.status_code == status.HTTP_200_OK
        assert read_one.json()["is_read"] is True
        assert (await client.get("/api/v1/notifications", headers=alice_h)).json()[
            "unread_count"
        ] == 0

        # Friends list returns accepted friend on both sides
        alice_friends = (await client.get("/api/v1/friends", headers=alice_h)).json()
        bob_friends = (await client.get("/api/v1/friends", headers=bob_h)).json()
        assert [f["user"]["username"] for f in alice_friends["items"]] == ["fer_br"]
        assert [f["user"]["username"] for f in bob_friends["items"]] == ["fallen_br"]

        # 4. Outsider cannot remove friendship (403); participant removes it (204)
        assert (
            await client.delete(f"/api/v1/friends/{friendship_id}", headers=charlie_h)
        ).status_code == 403
        assert (
            await client.delete(f"/api/v1/friends/{friendship_id}", headers=alice_h)
        ).status_code == 204
        assert (await client.get("/api/v1/friends", headers=alice_h)).json()["total"] == 0

        # 5. Reverse request auto-accepts + read-all clears inbox
        req_charlie = await client.post(f"/api/v1/friends/request/{alice.id}", headers=charlie_h)
        assert req_charlie.status_code == status.HTTP_201_CREATED

        auto_accept = await client.post(f"/api/v1/friends/request/{charlie.id}", headers=alice_h)
        assert auto_accept.status_code == status.HTTP_201_CREATED
        assert auto_accept.json()["status"] == "accepted"
        assert auto_accept.json()["auto_accepted"] is True

        # Charlie has unread friend_accepted -> mark all read via POST /notifications/read-all
        charlie_unread = (
            await client.get(
                "/api/v1/notifications",
                params={"unread_only": True},
                headers=charlie_h,
            )
        ).json()
        assert charlie_unread["unread_count"] == 1

        read_all = await client.post("/api/v1/notifications/read-all", headers=charlie_h)
        assert read_all.status_code == status.HTTP_200_OK
        assert read_all.json()["updated_count"] == 1
        assert (await client.get("/api/v1/notifications", headers=charlie_h)).json()[
            "unread_count"
        ] == 0


async def test_invite_links_and_register_with_invite_code(community_env: dict[str, Any]) -> None:
    """Verify /invites/me, public /invites/{code}, /invites/{code}/accept, and register invite."""
    app = community_env["app"]
    alice: User = community_env["alice"]
    alice_h = community_env["alice_headers"]
    bob_h = community_env["bob_headers"]

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        # 1. GET /invites/me
        me_inv = await client.get("/api/v1/invites/me", headers=alice_h)
        assert me_inv.status_code == status.HTTP_200_OK
        assert me_inv.json()["invite_code"] == alice.invite_code
        assert me_inv.json()["invite_url"].endswith(f"/invite/{alice.invite_code}")

        # 2. Public GET /invites/{code}
        invalid_prev = await client.get("/api/v1/invites/does-not-exist")
        assert invalid_prev.status_code == status.HTTP_404_NOT_FOUND

        preview = await client.get(f"/api/v1/invites/{alice.invite_code}")
        assert preview.status_code == status.HTTP_200_OK
        assert preview.json()["username"] == "fallen_br"
        assert preview.json()["display_name"] == "FalleN"
        assert preview.json()["discord_username"] == "fallen_disc"
        assert "email" not in preview.json()

        # 3. POST /invites/{code}/accept: self blocked (400), Bob accepts (200), idempotent retry
        assert (
            await client.post(f"/api/v1/invites/{alice.invite_code}/accept", headers=alice_h)
        ).status_code == 400

        acc1 = await client.post(f"/api/v1/invites/{alice.invite_code}/accept", headers=bob_h)
        assert acc1.status_code == status.HTTP_200_OK
        assert acc1.json()["status"] == "accepted"
        assert acc1.json()["already_friends"] is False

        acc2 = await client.post(f"/api/v1/invites/{alice.invite_code}/accept", headers=bob_h)
        assert acc2.status_code == status.HTTP_200_OK
        assert acc2.json()["already_friends"] is True

        # Only 1 friend_accepted notification created for Alice
        alice_notifs = (await client.get("/api/v1/notifications", headers=alice_h)).json()
        assert alice_notifs["total"] == 1
        assert alice_notifs["items"][0]["type"] == "friend_accepted"
        assert alice_notifs["items"][0]["actor"]["username"] == "fer_br"

        # 4. POST /auth/register with optional invite_code creates instant accepted friendship
        reg_resp = await client.post(
            "/api/v1/auth/register",
            json={
                "username": "taco_br",
                "email": "taco@clipai.com.br",
                "password": "StrongPassword!2026",
                "display_name": "TACO",
                "invite_code": alice.invite_code,
            },
        )
        assert reg_resp.status_code == status.HTTP_201_CREATED

        alice_friends = (await client.get("/api/v1/friends", headers=alice_h)).json()
        assert {f["user"]["username"] for f in alice_friends["items"]} == {"fer_br", "taco_br"}


async def test_strict_friends_privacy_across_clips_and_collections(
    community_env: dict[str, Any],
) -> None:
    """Validate owner vs accepted friend vs pending vs stranger vs anonymous on friends content."""
    app = community_env["app"]
    alice: User = community_env["alice"]
    bob: User = community_env["bob"]
    alice_h = community_env["alice_headers"]
    bob_h = community_env["bob_headers"]
    charlie_h = community_env["charlie_headers"]
    pub_clip: Clip = community_env["pub_clip"]
    friends_clip: Clip = community_env["friends_clip"]
    friends_coll: Collection = community_env["friends_coll"]
    pub_coll: Collection = community_env["pub_coll"]

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        # 1. Anonymous: 401 on friends clip & collection; listings show only public
        assert (await client.get(f"/api/v1/clips/{friends_clip.slug}")).status_code == 401
        assert (await client.get(f"/api/v1/collections/{friends_coll.id}")).status_code == 401
        assert [c["slug"] for c in (await client.get("/api/v1/clips")).json()["items"]] == [
            pub_clip.slug
        ]
        assert [
            c["id"]
            for c in (await client.get("/api/v1/users/fallen_br/collections")).json()["items"]
        ] == [str(pub_coll.id)]

        # 2. Logged-in non-friend (Bob) gets 403 with clear PT-BR message and sees only public
        stranger_clip = await client.get(f"/api/v1/clips/{friends_clip.slug}", headers=bob_h)
        assert stranger_clip.status_code == status.HTTP_403_FORBIDDEN
        assert stranger_clip.json()["error"]["message"] == (
            "Este conteúdo é visível apenas para amigos."
        )

        stranger_coll = await client.get(f"/api/v1/collections/{friends_coll.id}", headers=bob_h)
        assert stranger_coll.status_code == status.HTTP_403_FORBIDDEN
        assert stranger_coll.json()["error"]["message"] == (
            "Este conteúdo é visível apenas para amigos."
        )

        bob_clips_before = (await client.get("/api/v1/clips", headers=bob_h)).json()["items"]
        assert [c["slug"] for c in bob_clips_before] == [pub_clip.slug]
        bob_colls_before = (
            await client.get("/api/v1/users/fallen_br/collections", headers=bob_h)
        ).json()["items"]
        assert [c["id"] for c in bob_colls_before] == [str(pub_coll.id)]

        # 3. Pending friend request is NOT enough -> still 403!
        pending_req = await client.post(f"/api/v1/friends/request/{bob.id}", headers=alice_h)
        friendship_id = pending_req.json()["friendship_id"]
        assert (
            await client.get(f"/api/v1/clips/{friends_clip.slug}", headers=bob_h)
        ).status_code == 403
        assert (
            await client.get(f"/api/v1/collections/{friends_coll.id}", headers=bob_h)
        ).status_code == 403

        # 4. Once Bob accepts, Bob (accepted friend) can view friends clip & collection;
        # Charlie (still non-friend) remains blocked with 403; Owner always has access.
        await client.post(f"/api/v1/friends/{friendship_id}/accept", headers=bob_h)

        assert (
            await client.get(f"/api/v1/clips/{friends_clip.slug}", headers=bob_h)
        ).status_code == 200
        assert (
            await client.get(f"/api/v1/collections/{friends_coll.id}", headers=bob_h)
        ).status_code == 200
        assert {
            c["slug"] for c in (await client.get("/api/v1/clips", headers=bob_h)).json()["items"]
        } == {pub_clip.slug, friends_clip.slug}
        assert {
            c["id"]
            for c in (
                await client.get("/api/v1/users/fallen_br/collections", headers=bob_h)
            ).json()["items"]
        } == {str(pub_coll.id), str(friends_coll.id)}

        # Owner always sees own content
        assert (
            await client.get(f"/api/v1/clips/{friends_clip.slug}", headers=alice_h)
        ).status_code == 200
        assert (
            await client.get(f"/api/v1/collections/{friends_coll.id}", headers=alice_h)
        ).status_code == 200

        # Non-friend Charlie still blocked
        assert (
            await client.get(f"/api/v1/clips/{friends_clip.slug}", headers=charlie_h)
        ).status_code == 403
        assert (
            await client.get(f"/api/v1/collections/{friends_coll.id}", headers=charlie_h)
        ).status_code == 403

        # 5. Unfriending immediately revokes Bob's access back to 403
        await client.delete(f"/api/v1/friends/{friendship_id}", headers=alice_h)
        assert (
            await client.get(f"/api/v1/clips/{friends_clip.slug}", headers=bob_h)
        ).status_code == 403
        assert (
            await client.get(f"/api/v1/collections/{friends_coll.id}", headers=bob_h)
        ).status_code == 403
        assert alice.id is not None
