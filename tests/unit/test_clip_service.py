"""Unit tests for ClipService upload validation, confirmation, privacy, and CRUD."""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AppError
from app.models.clip import Clip, Tag
from app.models.game import Game
from app.models.user import User
from app.schemas.clip import ClipUpdateRequest, UploadUrlRequest
from app.services.clip_service import ClipService
from app.services.storage_service import StorageService, StoredObjectMetadata


def _make_user() -> User:
    now = datetime.now(UTC)
    return User(
        id=uuid4(),
        username="fallen_br",
        email="fallen@clipai.com.br",
        password_hash="hashed",
        display_name="FalleN",
        avatar_url=None,
        bio=None,
        is_email_verified=True,
        is_active=True,
        is_2fa_enabled=False,
        created_at=now,
        updated_at=now,
    )


def _make_clip(
    owner: User,
    *,
    status: str = "ready",
    privacy: str = "public",
    with_thumb: bool = True,
) -> Clip:
    now = datetime.now(UTC)
    clip_id = uuid4()
    return Clip(
        id=clip_id,
        slug="slug1234567",
        user_id=owner.id,
        game_id=None,
        title="Clutch 1v4",
        description="Round decisivo",
        status=status,
        privacy=privacy,
        video_object_key=f"clips/{owner.id}/{clip_id}/video.mp4",
        thumbnail_object_key=f"clips/{owner.id}/{clip_id}/thumb.webp" if with_thumb else None,
        file_size_bytes=5_000_000,
        duration_seconds=Decimal("30.00"),
        mime_type="video/mp4",
        technical_metadata={"codec": "h264", "container": "mp4"},
        views_count=10,
        likes_count=2,
        favorites_count=1,
        author=owner,
        game=Game(id=uuid4(), name="CS2", slug="cs2"),
        tags=[Tag(id=uuid4(), name="clutch", slug="clutch")],
        created_at=now,
        updated_at=now,
    )


def _build_service(test_settings: Settings) -> tuple[ClipService, AsyncMock, MagicMock]:
    session = AsyncMock(spec=AsyncSession)
    storage = MagicMock(spec=StorageService)
    storage.build_video_key.side_effect = StorageService.build_video_key
    storage.build_thumbnail_key.side_effect = StorageService.build_thumbnail_key
    storage.build_public_url.side_effect = lambda key: f"https://cdn.clipai.test/{key}"
    storage.generate_presigned_upload_url = AsyncMock(return_value="https://r2.test/put")
    storage.verify_object_exists = AsyncMock(
        return_value=StoredObjectMetadata(content_length=5_000_000, content_type="video/mp4")
    )
    storage.delete_objects = AsyncMock()
    service = ClipService(session=session, settings=test_settings, storage_service=storage)
    return service, session, storage


async def test_create_upload_url_validates_mime_size_quota_and_game(
    test_settings: Settings,
) -> None:
    """Enforce MIME, byte limits, pending quota, and game existence before signing."""
    service, _session, _storage = _build_service(test_settings)
    user = _make_user()

    bad_mime = UploadUrlRequest(
        title="Video invalido",
        video_mime_type="video/quicktime",
        video_size_bytes=1024,
        duration_seconds=10.0,
    )
    with pytest.raises(AppError) as exc_mime:
        await service.create_upload_url(bad_mime, user)
    assert exc_mime.value.status_code == 415

    too_big = UploadUrlRequest(
        title="Video gigante",
        video_mime_type="video/mp4",
        video_size_bytes=104_857_601,
        duration_seconds=10.0,
    )
    with pytest.raises(AppError) as exc_big:
        await service.create_upload_url(too_big, user)
    assert exc_big.value.status_code == 413

    bad_thumb_mime = UploadUrlRequest(
        title="Thumb invalida",
        video_mime_type="video/mp4",
        video_size_bytes=1024,
        duration_seconds=10.0,
        thumbnail_mime_type="image/gif",
        thumbnail_size_bytes=100,
    )
    with pytest.raises(AppError) as exc_thumb_mime:
        await service.create_upload_url(bad_thumb_mime, user)
    assert exc_thumb_mime.value.status_code == 415

    missing_thumb_size = UploadUrlRequest(
        title="Thumb sem tamanho",
        video_mime_type="video/mp4",
        video_size_bytes=1024,
        duration_seconds=10.0,
        thumbnail_mime_type="image/webp",
    )
    with pytest.raises(AppError) as exc_thumb_size:
        await service.create_upload_url(missing_thumb_size, user)
    assert exc_thumb_size.value.status_code == 422

    big_thumb = UploadUrlRequest(
        title="Thumb gigante",
        video_mime_type="video/mp4",
        video_size_bytes=1024,
        duration_seconds=10.0,
        thumbnail_mime_type="image/png",
        thumbnail_size_bytes=5_242_881,
    )
    with pytest.raises(AppError) as exc_big_thumb:
        await service.create_upload_url(big_thumb, user)
    assert exc_big_thumb.value.status_code == 413

    service._clip_repo = AsyncMock()
    service._clip_repo.count_pending_by_user.return_value = 3
    valid_req = UploadUrlRequest(
        title="Clipe valido",
        video_mime_type="video/mp4",
        video_size_bytes=1024,
        duration_seconds=10.0,
    )
    with pytest.raises(AppError) as exc_quota:
        await service.create_upload_url(valid_req, user)
    assert exc_quota.value.status_code == 429

    service._clip_repo.count_pending_by_user.return_value = 0
    service._game_repo = AsyncMock()
    service._game_repo.get_by_id.return_value = None
    with_game = UploadUrlRequest(
        title="Jogo inexistente",
        game_id=uuid4(),
        video_mime_type="video/mp4",
        video_size_bytes=1024,
        duration_seconds=10.0,
    )
    with pytest.raises(AppError) as exc_game:
        await service.create_upload_url(with_game, user)
    assert exc_game.value.status_code == 404


