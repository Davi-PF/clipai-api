"""Users HTTP router (/api/v1/users)."""

from fastapi import APIRouter, Depends, Query

from app.core.dependencies import get_current_user, get_optional_user
from app.models.user import User
from app.routers.clips import get_clip_service
from app.routers.collections import get_collection_service
from app.schemas.clip import ClipSummaryResponse
from app.schemas.collection import CollectionSummaryResponse
from app.schemas.common import PaginatedResponse
from app.schemas.user import UserMeResponse
from app.services.clip_service import ClipService
from app.services.collection_service import CollectionService

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


@router.get(
    "/me/favorites",
    response_model=PaginatedResponse[ClipSummaryResponse],
    summary="Lista paginada dos clipes favoritados pelo usuário autenticado",
)
async def list_my_favorites(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    clip_service: ClipService = Depends(get_clip_service),
) -> PaginatedResponse[ClipSummaryResponse]:
    """List favorited ready clips for the authenticated user."""
    return await clip_service.list_clips(
        favorited=True,
        page=page,
        page_size=page_size,
        current_user=current_user,
    )


@router.get(
    "/{username}/collections",
    response_model=PaginatedResponse[CollectionSummaryResponse],
    summary="Lista paginada das coleções de um usuário",
)
async def list_user_collections(
    username: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
    current_user: User | None = Depends(get_optional_user),
    collection_service: CollectionService = Depends(get_collection_service),
) -> PaginatedResponse[CollectionSummaryResponse]:
    """List collections owned by the specified username respecting privacy."""
    return await collection_service.list_collections(
        author=username,
        page=page,
        page_size=page_size,
        current_user=current_user,
    )
