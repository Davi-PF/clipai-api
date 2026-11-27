"""Creator Studio analytics HTTP router (/api/v1/analytics)."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.routers.clips import get_storage_service
from app.schemas.analytics import (
    AnalyticsOverviewResponse,
    AnalyticsPeriod,
    ClipRetentionResponse,
    CreatorClipAnalyticsItem,
    CreatorClipSort,
)
from app.schemas.clip import ClipPrivacy
from app.schemas.common import PaginatedResponse
from app.services.analytics_service import AnalyticsService
from app.services.storage_service import StorageService

router = APIRouter(prefix="/analytics", tags=["Analytics"])


def get_analytics_service(
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    storage_service: StorageService = Depends(get_storage_service),
) -> AnalyticsService:
    """Provide AnalyticsService dependency."""
    return AnalyticsService(
        session=session,
        storage_service=storage_service,
        cache_ttl_seconds=settings.analytics_cache_ttl_seconds,
    )


@router.get(
    "/overview",
    response_model=AnalyticsOverviewResponse,
    summary="Retorna KPIs consolidados, série diária, resumo por jogo e top 5 clipes do criador",
)
async def get_analytics_overview(
    period: AnalyticsPeriod = Query(default="30d"),
    current_user: User = Depends(get_current_user),
    analytics_service: AnalyticsService = Depends(get_analytics_service),
) -> AnalyticsOverviewResponse:
    """Return creator overview KPIs and charts for the requested period."""
    return await analytics_service.get_overview(current_user, period=period)


@router.get(
    "/clips",
    response_model=PaginatedResponse[CreatorClipAnalyticsItem],
    summary="Lista paginada dos clipes do criador com métricas de engajamento e retenção",
)
async def list_creator_analytics_clips(
    q: str | None = Query(default=None, max_length=120),
    privacy: ClipPrivacy | None = Query(default=None),
    game_id: UUID | None = Query(default=None),
    period: AnalyticsPeriod = Query(default="30d"),
    sort: CreatorClipSort = Query(default="recent"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    analytics_service: AnalyticsService = Depends(get_analytics_service),
) -> PaginatedResponse[CreatorClipAnalyticsItem]:
    """List all ready clips owned by the creator with filters, sorting, and retention metrics."""
    return await analytics_service.list_creator_clips(
        current_user,
        q=q,
        privacy=privacy,
        game_id=game_id,
        period=period,
        sort=sort,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/clips/{clip_id}/retention",
    response_model=ClipRetentionResponse,
    summary="Retorna a curva de retenção (0 a 100%) e ponto de maior queda de um clipe do criador",
)
async def get_clip_retention_analytics(
    clip_id: UUID,
    period: AnalyticsPeriod = Query(default="30d"),
    current_user: User = Depends(get_current_user),
    analytics_service: AnalyticsService = Depends(get_analytics_service),
) -> ClipRetentionResponse:
    """Return detailed 11-bucket retention curve and daily watch trend for an owned clip."""
    return await analytics_service.get_clip_retention(
        clip_id,
        current_user,
        period=period,
    )
