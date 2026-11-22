"""Unit tests for application settings validation and loading."""

import secrets

import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings


def _valid_env_overrides() -> dict[str, str]:
    """Build a valid set of environment values without hardcoded secrets."""
    return {
        "DATABASE_URL": "postgresql+asyncpg://localhost:5432/clipai_unit",
        "JWT_SECRET_KEY": secrets.token_hex(32),
        "R2_ACCOUNT_ID": "acct_unit",
        "R2_ACCESS_KEY_ID": secrets.token_hex(16),
        "R2_SECRET_ACCESS_KEY": secrets.token_hex(32),
        "R2_PUBLIC_BASE_URL": "https://cdn.clipai.com.br",
        "RESEND_API_KEY": f"re_{secrets.token_hex(16)}",
        "CORS_ALLOWED_ORIGINS": "http://localhost:3000, https://clipai.com.br",
    }


def test_settings_loads_valid_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify required settings and defaults load properly from environment."""
    for key, value in _valid_env_overrides().items():
        monkeypatch.setenv(key, value)

    settings = Settings(_env_file=None)

    assert settings.app_name == "clipai-api"
    assert settings.app_version == "1.0.0"
    assert settings.api_v1_prefix == "/api/v1"
    assert settings.cors_origins == ["http://localhost:3000", "https://clipai.com.br"]
    assert settings.max_video_size_bytes == 104_857_600
    assert settings.max_video_duration_seconds == 120
    assert settings.max_thumbnail_size_bytes == 5_242_880
    assert settings.refresh_cookie_name == "clipai_refresh_token"
    assert settings.totp_issuer_name == "CLIPAI"


def test_settings_rejects_non_async_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject synchronous PostgreSQL URLs to enforce asyncpg."""
    env = _valid_env_overrides()
    env["DATABASE_URL"] = "postgresql://localhost:5432/clipai_unit"
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(ValidationError, match=r"postgresql\+asyncpg://"):
        Settings(_env_file=None)


def test_settings_rejects_wildcard_cors_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject wildcard '*' in CORS_ALLOWED_ORIGINS per docs/SECURITY.md."""
    env = _valid_env_overrides()
    env["CORS_ALLOWED_ORIGINS"] = "*"
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(ValidationError, match="Wildcard"):
        Settings(_env_file=None)


def test_settings_rejects_empty_cors_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject empty CORS_ALLOWED_ORIGINS."""
    env = _valid_env_overrides()
    env["CORS_ALLOWED_ORIGINS"] = " ,  "
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(ValidationError, match="at least one explicit origin"):
        Settings(_env_file=None)


def test_settings_fails_fast_when_required_var_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure fail-fast validation when mandatory env variables are omitted."""
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)

    with pytest.raises(ValidationError, match="jwt_secret_key"):
        Settings(_env_file=None)


def test_get_settings_returns_cached_instance() -> None:
    """Verify get_settings uses LRU cache and returns identical instance."""
    get_settings.cache_clear()
    first = get_settings()
    second = get_settings()

    assert first is second


def test_settings_cookie_secure_follows_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Derive cookie_secure from ENVIRONMENT=production unless explicitly overridden."""
    monkeypatch.delenv("APP_ENV", raising=False)
    env = _valid_env_overrides()
    env["ENVIRONMENT"] = "development"
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    dev_settings = Settings(_env_file=None)
    assert dev_settings.app_env == "development"
    assert dev_settings.cookie_secure is False

    monkeypatch.setenv("ENVIRONMENT", "production")
    prod_settings = Settings(_env_file=None)
    assert prod_settings.is_production is True
    assert prod_settings.cookie_secure is True

    monkeypatch.setenv("REFRESH_COOKIE_SECURE", "false")
    overridden = Settings(_env_file=None)
    assert overridden.cookie_secure is False


def test_settings_requires_resend_api_key_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail fast in production when RESEND_API_KEY is missing or blank."""
    monkeypatch.delenv("APP_ENV", raising=False)
    env = _valid_env_overrides()
    env["ENVIRONMENT"] = "production"
    env["RESEND_API_KEY"] = "  "
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(ValidationError, match="RESEND_API_KEY is required"):
        Settings(_env_file=None)
