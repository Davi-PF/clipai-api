"""Clips upload, discovery, and CRUD HTTP router (/api/v1/clips)."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.core.dependencies import get_current_user, get_optional_user, get_verified_user
from app.core.rate_limit import (
    check_clip_telemetry_rate_limit,
    check_clip_view_rate_limit,
    check_user_rate_limit,
)
from app.models.user import User
from app.schemas.clip import (
    ClipDetailResponse,
    ClipFavoriteResponse,
    ClipLikeResponse,
    ClipSummaryResponse,
    ClipUpdateRequest,
    ClipViewResponse,
    UploadUrlRequest,
    UploadUrlResponse,
)
from app.schemas.common import PaginatedResponse
from app.schemas.telemetry import ClipTelemetryRequest, ClipTelemetryResponse
from app.services.clip_service import ClipService
from app.services.storage_service import StorageService
from app.services.telemetry_service import TelemetryService

router = APIRouter(prefix="/clips", tags=["Clips"])


def get_storage_service(settings: Settings = Depends(get_settings)) -> StorageService:
    """Provide StorageService dependency."""
    return StorageService(settings=settings)


def get_clip_service(
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    storage_service: StorageService = Depends(get_storage_service),
) -> ClipService:
    """Provide ClipService dependency."""
    return ClipService(session=session, settings=settings, storage_service=storage_service)


def get_telemetry_service(
    session: AsyncSession = Depends(get_db),
) -> TelemetryService:
    """Provide TelemetryService dependency."""
    return TelemetryService(session=session)


@router.post(
    "/upload-url",
    response_model=UploadUrlResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Gera Presigned PUT URLs do Cloudflare R2 para upload direto",
)
async def request_upload_url(
    payload: UploadUrlRequest,
    current_user: User = Depends(get_verified_user),
    clip_service: ClipService = Depends(get_clip_service),
) -> UploadUrlResponse:
    """Validate clip metadata and issue presigned R2 upload URLs."""
    await check_user_rate_limit(
        scope="clips_upload_url",
        user_id=current_user.id,
        max_requests=10,
        window_seconds=3600,
    )
    return await clip_service.create_upload_url(payload, current_user)


@router.post(
    "/{clip_id}/confirm-upload",
    response_model=ClipDetailResponse,
    summary="Confirma upload direto no R2 via HeadObject e publica o clipe",
)
async def confirm_clip_upload(
    clip_id: UUID,
    current_user: User = Depends(get_verified_user),
    clip_service: ClipService = Depends(get_clip_service),
) -> ClipDetailResponse:
    """Confirm R2 object upload and transition clip status to ready."""
    return await clip_service.confirm_upload(clip_id, current_user)


@router.post(
    "/{clip_id}/confirm",
    response_model=ClipDetailResponse,
    include_in_schema=False,
)
async def confirm_clip_upload_alias(
    clip_id: UUID,
    current_user: User = Depends(get_verified_user),
    clip_service: ClipService = Depends(get_clip_service),
) -> ClipDetailResponse:
    """Alias route for POST /api/v1/clips/{clip_id}/confirm-upload."""
    return await clip_service.confirm_upload(clip_id, current_user)


@router.get(
    "",
    response_model=PaginatedResponse[ClipSummaryResponse],
    summary="Lista e busca clipes prontos respeitando privacidade",
)
async def list_clips(
    q: str | None = Query(default=None, max_length=120),
    game: str | None = Query(default=None, max_length=120),
    game_id: UUID | None = Query(default=None),
    game_slug: str | None = Query(default=None, max_length=120),
    tag: str | None = Query(default=None, max_length=30),
    author: str | None = Query(default=None, max_length=30),
    mine: bool = Query(default=False),
    favorited: bool = Query(default=False),
    collection_id: UUID | None = Query(default=None),
    sort: Literal["recent", "popular", "featured"] = Query(default="recent"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
    current_user: User | None = Depends(get_optional_user),
    clip_service: ClipService = Depends(get_clip_service),
) -> PaginatedResponse[ClipSummaryResponse]:
    """List ready clips with search, filtering, and pagination."""
    return await clip_service.list_clips(
        q=q,
        game=game,
        game_id=game_id,
        game_slug=game_slug,
        tag=tag,
        author=author,
        mine=mine,
        favorited=favorited,
        collection_id=collection_id,
        sort=sort,
        page=page,
        page_size=page_size,
        current_user=current_user,
    )


@router.get(
    "/{clip_ref}",
    response_model=ClipDetailResponse,
    summary="Retorna detalhes completos do clipe por slug ou UUID",
)
async def get_clip_detail(
    clip_ref: str,
    current_user: User | None = Depends(get_optional_user),
    clip_service: ClipService = Depends(get_clip_service),
) -> ClipDetailResponse:
    """Get clip details by URL slug or UUID."""
    return await clip_service.get_clip_detail(clip_ref, current_user)


@router.patch(
    "/{clip_id}",
    response_model=ClipDetailResponse,
    summary="Atualiza metadados, jogo, tags ou privacidade do clipe",
)
async def update_clip(
    clip_id: UUID,
    payload: ClipUpdateRequest,
    current_user: User = Depends(get_current_user),
    clip_service: ClipService = Depends(get_clip_service),
) -> ClipDetailResponse:
    """Edit clip metadata (owner only)."""
    return await clip_service.update_clip(clip_id, payload, current_user)


@router.delete(
    "/{clip_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Exclui o clipe do banco e remove arquivos do Cloudflare R2",
)
async def delete_clip(
    clip_id: UUID,
    current_user: User = Depends(get_current_user),
    clip_service: ClipService = Depends(get_clip_service),
) -> None:
    """Delete clip and associated R2 media objects (owner only)."""
    await clip_service.delete_clip(clip_id, current_user)


@router.post(
    "/{clip_id}/like",
    response_model=ClipLikeResponse,
    summary="Curte o clipe de forma idempotente",
)
async def like_clip(
    clip_id: UUID,
    current_user: User = Depends(get_verified_user),
    clip_service: ClipService = Depends(get_clip_service),
) -> ClipLikeResponse:
    """Like a ready clip."""
    return await clip_service.like_clip(clip_id, current_user)


@router.delete(
    "/{clip_id}/like",
    response_model=ClipLikeResponse,
    summary="Remove a curtida do clipe de forma idempotente",
)
async def unlike_clip(
    clip_id: UUID,
    current_user: User = Depends(get_current_user),
    clip_service: ClipService = Depends(get_clip_service),
) -> ClipLikeResponse:
    """Remove like from a ready clip."""
    return await clip_service.unlike_clip(clip_id, current_user)


@router.post(
    "/{clip_id}/favorite",
    response_model=ClipFavoriteResponse,
    summary="Favorita o clipe de forma idempotente",
)
async def favorite_clip(
    clip_id: UUID,
    current_user: User = Depends(get_verified_user),
    clip_service: ClipService = Depends(get_clip_service),
) -> ClipFavoriteResponse:
    """Favorite a ready clip."""
    return await clip_service.favorite_clip(clip_id, current_user)


@router.delete(
    "/{clip_id}/favorite",
    response_model=ClipFavoriteResponse,
    summary="Remove o clipe dos favoritos de forma idempotente",
)
async def unfavorite_clip(
    clip_id: UUID,
    current_user: User = Depends(get_current_user),
    clip_service: ClipService = Depends(get_clip_service),
) -> ClipFavoriteResponse:
    """Remove clip from user favorites."""
    return await clip_service.unfavorite_clip(clip_id, current_user)


@router.post(
    "/{clip_id}/views",
    response_model=ClipViewResponse,
    summary="Incrementa atomicamente o contador de visualizações do clipe",
)
async def register_clip_view(
    clip_id: UUID,
    request: Request,
    current_user: User | None = Depends(get_optional_user),
    clip_service: ClipService = Depends(get_clip_service),
) -> ClipViewResponse:
    """Increment clip view count with anti-F5 rate limit."""
    await check_clip_view_rate_limit(
        request=request,
        clip_id=clip_id,
        user_id=current_user.id if current_user else None,
    )
    return await clip_service.register_view(clip_id, current_user)


@router.post(
    "/{clip_id}/view",
    response_model=ClipViewResponse,
    include_in_schema=False,
)
async def register_clip_view_alias(
    clip_id: UUID,
    request: Request,
    current_user: User | None = Depends(get_optional_user),
    clip_service: ClipService = Depends(get_clip_service),
) -> ClipViewResponse:
    """Alias route for POST /api/v1/clips/{clip_id}/views."""
    await check_clip_view_rate_limit(
        request=request,
        clip_id=clip_id,
        user_id=current_user.id if current_user else None,
    )
    return await clip_service.register_view(clip_id, current_user)


@router.post(
    "/{clip_id}/telemetry",
    response_model=ClipTelemetryResponse,
    summary="Registra ou atualiza telemetria de retenção de visualização do clipe",
)
async def record_clip_telemetry(
    clip_id: UUID,
    payload: ClipTelemetryRequest,
    request: Request,
    current_user: User | None = Depends(get_optional_user),
    telemetry_service: TelemetryService = Depends(get_telemetry_service),
) -> ClipTelemetryResponse:
    """Idempotently upsert a viewer watch session with retention checkpoints."""
    await check_clip_telemetry_rate_limit(
        request=request,
        clip_id=clip_id,
        user_id=current_user.id if current_user else None,
    )
    return await telemetry_service.record_telemetry(
        clip_id,
        payload,
        current_user=current_user,
    )
