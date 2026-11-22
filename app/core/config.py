"""Application configuration loaded and validated via pydantic-settings."""

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed application settings loaded from environment variables or .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application & Environment
    app_name: str = "clipai-api"
    app_version: str = "1.0.0"
    app_env: Literal["development", "staging", "production"] = Field(
        default="development",
        validation_alias=AliasChoices("APP_ENV", "ENVIRONMENT"),
    )
    app_debug: bool = False
    api_v1_prefix: str = "/api/v1"
    frontend_url: str = "http://localhost:3000"
    cors_allowed_origins: str = "http://localhost:3000"

    # Database (PostgreSQL - Neon / Supabase)
    database_url: str
    db_pool_size: int = Field(default=10, ge=1, le=100)
    db_max_overflow: int = Field(default=20, ge=0, le=100)
    db_echo_sql: bool = False
    db_statement_cache_size: int | None = Field(default=None, ge=0)

    # Security & Authentication
    jwt_secret_key: SecretStr
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = Field(default=15, ge=1)
    refresh_token_expire_days: int = Field(default=7, ge=1)
    email_verification_token_expire_hours: int = Field(default=24, ge=1)
    password_reset_token_expire_minutes: int = Field(default=30, ge=1)

    # Refresh Token Cookie
    refresh_cookie_name: str = "clipai_refresh_token"
    refresh_cookie_secure: bool | None = None
    refresh_cookie_samesite: Literal["lax", "strict"] = "lax"
    refresh_cookie_domain: str = ""

    # 2FA TOTP (Reserved for v1.1)
    totp_encryption_key: SecretStr | None = None
    totp_issuer_name: str = "CLIPAI"

    # Cloudflare R2 (S3 Compatible Storage)
    r2_account_id: str
    r2_access_key_id: SecretStr
    r2_secret_access_key: SecretStr
    r2_bucket_name: str = "clipai-media"
    r2_public_base_url: str
    r2_presigned_url_expire_seconds: int = Field(default=900, ge=60, le=3600)

    # Upload Limits
    max_video_size_bytes: int = Field(default=104_857_600, ge=1)
    max_video_duration_seconds: int = Field(default=120, ge=1)
    max_thumbnail_size_bytes: int = Field(default=5_242_880, ge=1)

    # Resend (Transactional Email)
    resend_api_key: SecretStr | None = None
    email_from_address: str = "CLIPAÍ <onboarding@resend.dev>"

    @model_validator(mode="after")
    def validate_production_requirements(self) -> "Settings":
        """Require critical external credentials in production environments."""
        if self.app_env == "production" and (
            self.resend_api_key is None or not self.resend_api_key.get_secret_value().strip()
        ):
            msg = "RESEND_API_KEY is required when APP_ENV/ENVIRONMENT is 'production'."
            raise ValueError(msg)
        return self

    @property
    def is_production(self) -> bool:
        """Return True when running in production environment."""
        return self.app_env == "production"

    @property
    def cookie_secure(self) -> bool:
        """Resolve Secure attribute for cookies based on explicit override or environment."""
        if self.refresh_cookie_secure is not None:
            return self.refresh_cookie_secure
        return self.is_production

    @field_validator("database_url")
    @classmethod
    def validate_async_database_url(cls, value: str) -> str:
        """Ensure PostgreSQL URL uses an async driver (asyncpg)."""
        if not value.startswith("postgresql+asyncpg://"):
            msg = "DATABASE_URL must use the 'postgresql+asyncpg://' async driver scheme."
            raise ValueError(msg)
        return value

    @field_validator("cors_allowed_origins")
    @classmethod
    def validate_cors_origins_no_wildcard(cls, value: str) -> str:
        """Reject wildcard origins to comply with strict CORS and cookie security."""
        origins = [origin.strip() for origin in value.split(",") if origin.strip()]
        if not origins:
            msg = "CORS_ALLOWED_ORIGINS must contain at least one explicit origin."
            raise ValueError(msg)
        if "*" in origins:
            msg = "Wildcard '*' is not allowed in CORS_ALLOWED_ORIGINS."
            raise ValueError(msg)
        return value

    @property
    def cors_origins(self) -> list[str]:
        """Return parsed list of allowed CORS origins."""
        return [origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    """Return cached application Settings instance."""
    return Settings()
