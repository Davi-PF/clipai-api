"""Security primitives: Argon2id password hashing, PyJWT tokens, and SHA-256 helpers."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

import jwt
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import status

from app.core.config import Settings
from app.core.exceptions import AppError


class TokenType(StrEnum):
    """Supported JWT token scope claims."""

    ACCESS = "access"
    EMAIL_VERIFICATION = "email_verification"
    PASSWORD_RESET = "password_reset"  # noqa: S105
    TWO_FA_PENDING = "2fa_pending"


# OWASP-recommended Argon2id parameters per docs/SECURITY.md
_PASSWORD_HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)

# Precomputed dummy hash to equalize login latency when e-mail does not exist
_DUMMY_PASSWORD_HASH = _PASSWORD_HASHER.hash(secrets.token_urlsafe(32))


def hash_password(plain_password: str) -> str:
    """Hash a plain password using Argon2id."""
    return _PASSWORD_HASHER.hash(plain_password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    """Verify a plain password against a stored Argon2id hash."""
    try:
        return _PASSWORD_HASHER.verify(password_hash, plain_password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def verify_dummy_password(plain_password: str) -> None:
    """Execute a dummy Argon2 verification to mitigate login timing attacks."""
    verify_password(plain_password, _DUMMY_PASSWORD_HASH)


def needs_password_rehash(password_hash: str) -> bool:
    """Return True if the stored Argon2id hash uses outdated cost parameters."""
    return _PASSWORD_HASHER.check_needs_rehash(password_hash)


def generate_refresh_token() -> str:
    """Generate a cryptographically secure opaque refresh token."""
    return secrets.token_urlsafe(48)


def hash_token(raw_token: str) -> str:
    """Compute hex SHA-256 digest for safe database storage of opaque tokens."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def compute_password_fingerprint(password_hash: str) -> str:
    """Compute short SHA-256 fingerprint of a password hash to invalidate reset tokens."""
    return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()[:16]


def create_jwt_token(
    *,
    subject: str | UUID,
    token_type: TokenType,
    expires_delta: timedelta,
    settings: Settings,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Encode a signed JWT with mandatory standard claims per docs/SECURITY.md."""
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(subject),
        "type": token_type,
        "iat": now,
        "exp": now + expires_delta,
        "jti": str(uuid4()),
    }
    if extra_claims:
        payload.update(extra_claims)

    return jwt.encode(
        payload,
        settings.jwt_secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )


def create_access_token(user_id: UUID, settings: Settings) -> str:
    """Create short-lived access JWT for API authorization."""
    return create_jwt_token(
        subject=user_id,
        token_type=TokenType.ACCESS,
        expires_delta=timedelta(minutes=settings.access_token_expire_minutes),
        settings=settings,
    )


def create_email_verification_token(user_id: UUID, settings: Settings) -> str:
    """Create signed JWT for e-mail address confirmation."""
    return create_jwt_token(
        subject=user_id,
        token_type=TokenType.EMAIL_VERIFICATION,
        expires_delta=timedelta(hours=settings.email_verification_token_expire_hours),
        settings=settings,
    )


def create_password_reset_token(
    user_id: UUID,
    password_hash: str,
    settings: Settings,
) -> str:
    """Create single-use signed JWT bound to the current password hash."""
    return create_jwt_token(
        subject=user_id,
        token_type=TokenType.PASSWORD_RESET,
        expires_delta=timedelta(minutes=settings.password_reset_token_expire_minutes),
        settings=settings,
        extra_claims={"pwd_fp": compute_password_fingerprint(password_hash)},
    )


def decode_jwt_token(
    token: str,
    *,
    expected_type: TokenType | str,
    settings: Settings,
    invalid_status_code: int = status.HTTP_401_UNAUTHORIZED,
) -> dict[str, Any]:
    """Decode and strictly validate JWT signature, expiration, and token type."""
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.ExpiredSignatureError as exc:
        raise AppError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="TOKEN_EXPIRED",
            message="Sua sessão expirou. Faça login novamente.",
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise AppError(
            status_code=invalid_status_code,
            code="INVALID_TOKEN",
            message="Token de autenticação inválido ou revogado.",
        ) from exc

    if payload.get("type") != expected_type or not payload.get("sub"):
        raise AppError(
            status_code=invalid_status_code,
            code="INVALID_TOKEN",
            message="Token de autenticação inválido ou revogado.",
        )

    return payload
