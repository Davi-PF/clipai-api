"""Games catalog HTTP router (/api/v1/games)."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.schemas.common import PaginatedResponse
from app.schemas.game import GameResponse
from app.services.clip_service import ClipService
from app.services.storage_service import StorageService

router = APIRouter(prefix="/games", tags=["Games"])


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


@router.get(
    "",
    response_model=PaginatedResponse[GameResponse],
    summary="Lista jogos disponíveis com busca opcional",
)
async def list_games(
    q: str | None = Query(default=None, max_length=120),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
    clip_service: ClipService = Depends(get_clip_service),
) -> PaginatedResponse[GameResponse]:
    """List supported games paginated."""
    return await clip_service.list_games(q=q, page=page, page_size=page_size)
