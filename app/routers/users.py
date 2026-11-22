"""Users HTTP router (/api/v1/users)."""

from fastapi import APIRouter, Depends

from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.user import UserMeResponse

router = APIRouter(prefix="/users", tags=["Users"])


@router.get(
    "/me",
    response_model=UserMeResponse,
    summary="Retorna o perfil completo do usuário autenticado",
)
async def get_current_user_profile(
    current_user: User = Depends(get_current_user),
) -> UserMeResponse:
    """Return authenticated user profile per docs/API_CONTRACT.md."""
    return UserMeResponse.model_validate(current_user)
