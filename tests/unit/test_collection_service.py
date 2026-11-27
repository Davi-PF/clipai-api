"""Unit tests for CollectionService, collection schemas, and ClipService social logic."""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AppError
from app.models.clip import Clip
from app.models.collection import Collection, CollectionClip
from app.models.user import User
from app.schemas.collection import (
    CollectionAddClipRequest,
    CollectionCreateRequest,
    CollectionUpdateRequest,
)
from app.services.clip_service import ClipService
from app.services.collection_service import CollectionService
from app.services.storage_service import StorageService


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


def _make_clip(owner: User, *, status: str = "ready", privacy: str = "public") -> Clip:
    now = datetime.now(UTC)
    clip_id = uuid4()
    return Clip(
        id=clip_id,
        slug="slugxyz1234",
        user_id=owner.id,
        game_id=None,
        title="Ace na Inferno",
        description=None,
        status=status,
        privacy=privacy,
        video_object_key=f"clips/{owner.id}/{clip_id}/video.mp4",
        thumbnail_object_key=f"clips/{owner.id}/{clip_id}/thumb.webp",
        file_size_bytes=2_000_000,
        duration_seconds=Decimal("25.00"),
        mime_type="video/mp4",
        technical_metadata={},
        views_count=5,
        likes_count=1,
        favorites_count=1,
        author=owner,
        game=None,
        tags=[],
        created_at=now,
        updated_at=now,
    )


def _make_storage_mock() -> MagicMock:
    storage = MagicMock(spec=StorageService)
    storage.build_public_url.side_effect = lambda key: f"https://cdn.clipai.test/{key}"
    return storage


def test_collection_schema_accepts_name_alias_and_validates_hex_color() -> None:
    """Support both 'title' and 'name' alias and reject malformed hex accent_color."""
    req = CollectionCreateRequest.model_validate(
        {"name": "Clutches 2026", "accent_color": "#2eb886", "description": "  "}
    )
    assert req.title == "Clutches 2026"
    assert req.accent_color == "#2EB886"
    assert req.description is None

    with pytest.raises(ValidationError):
        CollectionCreateRequest(title="Valida", accent_color="verde")

    with pytest.raises(ValidationError):
        CollectionUpdateRequest(title="  a ")


async def test_collection_service_guards_privacy_ownership_and_idempotency() -> None:
    """Enforce ownership, privacy, ready-only clips, and idempotent add/remove."""
    session = AsyncMock(spec=AsyncSession)
    storage = _make_storage_mock()
    service = CollectionService(session=session, storage_service=storage)
    owner = _make_user("owner")
    other = _make_user("other")
    now = datetime.now(UTC)

    with pytest.raises(AppError) as mine_anon:
        await service.list_collections(mine=True, current_user=None)
    assert mine_anon.value.status_code == 401

    friends_coll = Collection(
        id=uuid4(),
        user_id=owner.id,
        title="Amigos",
        description=None,
        privacy="friends",
        accent_color="#2EB886",
        owner=owner,
        created_at=now,
        updated_at=now,
    )

    service._collection_repo = AsyncMock()
    service._clip_repo = AsyncMock()
    service._collection_repo.get_by_id.return_value = None

    with pytest.raises(AppError) as not_found:
        await service.get_collection_detail(friends_coll.id, current_user=None)
    assert not_found.value.status_code == 404

    service._collection_repo.get_by_id.return_value = friends_coll
    with pytest.raises(AppError) as anon_friends:
        await service.get_collection_detail(friends_coll.id, current_user=None)
    assert anon_friends.value.status_code == 401

    # Non-owner blocked from update/delete/add/remove
    with pytest.raises(AppError) as forbid_upd:
        await service.update_collection(
            friends_coll.id,
            CollectionUpdateRequest(title="Novo"),
            other,
        )
    assert forbid_upd.value.status_code == 403

    # Adding missing or non-ready clip fails appropriately
    service._clip_repo.get_by_id.return_value = None
    with pytest.raises(AppError) as missing_clip:
        await service.add_clip_to_collection(
            friends_coll.id,
            CollectionAddClipRequest(clip_id=uuid4()),
            owner,
        )
    assert missing_clip.value.status_code == 404

    pending_other = _make_clip(other, status="pending_upload")
    service._clip_repo.get_by_id.return_value = pending_other
    with pytest.raises(AppError) as hidden_pending:
        await service.add_clip_to_collection(
            friends_coll.id,
            CollectionAddClipRequest(clip_id=pending_other.id),
            owner,
        )
    assert hidden_pending.value.status_code == 404

    pending_own = _make_clip(owner, status="pending_upload")
    service._clip_repo.get_by_id.return_value = pending_own
    with pytest.raises(AppError) as own_pending:
        await service.add_clip_to_collection(
            friends_coll.id,
            CollectionAddClipRequest(clip_id=pending_own.id),
            owner,
        )
    assert own_pending.value.status_code == 400

    # Idempotent add updates position when already linked
    ready_clip = _make_clip(owner, status="ready")
    existing_link = CollectionClip(
        collection_id=friends_coll.id,
        clip_id=ready_clip.id,
        position=0,
        added_at=now,
    )
    service._clip_repo.get_by_id.return_value = ready_clip
    service._collection_repo.get_clip_link.return_value = existing_link
    res = await service.add_clip_to_collection(
        friends_coll.id,
        CollectionAddClipRequest(clip_id=ready_clip.id, position=2),
        owner,
    )
    assert res.position == 2
    session.commit.assert_awaited()


