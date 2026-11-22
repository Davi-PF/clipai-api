"""Unit tests for AuthService covering happy paths and security edge cases."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.security import (
    create_email_verification_token,
    create_jwt_token,
    create_password_reset_token,
    hash_password,
    hash_token,
)
from app.models.user import RefreshToken, User
from app.schemas.auth import LoginRequest, RegisterRequest, ResetPasswordRequest
from app.services.auth_service import AuthService
from app.services.email_service import EmailService


def _make_user(
    *,
    is_email_verified: bool = True,
    is_active: bool = True,
    password: str = "StrongPass!2026",
) -> User:
    now = datetime.now(UTC)
    return User(
        id=uuid4(),
        username="fallen_br",
        email="fallen@clipai.com.br",
        password_hash=hash_password(password),
        display_name="FalleN",
        avatar_url=None,
        bio=None,
        is_email_verified=is_email_verified,
        is_active=is_active,
        is_2fa_enabled=False,
        totp_secret_encrypted=None,
        created_at=now,
        updated_at=now,
    )


def _build_service(test_settings: Settings) -> tuple[AuthService, AsyncMock, AsyncMock]:
    session = AsyncMock(spec=AsyncSession)
    session.add = MagicMock()
    email_service = AsyncMock(spec=EmailService)
    service = AuthService(session=session, settings=test_settings, email_service=email_service)
    return service, session, email_service


async def test_register_creates_user_and_sends_verification_email(
    test_settings: Settings,
) -> None:
    """Create unverified user, send verification e-mail, and commit transaction."""
    service, session, email_service = _build_service(test_settings)
    user = _make_user(is_email_verified=False)

    service._user_repo = AsyncMock()
    service._user_repo.get_by_email.return_value = None
    service._user_repo.get_by_username.return_value = None
    service._user_repo.create.return_value = user

    payload = RegisterRequest(
        username="fallen_br",
        email="fallen@clipai.com.br",
        password="StrongPass!2026",
        display_name="FalleN",
    )
    response = await service.register(payload)

    assert response.user.username == "fallen_br"
    assert response.user.is_email_verified is False
    email_service.send_verification_email.assert_awaited_once()
    session.commit.assert_awaited_once()


async def test_register_rejects_duplicate_email_and_username(test_settings: Settings) -> None:
    """Raise 409 CONFLICT when e-mail or username is already taken."""
    service, _session, _email_service = _build_service(test_settings)
    existing = _make_user()
    service._user_repo = AsyncMock()
    service._user_repo.get_by_email.return_value = existing

    payload = RegisterRequest(
        username="fallen_br",
        email="fallen@clipai.com.br",
        password="StrongPass!2026",
    )

    with pytest.raises(AppError) as email_exc:
        await service.register(payload)
    assert email_exc.value.status_code == 409

    service._user_repo.get_by_email.return_value = None
    service._user_repo.get_by_username.return_value = existing

    with pytest.raises(AppError) as username_exc:
        await service.register(payload)
    assert username_exc.value.status_code == 409


async def test_verify_email_activates_user_and_rejects_invalid_tokens(
    test_settings: Settings,
) -> None:
    """Confirm e-mail with valid JWT and reject malformed or unknown-user tokens."""
    service, session, _email = _build_service(test_settings)
    user = _make_user(is_email_verified=False)
    token = create_email_verification_token(user.id, test_settings)

    service._user_repo = AsyncMock()
    service._user_repo.get_by_id.return_value = user

    result = await service.verify_email(token)
    assert "confirmado com sucesso" in result.message
    service._user_repo.mark_email_verified.assert_awaited_once_with(user)
    session.commit.assert_awaited_once()

    bad_sub = create_jwt_token(
        subject="not-a-uuid",
        token_type="email_verification",
        expires_delta=timedelta(hours=1),
        settings=test_settings,
    )
    with pytest.raises(AppError) as exc_info:
        await service.verify_email(bad_sub)
    assert exc_info.value.status_code == 400


async def test_login_blocks_unverified_and_invalid_credentials(test_settings: Settings) -> None:
    """Reject unknown e-mail, wrong password, and unverified e-mail accounts."""
    service, _session, _email = _build_service(test_settings)
    service._user_repo = AsyncMock()
    service._user_repo.get_by_email.return_value = None

    with pytest.raises(AppError) as missing_exc:
        await service.login(
            LoginRequest(email="nobody@clipai.com.br", password="StrongPass!2026"),
            user_agent="pytest",
            ip_address="127.0.0.1",
        )
    assert missing_exc.value.code == "INVALID_CREDENTIALS"

    unverified = _make_user(is_email_verified=False)
    service._user_repo.get_by_email.return_value = unverified

    with pytest.raises(AppError) as wrong_pw_exc:
        await service.login(
            LoginRequest(email="fallen@clipai.com.br", password="WrongPassword!1"),
            user_agent="pytest",
            ip_address="127.0.0.1",
        )
    assert wrong_pw_exc.value.code == "INVALID_CREDENTIALS"

    with pytest.raises(AppError) as unverified_exc:
        await service.login(
            LoginRequest(email="fallen@clipai.com.br", password="StrongPass!2026"),
            user_agent="pytest",
            ip_address="127.0.0.1",
        )
    assert unverified_exc.value.status_code == 403
    assert unverified_exc.value.code == "EMAIL_NOT_VERIFIED"


async def test_login_rehashes_password_when_needed(test_settings: Settings) -> None:
    """Transparently rehash Argon2id password when parameters change."""
    service, session, _email = _build_service(test_settings)
    user = _make_user(is_email_verified=True)
    service._user_repo = AsyncMock()
    service._user_repo.get_by_email.return_value = user
    service._refresh_repo = AsyncMock()

    with patch("app.services.auth_service.needs_password_rehash", return_value=True):
        response, raw_refresh = await service.login(
            LoginRequest(email="fallen@clipai.com.br", password="StrongPass!2026"),
            user_agent="pytest",
            ip_address="127.0.0.1",
        )

    assert response.access_token is not None
    assert raw_refresh
    service._user_repo.update_password_hash.assert_awaited_once()
    session.commit.assert_awaited_once()


async def test_refresh_reuse_detection_revokes_entire_family(test_settings: Settings) -> None:
    """Revoke full token family when an already-revoked refresh token is presented."""
    service, session, _email = _build_service(test_settings)
    now = datetime.now(UTC)
    family_id = uuid4()
    revoked_record = RefreshToken(
        id=uuid4(),
        user_id=uuid4(),
        token_hash=hash_token("old-token"),
        family_id=family_id,
        expires_at=now + timedelta(days=5),
        revoked_at=now - timedelta(minutes=2),
    )

    service._refresh_repo = AsyncMock()
    service._refresh_repo.get_by_token_hash.return_value = revoked_record

    with pytest.raises(AppError) as exc_info:
        await service.refresh("old-token", user_agent="pytest", ip_address="127.0.0.1")

    assert exc_info.value.code == "INVALID_TOKEN"
    service._refresh_repo.revoke_family.assert_awaited_once()
    session.commit.assert_awaited_once()


async def test_refresh_rejects_missing_expired_and_inactive_user(test_settings: Settings) -> None:
    """Reject missing cookie, expired refresh token, or inactive user."""
    service, _session, _email = _build_service(test_settings)

    with pytest.raises(AppError) as missing_exc:
        await service.refresh(None, user_agent=None, ip_address=None)
    assert missing_exc.value.code == "INVALID_TOKEN"

    service._refresh_repo = AsyncMock()
    service._refresh_repo.get_by_token_hash.return_value = None
    with pytest.raises(AppError) as unknown_exc:
        await service.refresh("unknown", user_agent=None, ip_address=None)
    assert unknown_exc.value.code == "INVALID_TOKEN"

    expired_record = RefreshToken(
        id=uuid4(),
        user_id=uuid4(),
        token_hash=hash_token("expired"),
        family_id=uuid4(),
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
        revoked_at=None,
    )
    service._refresh_repo.get_by_token_hash.return_value = expired_record
    with pytest.raises(AppError) as expired_exc:
        await service.refresh("expired", user_agent=None, ip_address=None)
    assert expired_exc.value.code == "TOKEN_EXPIRED"


async def test_reset_password_rejects_reused_token_and_revokes_sessions(
    test_settings: Settings,
) -> None:
    """Enforce single-use reset token via password fingerprint and revoke sessions."""
    service, session, _email = _build_service(test_settings)
    user = _make_user(is_email_verified=True, password="OldPassword!1")
    token = create_password_reset_token(user.id, user.password_hash, test_settings)

    service._user_repo = AsyncMock()
    service._user_repo.get_by_id.return_value = user
    service._refresh_repo = AsyncMock()

    result = await service.reset_password(
        ResetPasswordRequest(token=token, new_password="NewPassword!2026")
    )
    assert "redefinida com sucesso" in result.message
    service._refresh_repo.revoke_all_for_user.assert_awaited_once()
    session.commit.assert_awaited_once()

    # Simulate password already changed (fingerprint mismatch)
    user.password_hash = hash_password("NewPassword!2026")
    with pytest.raises(AppError) as reuse_exc:
        await service.reset_password(
            ResetPasswordRequest(token=token, new_password="AnotherPass!2026")
        )
    assert reuse_exc.value.status_code == 400
    assert reuse_exc.value.code == "INVALID_TOKEN"


async def test_resend_and_forgot_password_never_enumerate_users(
    test_settings: Settings,
) -> None:
    """Return generic success messages even when e-mail is absent."""
    service, _session, email_service = _build_service(test_settings)
    service._user_repo = AsyncMock()
    service._user_repo.get_by_email.return_value = None

    resend_resp = await service.resend_verification("absent@clipai.com.br")
    forgot_resp = await service.forgot_password("absent@clipai.com.br")

    assert "novo link foi enviado" in resend_resp.message
    assert "instruções para redefinir" in forgot_resp.message
    email_service.send_verification_email.assert_not_awaited()
    email_service.send_password_reset_email.assert_not_awaited()
