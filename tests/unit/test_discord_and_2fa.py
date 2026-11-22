"""Unit tests for DiscordOAuthService (httpx mocked) and AuthService 2FA & Discord flows."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pyotp
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.security import create_discord_state_token, decrypt_totp_secret, hash_password
from app.models.user import User
from app.schemas.auth import (
    DiscordCallbackRequest,
    LoginRequest,
    TwoFactorDisableRequest,
    TwoFactorEnableRequest,
    TwoFactorVerifyLoginRequest,
)
from app.services.auth_service import AuthService
from app.services.discord_service import DiscordOAuthService, DiscordUserProfile
from app.services.email_service import EmailService


def _make_user(
    *,
    password: str | None = "StrongPass!2026",
    is_email_verified: bool = True,
    is_2fa_enabled: bool = False,
    discord_id: str | None = None,
) -> User:
    now = datetime.now(UTC)
    return User(
        id=uuid4(),
        username="fallen_br",
        email="fallen@clipai.com.br",
        password_hash=hash_password(password) if password else None,
        display_name="FalleN",
        avatar_source="none",
        discord_id=discord_id,
        is_email_verified=is_email_verified,
        is_active=True,
        is_2fa_enabled=is_2fa_enabled,
        created_at=now,
        updated_at=now,
    )


async def test_discord_oauth_service_url_and_code_exchange_via_httpx(
    test_settings: Settings,
) -> None:
    """Build Discord OAuth2 URL and exchange authorization code via mocked httpx.AsyncClient."""
    service = DiscordOAuthService(test_settings)
    auth_url = service.build_authorization_url(state="signed-state-123")
    assert "https://discord.com/api/oauth2/authorize?" in auth_url
    assert "scope=identify+email" in auth_url
    assert "state=signed-state-123" in auth_url

    token_resp = MagicMock()
    token_resp.raise_for_status = MagicMock()
    token_resp.json.return_value = {"access_token": "discord_access_tok"}

    user_resp = MagicMock()
    user_resp.raise_for_status = MagicMock()
    user_resp.json.return_value = {
        "id": "8877665544",
        "username": "fallen.cs",
        "global_name": "Gabriel FalleN",
        "avatar": "a_animatedhash",
        "email": "FALLEN@ClipAI.com.br",
        "verified": True,
    }

    mock_client = AsyncMock()
    mock_client.post.return_value = token_resp
    mock_client.get.return_value = user_resp
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    with patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_client):
        profile = await service.exchange_code_for_profile("oauth_code_abc")

    assert profile.discord_id == "8877665544"
    assert profile.username == "fallen.cs"
    assert profile.display_name == "Gabriel FalleN"
    assert profile.email == "fallen@clipai.com.br"
    assert profile.email_verified is True
    assert (
        profile.avatar_url
        == "https://cdn.discordapp.com/avatars/8877665544/a_animatedhash.gif?size=256"
    )

    # HTTP error from Discord is mapped to 400 DISCORD_OAUTH_FAILED
    req = httpx.Request("POST", "https://discord.com/api/v10/oauth2/token")
    res = httpx.Response(400, request=req, text='{"error":"invalid_grant"}')
    token_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "400 Bad Request",
        request=req,
        response=res,
    )
    with (
        patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_client),
        pytest.raises(AppError) as http_exc,
    ):
        await service.exchange_code_for_profile("bad_code")
    assert http_exc.value.status_code == 400
    assert http_exc.value.code == "DISCORD_OAUTH_FAILED"

    # Network ConnectError mapped to 502
    mock_client.post.side_effect = httpx.ConnectError("connection reset")
    with (
        patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_client),
        pytest.raises(AppError) as net_exc,
    ):
        await service.exchange_code_for_profile("net_fail")
    assert net_exc.value.status_code == 502

    # Missing client_id / client_secret raises 400
    unconfigured = DiscordOAuthService(
        test_settings.model_copy(update={"discord_client_id": None, "discord_client_secret": None})
    )
    with pytest.raises(AppError) as no_id_exc:
        unconfigured.build_authorization_url(state="s")
    assert no_id_exc.value.status_code == 400

    with pytest.raises(AppError) as no_secret_exc:
        await unconfigured.exchange_code_for_profile("c")
    assert no_secret_exc.value.status_code == 400

    # _parse_profile fallback avatar_url, missing avatar, and missing id
    parsed_fallback = DiscordOAuthService._parse_profile(
        {
            "id": "111",
            "username": "u",
            "avatar": None,
            "avatar_url": "https://custom.av/u.png",
            "email": "u@clipai.com.br",
            "verified": False,
        }
    )
    assert parsed_fallback.avatar_url == "https://custom.av/u.png"

    parsed_no_av = DiscordOAuthService._parse_profile({"id": "222", "username": "u2"})
    assert parsed_no_av.avatar_url is None

    with pytest.raises(AppError) as missing_id_exc:
        DiscordOAuthService._parse_profile({"username": "noid"})
    assert missing_id_exc.value.status_code == 400


async def test_2fa_setup_enable_login_challenge_backup_code_and_disable(
    test_settings: Settings,
) -> None:
    """Verify full 2FA lifecycle: setup -> enable -> challenge -> backup single-use -> disable."""
    session = AsyncMock(spec=AsyncSession)
    email_service = AsyncMock(spec=EmailService)
    service = AuthService(session=session, settings=test_settings, email_service=email_service)
    service._user_repo = AsyncMock()
    service._refresh_repo = AsyncMock()

    user = _make_user()
    service._user_repo.get_by_email.return_value = user
    service._user_repo.get_by_id.return_value = user

    # 1. Setup 2FA saves encrypted secret while keeping is_2fa_enabled=False
    setup_resp = await service.setup_2fa(user)
    assert user.is_2fa_enabled is False
    assert user.totp_secret_encrypted is not None
    assert user.totp_secret_encrypted != setup_resp.manual_secret
    assert (
        decrypt_totp_secret(user.totp_secret_encrypted, test_settings) == setup_resp.manual_secret
    )
    assert "CLIPA%C3%8D" in setup_resp.otpauth_uri or "CLIPAI" in setup_resp.otpauth_uri

    # 2. Enable 2FA rejects invalid code, accepts valid code, and issues 8 backup codes
    with pytest.raises(AppError) as invalid_enable:
        await service.enable_2fa(user, TwoFactorEnableRequest(code="000000"))
    assert invalid_enable.value.status_code == 400

    valid_totp = pyotp.TOTP(setup_resp.manual_secret).now()
    enable_resp = await service.enable_2fa(user, TwoFactorEnableRequest(code=valid_totp))
    assert user.is_2fa_enabled is True
    assert len(enable_resp.backup_codes) == 8
    assert len(user.totp_backup_codes or []) == 8
    assert enable_resp.backup_codes[0] not in (user.totp_backup_codes or [])

    # 3. Password login returns requires_2fa=True and challenge_token without issuing tokens
    login_resp, raw_refresh = await service.login(
        LoginRequest(email=user.email, password="StrongPass!2026"),
        user_agent="pytest",
        ip_address="127.0.0.1",
    )
    assert login_resp.requires_2fa is True
    assert login_resp.access_token is None
    assert login_resp.challenge_token is not None
    assert raw_refresh is None

    # 4. verify_2fa_login rejects wrong code, succeeds with TOTP, and consumes backup code once
    with pytest.raises(AppError) as bad_verify:
        await service.verify_2fa_login(
            TwoFactorVerifyLoginRequest(
                challenge_token=login_resp.challenge_token,
                code="000000",
            ),
            user_agent="pytest",
            ip_address="127.0.0.1",
        )
    assert bad_verify.value.status_code == 401

    totp_verified, refresh_1 = await service.verify_2fa_login(
        TwoFactorVerifyLoginRequest(
            challenge_token=login_resp.challenge_token,
            code=pyotp.TOTP(setup_resp.manual_secret).now(),
        ),
        user_agent="pytest",
        ip_address="127.0.0.1",
    )
    assert totp_verified.access_token is not None
    assert totp_verified.requires_2fa is False
    assert refresh_1

    # Single-use backup code works once and is rejected on second attempt
    first_backup = enable_resp.backup_codes[0]
    backup_verified, _refresh_2 = await service.verify_2fa_login(
        TwoFactorVerifyLoginRequest(
            challenge_token=login_resp.challenge_token,
            code=first_backup,
        ),
        user_agent="pytest",
        ip_address="127.0.0.1",
    )
    assert backup_verified.access_token is not None
    assert len(user.totp_backup_codes or []) == 7

    with pytest.raises(AppError) as reused_backup:
        await service.verify_2fa_login(
            TwoFactorVerifyLoginRequest(
                challenge_token=login_resp.challenge_token,
                code=first_backup,
            ),
            user_agent="pytest",
            ip_address="127.0.0.1",
        )
    assert reused_backup.value.status_code == 401

    # 5. Disable 2FA requires current password + valid TOTP/backup code
    with pytest.raises(AppError) as missing_pw:
        await service.disable_2fa(
            user,
            TwoFactorDisableRequest(code=enable_resp.backup_codes[1], password=None),
        )
    assert missing_pw.value.status_code == 400

    with pytest.raises(AppError) as wrong_pw:
        await service.disable_2fa(
            user,
            TwoFactorDisableRequest(code=enable_resp.backup_codes[1], password="WrongPassword!1"),
        )
    assert wrong_pw.value.status_code == 401

    disable_resp = await service.disable_2fa(
        user,
        TwoFactorDisableRequest(
            code=enable_resp.backup_codes[1],
            password="StrongPass!2026",
        ),
    )
    assert "desativada com sucesso" in disable_resp.message
    assert user.is_2fa_enabled is False
    assert user.totp_secret_encrypted is None
    assert user.totp_backup_codes is None


async def test_discord_callback_link_and_login_modes(test_settings: Settings) -> None:
    """Verify Discord link mode, auto-linking verified emails, and new user creation."""
    session = AsyncMock(spec=AsyncSession)
    email_service = AsyncMock(spec=EmailService)
    discord_service = MagicMock(spec=DiscordOAuthService)
    service = AuthService(
        session=session,
        settings=test_settings,
        email_service=email_service,
        discord_service=discord_service,
    )
    service._user_repo = AsyncMock()
    service._refresh_repo = AsyncMock()

    user = _make_user()
    service._user_repo.get_by_id.return_value = user
    service._user_repo.get_by_discord_id.return_value = None

    discord_profile = DiscordUserProfile(
        discord_id="disc_999",
        username="fallen.pro!",
        display_name="FalleN Discord",
        email="fallen@clipai.com.br",
        email_verified=True,
        avatar_url="https://cdn.discordapp.com/avatars/disc_999/av.png",
    )
    discord_service.exchange_code_for_profile = AsyncMock(return_value=discord_profile)

    # Link mode binds Discord identity and sets avatar_source="discord" when no custom avatar
    link_state = create_discord_state_token(mode="link", user_id=user.id, settings=test_settings)
    link_resp, link_refresh = await service.handle_discord_callback(
        DiscordCallbackRequest(code="code123", state=link_state),
        current_user=user,
        user_agent="pytest",
        ip_address="127.0.0.1",
    )
    assert link_resp.mode == "link"
    assert link_refresh is None
    assert user.discord_id == "disc_999"
    assert user.avatar_source == "discord"

    # Conflict when Discord ID already belongs to a different account
    other_owner = _make_user()
    service._user_repo.get_by_discord_id.return_value = other_owner
    with pytest.raises(AppError) as conflict_exc:
        await service.handle_discord_callback(
            DiscordCallbackRequest(code="code123", state=link_state),
            current_user=user,
            user_agent="pytest",
            ip_address="127.0.0.1",
        )
    assert conflict_exc.value.status_code == 409

    # Login mode creates a new user with normalized username when not found
    service._user_repo.get_by_discord_id.return_value = None
    service._user_repo.get_by_email.return_value = None
    service._user_repo.get_by_username.side_effect = [user, None]
    created_discord_user = _make_user(password=None, discord_id="disc_new")
    created_discord_user.username = "fallen_pro_a1b2"
    created_discord_user.avatar_source = "discord"
    created_discord_user.discord_avatar_url = discord_profile.avatar_url
    service._user_repo.create.return_value = created_discord_user

    login_state = create_discord_state_token(mode="login", user_id=None, settings=test_settings)
    login_resp, login_refresh = await service.handle_discord_callback(
        DiscordCallbackRequest(code="code_new", state=login_state),
        current_user=None,
        user_agent="pytest",
        ip_address="127.0.0.1",
    )
    assert login_resp.mode == "login"
    assert login_resp.access_token is not None
    assert login_refresh is not None
    service._user_repo.create.assert_awaited_once()

    # Auto-link when both Discord email and existing local account are verified
    existing_verified = _make_user(is_email_verified=True, discord_id=None)
    service._user_repo.get_by_discord_id.return_value = None
    service._user_repo.get_by_email.return_value = existing_verified
    auto_resp, _auto_refresh = await service.handle_discord_callback(
        DiscordCallbackRequest(code="code_autolink", state=login_state),
        current_user=None,
        user_agent="pytest",
        ip_address="127.0.0.1",
    )
    assert auto_resp.user is not None
    assert existing_verified.discord_id == "disc_999"