async def test_clip_service_social_and_collection_filter_guards(
    test_settings: Settings,
) -> None:
    """Validate favorited/collection_id guards and friends view restriction."""
    session = AsyncMock(spec=AsyncSession)
    service = ClipService(
        session=session, settings=test_settings, storage_service=_make_storage_mock()
    )
    owner = _make_user("owner")
    friends_clip = _make_clip(owner, status="ready", privacy="friends")

    with pytest.raises(AppError) as anon_fav:
        await service.list_clips(favorited=True, current_user=None)
    assert anon_fav.value.status_code == 401

    service._collection_repo = AsyncMock()
    service._collection_repo.get_by_id.return_value = None
    with pytest.raises(AppError) as missing_coll:
        await service.list_clips(collection_id=uuid4(), current_user=None)
    assert missing_coll.value.status_code == 404

    service._collection_repo.get_by_id.return_value = Collection(
        id=uuid4(),
        user_id=owner.id,
        title="Privada",
        privacy="friends",
    )
    with pytest.raises(AppError) as anon_coll:
        await service.list_clips(collection_id=uuid4(), current_user=None)
    assert anon_coll.value.status_code == 401

    service._clip_repo = AsyncMock()
    service._clip_repo.get_by_id.return_value = friends_clip
    with pytest.raises(AppError) as anon_view:
        await service.register_view(friends_clip.id, current_user=None)
    assert anon_view.value.status_code == 401


