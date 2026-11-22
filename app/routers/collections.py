"""Collections HTTP router (/api/v1/collections)."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user, get_optional_user, get_verified_user
from app.models.user import User
from app.routers.clips import get_storage_service
from app.schemas.collection import (
    CollectionAddClipRequest,
    CollectionAddClipResponse,
    CollectionCreateRequest,
    CollectionDetailResponse,
    CollectionSummaryResponse,
    CollectionUpdateRequest,
)
from app.schemas.common import PaginatedResponse
from app.services.collection_service import CollectionService
from app.services.storage_service import StorageService

router = APIRouter(prefix="/collections", tags=["Collections"])


def get_collection_service(
    session: AsyncSession = Depends(get_db),
    storage_service: StorageService = Depends(get_storage_service),
) -> CollectionService:
    """Provide CollectionService dependency."""
    return CollectionService(session=session, storage_service=storage_service)


@router.get(
    "",
    response_model=PaginatedResponse[CollectionSummaryResponse],
    summary="Lista coleções acessíveis com contagem de clipes e preview de capas",
)
async def list_collections(
    mine: bool = Query(default=False),
    author: str | None = Query(default=None, max_length=30),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
    current_user: User | None = Depends(get_optional_user),
    collection_service: CollectionService = Depends(get_collection_service),
) -> PaginatedResponse[CollectionSummaryResponse]:
    """List collections respecting privacy visibility."""
    return await collection_service.list_collections(
        mine=mine,
        author=author,
        page=page,
        page_size=page_size,
        current_user=current_user,
    )


@router.post(
    "",
    response_model=CollectionSummaryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Cria uma nova coleção de clipes",
)
async def create_collection(
    payload: CollectionCreateRequest,
    current_user: User = Depends(get_verified_user),
    collection_service: CollectionService = Depends(get_collection_service),
) -> CollectionSummaryResponse:
    """Create a new user collection."""
    return await collection_service.create_collection(payload, current_user)


@router.get(
    "/{collection_id}",
    response_model=CollectionDetailResponse,
    summary="Retorna detalhes da coleção e sua lista paginada de clipes prontos",
)
async def get_collection_detail(
    collection_id: UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
    current_user: User | None = Depends(get_optional_user),
    collection_service: CollectionService = Depends(get_collection_service),
) -> CollectionDetailResponse:
    """Get collection details and ordered clips."""
    return await collection_service.get_collection_detail(
        collection_id,
        page=page,
        page_size=page_size,
        current_user=current_user,
    )


@router.patch(
    "/{collection_id}",
    response_model=CollectionSummaryResponse,
    summary="Atualiza título, descrição, cor de destaque ou privacidade da coleção",
)
async def update_collection(
    collection_id: UUID,
    payload: CollectionUpdateRequest,
    current_user: User = Depends(get_current_user),
    collection_service: CollectionService = Depends(get_collection_service),
) -> CollectionSummaryResponse:
    """Edit collection metadata (owner only)."""
    return await collection_service.update_collection(collection_id, payload, current_user)


@router.delete(
    "/{collection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Exclui a coleção preservando os clipes originais",
)
async def delete_collection(
    collection_id: UUID,
    current_user: User = Depends(get_current_user),
    collection_service: CollectionService = Depends(get_collection_service),
) -> None:
    """Delete collection (owner only)."""
    await collection_service.delete_collection(collection_id, current_user)


@router.post(
    "/{collection_id}/clips",
    response_model=CollectionAddClipResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Adiciona um clipe pronto à coleção de forma idempotente",
)
async def add_clip_to_collection(
    collection_id: UUID,
    payload: CollectionAddClipRequest,
    current_user: User = Depends(get_current_user),
    collection_service: CollectionService = Depends(get_collection_service),
) -> CollectionAddClipResponse:
    """Add clip to collection (owner only)."""
    return await collection_service.add_clip_to_collection(collection_id, payload, current_user)


@router.delete(
    "/{collection_id}/clips/{clip_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove um clipe da coleção",
)
async def remove_clip_from_collection(
    collection_id: UUID,
    clip_id: UUID,
    current_user: User = Depends(get_current_user),
    collection_service: CollectionService = Depends(get_collection_service),
) -> None:
    """Remove clip from collection (owner only)."""
    await collection_service.remove_clip_from_collection(collection_id, clip_id, current_user)
