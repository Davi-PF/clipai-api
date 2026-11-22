"""Authentication HTTP router (/api/v1/auth)."""

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import AppError
from app.core.rate_limit import rate_limit
from app.models.user import User
from app.schemas.auth import (
    ForgotPasswordRequest,
    LoginRequest,
    LoginResponse,
    MessageResponse,
    RefreshResponse,
    RegisterRequest,
    RegisterResponse,
    ResendVerificationRequest,
    ResetPasswordRequest,
    TwoFactorDisableRequest,
    TwoFactorEnableRequest,
    TwoFactorEnableResponse,
    TwoFactorSetupResponse,
    TwoFactorVerifyLoginRequest,
    VerifyEmailRequest,
)
from app.schemas.user import UserMeResponse
from app.services.auth_service import AuthService
from app.services.email_service import EmailService

router = APIRouter(prefix="/auth", tags=["Authentication"])


def get_email_service(settings: Settings = Depends(get_settings)) -> EmailService:
    """Provide EmailService dependency."""
    return EmailService(settings=settings)


def get_auth_service(
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    email_service: EmailService = Depends(get_email_service),
) -> AuthService:
    """Provide AuthService dependency."""
    return AuthService(session=session, settings=settings, email_service=email_service)


def _refresh_cookie_path(settings: Settings) -> str:
    """Build scoped cookie path for refresh token rotation and logout."""
    return f"{settings.api_v1_prefix}/auth"


def _set_refresh_cookie(response: Response, raw_token: str, settings: Settings) -> None:
    """Attach HttpOnly refresh token cookie to response."""
    response.set_cookie(
        key=settings.refresh_cookie_name,
        value=raw_token,
        max_age=settings.refresh_token_expire_days * 86400,
        path=_refresh_cookie_path(settings),
        domain=settings.refresh_cookie_domain or None,
        secure=settings.cookie_secure,
        httponly=True,
        samesite=settings.refresh_cookie_samesite,
    )


def _clear_refresh_cookie(response: Response, settings: Settings) -> None:
    """Clear HttpOnly refresh token cookie on logout or token revocation."""
    response.delete_cookie(
        key=settings.refresh_cookie_name,
        path=_refresh_cookie_path(settings),
        domain=settings.refresh_cookie_domain or None,
        secure=settings.cookie_secure,
        httponly=True,
        samesite=settings.refresh_cookie_samesite,
    )


def _extract_request_metadata(request: Request) -> tuple[str | None, str | None]:
    """Extract User-Agent and client IP for refresh token audit fields."""
    user_agent = request.headers.get("User-Agent")
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        ip_address = forwarded_for.split(",")[0].strip() or None
    elif request.client:
        ip_address = request.client.host
    else:
        ip_address = None
    return user_agent, ip_address


