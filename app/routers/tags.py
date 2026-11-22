"""Tags discovery HTTP router (/api/v1/tags)."""

from fastapi import APIRouter, Depends, Query

from app.routers.games import get_clip_service
from app.schemas.game import TagListResponse
from app.services.clip_service import ClipService

router = APIRouter(prefix="/tags", tags=["Tags"])


@router.get(
    "",
    response_model=TagListResponse,
    summary="Lista as tags mais populares ou filtradas por prefixo",
)
async def list_tags(
    q: str | None = Query(default=None, max_length=30),
    limit: int = Query(default=20, ge=1, le=50),
    clip_service: ClipService = Depends(get_clip_service),
) -> TagListResponse:
    """List existing tags ordered by popularity."""
    return await clip_service.list_tags(q=q, limit=limit)
