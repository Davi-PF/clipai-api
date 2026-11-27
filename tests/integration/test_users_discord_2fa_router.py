"""End-to-end integration tests for Sprint 4: Profile, R2 Avatar, Discord OAuth2, and 2FA TOTP."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pyotp
import pytest
from fastapi import status
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.database import get_db
from app.core.security import create_access_token, hash_password
from app.main import create_app
from app.models.user import RefreshToken, User
from app.routers.auth import get_email_service
from app.routers.clips import get_storage_service
from app.services.email_service import EmailService
from app.services.storage_service import StorageService, StoredObjectMetadata


class _Sprint4Store:
    """In-memory backing store for users and refresh tokens in Sprint 4 integration tests."""

    def __init__(self) -> None:
        self.users: dict[UUID, User] = {}
        self.refresh_tokens: dict[UUID, RefreshToken] = {}


class _FakeUserRepo:
    def __init__(self, store: _Sprint4Store, _session: AsyncSession) -> None:
        self._store = store

    async def get_by_id(self, user_id: UUID) -> User | None:
        return self._store.users.get(user_id)

    async def get_by_email(self, email: str) -> User | None:
        normalized = email.strip().lower()
        return next(
            (u for u in self._store.users.values() if u.email.lower() == normalized),
            None,
        )

    async def get_by_username(self, username: str) -> User | None:
        normalized = username.strip().lower()
        return next(
            (u for u in self._store.users.values() if u.username.lower() == normalized),
            None,
        )

    async def get_by_discord_id(self, discord_id: str) -> User | None:
        cleaned = discord_id.strip()
        return next(
            (u for u in self._store.users.values() if u.discord_id == cleaned),
            None,
        )

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
    ) -> User:
        now = datetime.now(UTC)
        user = User(
            id=uuid4(),
            username=username.strip(),
            email=email.strip().lower(),
            password_hash=password_hash,
            display_name=display_name.strip() if display_name else None,
            avatar_source=avatar_source,
            avatar_url=discord_avatar_url if avatar_source == "discord" else None,
            discord_id=discord_id,
            discord_username=discord_username,
            discord_avatar_url=discord_avatar_url,
            discord_linked_at=discord_linked_at,
            is_email_verified=is_email_verified,
            is_active=True,
            is_2fa_enabled=False,
            created_at=now,
            updated_at=now,
        )
        self._store.users[user.id] = user
        return user

    async def save(self, user: User) -> User:
        user.updated_at = datetime.now(UTC)
        self._store.users[user.id] = user
        return user

    async def mark_email_verified(self, user: User) -> User:
        user.is_email_verified = True
        return user

    async def update_password_hash(self, user: User, password_hash: str) -> User:
        user.password_hash = password_hash
        return user

    async def get_public_stats(
        self,
        _user_id: UUID,
        *,
        allowed_collection_privacies: tuple[str, ...] = ("public",),
    ) -> dict[str, int]:
        return {
            "clips_count": 3,
            "total_views": 250,
            "total_likes": 42,
            "collections_count": 1 if allowed_collection_privacies else 0,
        }


class _FakeRefreshRepo:
    def __init__(self, store: _Sprint4Store, _session: AsyncSession) -> None:
        self._store = store

    async def create(
        self,
        *,
        user_id: UUID,
        token_hash: str,
        family_id: UUID,
        expires_at: datetime,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> RefreshToken:
        record = RefreshToken(
            id=uuid4(),
            user_id=user_id,
            token_hash=token_hash,
            family_id=family_id,
            expires_at=expires_at,
            user_agent=user_agent,
            ip_address=ip_address,
            created_at=datetime.now(UTC),
        )
        self._store.refresh_tokens[record.id] = record
        return record


@pytest.fixture
def sprint4_env(
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """Provide wired FastAPI app with in-memory user store and mocked R2/Email dependencies."""
    store = _Sprint4Store()
    now = datetime.now(UTC)

    user = User(
        id=uuid4(),
        username="fallen_br",
        email="fallen@clipai.com.br",
        password_hash=hash_password("StrongPass!2026"),
        display_name="FalleN",
        bio="Professor do CS2",
        avatar_source="none",
        is_email_verified=True,
        is_active=True,
        is_2fa_enabled=False,
        created_at=now,
        updated_at=now,
    )
    store.users[user.id] = user

    monkeypatch.setattr("app.core.dependencies.UserRepository", lambda s: _FakeUserRepo(store, s))
    monkeypatch.setattr(
        "app.services.auth_service.UserRepository", lambda s: _FakeUserRepo(store, s)
    )
    monkeypatch.setattr(
        "app.services.user_service.UserRepository", lambda s: _FakeUserRepo(store, s)
    )
    monkeypatch.setattr(
        "app.services.auth_service.RefreshTokenRepository",
        lambda s: _FakeRefreshRepo(store, s),
    )

    storage_mock = MagicMock(spec=StorageService)
    storage_mock.build_avatar_key.side_effect = StorageService.build_avatar_key
    storage_mock.build_public_url.side_effect = lambda key: f"https://cdn.clipai.test/{key}"
    storage_mock.generate_presigned_upload_url = AsyncMock(
        side_effect=lambda key, _ct, expires_in=600: (
            f"https://r2.test/upload/{key}?exp={expires_in}"
        )
    )
    storage_mock.verify_object_exists = AsyncMock(
        return_value=StoredObjectMetadata(content_length=150_000, content_type="image/webp")
    )
    storage_mock.delete_objects = AsyncMock()

    email_mock = AsyncMock(spec=EmailService)
    application = create_app(settings=test_settings)
    db_session = AsyncMock(spec=AsyncSession)

    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    application.dependency_overrides[get_db] = _override_get_db
    application.dependency_overrides[get_storage_service] = lambda: storage_mock
    application.dependency_overrides[get_email_service] = lambda: email_mock

    return {
        "app": application,
        "store": store,
        "storage": storage_mock,
        "user": user,
        "headers": {"Authorization": f"Bearer {create_access_token(user.id, test_settings)}"},
        "settings": test_settings,
    }


async def test_profile_edit_avatar_upload_replacement_and_public_profile(
    sprint4_env: dict[str, Any],
) -> None:
    """Verify PATCH /users/me, R2 avatar upload + replacement, and GET /users/{username}."""
    app = sprint4_env["app"]
    headers = sprint4_env["headers"]
    storage: MagicMock = sprint4_env["storage"]

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        # 1. Request first avatar upload URL
        up1 = await client.post(
            "/api/v1/users/me/avatar/upload-url",
            headers=headers,
            json={"mime_type": "image/webp", "size_bytes": 150_000},
        )
        assert up1.status_code == status.HTTP_200_OK
        first_key = up1.json()["avatar_key"]
        assert first_key.endswith(".webp")

        # 2. Confirm first avatar upload
        conf1 = await client.post(
            "/api/v1/users/me/avatar/confirm",
            headers=headers,
            json={"avatar_key": first_key},
        )
        assert conf1.status_code == status.HTTP_200_OK
        assert conf1.json()["avatar_source"] == "custom"
        assert conf1.json()["avatar_url"] == f"https://cdn.clipai.test/{first_key}"
        storage.delete_objects.assert_not_awaited()

        # 3. Upload and confirm second avatar -> deletes first_key from R2
        up2 = await client.post(
            "/api/v1/users/me/avatar/upload-url",
            headers=headers,
            json={"mime_type": "image/png", "size_bytes": 200_000},
        )
        second_key = up2.json()["avatar_key"]
        conf2 = await client.post(
            "/api/v1/users/me/avatar/confirm",
            headers=headers,
            json={"avatar_key": second_key},
        )
        assert conf2.status_code == status.HTTP_200_OK
        assert conf2.json()["avatar_url"] == f"https://cdn.clipai.test/{second_key}"
        storage.delete_objects.assert_awaited_once_with([first_key])

        # 4. PATCH /api/v1/users/me updates display_name, bio, and avatar_source
        patch_resp = await client.patch(
            "/api/v1/users/me",
            headers=headers,
            json={"display_name": "Gabriel FalleN", "bio": "IGL e AWPer", "avatar_source": "none"},
        )
        assert patch_resp.status_code == status.HTTP_200_OK
        assert patch_resp.json()["display_name"] == "Gabriel FalleN"
        assert patch_resp.json()["bio"] == "IGL e AWPer"
        assert patch_resp.json()["avatar_source"] == "none"
        assert patch_resp.json()["avatar_url"] is None

        # Switch back to custom avatar via PATCH /users/me
        restore_custom = await client.patch(
            "/api/v1/users/me",
            headers=headers,
            json={"avatar_source": "custom"},
        )
        assert restore_custom.status_code == status.HTTP_200_OK
        assert restore_custom.json()["avatar_url"] == f"https://cdn.clipai.test/{second_key}"

        # 5. Public profile GET /api/v1/users/{username} returns resolved avatar and stats
        pub_resp = await client.get("/api/v1/users/fallen_br")
        assert pub_resp.status_code == status.HTTP_200_OK
        pub_data = pub_resp.json()
        assert pub_data["username"] == "fallen_br"
        assert pub_data["display_name"] == "Gabriel FalleN"
        assert pub_data["avatar_url"] == f"https://cdn.clipai.test/{second_key}"
        assert pub_data["clips_count"] == 3
        assert pub_data["stats"]["total_views"] == 250


async def test_discord_oauth_link_login_and_unlink_password_protection(
    sprint4_env: dict[str, Any],
) -> None:
    """Verify Discord OAuth2 link/login flows via mocked httpx and password guard on unlink."""
    app = sprint4_env["app"]
    headers = sprint4_env["headers"]
    settings: Settings = sprint4_env["settings"]

    def _mock_httpx_discord(profile_payload: dict[str, Any]) -> AsyncMock:
        token_resp = MagicMock()
        token_resp.raise_for_status = MagicMock()
        token_resp.json.return_value = {"access_token": "disc_tok_123"}

        user_resp = MagicMock()
        user_resp.raise_for_status = MagicMock()
        user_resp.json.return_value = profile_payload

        mock_client = AsyncMock()
        mock_client.post.return_value = token_resp
        mock_client.get.return_value = user_resp
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        return mock_client

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        # 1. Mode=link requires authentication
        unauth_link_url = await client.get("/api/v1/auth/discord/url", params={"mode": "link"})
        assert unauth_link_url.status_code == status.HTTP_401_UNAUTHORIZED

        link_url_resp = await client.get(
            "/api/v1/auth/discord/url",
            params={"mode": "link"},
            headers=headers,
        )
        assert link_url_resp.status_code == status.HTTP_200_OK
        link_state = link_url_resp.json()["state"]

        # 2. Callback in link mode links Discord and switches avatar_source to discord
        mock_client = _mock_httpx_discord(
            {
                "id": "discord_111",
                "username": "fallen_disc",
                "global_name": "FalleN",
                "avatar": "hash111",
                "email": "fallen@clipai.com.br",
                "verified": True,
            }
        )
        with patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_client):
            cb_link = await client.post(
                "/api/v1/auth/discord/callback",
                headers=headers,
                json={"code": "oauth_code_1", "state": link_state},
            )
        assert cb_link.status_code == status.HTTP_200_OK
        assert cb_link.json()["mode"] == "link"
        assert cb_link.json()["user"]["discord_linked"] is True
        assert cb_link.json()["user"]["discord_username"] == "fallen_disc"

        # 3. Unlink Discord succeeds because fallen_br has a password
        unlink_resp = await client.delete("/api/v1/users/me/discord", headers=headers)
        assert unlink_resp.status_code == status.HTTP_200_OK
        assert unlink_resp.json()["discord_linked"] is False

        # 4. Login via Discord creates a new passwordless account when email is new
        login_url_resp = await client.get("/api/v1/auth/discord/url", params={"mode": "login"})
        assert login_url_resp.status_code == status.HTTP_200_OK
        login_state = login_url_resp.json()["state"]

        mock_new_client = _mock_httpx_discord(
            {
                "id": "discord_222",
                "username": "coldzera.cs",
                "global_name": "Marcelo David",
                "avatar": "hash222",
                "email": "coldzera@clipai.com.br",
                "verified": True,
            }
        )
        with patch("app.services.discord_service.httpx.AsyncClient", return_value=mock_new_client):
            cb_login = await client.post(
                "/api/v1/auth/discord/callback",
                json={"code": "oauth_code_2", "state": login_state},
            )
        assert cb_login.status_code == status.HTTP_200_OK
        login_body = cb_login.json()
        assert login_body["access_token"]
        assert "clipai_refresh_token" in cb_login.cookies
        assert login_body["user"]["username"] == "coldzera_cs"
        assert login_body["user"]["has_password"] is False
        assert login_body["user"]["discord_linked"] is True

        # 5. Passwordless Discord user is blocked from unlinking Discord with HTTP 400
        new_user_headers = {"Authorization": f"Bearer {login_body['access_token']}"}
        blocked_unlink = await client.delete(
            "/api/v1/users/me/discord",
            headers=new_user_headers,
        )
        assert blocked_unlink.status_code == status.HTTP_400_BAD_REQUEST
        assert blocked_unlink.json()["error"]["code"] == "PASSWORD_REQUIRED_BEFORE_UNLINK"

        # 6. POST /api/v1/users/me/discord/use-avatar switches avatar_source to discord
        use_av = await client.post(
            "/api/v1/users/me/discord/use-avatar",
            headers=new_user_headers,
        )
        assert use_av.status_code == status.HTTP_200_OK
        assert use_av.json()["avatar_source"] == "discord"
        _ = settings


async def test_2fa_end_to_end_login_challenge_and_backup_code_consumption(
    sprint4_env: dict[str, Any],
) -> None:
    """Verify full HTTP 2FA setup, enable, login challenge, TOTP/backup verify, and disable."""
    app = sprint4_env["app"]
    headers = sprint4_env["headers"]

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        # 1. Setup 2FA
        setup_resp = await client.post("/api/v1/auth/2fa/setup", headers=headers)
        assert setup_resp.status_code == status.HTTP_200_OK
        manual_secret = setup_resp.json()["manual_secret"]
        assert setup_resp.json()["otpauth_uri"].startswith("otpauth://totp/")

        # 2. Enable 2FA with valid TOTP
        totp = pyotp.TOTP(manual_secret)
        enable_resp = await client.post(
            "/api/v1/auth/2fa/enable",
            headers=headers,
            json={"code": totp.now()},
        )
        assert enable_resp.status_code == status.HTTP_200_OK
        backup_codes = enable_resp.json()["backup_codes"]
        assert len(backup_codes) == 8

        # 3. GET /api/v1/auth/me now shows is_2fa_enabled=True and has_password=True
        me_resp = await client.get("/api/v1/auth/me", headers=headers)
        assert me_resp.status_code == status.HTTP_200_OK
        assert me_resp.json()["is_2fa_enabled"] is True
        assert me_resp.json()["has_password"] is True

        # 4. Login returns 2FA challenge without setting refresh token cookie
        login_resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "fallen@clipai.com.br", "password": "StrongPass!2026"},
        )
        assert login_resp.status_code == status.HTTP_200_OK
        login_data = login_resp.json()
        assert login_data["requires_2fa"] is True
        assert login_data["access_token"] is None
        assert login_data["challenge_token"]
        assert "clipai_refresh_token" not in login_resp.cookies

        challenge_token = login_data["challenge_token"]

        # 5. Invalid 2FA code rejected with 401
        bad_verify = await client.post(
            "/api/v1/auth/2fa/verify-login",
            json={"challenge_token": challenge_token, "code": "000000"},
        )
        assert bad_verify.status_code == status.HTTP_401_UNAUTHORIZED
        assert bad_verify.json()["error"]["code"] == "INVALID_2FA_CODE"

        # 6. Valid TOTP code completes login and issues access token + HttpOnly refresh cookie
        good_verify = await client.post(
            "/api/v1/auth/2fa/verify-login",
            json={"challenge_token": challenge_token, "code": totp.now()},
        )
        assert good_verify.status_code == status.HTTP_200_OK
        assert good_verify.json()["access_token"]
        assert good_verify.json()["requires_2fa"] is False
        assert "clipai_refresh_token" in good_verify.cookies

        # 7. Backup code works once, then fails if reused
        backup_verify = await client.post(
            "/api/v1/auth/2fa/verify-login",
            json={"challenge_token": challenge_token, "code": backup_codes[0]},
        )
        assert backup_verify.status_code == status.HTTP_200_OK
        assert backup_verify.json()["access_token"]

        reused_backup = await client.post(
            "/api/v1/auth/2fa/verify-login",
            json={"challenge_token": challenge_token, "code": backup_codes[0]},
        )
        assert reused_backup.status_code == status.HTTP_401_UNAUTHORIZED

        # 8. Disable 2FA
        disable_resp = await client.post(
            "/api/v1/auth/2fa/disable",
            headers=headers,
            json={"code": totp.now(), "password": "StrongPass!2026"},
        )
        assert disable_resp.status_code == status.HTTP_200_OK

        me_after = await client.get("/api/v1/auth/me", headers=headers)
        assert me_after.json()["is_2fa_enabled"] is False
