"""Unit tests for Argon2id password hashing, SHA-256 helpers, and PyJWT tokens."""

import hashlib
from datetime import timedelta
from uuid import uuid4

import jwt
import pyotp
import pytest

from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.security import (
    build_totp_provisioning_uri,
    compute_password_fingerprint,
    create_2fa_challenge_token,
    create_access_token,
    create_discord_state_token,
    create_email_verification_token,
    create_jwt_token,
    create_password_reset_token,
    decode_2fa_challenge_token,
    decode_discord_state_token,
    decode_jwt_token,
    decrypt_totp_secret,
    encrypt_totp_secret,
    generate_backup_codes,
    generate_refresh_token,
    generate_totp_secret,
    hash_backup_code,
    hash_password,
    hash_token,
    needs_password_rehash,
    verify_dummy_password,
    verify_password,
    verify_totp_code,
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


def test_totp_fernet_backup_codes_and_oauth_state_tokens(test_settings: Settings) -> None:
    """Verify Fernet encryption, TOTP code verification, backup codes, and state/2FA JWTs."""
    secret = generate_totp_secret()
    encrypted = encrypt_totp_secret(secret, test_settings)
    assert encrypted != secret
    assert decrypt_totp_secret(encrypted, test_settings) == secret

    # Fallback derivation when totp_encryption_key is None in dev
    no_key_settings = test_settings.model_copy(update={"totp_encryption_key": None})
    enc_fallback = encrypt_totp_secret(secret, no_key_settings)
    assert decrypt_totp_secret(enc_fallback, no_key_settings) == secret

    # Corrupt ciphertext raises 400 INVALID_TOKEN
    with pytest.raises(AppError) as bad_cipher:
        decrypt_totp_secret("not-a-valid-fernet-token", test_settings)
    assert bad_cipher.value.status_code == 400

    uri = build_totp_provisioning_uri(
        secret=secret,
        email="fallen@clipai.com.br",
        issuer_name="CLIPAÍ",
    )
    assert uri.startswith("otpauth://totp/")
    assert "fallen%40clipai.com.br" in uri

    valid_code = pyotp.TOTP(secret).now()
    assert verify_totp_code(secret, valid_code) is True
    assert verify_totp_code(secret, "000000") is False or valid_code == "000000"
    assert verify_totp_code(secret, "12ab") is False

    plain_codes, hashed_codes = generate_backup_codes(count=8)
    assert len(plain_codes) == 8
    assert len(hashed_codes) == 8
    assert hash_backup_code(plain_codes[0]) == hashed_codes[0]
    assert hash_backup_code(plain_codes[0].replace("-", "").lower()) == hashed_codes[0]

    # 2FA challenge token
    user_id = uuid4()
    challenge = create_2fa_challenge_token(user_id, test_settings)
    decoded_challenge = decode_2fa_challenge_token(challenge, settings=test_settings)
    assert decoded_challenge["sub"] == str(user_id)
    assert decoded_challenge["scope"] == "2fa_challenge"

    with pytest.raises(AppError) as wrong_type_exc:
        decode_2fa_challenge_token(
            create_access_token(user_id, test_settings),
            settings=test_settings,
        )
    assert wrong_type_exc.value.status_code == 401

    # Discord OAuth2 state token
    link_state = create_discord_state_token(mode="link", user_id=user_id, settings=test_settings)
    decoded_link = decode_discord_state_token(link_state, settings=test_settings)
    assert decoded_link["mode"] == "link"
    assert decoded_link["user_id"] == str(user_id)

    login_state = create_discord_state_token(mode="login", user_id=None, settings=test_settings)
    decoded_login = decode_discord_state_token(login_state, settings=test_settings)
    assert decoded_login["mode"] == "login"
    assert decoded_login["user_id"] is None

    bad_mode_state = create_jwt_token(
        subject="anonymous",
        token_type="discord_oauth_state",  # type: ignore[arg-type]
        expires_delta=timedelta(minutes=5),
        settings=test_settings,
        extra_claims={"mode": "invalid"},
    )
    with pytest.raises(AppError) as bad_mode_exc:
        decode_discord_state_token(bad_mode_state, settings=test_settings)
    assert bad_mode_exc.value.status_code == 400

    bad_link_state = create_jwt_token(
        subject="anonymous",
        token_type="discord_oauth_state",  # type: ignore[arg-type]
        expires_delta=timedelta(minutes=5),
        settings=test_settings,
        extra_claims={"mode": "link", "user_id": "not-a-uuid"},
    )
    with pytest.raises(AppError) as bad_link_exc:
        decode_discord_state_token(bad_link_state, settings=test_settings)
    assert bad_link_exc.value.status_code == 400
