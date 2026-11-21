"""Healthcheck HTTP router."""

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.schemas.health import HealthResponse
from app.services.health_service import HealthService

router = APIRouter(tags=["Health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Verifica saúde da API e conectividade com o banco",
)
async def health_check(
    response: Response,
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HealthResponse:
    """Return application version and PostgreSQL connection status."""
    service = HealthService(session=session, settings=settings)
    health_payload, is_healthy = await service.get_health_status()
    if not is_healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return health_payload
