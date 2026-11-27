"""Pydantic v2 request and response schemas for authentication endpoints."""

import re
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.schemas.user import UserAuthSummary, UserRegisteredResponse

_USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9_]{3,30}$")
_SPECIAL_CHAR_PATTERN = re.compile(r"[^a-zA-Z0-9\s]")


def _validate_password_strength(password: str) -> str:
    """Enforce RN-AUTH-03 password complexity rules with PT-BR feedback."""
    if len(password) < 8 or len(password) > 128:
        msg = "A senha deve ter entre 8 e 128 caracteres."
        raise ValueError(msg)
    if not any(char.isupper() for char in password):
        msg = "A senha deve conter pelo menos uma letra maiúscula."
        raise ValueError(msg)
    if not any(char.islower() for char in password):
        msg = "A senha deve conter pelo menos uma letra minúscula."
        raise ValueError(msg)
    if not any(char.isdigit() for char in password):
        msg = "A senha deve conter pelo menos um número."
        raise ValueError(msg)
    if not _SPECIAL_CHAR_PATTERN.search(password):
        msg = "A senha deve conter pelo menos um caractere especial."
        raise ValueError(msg)
    return password


class RegisterRequest(BaseModel):
    """Payload for POST /api/v1/auth/register."""

    username: str
    email: EmailStr
    password: str
    display_name: str | None = Field(default=None, max_length=50)
    invite_code: str | None = Field(default=None, max_length=16)

    @field_validator("invite_code")
    @classmethod
    def normalize_invite_code(cls, value: str | None) -> str | None:
        """Trim whitespace from optional invite_code and convert blank to None."""
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        """Validate username format per RN-AUTH-01."""
        cleaned = value.strip()
        if not _USERNAME_PATTERN.fullmatch(cleaned):
            msg = (
                "O nome de usuário deve ter entre 3 e 30 caracteres usando apenas letras, "
                "números e sublinhado (_)."
            )
            raise ValueError(msg)
        return cleaned

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: EmailStr) -> str:
        """Normalize e-mail to lowercase."""
        return str(value).strip().lower()

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        """Validate password complexity."""
        return _validate_password_strength(value)


class RegisterResponse(BaseModel):
    """Response for POST /api/v1/auth/register."""

    model_config = ConfigDict(from_attributes=True)

    message: str
    user: UserRegisteredResponse


class VerifyEmailRequest(BaseModel):
    """Payload for POST /api/v1/auth/verify-email."""

    token: str = Field(min_length=1)


class ResendVerificationRequest(BaseModel):
    """Payload for POST /api/v1/auth/resend-verification."""

    email: EmailStr

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: EmailStr) -> str:
        """Normalize e-mail to lowercase."""
        return str(value).strip().lower()


class LoginRequest(BaseModel):
    """Payload for POST /api/v1/auth/login."""

    email: EmailStr
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: EmailStr) -> str:
        """Normalize e-mail to lowercase."""
        return str(value).strip().lower()


class LoginResponse(BaseModel):
    """Response for POST /api/v1/auth/login, /auth/discord/callback, and /auth/2fa/verify-login."""

    model_config = ConfigDict(from_attributes=True)

    access_token: str | None = None
    token_type: Literal["bearer"] = Field(default="bearer")
    expires_in: int = 900
    requires_2fa: bool = False
    challenge_token: str | None = None
    temp_2fa_token: str | None = None
    user: UserAuthSummary | None = None


class TwoFactorSetupResponse(BaseModel):
    """Response for POST /api/v1/auth/2fa/setup."""

    model_config = ConfigDict(from_attributes=True)

    otpauth_uri: str
    manual_secret: str
    secret: str
    issuer: str = "CLIPAÍ"


class TwoFactorEnableRequest(BaseModel):
    """Payload for POST /api/v1/auth/2fa/enable."""

    code: str = Field(min_length=6, max_length=16)

    @field_validator("code")
    @classmethod
    def strip_code(cls, value: str) -> str:
        """Trim whitespace from TOTP code."""
        return value.strip()


class TwoFactorEnableResponse(BaseModel):
    """Response for POST /api/v1/auth/2fa/enable with single-display backup codes."""

    model_config = ConfigDict(from_attributes=True)

    is_2fa_enabled: bool = True
    backup_codes: list[str]
    message: str


class TwoFactorDisableRequest(BaseModel):
    """Payload for POST /api/v1/auth/2fa/disable."""

    code: str = Field(min_length=6, max_length=32)
    password: str | None = Field(default=None, max_length=128)

    @field_validator("code")
    @classmethod
    def strip_code(cls, value: str) -> str:
        """Trim whitespace from TOTP or backup code."""
        return value.strip()


class TwoFactorVerifyLoginRequest(BaseModel):
    """Payload for POST /api/v1/auth/2fa/verify-login."""

    challenge_token: str = Field(
        min_length=1,
        validation_alias=AliasChoices("challenge_token", "temp_2fa_token"),
    )
    code: str = Field(min_length=6, max_length=32)

    @field_validator("code")
    @classmethod
    def strip_code(cls, value: str) -> str:
        """Trim whitespace from TOTP or backup code."""
        return value.strip()


class RefreshResponse(BaseModel):
    """Response for POST /api/v1/auth/refresh."""

    model_config = ConfigDict(from_attributes=True)

    access_token: str
    token_type: Literal["bearer"] = Field(default="bearer")
    expires_in: int


class ForgotPasswordRequest(BaseModel):
    """Payload for POST /api/v1/auth/forgot-password."""

    email: EmailStr

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: EmailStr) -> str:
        """Normalize e-mail to lowercase."""
        return str(value).strip().lower()


class ResetPasswordRequest(BaseModel):
    """Payload for POST /api/v1/auth/reset-password."""

    token: str = Field(min_length=1)
    new_password: str

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, value: str) -> str:
        """Validate new password complexity."""
        return _validate_password_strength(value)


DiscordAuthMode = Literal["login", "link"]


class DiscordAuthUrlResponse(BaseModel):
    """Response for GET /api/v1/auth/discord/url."""

    model_config = ConfigDict(from_attributes=True)

    url: str
    authorization_url: str
    state: str
    mode: DiscordAuthMode


class DiscordCallbackRequest(BaseModel):
    """Payload for POST /api/v1/auth/discord/callback."""

    code: str = Field(min_length=1)
    state: str = Field(min_length=1)


class DiscordCallbackResponse(LoginResponse):
    """Response for POST /api/v1/auth/discord/callback (supports both login and link modes)."""

    model_config = ConfigDict(from_attributes=True)

    mode: DiscordAuthMode = "login"
    linked: bool = True
    message: str | None = None


class MessageResponse(BaseModel):
    """Generic PT-BR confirmation message envelope."""

    model_config = ConfigDict(from_attributes=True)

    message: str
