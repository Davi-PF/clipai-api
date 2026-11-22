"""Authentication business logic service."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.security import (
    TokenType,
    compute_password_fingerprint,
    create_access_token,
    create_email_verification_token,
    create_password_reset_token,
    decode_jwt_token,
    generate_refresh_token,
    hash_password,
    hash_token,
    needs_password_rehash,
    verify_dummy_password,
    verify_password,
)
from app.models.user import User
from app.repositories.refresh_token_repository import RefreshTokenRepository
from app.repositories.user_repository import UserRepository
from app.schemas.auth import (
    LoginRequest,
    LoginResponse,
    MessageResponse,
    RefreshResponse,
    RegisterRequest,
    RegisterResponse,
    ResetPasswordRequest,
)
from app.schemas.user import UserAuthSummary, UserRegisteredResponse
from app.services.email_service import EmailService


class AuthService:
    """Orchestrates registration, verification, login, token rotation, and password resets."""

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        email_service: EmailService,
    ) -> None:
        self._session = session
        self._settings = settings
        self._email_service = email_service
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
    ) -> tuple[LoginResponse, str]:
        """Authenticate credentials and issue access JWT + rotating refresh token."""
        user = await self._user_repo.get_by_email(payload.email)
        if user is None:
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
            temp_2fa_token=None,
            user=UserAuthSummary.model_validate(user),
        )
        return response, raw_refresh_token

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
