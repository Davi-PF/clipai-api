"""Authentication business logic service."""

import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.security import (
    TokenType,
    build_totp_provisioning_uri,
    compute_password_fingerprint,
    create_2fa_challenge_token,
    create_access_token,
    create_discord_state_token,
    create_email_verification_token,
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
from app.models.user import User
from app.repositories.refresh_token_repository import RefreshTokenRepository
from app.repositories.user_repository import UserRepository
from app.schemas.auth import (
    DiscordAuthMode,
    DiscordAuthUrlResponse,
    DiscordCallbackRequest,
    DiscordCallbackResponse,
    LoginRequest,
    LoginResponse,
    MessageResponse,
    RefreshResponse,
    RegisterRequest,
    RegisterResponse,
    ResetPasswordRequest,
    TwoFactorDisableRequest,
    TwoFactorEnableRequest,
    TwoFactorEnableResponse,
    TwoFactorSetupResponse,
    TwoFactorVerifyLoginRequest,
)
from app.schemas.user import UserAuthSummary, UserRegisteredResponse
from app.services.discord_service import DiscordOAuthService, DiscordUserProfile
from app.services.email_service import EmailService


class AuthService:
    """Orchestrates registration, verification, login, Discord OAuth2, 2FA, and password resets."""

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        email_service: EmailService,
        discord_service: DiscordOAuthService | None = None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._email_service = email_service
        self._discord_service = discord_service or DiscordOAuthService(settings)
        self._user_repo = UserRepository(session)
        self._refresh_repo = RefreshTokenRepository(session)

    async def register(self, payload: RegisterRequest) -> RegisterResponse:
        """Create an unverified user account and send confirmation e-mail."""
        if await self._user_repo.get_by_email(payload.email) is not None:
            raise AppError(
                status_code=status.HTTP_409_CONFLICT,
                code="CONFLICT",
                message="Já existe uma conta cadastrada com este endereço de e-mail.",
            )

        if await self._user_repo.get_by_username(payload.username) is not None:
            raise AppError(
                status_code=status.HTTP_409_CONFLICT,
                code="CONFLICT",
                message="Este nome de usuário já está em uso.",
            )

        user = await self._user_repo.create(
            username=payload.username,
            email=payload.email,
            password_hash=hash_password(payload.password),
            display_name=payload.display_name,
        )

        verification_token = create_email_verification_token(user.id, self._settings)
        await self._email_service.send_verification_email(
            to_email=user.email,
            username=user.username,
            token=verification_token,
        )

        await self._session.commit()
        await self._session.refresh(user)

        return RegisterResponse(
            message=(
                "Cadastro realizado com sucesso. "
                "Verifique sua caixa de entrada para confirmar seu e-mail."
            ),
            user=UserRegisteredResponse.model_validate(user),
        )

    async def verify_email(self, token: str) -> MessageResponse:
        """Confirm e-mail address from signed verification JWT."""
        payload = decode_jwt_token(
            token,
            expected_type=TokenType.EMAIL_VERIFICATION,
            settings=self._settings,
            invalid_status_code=status.HTTP_400_BAD_REQUEST,
        )

        user = await self._resolve_user_from_sub(
            payload["sub"],
            invalid_status_code=status.HTTP_400_BAD_REQUEST,
        )

        if not user.is_email_verified:
            await self._user_repo.mark_email_verified(user)
            await self._session.commit()

        return MessageResponse(
            message="E-mail confirmado com sucesso. Sua conta está pronta para uso."
        )

    async def resend_verification(self, email: str) -> MessageResponse:
        """Resend verification e-mail silently without leaking account existence."""
        user = await self._user_repo.get_by_email(email)
        if user is not None and user.is_active and not user.is_email_verified:
            token = create_email_verification_token(user.id, self._settings)
            await self._email_service.send_verification_email(
                to_email=user.email,
                username=user.username,
                token=token,
            )

        return MessageResponse(
            message=(
                "Se o e-mail estiver cadastrado e pendente de confirmação, "
                "um novo link foi enviado."
            )
        )

    async def login(
        self,
        payload: LoginRequest,
        *,
        user_agent: str | None,
        ip_address: str | None,
    ) -> tuple[LoginResponse, str | None]:
        """Authenticate credentials and issue tokens or a 2FA login challenge."""
        user = await self._user_repo.get_by_email(payload.email)
        if user is None or not user.password_hash:
            verify_dummy_password(payload.password)
            raise self._invalid_credentials_error()

        if not user.is_active or not verify_password(payload.password, user.password_hash):
            raise self._invalid_credentials_error()

        if not user.is_email_verified:
            raise AppError(
                status_code=status.HTTP_403_FORBIDDEN,
                code="EMAIL_NOT_VERIFIED",
                message="Confirme seu endereço de e-mail antes de realizar esta ação.",
            )

        if needs_password_rehash(user.password_hash):
            await self._user_repo.update_password_hash(user, hash_password(payload.password))

        return await self._issue_login_or_2fa_challenge(
            user,
            user_agent=user_agent,
            ip_address=ip_address,
        )

    async def setup_2fa(self, current_user: User) -> TwoFactorSetupResponse:
        """Generate a pending Base32 TOTP secret encrypted at rest with Fernet."""
        if current_user.is_2fa_enabled:
            raise AppError(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="BAD_REQUEST",
                message="A autenticação em duas etapas (2FA) já está ativada nesta conta.",
            )

        plain_secret = generate_totp_secret()
        current_user.totp_secret_encrypted = encrypt_totp_secret(plain_secret, self._settings)
        current_user.is_2fa_enabled = False
        await self._user_repo.save(current_user)
        await self._session.commit()

        otpauth_uri = build_totp_provisioning_uri(
            secret=plain_secret,
            email=current_user.email,
            issuer_name=self._settings.totp_issuer_name,
        )
        return TwoFactorSetupResponse(
            otpauth_uri=otpauth_uri,
            manual_secret=plain_secret,
            secret=plain_secret,
            issuer=self._settings.totp_issuer_name,
        )

    async def enable_2fa(
        self,
        current_user: User,
        payload: TwoFactorEnableRequest,
    ) -> TwoFactorEnableResponse:
        """Verify first TOTP code, enable 2FA, and return 8 single-use backup codes."""
        if current_user.is_2fa_enabled:
            raise AppError(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="BAD_REQUEST",
                message="A autenticação em duas etapas (2FA) já está ativada nesta conta.",
            )
        if not current_user.totp_secret_encrypted:
            raise AppError(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="BAD_REQUEST",
                message="Inicie a configuração do 2FA antes de tentar ativá-lo.",
            )

        secret = decrypt_totp_secret(current_user.totp_secret_encrypted, self._settings)
        if not verify_totp_code(secret, payload.code):
            raise AppError(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="INVALID_2FA_CODE",
                message="Código do autenticador inválido. Verifique o código de 6 dígitos.",
            )

        plain_codes, hashed_codes = generate_backup_codes(count=8)
        current_user.is_2fa_enabled = True
        current_user.totp_backup_codes = hashed_codes
        await self._user_repo.save(current_user)
        await self._session.commit()

        return TwoFactorEnableResponse(
            is_2fa_enabled=True,
            backup_codes=plain_codes,
            message=(
                "Autenticação em duas etapas ativada com sucesso. "
                "Guarde seus códigos de backup em local seguro."
            ),
        )

    async def disable_2fa(
        self,
        current_user: User,
        payload: TwoFactorDisableRequest,
    ) -> MessageResponse:
        """Validate current password (if present) and TOTP/backup code, then disable 2FA."""
        if not current_user.is_2fa_enabled or not current_user.totp_secret_encrypted:
            raise AppError(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="BAD_REQUEST",
                message="A autenticação em duas etapas (2FA) não está ativada nesta conta.",
            )

        if current_user.password_hash:
            if not payload.password:
                raise AppError(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    code="BAD_REQUEST",
                    message="Informe sua senha atual para desativar a autenticação em duas etapas.",
                )
            if not verify_password(payload.password, current_user.password_hash):
                raise self._invalid_credentials_error()

        if not self._verify_totp_or_consume_backup_code(current_user, payload.code):
            raise AppError(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="INVALID_2FA_CODE",
                message="Código de verificação 2FA ou código de backup inválido.",
            )

        current_user.is_2fa_enabled = False
        current_user.totp_secret_encrypted = None
        current_user.totp_backup_codes = None
        await self._user_repo.save(current_user)
        await self._session.commit()

        return MessageResponse(
            message="A autenticação em duas etapas (2FA) foi desativada com sucesso."
        )

    async def verify_2fa_login(
        self,
        payload: TwoFactorVerifyLoginRequest,
        *,
        user_agent: str | None,
        ip_address: str | None,
    ) -> tuple[LoginResponse, str]:
        """Validate 2FA challenge token and TOTP/backup code, then issue session tokens."""
        jwt_payload = decode_2fa_challenge_token(
            payload.challenge_token,
            settings=self._settings,
        )
        user = await self._resolve_user_from_sub(
            jwt_payload["sub"],
            invalid_status_code=status.HTTP_401_UNAUTHORIZED,
        )
        if not user.is_2fa_enabled or not user.totp_secret_encrypted:
            raise self._invalid_token_error(status_code=status.HTTP_401_UNAUTHORIZED)

        if not self._verify_totp_or_consume_backup_code(user, payload.code):
            raise AppError(
                status_code=status.HTTP_401_UNAUTHORIZED,
                code="INVALID_2FA_CODE",
                message="Código de verificação 2FA ou código de backup inválido.",
            )

        await self._user_repo.save(user)
        return await self._issue_authenticated_session(
            user,
            user_agent=user_agent,
            ip_address=ip_address,
        )

    def get_discord_auth_url(
        self,
        *,
        mode: DiscordAuthMode,
        current_user: User | None,
    ) -> DiscordAuthUrlResponse:
        """Generate official Discord OAuth2 authorization URL with signed CSRF state JWT."""
        if mode == "link":
            if current_user is None:
                raise AppError(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    code="INVALID_TOKEN",
                    message="Faça login para vincular sua conta do Discord.",
                )
            user_id: UUID | None = current_user.id
        else:
            user_id = None

        state = create_discord_state_token(
            mode=mode,
            user_id=user_id,
            settings=self._settings,
        )
        url = self._discord_service.build_authorization_url(state=state)
        return DiscordAuthUrlResponse(
            url=url,
            authorization_url=url,
            state=state,
            mode=mode,
        )

    async def handle_discord_callback(
        self,
        payload: DiscordCallbackRequest,
        *,
        current_user: User | None,
        user_agent: str | None,
        ip_address: str | None,
    ) -> tuple[DiscordCallbackResponse, str | None]:
        """Process Discord OAuth2 callback in 'link' or 'login' mode."""
        state_payload = decode_discord_state_token(payload.state, settings=self._settings)
        mode: DiscordAuthMode = state_payload["mode"]
        profile = await self._discord_service.exchange_code_for_profile(payload.code)

        if mode == "link":
            response = await self._handle_discord_link(
                state_payload,
                profile,
                current_user=current_user,
            )
            return response, None

        return await self._handle_discord_login(
            profile,
            user_agent=user_agent,
            ip_address=ip_address,
        )

    async def _handle_discord_link(
        self,
        state_payload: dict[str, Any],
        profile: DiscordUserProfile,
        *,
        current_user: User | None,
    ) -> DiscordCallbackResponse:
        """Link Discord account to an existing authenticated user."""
        user = await self._resolve_user_from_sub(
            state_payload["user_id"],
            invalid_status_code=status.HTTP_401_UNAUTHORIZED,
        )
        if current_user is not None and current_user.id != user.id:
            raise AppError(
                status_code=status.HTTP_403_FORBIDDEN,
                code="FORBIDDEN",
                message="O estado de vinculação do Discord pertence a outro usuário.",
            )

        existing_owner = await self._user_repo.get_by_discord_id(profile.discord_id)
        if existing_owner is not None and existing_owner.id != user.id:
            raise AppError(
                status_code=status.HTTP_409_CONFLICT,
                code="CONFLICT",
                message="Esta conta do Discord já está vinculada a outro usuário.",
            )

        self._apply_discord_link(user, profile)
        await self._user_repo.save(user)
        await self._session.commit()

        return DiscordCallbackResponse(
            access_token=None,
            expires_in=0,
            requires_2fa=False,
            challenge_token=None,
            temp_2fa_token=None,
            user=UserAuthSummary.model_validate(user),
            mode="link",
            linked=True,
            message="Conta do Discord vinculada com sucesso.",
        )

    async def _handle_discord_login(
        self,
        profile: DiscordUserProfile,
        *,
        user_agent: str | None,
        ip_address: str | None,
    ) -> tuple[DiscordCallbackResponse, str | None]:
        """Authenticate, auto-link verified account, or provision a new Discord user."""
        user = await self._user_repo.get_by_discord_id(profile.discord_id)
        if user is not None:
            if not user.is_active:
                raise AppError(
                    status_code=status.HTTP_403_FORBIDDEN,
                    code="FORBIDDEN",
                    message="Esta conta de usuário está desativada.",
                )
            user.discord_username = profile.username
            user.discord_avatar_url = profile.avatar_url
            if user.avatar_source == "discord":
                user.avatar_url = profile.avatar_url
            await self._user_repo.save(user)
        else:
            user = await self._resolve_or_create_discord_user(profile)

        login_resp, raw_refresh = await self._issue_login_or_2fa_challenge(
            user,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        return (
            DiscordCallbackResponse(
                **login_resp.model_dump(),
                mode="login",
                linked=True,
                message="Autenticação via Discord realizada com sucesso.",
            ),
            raw_refresh,
        )

    async def _resolve_or_create_discord_user(self, profile: DiscordUserProfile) -> User:
        """Auto-link an existing verified account by email or create a new user."""
        if not profile.email:
            raise AppError(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="BAD_REQUEST",
                message="Sua conta do Discord não forneceu um endereço de e-mail válido.",
            )

        existing_email_user = await self._user_repo.get_by_email(profile.email)
        if existing_email_user is not None:
            if not existing_email_user.is_active:
                raise AppError(
                    status_code=status.HTTP_403_FORBIDDEN,
                    code="FORBIDDEN",
                    message="Esta conta de usuário está desativada.",
                )
            if profile.email_verified and existing_email_user.is_email_verified:
                if (
                    existing_email_user.discord_id
                    and existing_email_user.discord_id != profile.discord_id
                ):
                    raise AppError(
                        status_code=status.HTTP_409_CONFLICT,
                        code="CONFLICT",
                        message=(
                            "A conta associada a este e-mail já possui outro Discord vinculado."
                        ),
                    )
                self._apply_discord_link(existing_email_user, profile)
                await self._user_repo.save(existing_email_user)
                return existing_email_user

            # When automatic link is not safe (unverified email), create a new account
            # or reject email collision safely
            unique_username = await self._generate_unique_discord_username(profile.username)
            scoped_email = f"discord_{profile.discord_id}+{profile.email}"
            return await self._user_repo.create(
                username=unique_username,
                email=scoped_email,
                password_hash=None,
                display_name=profile.display_name,
                is_email_verified=profile.email_verified,
                avatar_source="discord",
                discord_id=profile.discord_id,
                discord_username=profile.username,
                discord_avatar_url=profile.avatar_url,
                discord_linked_at=datetime.now(UTC),
            )

        unique_username = await self._generate_unique_discord_username(profile.username)
        return await self._user_repo.create(
            username=unique_username,
            email=profile.email,
            password_hash=None,
            display_name=profile.display_name,
            is_email_verified=profile.email_verified,
            avatar_source="discord",
            discord_id=profile.discord_id,
            discord_username=profile.username,
            discord_avatar_url=profile.avatar_url,
            discord_linked_at=datetime.now(UTC),
        )

    @staticmethod
    def _apply_discord_link(user: User, profile: DiscordUserProfile) -> None:
        """Populate Discord fields on User and default avatar_source to discord when no custom."""
        user.discord_id = profile.discord_id
        user.discord_username = profile.username
        user.discord_avatar_url = profile.avatar_url
        user.discord_linked_at = datetime.now(UTC)
        if not user.avatar_key and user.avatar_source != "custom":
            user.avatar_source = "discord"
            user.avatar_url = profile.avatar_url

    async def _generate_unique_discord_username(self, raw_username: str) -> str:
        """Normalize Discord username to ^[a-zA-Z0-9_]{3,30}$ and ensure uniqueness."""
        normalized = re.sub(r"[^a-zA-Z0-9_]", "_", raw_username.strip().lower())
        normalized = re.sub(r"_+", "_", normalized).strip("_")
        if len(normalized) < 3:
            normalized = f"user_{normalized or 'player'}"

        base = normalized[:24]
        if await self._user_repo.get_by_username(base) is None:
            return base

        for _ in range(10):
            candidate = f"{base}_{secrets.token_hex(2)}"[:30]
            if await self._user_repo.get_by_username(candidate) is None:
                return candidate
        return f"user_{secrets.token_hex(6)}"

    async def _issue_login_or_2fa_challenge(
        self,
        user: User,
        *,
        user_agent: str | None,
        ip_address: str | None,
    ) -> tuple[LoginResponse, str | None]:
        """Issue 2FA challenge JWT if 2FA is active, otherwise issue full session tokens."""
        if user.is_2fa_enabled:
            await self._session.commit()
            challenge_token = create_2fa_challenge_token(user.id, self._settings)
            return (
                LoginResponse(
                    access_token=None,
                    expires_in=300,
                    requires_2fa=True,
                    challenge_token=challenge_token,
                    temp_2fa_token=challenge_token,
                    user=UserAuthSummary.model_validate(user),
                ),
                None,
            )

        response, raw_refresh = await self._issue_authenticated_session(
            user,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        return response, raw_refresh

    async def _issue_authenticated_session(
        self,
        user: User,
        *,
        user_agent: str | None,
        ip_address: str | None,
    ) -> tuple[LoginResponse, str]:
        """Persist a new rotating refresh token and build LoginResponse with access JWT."""
        raw_refresh_token = generate_refresh_token()
        expires_at = datetime.now(UTC) + timedelta(days=self._settings.refresh_token_expire_days)
        await self._refresh_repo.create(
            user_id=user.id,
            token_hash=hash_token(raw_refresh_token),
            family_id=uuid4(),
            expires_at=expires_at,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        await self._session.commit()

        access_token = create_access_token(user.id, self._settings)
        response = LoginResponse(
            access_token=access_token,
            expires_in=self._settings.access_token_expire_minutes * 60,
            requires_2fa=False,
            challenge_token=None,
            temp_2fa_token=None,
            user=UserAuthSummary.model_validate(user),
        )
        return response, raw_refresh_token

    def _verify_totp_or_consume_backup_code(self, user: User, raw_code: str) -> bool:
        """Verify 6-digit TOTP or consume a matching single-use SHA-256 backup code."""
        if not user.totp_secret_encrypted:
            return False

        secret = decrypt_totp_secret(user.totp_secret_encrypted, self._settings)
        if verify_totp_code(secret, raw_code):
            return True

        stored_hashes = list(user.totp_backup_codes or [])
        if not stored_hashes:
            return False

        candidate_hash = hash_backup_code(raw_code)
        if candidate_hash not in stored_hashes:
            return False

        user.totp_backup_codes = [item for item in stored_hashes if item != candidate_hash]
        return True

    async def refresh(
        self,
        raw_refresh_token: str | None,
        *,
        user_agent: str | None,
        ip_address: str | None,
    ) -> tuple[RefreshResponse, str]:
        """Rotate refresh token and revoke entire token family if reuse is detected."""
        if not raw_refresh_token:
            raise self._invalid_token_error()

        token_record = await self._refresh_repo.get_by_token_hash(hash_token(raw_refresh_token))
        if token_record is None:
            raise self._invalid_token_error()

        now = datetime.now(UTC)
        if token_record.is_revoked:
            await self._refresh_repo.revoke_family(token_record.family_id, revoked_at=now)
            await self._session.commit()
            raise self._invalid_token_error()

        if token_record.expires_at <= now:
            raise AppError(
                status_code=status.HTTP_401_UNAUTHORIZED,
                code="TOKEN_EXPIRED",
                message="Sua sessão expirou. Faça login novamente.",
            )

        user = await self._user_repo.get_by_id(token_record.user_id)
        if user is None or not user.is_active:
            raise self._invalid_token_error()

        new_raw_token = generate_refresh_token()
        new_record = await self._refresh_repo.create(
            user_id=user.id,
            token_hash=hash_token(new_raw_token),
            family_id=token_record.family_id,
            expires_at=now + timedelta(days=self._settings.refresh_token_expire_days),
            user_agent=user_agent,
            ip_address=ip_address,
        )
        await self._refresh_repo.revoke_and_replace(
            token_record,
            revoked_at=now,
            replaced_by_id=new_record.id,
        )
        await self._session.commit()

        access_token = create_access_token(user.id, self._settings)
        return (
            RefreshResponse(
                access_token=access_token,
                expires_in=self._settings.access_token_expire_minutes * 60,
            ),
            new_raw_token,
        )

    async def logout(self, raw_refresh_token: str | None) -> None:
        """Revoke the active refresh token when present."""
        if not raw_refresh_token:
            return
        await self._refresh_repo.revoke_by_token_hash(
            hash_token(raw_refresh_token),
            revoked_at=datetime.now(UTC),
        )
        await self._session.commit()

    async def forgot_password(self, email: str) -> MessageResponse:
        """Send password reset link without disclosing account existence."""
        user = await self._user_repo.get_by_email(email)
        if user is not None and user.is_active:
            reset_token = create_password_reset_token(
                user.id,
                user.password_hash,
                self._settings,
            )
            await self._email_service.send_password_reset_email(
                to_email=user.email,
                username=user.username,
                token=reset_token,
            )

        return MessageResponse(
            message=(
                "Se o endereço de e-mail estiver cadastrado, "
                "enviamos as instruções para redefinir sua senha."
            )
        )

    async def reset_password(self, payload: ResetPasswordRequest) -> MessageResponse:
        """Reset user password and revoke all active refresh sessions."""
        jwt_payload = decode_jwt_token(
            payload.token,
            expected_type=TokenType.PASSWORD_RESET,
            settings=self._settings,
            invalid_status_code=status.HTTP_400_BAD_REQUEST,
        )

        user = await self._resolve_user_from_sub(
            jwt_payload["sub"],
            invalid_status_code=status.HTTP_400_BAD_REQUEST,
        )

        expected_fp = compute_password_fingerprint(user.password_hash)
        if jwt_payload.get("pwd_fp") != expected_fp:
            raise self._invalid_token_error(status_code=status.HTTP_400_BAD_REQUEST)

        await self._user_repo.update_password_hash(user, hash_password(payload.new_password))
        await self._refresh_repo.revoke_all_for_user(user.id, revoked_at=datetime.now(UTC))
        await self._session.commit()

        return MessageResponse(
            message="Sua senha foi redefinida com sucesso. Faça login com a nova senha."
        )

    async def _resolve_user_from_sub(
        self,
        raw_sub: object,
        *,
        invalid_status_code: int,
    ) -> User:
        """Parse UUID subject claim and return active User or raise INVALID_TOKEN."""
        try:
            user_id = UUID(str(raw_sub))
        except ValueError as exc:
            raise self._invalid_token_error(status_code=invalid_status_code) from exc

        user = await self._user_repo.get_by_id(user_id)
        if user is None or not user.is_active:
            raise self._invalid_token_error(status_code=invalid_status_code)
        return user

    @staticmethod
    def _invalid_credentials_error() -> AppError:
        return AppError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="INVALID_CREDENTIALS",
            message="E-mail ou senha incorretos.",
        )

    @staticmethod
    def _invalid_token_error(status_code: int = status.HTTP_401_UNAUTHORIZED) -> AppError:
        return AppError(
            status_code=status_code,
            code="INVALID_TOKEN",
            message="Token de autenticação inválido ou revogado.",
        )
