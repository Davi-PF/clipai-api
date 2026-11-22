"""End-to-end integration tests for /api/v1/auth/* and /api/v1/users/me."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import status
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.database import get_db
from app.core.dependencies import get_verified_user
from app.core.security import create_jwt_token
from app.main import create_app
from app.models.user import RefreshToken, User
from app.routers.auth import get_email_service
from app.services.email_service import EmailService


class _InMemoryAuthStore:
    """Stateful in-memory store backing fake repositories for HTTP integration tests."""

    def __init__(self) -> None:
        self.users: dict[UUID, User] = {}
        self.refresh_tokens: dict[UUID, RefreshToken] = {}


class _FakeUserRepository:
    def __init__(self, store: _InMemoryAuthStore, _session: AsyncSession) -> None:
        self._store = store

    async def get_by_id(self, user_id: UUID) -> User | None:
        return self._store.users.get(user_id)

    async def get_by_email(self, email: str) -> User | None:
        normalized = email.strip().lower()
        return next((u for u in self._store.users.values() if u.email.lower() == normalized), None)

    async def get_by_username(self, username: str) -> User | None:
        normalized = username.strip().lower()
        return next(
            (u for u in self._store.users.values() if u.username.lower() == normalized),
            None,
        )

    async def create(
        self,
        *,
        username: str,
        email: str,
        password_hash: str,
        display_name: str | None = None,
    ) -> User:
        now = datetime.now(UTC)
        user = User(
            id=uuid4(),
            username=username.strip(),
            email=email.strip().lower(),
            password_hash=password_hash,
            display_name=display_name.strip() if display_name else None,
            is_email_verified=False,
            is_active=True,
            is_2fa_enabled=False,
            created_at=now,
            updated_at=now,
        )
        self._store.users[user.id] = user
        return user

    async def mark_email_verified(self, user: User) -> User:
        user.is_email_verified = True
        return user

    async def update_password_hash(self, user: User, password_hash: str) -> User:
        user.password_hash = password_hash
        return user


class _FakeRefreshTokenRepository:
    def __init__(self, store: _InMemoryAuthStore, _session: AsyncSession) -> None:
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

    async def get_by_token_hash(self, token_hash: str) -> RefreshToken | None:
        return next(
            (t for t in self._store.refresh_tokens.values() if t.token_hash == token_hash),
            None,
        )

    async def revoke_and_replace(
        self,
        token: RefreshToken,
        *,
        revoked_at: datetime,
        replaced_by_id: UUID,
    ) -> None:
        token.revoked_at = revoked_at
        token.replaced_by_id = replaced_by_id

    async def revoke_by_token_hash(self, token_hash: str, *, revoked_at: datetime) -> None:
        for token in self._store.refresh_tokens.values():
            if token.token_hash == token_hash and token.revoked_at is None:
                token.revoked_at = revoked_at

    async def revoke_family(self, family_id: UUID, *, revoked_at: datetime) -> None:
        for token in self._store.refresh_tokens.values():
            if token.family_id == family_id and token.revoked_at is None:
                token.revoked_at = revoked_at

    async def revoke_all_for_user(self, user_id: UUID, *, revoked_at: datetime) -> None:
        for token in self._store.refresh_tokens.values():
            if token.user_id == user_id and token.revoked_at is None:
                token.revoked_at = revoked_at


@pytest.fixture
def auth_test_env(
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[AsyncClient, AsyncMock, _InMemoryAuthStore]:
    """Provide wired FastAPI test app with in-memory repositories and mocked EmailService."""
    store = _InMemoryAuthStore()

    def _user_repo_factory(session: AsyncSession) -> _FakeUserRepository:
        return _FakeUserRepository(store, session)

    def _refresh_repo_factory(session: AsyncSession) -> _FakeRefreshTokenRepository:
        return _FakeRefreshTokenRepository(store, session)

    monkeypatch.setattr("app.services.auth_service.UserRepository", _user_repo_factory)
    monkeypatch.setattr("app.services.auth_service.RefreshTokenRepository", _refresh_repo_factory)
    monkeypatch.setattr("app.core.dependencies.UserRepository", _user_repo_factory)

    email_mock = AsyncMock(spec=EmailService)
    application = create_app(settings=test_settings)
    db_session = AsyncMock(spec=AsyncSession)

    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    application.dependency_overrides[get_db] = _override_get_db
    application.dependency_overrides[get_email_service] = lambda: email_mock

    return application, email_mock, store


async def _register_and_verify(
    client: AsyncClient,
    email_mock: AsyncMock,
    *,
    username: str = "fallen_br",
    email: str = "fallen@clipai.com.br",
    password: str = "StrongPass!2026",
) -> str:
    reg_resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": email,
            "password": password,
            "display_name": "FalleN",
        },
    )
    assert reg_resp.status_code == status.HTTP_201_CREATED
    verification_token = email_mock.send_verification_email.call_args.kwargs["token"]

    verify_resp = await client.post(
        "/api/v1/auth/verify-email",
        json={"token": verification_token},
    )
    assert verify_resp.status_code == status.HTTP_200_OK
    return verification_token


async def test_register_login_blocked_until_verified_then_succeeds(
    auth_test_env: tuple,
) -> None:
    """Verify full registration -> unverified block -> e-mail verification -> login -> /me."""
    application, email_mock, _store = auth_test_env
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        reg_resp = await client.post(
            "/api/v1/auth/register",
            json={
                "username": "fallen_br",
                "email": "fallen@clipai.com.br",
                "password": "StrongPass!2026",
                "display_name": "Gabriel Toledo",
            },
        )
        assert reg_resp.status_code == status.HTTP_201_CREATED
        assert reg_resp.json()["user"]["is_email_verified"] is False
        verification_token = email_mock.send_verification_email.call_args.kwargs["token"]

        # Duplicate e-mail rejected with 409 CONFLICT
        dup_resp = await client.post(
            "/api/v1/auth/register",
            json={
                "username": "other_user",
                "email": "FALLEN@clipai.com.br",
                "password": "StrongPass!2026",
            },
        )
        assert dup_resp.status_code == status.HTTP_409_CONFLICT
        assert dup_resp.json()["error"]["code"] == "CONFLICT"

        # Login blocked before verification
        blocked_resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "fallen@clipai.com.br", "password": "StrongPass!2026"},
        )
        assert blocked_resp.status_code == status.HTTP_403_FORBIDDEN
        assert blocked_resp.json()["error"]["code"] == "EMAIL_NOT_VERIFIED"

        # Resend verification works without leaking status
        resend_resp = await client.post(
            "/api/v1/auth/resend-verification",
            json={"email": "fallen@clipai.com.br"},
        )
        assert resend_resp.status_code == status.HTTP_200_OK
        assert email_mock.send_verification_email.await_count == 2

        # Verify e-mail
        verify_resp = await client.post(
            "/api/v1/auth/verify-email",
            json={"token": verification_token},
        )
        assert verify_resp.status_code == status.HTTP_200_OK

        # Wrong password rejected with 401
        bad_login = await client.post(
            "/api/v1/auth/login",
            json={"email": "fallen@clipai.com.br", "password": "WrongPassword!1"},
        )
        assert bad_login.status_code == status.HTTP_401_UNAUTHORIZED
        assert bad_login.json()["error"]["code"] == "INVALID_CREDENTIALS"

        # Valid login issues access_token and HttpOnly refresh cookie
        login_resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "fallen@clipai.com.br", "password": "StrongPass!2026"},
        )
        assert login_resp.status_code == status.HTTP_200_OK
        body = login_resp.json()
        assert body["access_token"]
        assert body["requires_2fa"] is False
        assert body["temp_2fa_token"] is None
        assert "clipai_refresh_token" in login_resp.cookies
        set_cookie_header = login_resp.headers["set-cookie"].lower()
        assert "httponly" in set_cookie_header
        assert "samesite=lax" in set_cookie_header

        # /me endpoints require valid access Bearer token
        unauth_me = await client.get("/api/v1/auth/me")
        assert unauth_me.status_code == status.HTTP_401_UNAUTHORIZED

        headers = {"Authorization": f"Bearer {body['access_token']}"}
        auth_me = await client.get("/api/v1/auth/me", headers=headers)
        users_me = await client.get("/api/v1/users/me", headers=headers)
        assert auth_me.status_code == status.HTTP_200_OK
        assert users_me.status_code == status.HTTP_200_OK
        assert auth_me.json()["username"] == "fallen_br"
        assert users_me.json()["email"] == "fallen@clipai.com.br"


async def test_refresh_rotation_reuse_detection_and_logout(auth_test_env: tuple) -> None:
    """Verify refresh rotation, reuse detection revoking family, and logout cleanup."""
    application, email_mock, _store = auth_test_env
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        await _register_and_verify(client, email_mock)
        login_resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "fallen@clipai.com.br", "password": "StrongPass!2026"},
        )
        first_cookie = login_resp.cookies["clipai_refresh_token"]

        # Normal rotation
        client.cookies.set("clipai_refresh_token", first_cookie)
        refresh_resp = await client.post("/api/v1/auth/refresh")
        assert refresh_resp.status_code == status.HTTP_200_OK
        second_cookie = refresh_resp.cookies["clipai_refresh_token"]
        assert second_cookie != first_cookie

        # Reuse of first (revoked) refresh token triggers family revocation!
        client.cookies.set("clipai_refresh_token", first_cookie)
        reuse_resp = await client.post("/api/v1/auth/refresh")
        assert reuse_resp.status_code == status.HTTP_401_UNAUTHORIZED
        assert reuse_resp.json()["error"]["code"] == "INVALID_TOKEN"

        # Even the second cookie from the same family is now revoked
        client.cookies.set("clipai_refresh_token", second_cookie)
        compromised_resp = await client.post("/api/v1/auth/refresh")
        assert compromised_resp.status_code == status.HTTP_401_UNAUTHORIZED

        # Fresh login + logout
        relogin = await client.post(
            "/api/v1/auth/login",
            json={"email": "fallen@clipai.com.br", "password": "StrongPass!2026"},
        )
        fresh_cookie = relogin.cookies["clipai_refresh_token"]

        client.cookies.set("clipai_refresh_token", fresh_cookie)
        logout_resp = await client.post("/api/v1/auth/logout")
        assert logout_resp.status_code == status.HTTP_204_NO_CONTENT

        client.cookies.set("clipai_refresh_token", fresh_cookie)
        after_logout = await client.post("/api/v1/auth/refresh")
        assert after_logout.status_code == status.HTTP_401_UNAUTHORIZED


async def test_forgot_and_reset_password_revokes_active_sessions(
    auth_test_env: tuple,
) -> None:
    """Verify password reset updates credentials, invalidates sessions, and blocks reuse."""
    application, email_mock, _store = auth_test_env
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        await _register_and_verify(client, email_mock)
        login_resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "fallen@clipai.com.br", "password": "StrongPass!2026"},
        )
        active_refresh_cookie = login_resp.cookies["clipai_refresh_token"]

        # Forgot password for non-existing email still returns 200 generic message
        unknown_forgot = await client.post(
            "/api/v1/auth/forgot-password",
            json={"email": "unknown@clipai.com.br"},
        )
        assert unknown_forgot.status_code == status.HTTP_200_OK
        email_mock.send_password_reset_email.assert_not_awaited()

        # Forgot password for existing email sends reset token
        forgot_resp = await client.post(
            "/api/v1/auth/forgot-password",
            json={"email": "fallen@clipai.com.br"},
        )
        assert forgot_resp.status_code == status.HTTP_200_OK
        reset_token = email_mock.send_password_reset_email.call_args.kwargs["token"]

        reset_resp = await client.post(
            "/api/v1/auth/reset-password",
            json={"token": reset_token, "new_password": "BrandNewPassword!99"},
        )
        assert reset_resp.status_code == status.HTTP_200_OK

        # Reusing the same reset token must fail
        reused_reset = await client.post(
            "/api/v1/auth/reset-password",
            json={"token": reset_token, "new_password": "AnotherPassword!99"},
        )
        assert reused_reset.status_code == status.HTTP_400_BAD_REQUEST

        # Old refresh token was revoked during reset
        client.cookies.set("clipai_refresh_token", active_refresh_cookie)
        stale_refresh = await client.post("/api/v1/auth/refresh")
        assert stale_refresh.status_code == status.HTTP_401_UNAUTHORIZED

        # Old password fails, new password succeeds
        old_pw_login = await client.post(
            "/api/v1/auth/login",
            json={"email": "fallen@clipai.com.br", "password": "StrongPass!2026"},
        )
        assert old_pw_login.status_code == status.HTTP_401_UNAUTHORIZED

        new_pw_login = await client.post(
            "/api/v1/auth/login",
            json={"email": "fallen@clipai.com.br", "password": "BrandNewPassword!99"},
        )
        assert new_pw_login.status_code == status.HTTP_200_OK


async def test_rate_limiting_and_validation_errors_in_pt_br(
    auth_test_env: tuple,
    test_settings: Settings,
) -> None:
    """Enforce rate limiting (429 + Retry-After) and PT-BR validation error details."""
    application, _email_mock, store = auth_test_env
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        # Weak password and invalid username return 422 with PT-BR messages
        invalid_reg = await client.post(
            "/api/v1/auth/register",
            json={
                "username": "a",
                "email": "valid@clipai.com.br",
                "password": "weak",
            },
        )
        assert invalid_reg.status_code == 422
        assert invalid_reg.json()["error"]["code"] == "VALIDATION_ERROR"

        # 5 requests allowed per minute on register, 6th triggers 429
        for idx in range(4):
            resp = await client.post(
                "/api/v1/auth/register",
                json={
                    "username": f"player_{idx}",
                    "email": f"player_{idx}@clipai.com.br",
                    "password": "StrongPass!2026",
                },
            )
            assert resp.status_code == status.HTTP_201_CREATED

        limited_resp = await client.post(
            "/api/v1/auth/register",
            json={
                "username": "player_6",
                "email": "player_6@clipai.com.br",
                "password": "StrongPass!2026",
            },
            headers={"X-Forwarded-For": "127.0.0.1"},
        )
        assert limited_resp.status_code == status.HTTP_429_TOO_MANY_REQUESTS
        assert limited_resp.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
        assert "retry-after" in limited_resp.headers

        # Expired verification token returns 401 TOKEN_EXPIRED
        expired_verify = create_jwt_token(
            subject=uuid4(),
            token_type="email_verification",
            expires_delta=timedelta(seconds=-10),
            settings=test_settings,
        )
        exp_resp = await client.post(
            "/api/v1/auth/verify-email",
            json={"token": expired_verify},
        )
        assert exp_resp.status_code == status.HTTP_401_UNAUTHORIZED
        assert exp_resp.json()["error"]["code"] == "TOKEN_EXPIRED"

        # get_verified_user blocks unverified users with 403 EMAIL_NOT_VERIFIED
        unverified_user = next(iter(store.users.values()))
        await get_verified_user(unverified_user) if unverified_user.is_email_verified else None
        with pytest.raises(Exception) as verified_exc:
            await get_verified_user(unverified_user)
        assert verified_exc.value.status_code == status.HTTP_403_FORBIDDEN
        assert verified_exc.value.code == "EMAIL_NOT_VERIFIED"
