"""Unit tests for UserService covering profile editing, R2 avatar lifecycle, and Discord guards."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AppError
from app.models.user import User
from app.schemas.user import (
    AvatarConfirmRequest,
    AvatarUploadUrlRequest,
    UserUpdateRequest,
)
from app.services.storage_service import StorageService, StoredObjectMetadata
from app.services.user_service import UserService


def _make_user(
    *,
    password_hash: str | None = "hashed-pw",
    discord_id: str | None = None,
    discord_username: str | None = None,
    discord_avatar_url: str | None = None,
    avatar_key: str | None = None,
    avatar_source: str = "none",
    display_name: str | None = "FalleN",
) -> User:
    now = datetime.now(UTC)
    return User(
        id=uuid4(),
        username="fallen_br",
        email="fallen@clipai.com.br",
        password_hash=password_hash,
        display_name=display_name,
        bio="Professor do CS2",
        avatar_key=avatar_key,
        avatar_source=avatar_source,
        discord_id=discord_id,
        discord_username=discord_username,
        discord_avatar_url=discord_avatar_url,
        discord_linked_at=now if discord_id else None,
        is_email_verified=True,
        is_active=True,
        is_2fa_enabled=False,
        created_at=now,
        updated_at=now,
    )


def _build_user_service(test_settings: Settings) -> tuple[UserService, AsyncMock, MagicMock]:
    session = AsyncMock(spec=AsyncSession)
    storage = MagicMock(spec=StorageService)
    storage.build_avatar_key.side_effect = StorageService.build_avatar_key
    storage.build_public_url.side_effect = lambda key: f"https://cdn.clipai.test/{key}"
    storage.generate_presigned_upload_url = AsyncMock(return_value="https://r2.test/put/avatar")
    storage.verify_object_exists = AsyncMock(
        return_value=StoredObjectMetadata(content_length=250_000, content_type="image/webp")
    )
    storage.delete_objects = AsyncMock()
    service = UserService(session=session, settings=test_settings, storage_service=storage)
    service._user_repo = AsyncMock()
    return service, session, storage


async def test_update_profile_display_name_bio_and_avatar_source(
    test_settings: Settings,
) -> None:
    """Update display_name (with fallback to username), bio, and validated avatar_source."""
    service, session, _storage = _build_user_service(test_settings)
    user = _make_user(
        avatar_key="avatars/123/avatar-abc.webp",
        avatar_source="custom",
        discord_id="disc_123",
        discord_username="fallen#0001",
        discord_avatar_url="https://cdn.discordapp.com/avatars/disc_123/a.png",
    )

    updated = await service.update_profile(
        user,
        UserUpdateRequest(
            display_name="   ",
            bio="AWPer bicampeão mundial",
            avatar_source="discord",
        ),
    )
    assert updated.display_name == "fallen_br"
    assert updated.bio == "AWPer bicampeão mundial"
    assert updated.avatar_source == "discord"
    assert updated.avatar_url == "https://cdn.discordapp.com/avatars/disc_123/a.png"
    session.commit.assert_awaited()

    # Switching to custom when avatar_key is missing raises 400
    user_no_custom = _make_user(avatar_key=None, discord_id=None)
    with pytest.raises(AppError) as no_custom_exc:
        await service.update_profile(user_no_custom, UserUpdateRequest(avatar_source="custom"))
    assert no_custom_exc.value.status_code == 400

    # Switching to discord when discord_id is missing raises 400
    with pytest.raises(AppError) as no_discord_exc:
        await service.update_profile(user_no_custom, UserUpdateRequest(avatar_source="discord"))
    assert no_discord_exc.value.status_code == 400


async def test_avatar_upload_url_and_confirm_replaces_previous_object(
    test_settings: Settings,
) -> None:
    """Validate MIME and size limits, confirm upload in R2, and delete previous avatar key."""
    service, session, storage = _build_user_service(test_settings)
    user = _make_user()
    old_key = f"avatars/{user.id}/avatar-{uuid4()}.png"
    user.avatar_key = old_key
    user.avatar_source = "custom"

    # Invalid MIME -> 415
    with pytest.raises(AppError) as bad_mime:
        await service.create_avatar_upload_url(
            user,
            AvatarUploadUrlRequest(mime_type="image/gif", size_bytes=1024),
        )
    assert bad_mime.value.status_code == 415

    # Exceeds 5 MB -> 413
    with pytest.raises(AppError) as too_large:
        await service.create_avatar_upload_url(
            user,
            AvatarUploadUrlRequest(mime_type="image/webp", size_bytes=5_242_881),
        )
    assert too_large.value.status_code == 413

    # Valid presigned upload URL
    upload_resp = await service.create_avatar_upload_url(
        user,
        AvatarUploadUrlRequest(mime_type="image/webp", size_bytes=250_000),
    )
    assert upload_resp.avatar_key.startswith(f"avatars/{user.id}/avatar-")
    assert upload_resp.avatar_key.endswith(".webp")
    assert upload_resp.upload_url == "https://r2.test/put/avatar"

    # Reject key belonging to another user or malformed key
    with pytest.raises(AppError) as wrong_owner:
        await service.confirm_avatar_upload(
            user,
            AvatarConfirmRequest(avatar_key=f"avatars/{uuid4()}/avatar-{uuid4()}.webp"),
        )
    assert wrong_owner.value.status_code == 400

    with pytest.raises(AppError) as bad_suffix:
        await service.confirm_avatar_upload(
            user,
            AvatarConfirmRequest(avatar_key=f"avatars/{user.id}/malicious.webp"),
        )
    assert bad_suffix.value.status_code == 400

    # Valid confirm deletes old_key and sets avatar_source="custom"
    confirmed = await service.confirm_avatar_upload(
        user,
        AvatarConfirmRequest(avatar_key=upload_resp.avatar_key),
    )
    assert confirmed.avatar_source == "custom"
    assert confirmed.avatar_url == f"https://cdn.clipai.test/{upload_resp.avatar_key}"
    storage.delete_objects.assert_awaited_once_with([old_key])
    session.commit.assert_awaited()

    # Oversized object in R2 during HeadObject is deleted and rejected with 400
    storage.verify_object_exists.return_value = StoredObjectMetadata(
        content_length=9_000_000,
        content_type="image/webp",
    )
    next_key = f"avatars/{user.id}/avatar-{uuid4()}.webp"
    with pytest.raises(AppError) as verify_fail:
        await service.confirm_avatar_upload(user, AvatarConfirmRequest(avatar_key=next_key))
    assert verify_fail.value.status_code == 400
    assert verify_fail.value.code == "UPLOAD_VERIFICATION_FAILED"


async def test_public_profile_and_discord_unlink_guards(test_settings: Settings) -> None:
    """Return public profile stats and block Discord unlinking when account has no password."""
    service, _session, _storage = _build_user_service(test_settings)
    user = _make_user(
        password_hash=None,
        discord_id="99887766",
        discord_username="fallen_discord",
        discord_avatar_url="https://cdn.discordapp.com/avatars/99887766/hash.png",
        avatar_source="discord",
    )
    service._user_repo.get_by_username.return_value = user
    service._user_repo.get_public_stats.return_value = {
        "clips_count": 7,
        "total_views": 420,
        "total_likes": 85,
        "collections_count": 2,
    }

    pub = await service.get_public_profile("fallen_br")
    assert pub.username == "fallen_br"
    assert pub.discord_linked is True
    assert pub.discord_username == "fallen_discord"
    assert pub.avatar_url == "https://cdn.discordapp.com/avatars/99887766/hash.png"
    assert pub.stats.clips_count == 7
    assert pub.total_views == 420

    # Unknown user raises 404
    service._user_repo.get_by_username.return_value = None
    with pytest.raises(AppError) as not_found:
        await service.get_public_profile("ghost_user")
    assert not_found.value.status_code == 404

    # Unlinking Discord when password_hash is None is strictly blocked with 400
    with pytest.raises(AppError) as no_pw_exc:
        await service.unlink_discord(user)
    assert no_pw_exc.value.status_code == 400
    assert no_pw_exc.value.code == "PASSWORD_REQUIRED_BEFORE_UNLINK"

    # Once user has a password, unlinking succeeds and falls back avatar_source
    user.password_hash = "argon2_hashed_password"
    unlinked = await service.unlink_discord(user)
    assert unlinked.discord_linked is False
    assert unlinked.discord_username is None
    assert unlinked.avatar_source == "none"
    assert unlinked.avatar_url is None

    # Unlinking again when not linked raises 400
    with pytest.raises(AppError) as already_unlinked:
        await service.unlink_discord(user)
    assert already_unlinked.value.status_code == 400

    # use_discord_avatar requires linked Discord
    with pytest.raises(AppError) as use_unlinked:
        await service.use_discord_avatar(user)
    assert use_unlinked.value.status_code == 400

    user.discord_id = "99887766"
    user.discord_avatar_url = "https://cdn.discordapp.com/avatars/99887766/hash.png"
    switched = await service.use_discord_avatar(user)
    assert switched.avatar_source == "discord"
    assert switched.avatar_url == "https://cdn.discordapp.com/avatars/99887766/hash.png"
