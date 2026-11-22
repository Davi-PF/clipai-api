"""FastAPI authentication and authorization dependencies."""

from uuid import UUID

from fastapi import Depends, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.core.exceptions import AppError
from app.core.security import TokenType, decode_jwt_token
from app.models.user import User
from app.repositories.user_repository import UserRepository

_bearer_scheme = HTTPBearer(auto_error=False)


def _invalid_token_error() -> AppError:
    """Create standardized 401 error for invalid or revoked access tokens."""
    return AppError(
        status_code=status.HTTP_401_UNAUTHORIZED,
        code="INVALID_TOKEN",
        message="Token de autenticação inválido ou revogado.",
    )


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> User:
    """Resolve and validate the authenticated user from a Bearer access JWT."""
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise _invalid_token_error()

    payload = decode_jwt_token(
        credentials.credentials,
        expected_type=TokenType.ACCESS,
        settings=settings,
    )

    try:
        user_id = UUID(str(payload["sub"]))
    except ValueError as exc:
        raise _invalid_token_error() from exc

    user = await UserRepository(session).get_by_id(user_id)
    if user is None or not user.is_active:
        raise _invalid_token_error()

    return user


async def get_verified_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """Ensure the authenticated user has verified their e-mail address."""
    if not current_user.is_email_verified:
        raise AppError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="EMAIL_NOT_VERIFIED",
            message="Confirme seu endereço de e-mail antes de realizar esta ação.",
        )
    return current_user