@router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit(scope="auth_register", max_requests=5, window_seconds=60))],
    summary="Registra um novo usuário e envia e-mail de confirmação",
)
async def register(
    payload: RegisterRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> RegisterResponse:
    """Register a new user account."""
    return await auth_service.register(payload)


@router.post(
    "/verify-email",
    response_model=MessageResponse,
    summary="Confirma o endereço de e-mail via token",
)
async def verify_email(
    payload: VerifyEmailRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> MessageResponse:
    """Confirm user e-mail address."""
    return await auth_service.verify_email(payload.token)


@router.post(
    "/resend-verification",
    response_model=MessageResponse,
    dependencies=[
        Depends(
            rate_limit(
                scope="auth_resend_verification",
                max_requests=3,
                window_seconds=900,
                include_email=True,
            )
        )
    ],
    summary="Reenvia o e-mail de confirmação",
)
async def resend_verification(
    payload: ResendVerificationRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> MessageResponse:
    """Resend e-mail verification link silently."""
    return await auth_service.resend_verification(payload.email)


@router.post(
    "/login",
    response_model=LoginResponse,
    dependencies=[
        Depends(
            rate_limit(
                scope="auth_login",
                max_requests=10,
                window_seconds=60,
                include_email=True,
            )
        )
    ],
    summary="Autentica usuário e define cookie HttpOnly de refresh token (ou desafio 2FA)",
)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    auth_service: AuthService = Depends(get_auth_service),
    settings: Settings = Depends(get_settings),
) -> LoginResponse:
    """Authenticate user credentials and issue tokens or 2FA challenge."""
    user_agent, ip_address = _extract_request_metadata(request)
    login_response, raw_refresh_token = await auth_service.login(
        payload,
        user_agent=user_agent,
        ip_address=ip_address,
    )
    if raw_refresh_token is not None:
        _set_refresh_cookie(response, raw_refresh_token, settings)
    return login_response


@router.post(
    "/2fa/setup",
    response_model=TwoFactorSetupResponse,
    summary="Gera segredo Base32 criptografado com Fernet e URI otpauth para 2FA TOTP",
)
async def setup_two_factor(
    current_user: User = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
) -> TwoFactorSetupResponse:
    """Initialize 2FA TOTP configuration for the authenticated user."""
    return await auth_service.setup_2fa(current_user)


@router.post(
    "/2fa/enable",
    response_model=TwoFactorEnableResponse,
    summary="Valida código TOTP de 6 dígitos, ativa o 2FA e gera 8 códigos de backup",
)
async def enable_two_factor(
    payload: TwoFactorEnableRequest,
    current_user: User = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
) -> TwoFactorEnableResponse:
    """Confirm TOTP setup and activate 2FA with one-time backup codes."""
    return await auth_service.enable_2fa(current_user, payload)


@router.post(
    "/2fa/disable",
    response_model=MessageResponse,
    summary="Valida senha atual e código TOTP/backup para desativar o 2FA",
)
async def disable_two_factor(
    payload: TwoFactorDisableRequest,
    current_user: User = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
) -> MessageResponse:
    """Disable 2FA and purge stored TOTP secret and backup codes."""
    return await auth_service.disable_2fa(current_user, payload)


@router.post(
    "/2fa/verify-login",
    response_model=LoginResponse,
    dependencies=[
        Depends(
            rate_limit(
                scope="auth_2fa_verify",
                max_requests=5,
                window_seconds=60,
            )
        )
    ],
    summary="Valida desafio 2FA (TOTP ou código de backup) e emite sessão autenticada",
)
@router.post(
    "/2fa/verify",
    response_model=LoginResponse,
    include_in_schema=False,
    dependencies=[
        Depends(
            rate_limit(
                scope="auth_2fa_verify",
                max_requests=5,
                window_seconds=60,
            )
        )
    ],
)
async def verify_two_factor_login(
    payload: TwoFactorVerifyLoginRequest,
    request: Request,
    response: Response,
    auth_service: AuthService = Depends(get_auth_service),
    settings: Settings = Depends(get_settings),
) -> LoginResponse:
    """Complete 2FA login challenge and issue access token + refresh cookie."""
    user_agent, ip_address = _extract_request_metadata(request)
    login_response, raw_refresh_token = await auth_service.verify_2fa_login(
        payload,
        user_agent=user_agent,
        ip_address=ip_address,
    )
    _set_refresh_cookie(response, raw_refresh_token, settings)
    return login_response


@router.post(
    "/refresh",
    response_model=RefreshResponse,
    summary="Rotaciona o refresh token e emite novo access token",
)
async def refresh(
    request: Request,
    response: Response,
    auth_service: AuthService = Depends(get_auth_service),
    settings: Settings = Depends(get_settings),
) -> RefreshResponse:
    """Rotate HttpOnly refresh token and issue a fresh access JWT."""
    raw_token = request.cookies.get(settings.refresh_cookie_name)
    user_agent, ip_address = _extract_request_metadata(request)
    try:
        refresh_response, new_raw_token = await auth_service.refresh(
            raw_token,
            user_agent=user_agent,
            ip_address=ip_address,
        )
    except AppError:
        _clear_refresh_cookie(response, settings)
        raise

    _set_refresh_cookie(response, new_raw_token, settings)
    return refresh_response


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoga a sessão atual e remove o cookie de refresh token",
)
async def logout(
    request: Request,
    response: Response,
    auth_service: AuthService = Depends(get_auth_service),
    settings: Settings = Depends(get_settings),
) -> None:
    """Revoke refresh token in DB and clear cookie."""
    raw_token = request.cookies.get(settings.refresh_cookie_name)
    await auth_service.logout(raw_token)
    _clear_refresh_cookie(response, settings)


@router.post(
    "/forgot-password",
    response_model=MessageResponse,
    dependencies=[
        Depends(
            rate_limit(
                scope="auth_forgot_password",
                max_requests=3,
                window_seconds=900,
                include_email=True,
            )
        )
    ],
    summary="Envia e-mail de recuperação de senha",
)
async def forgot_password(
    payload: ForgotPasswordRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> MessageResponse:
    """Initiate password recovery flow."""
    return await auth_service.forgot_password(payload.email)


@router.post(
    "/reset-password",
    response_model=MessageResponse,
    summary="Redefine a senha e revoga sessões ativas",
)
async def reset_password(
    payload: ResetPasswordRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> MessageResponse:
    """Complete password reset flow."""
    return await auth_service.reset_password(payload)


@router.get(
    "/me",
    response_model=UserMeResponse,
    summary="Retorna os dados do usuário autenticado",
)
async def get_authenticated_user(
    current_user: User = Depends(get_current_user),
) -> UserMeResponse:
    """Return authenticated user profile."""
    return UserMeResponse.model_validate(current_user)
