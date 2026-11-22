"""Unit tests for Argon2id password hashing, SHA-256 helpers, and PyJWT tokens."""

import hashlib
from datetime import timedelta
from uuid import uuid4

import jwt
import pytest

from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.security import (
    compute_password_fingerprint,
    create_access_token,
    create_email_verification_token,
    create_jwt_token,
    create_password_reset_token,
    decode_jwt_token,
    generate_refresh_token,
    hash_password,
    hash_token,
    needs_password_rehash,
    verify_dummy_password,
    verify_password,
)


def test_argon2_hash_and_verify_password() -> None:
    """Hash password with Argon2id and verify matching vs mismatching inputs."""
    password_hash = hash_password("StrongPass!2026")

    assert password_hash.startswith("$argon2id$")
    assert verify_password("StrongPass!2026", password_hash) is True
    assert verify_password("WrongPass!2026", password_hash) is False
    assert verify_password("StrongPass!2026", "not-a-valid-hash") is False
    assert needs_password_rehash(password_hash) is False
    verify_dummy_password("anything")


def test_refresh_token_generation_and_hashing() -> None:
    """Generate distinct opaque tokens and deterministic SHA-256 hashes."""
    first = generate_refresh_token()
    second = generate_refresh_token()
    expected_sha256 = hashlib.sha256(first.encode("utf-8")).hexdigest()

    assert first != second
    assert len(hash_token(first)) == 64
    assert hash_token(first) == expected_sha256
    assert hash_token(first) != hash_token(second)


def test_jwt_lifecycle_and_type_isolation(test_settings: Settings) -> None:
    """Ensure JWT tokens include mandatory claims and enforce expected token type."""
    user_id = uuid4()
    access = create_access_token(user_id, test_settings)
    verify = create_email_verification_token(user_id, test_settings)
    reset = create_password_reset_token(user_id, "hashed-pw", test_settings)

    access_payload = decode_jwt_token(access, expected_type="access", settings=test_settings)
    assert access_payload["sub"] == str(user_id)
    assert access_payload["type"] == "access"
    assert "jti" in access_payload

    reset_payload = decode_jwt_token(reset, expected_type="password_reset", settings=test_settings)
    assert reset_payload["pwd_fp"] == compute_password_fingerprint("hashed-pw")

    with pytest.raises(AppError) as exc_info:
        decode_jwt_token(verify, expected_type="access", settings=test_settings)
    assert exc_info.value.code == "INVALID_TOKEN"


def test_decode_jwt_rejects_expired_and_malformed_tokens(test_settings: Settings) -> None:
    """Raise TOKEN_EXPIRED for expired JWTs and INVALID_TOKEN for tampered JWTs."""
    expired = create_jwt_token(
        subject=uuid4(),
        token_type="access",
        expires_delta=timedelta(seconds=-10),
        settings=test_settings,
    )

    with pytest.raises(AppError) as expired_exc:
        decode_jwt_token(expired, expected_type="access", settings=test_settings)
    assert expired_exc.value.code == "TOKEN_EXPIRED"

    with pytest.raises(AppError) as invalid_exc:
        decode_jwt_token("invalid.jwt.token", expected_type="access", settings=test_settings)
    assert invalid_exc.value.code == "INVALID_TOKEN"

    no_sub_token = jwt.encode(
        {"type": "access"},
        test_settings.jwt_secret_key.get_secret_value(),
        algorithm=test_settings.jwt_algorithm,
    )
    with pytest.raises(AppError) as no_sub_exc:
        decode_jwt_token(no_sub_token, expected_type="access", settings=test_settings)
    assert no_sub_exc.value.code == "INVALID_TOKEN"