async def test_confirm_upload_marks_failed_on_r2_error_or_oversize(
    test_settings: Settings,
) -> None:
    """Mark clip failed and clean up R2 objects when HeadObject verification fails."""
    service, session, storage = _build_service(test_settings)
    owner = _make_user()
    pending = _make_clip(owner, status="pending_upload")

    service._clip_repo = AsyncMock()
    service._clip_repo.get_by_id.return_value = pending

    storage.verify_object_exists.return_value = StoredObjectMetadata(
        content_length=200_000_000,
        content_type="video/mp4",
    )

    with pytest.raises(AppError) as exc_info:
        await service.confirm_upload(pending.id, owner)
    assert exc_info.value.status_code == 400
    assert exc_info.value.code == "UPLOAD_VERIFICATION_FAILED"
    assert pending.status == "failed"
    session.commit.assert_awaited_once()
    storage.delete_objects.assert_awaited_once()

    # Non-pending clip cannot be confirmed again
    with pytest.raises(AppError) as not_pending_exc:
        await service.confirm_upload(pending.id, owner)
    assert not_pending_exc.value.status_code == 400
    assert not_pending_exc.value.code == "BAD_REQUEST"


async def test_privacy_and_ownership_guards_on_detail_list_update_delete(
    test_settings: Settings,
) -> None:
    """Enforce privacy rules on detail/list and ownership on update/delete."""
    service, session, storage = _build_service(test_settings)
    owner = _make_user()
    other_user = _make_user()

    service._clip_repo = AsyncMock()
    service._clip_repo.get_by_identifier.return_value = None
    with pytest.raises(AppError) as missing_exc:
        await service.get_clip_detail("missing-slug", None)
    assert missing_exc.value.status_code == 404

    pending = _make_clip(owner, status="pending_upload")
    service._clip_repo.get_by_identifier.return_value = pending
    with pytest.raises(AppError) as pending_exc:
        await service.get_clip_detail(pending.slug, other_user)
    assert pending_exc.value.status_code == 404

    friends_clip = _make_clip(owner, status="ready", privacy="friends")
    service._clip_repo.get_by_identifier.return_value = friends_clip
    with pytest.raises(AppError) as anon_friends:
        await service.get_clip_detail(friends_clip.slug, None)
    assert anon_friends.value.status_code == 401

    detail = await service.get_clip_detail(friends_clip.slug, other_user)
    assert detail.slug == friends_clip.slug

    with pytest.raises(AppError) as anon_mine:
        await service.list_clips(mine=True, current_user=None)
    assert anon_mine.value.status_code == 401

    # Update & delete forbid non-owners
    service._clip_repo.get_by_id.return_value = friends_clip
    with pytest.raises(AppError) as forbid_upd:
        await service.update_clip(friends_clip.id, ClipUpdateRequest(title="Hack"), other_user)
    assert forbid_upd.value.status_code == 403

    with pytest.raises(AppError) as forbid_del:
        await service.delete_clip(friends_clip.id, other_user)
    assert forbid_del.value.status_code == 403

    await service.delete_clip(friends_clip.id, owner)
    service._clip_repo.delete.assert_awaited_once_with(friends_clip)
    session.commit.assert_awaited()
    storage.delete_objects.assert_awaited_once()
