"""Application healthcheck service."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.database import check_database_connection
from app.schemas.health import HealthResponse


class HealthService:
    """Evaluates API and PostgreSQL readiness."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    async def get_health_status(self) -> tuple[HealthResponse, bool]:
        """Return health payload and boolean indicating full readiness."""
        is_db_connected = await check_database_connection(self._session)
        response = HealthResponse(
            status="ok" if is_db_connected else "degraded",
            version=self._settings.app_version,
            database="connected" if is_db_connected else "disconnected",
        )
        return response, is_db_connected