async def test_collection_detail_restricts_clip_technical_metadata_to_clip_owner() -> None:
    """Return file_size_bytes and technical_metadata in collection clips only to clip owners."""
    session = AsyncMock(spec=AsyncSession)
    storage = _make_storage_mock()
    service = CollectionService(session=session, storage_service=storage)
    owner = _make_user("owner")
    other = _make_user("other")
    now = datetime.now(UTC)

    public_coll = Collection(
        id=uuid4(),
        user_id=owner.id,
        title="Destaques",
        description=None,
        privacy="public",
        accent_color="#2EB886",
        owner=owner,
        created_at=now,
        updated_at=now,
    )
    owner_clip = _make_clip(owner, status="ready", privacy="public")
    other_clip = _make_clip(other, status="ready", privacy="public")
    links = [
        CollectionClip(
            collection_id=public_coll.id,
            clip_id=owner_clip.id,
            position=0,
            added_at=now,
            clip=owner_clip,
        ),
        CollectionClip(
            collection_id=public_coll.id,
            clip_id=other_clip.id,
            position=1,
            added_at=now,
            clip=other_clip,
        ),
    ]

    service._collection_repo = AsyncMock()
    service._clip_repo = AsyncMock()
    service._collection_repo.get_by_id.return_value = public_coll
    service._collection_repo.list_collection_clips.return_value = (links, 2)
    service._clip_repo.get_user_interaction_clip_ids.return_value = (set(), set())

    owner_view = await service.get_collection_detail(public_coll.id, current_user=owner)
    assert owner_view.clips[0].clip.file_size_bytes == 2_000_000
    assert owner_view.clips[0].clip.technical_metadata is not None
    assert owner_view.clips[0].clip.duration_seconds == 25.0
    assert owner_view.clips[1].clip.file_size_bytes is None
    assert owner_view.clips[1].clip.technical_metadata is None

    anon_view = await service.get_collection_detail(public_coll.id, current_user=None)
    assert all(item.clip.file_size_bytes is None for item in anon_view.clips)
    assert all(item.clip.technical_metadata is None for item in anon_view.clips)
    assert all(item.clip.duration_seconds == 25.0 for item in anon_view.clips)


async def test_collection_service_profile_showcase_and_private_collection_guards() -> None:
    """Verify profile showcase Global Ceiling short-circuiting and 403 on private collections."""
    session = AsyncMock(spec=AsyncSession)
    storage = _make_storage_mock()
    service = CollectionService(session=session, storage_service=storage)
    owner = _make_user("owner")
    owner.collections_visibility = "private"
    other = _make_user("other")
    now = datetime.now(UTC)

    service._user_repo = AsyncMock()
    service._collection_repo = AsyncMock()

    # Unknown user returns 404
    service._user_repo.get_by_username.return_value = None
    with pytest.raises(AppError) as missing_user:
        await service.list_user_profile_collections("ghost", current_user=None)
    assert missing_user.value.status_code == 404

    # Private global ceiling returns empty paginated response safely for non-owners
    service._user_repo.get_by_username.return_value = owner
    empty_resp = await service.list_user_profile_collections("owner", current_user=other)
    assert empty_resp.items == []
    assert empty_resp.total == 0
    service._collection_repo.list_profile_collections.assert_not_awaited()

    # Owner still sees their profile showcase even when global visibility is private
    priv_coll = Collection(
        id=uuid4(),
        user_id=owner.id,
        title="Meus Privados",
        description="Secreto",
        privacy="private",
        show_on_profile=True,
        accent_color="#2EB886",
        owner=owner,
        created_at=now,
        updated_at=now,
    )
    service._collection_repo.list_profile_collections.return_value = ([priv_coll], 1)
    service._collection_repo.get_collections_preview_metadata.return_value = {
        priv_coll.id: (2, ["clips/u/c1/thumb.webp", "clips/u/c2/thumb.webp"]),
    }
    owner_showcase = await service.list_user_profile_collections("owner", current_user=owner)
    assert owner_showcase.total == 1
    item = owner_showcase.items[0]
    assert item.name == "Meus Privados"
    assert item.privacy == "private"
    assert item.effective_privacy == "private"
    assert item.show_on_profile is True
    assert item.clips_count == 2
    assert item.cover_thumbnail_url == "https://cdn.clipai.test/clips/u/c1/thumb.webp"

    # Direct access to private collection blocks non-owner with 403 FORBIDDEN
    service._collection_repo.get_by_id.return_value = priv_coll
    with pytest.raises(AppError) as forbid_other:
        await service.get_collection_detail(priv_coll.id, current_user=other)
    assert forbid_other.value.status_code == 403
    assert forbid_other.value.code == "FORBIDDEN"
