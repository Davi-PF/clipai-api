"""Security primitives: Argon2id password hashing, PyJWT tokens, TOTP, and Fernet helpers."""

import base64
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

import jwt
import pyotp
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken
from fastapi import status

from app.core.config import Settings
from app.core.exceptions import AppError

_BACKUP_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class TokenType(StrEnum):
    """Supported JWT token scope claims."""

    ACCESS = "access"
    EMAIL_VERIFICATION = "email_verification"
    PASSWORD_RESET = "password_reset"  # noqa: S105
    TWO_FA_PENDING = "2fa_pending"
    TWO_FA_CHALLENGE = "2fa_challenge"
    DISCORD_OAUTH_STATE = "discord_oauth_state"


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


def compute_password_fingerprint(password_hash: str | None) -> str:
    """Compute short SHA-256 fingerprint of a password hash to invalidate reset tokens."""
    raw = password_hash or ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


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
    password_hash: str | None,
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


def create_2fa_challenge_token(user_id: UUID, settings: Settings) -> str:
    """Create short-lived (5 min) JWT for 2FA login verification challenge."""
    return create_jwt_token(
        subject=user_id,
        token_type=TokenType.TWO_FA_CHALLENGE,
        expires_delta=timedelta(minutes=5),
        settings=settings,
        extra_claims={"scope": TokenType.TWO_FA_CHALLENGE.value},
    )


def decode_2fa_challenge_token(token: str, *, settings: Settings) -> dict[str, Any]:
    """Decode and validate a 5-minute 2FA login challenge JWT."""
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
            message="O desafio de autenticação em duas etapas expirou. Faça login novamente.",
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise AppError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="INVALID_TOKEN",
            message="Token de desafio 2FA inválido ou revogado.",
        ) from exc

    token_type = payload.get("type")
    token_scope = payload.get("scope")
    valid_types = {TokenType.TWO_FA_CHALLENGE.value, TokenType.TWO_FA_PENDING.value}
    if (token_type not in valid_types and token_scope not in valid_types) or not payload.get("sub"):
        raise AppError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="INVALID_TOKEN",
            message="Token de desafio 2FA inválido ou revogado.",
        )
    return payload


def _resolve_fernet(settings: Settings) -> Fernet:
    """Build a Fernet instance from TOTP_ENCRYPTION_KEY (with dev fallback derivation)."""
    raw_key = (
        settings.totp_encryption_key.get_secret_value().strip()
        if settings.totp_encryption_key is not None
        else ""
    )
    if raw_key:
        try:
            return Fernet(raw_key.encode("utf-8"))
        except ValueError as exc:
            if settings.is_production:
                raise AppError(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    code="INTERNAL_SERVER_ERROR",
                    message="Chave de criptografia 2FA inválida no servidor.",
                ) from exc
            derived = base64.urlsafe_b64encode(hashlib.sha256(raw_key.encode("utf-8")).digest())
            return Fernet(derived)

    if settings.is_production:
        raise AppError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="INTERNAL_SERVER_ERROR",
            message="Chave de criptografia 2FA não configurada no servidor.",
        )

    fallback_seed = settings.jwt_secret_key.get_secret_value().encode("utf-8")
    derived_key = base64.urlsafe_b64encode(hashlib.sha256(fallback_seed).digest())
    return Fernet(derived_key)


def encrypt_totp_secret(plain_secret: str, settings: Settings) -> str:
    """Encrypt a Base32 TOTP secret using Fernet symmetric encryption."""
    fernet = _resolve_fernet(settings)
    return fernet.encrypt(plain_secret.encode("utf-8")).decode("utf-8")


def decrypt_totp_secret(encrypted_secret: str, settings: Settings) -> str:
    """Decrypt a Fernet-encrypted Base32 TOTP secret."""
    fernet = _resolve_fernet(settings)
    try:
        return fernet.decrypt(encrypted_secret.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise AppError(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="INVALID_TOKEN",
            message="Não foi possível validar o segredo de autenticação 2FA.",
        ) from exc


def generate_totp_secret() -> str:
    """Generate a random Base32 TOTP secret compatible with Google Authenticator."""
    return pyotp.random_base32()


def build_totp_provisioning_uri(*, secret: str, email: str, issuer_name: str) -> str:
    """Build an otpauth://totp/ URI for QR code generation."""
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=issuer_name)


def verify_totp_code(secret: str, code: str) -> bool:
    """Verify a 6-digit TOTP code with a +/- 30s tolerance window (valid_window=1)."""
    cleaned = code.strip().replace(" ", "")
    if len(cleaned) != 6 or not cleaned.isdigit():
        return False
    return bool(pyotp.TOTP(secret).verify(cleaned, valid_window=1))


def normalize_backup_code(code: str) -> str:
    """Normalize a 2FA backup recovery code by stripping hyphens/spaces and uppercasing."""
    return code.strip().replace("-", "").replace(" ", "").upper()


def hash_backup_code(code: str) -> str:
    """Compute SHA-256 hex digest of a normalized backup code."""
    return hash_token(normalize_backup_code(code))


def generate_backup_codes(count: int = 8) -> tuple[list[str], list[str]]:
    """Generate human-readable single-use backup codes (XXXX-XXXX) and their SHA-256 hashes."""
    plain_codes: list[str] = []
    hashed_codes: list[str] = []
    for _ in range(count):
        part_a = "".join(secrets.choice(_BACKUP_CODE_ALPHABET) for _ in range(4))
        part_b = "".join(secrets.choice(_BACKUP_CODE_ALPHABET) for _ in range(4))
        formatted = f"{part_a}-{part_b}"
        plain_codes.append(formatted)
        hashed_codes.append(hash_backup_code(formatted))
    return plain_codes, hashed_codes


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
