"""Users HTTP router (/api/v1/users)."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.core.dependencies import get_current_user, get_optional_user
from app.models.user import User
from app.routers.clips import get_clip_service, get_storage_service
from app.routers.collections import get_collection_service
from app.schemas.clip import ClipSummaryResponse
from app.schemas.collection import CollectionSummaryResponse
from app.schemas.common import PaginatedResponse
from app.schemas.user import (
    AvatarConfirmRequest,
    AvatarUploadUrlRequest,
    AvatarUploadUrlResponse,
    UserMeResponse,
    UserPublicResponse,
    UserUpdateRequest,
)
from app.services.clip_service import ClipService
from app.services.collection_service import CollectionService
from app.services.storage_service import StorageService
from app.services.user_service import UserService

router = APIRouter(prefix="/users", tags=["Users"])


def get_user_service(
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    storage_service: StorageService = Depends(get_storage_service),
) -> UserService:
    """Provide UserService dependency."""
    return UserService(
        session=session,
        settings=settings,
        storage_service=storage_service,
    )


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


@router.patch(
    "/me",
    response_model=UserMeResponse,
    summary="Atualiza nome de exibição, biografia e origem do avatar",
)
async def update_current_user_profile(
    payload: UserUpdateRequest,
    current_user: User = Depends(get_current_user),
    user_service: UserService = Depends(get_user_service),
) -> UserMeResponse:
    """Update authenticated user profile fields."""
    return await user_service.update_profile(current_user, payload)


@router.post(
    "/me/avatar/upload-url",
    response_model=AvatarUploadUrlResponse,
    summary="Gera Presigned PUT URL no Cloudflare R2 para envio de avatar personalizado",
)
async def create_avatar_upload_url(
    payload: AvatarUploadUrlRequest,
    current_user: User = Depends(get_current_user),
    user_service: UserService = Depends(get_user_service),
) -> AvatarUploadUrlResponse:
    """Validate avatar MIME/size and generate presigned R2 upload URL."""
    return await user_service.create_avatar_upload_url(current_user, payload)


@router.post(
    "/me/avatar/confirm",
    response_model=UserMeResponse,
    summary="Confirma o envio do avatar no R2, remove o anterior e ativa avatar customizado",
)
async def confirm_avatar_upload(
    payload: AvatarConfirmRequest,
    current_user: User = Depends(get_current_user),
    user_service: UserService = Depends(get_user_service),
) -> UserMeResponse:
    """Verify uploaded avatar in R2 via HeadObject and update user profile."""
    return await user_service.confirm_avatar_upload(current_user, payload)


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
    "/{username}",
    response_model=UserPublicResponse,
    summary="Retorna o perfil público e estatísticas básicas de um usuário",
)
async def get_public_user_profile(
    username: str,
    user_service: UserService = Depends(get_user_service),
) -> UserPublicResponse:
    """Return public user profile by username."""
    return await user_service.get_public_profile(username)


@router.get(
    "/{username}/clips",
    response_model=PaginatedResponse[ClipSummaryResponse],
    summary="Lista paginada dos clipes de um usuário",
)
async def list_user_clips(
    username: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
    current_user: User | None = Depends(get_optional_user),
    clip_service: ClipService = Depends(get_clip_service),
) -> PaginatedResponse[ClipSummaryResponse]:
    """List ready clips authored by the specified username respecting privacy."""
    is_self = current_user is not None and current_user.username.lower() == username.strip().lower()
    return await clip_service.list_clips(
        author=username,
        mine=is_self,
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
