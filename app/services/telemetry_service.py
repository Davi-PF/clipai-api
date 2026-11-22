"""Business logic service for clip watch session telemetry collection."""

from uuid import UUID

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError
from app.models.user import User
from app.repositories.clip_repository import ClipRepository
from app.repositories.telemetry_repository import TelemetryRepository
from app.schemas.telemetry import ClipTelemetryRequest, ClipTelemetryResponse


class TelemetryService:
    """Validates clip access and persists idempotent video retention telemetry."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._clip_repo = ClipRepository(session)
        self._telemetry_repo = TelemetryRepository(session)

    async def record_telemetry(
        self,
        clip_id: UUID,
        payload: ClipTelemetryRequest,
        *,
        current_user: User | None = None,
    ) -> ClipTelemetryResponse:
        """Validate ready clip visibility, cap watch time safely, and upsert watch session."""
        clip = await self._clip_repo.get_by_id(clip_id)
        if clip is None or clip.status != "ready":
            raise AppError(
                status_code=status.HTTP_404_NOT_FOUND,
                code="RESOURCE_NOT_FOUND",
                message="O clipe solicitado não foi encontrado.",
            )
        if clip.privacy == "friends" and current_user is None:
            raise AppError(
                status_code=status.HTTP_401_UNAUTHORIZED,
                code="INVALID_TOKEN",
                message="Faça login para enviar telemetria de clipes restritos a amigos.",
            )

        max_allowed_watch_seconds = float(clip.duration_seconds) * 3.0
        clamped_watched_seconds = round(
            min(float(payload.watched_seconds), max_allowed_watch_seconds),
            2,
        )

        record = await self._telemetry_repo.upsert_watch_session(
            clip_id=clip.id,
            viewer_id=current_user.id if current_user else None,
            session_token=payload.session_token,
            watched_seconds=clamped_watched_seconds,
            max_progress_pct=payload.max_progress_pct,
            reached_buckets=payload.reached_buckets,
        )
        await self._session.commit()

        return ClipTelemetryResponse(
            session_id=record.id,
            clip_id=record.clip_id,
            session_token=record.session_token,
            watched_seconds=float(record.watched_seconds),
            max_progress_pct=int(record.max_progress_pct),
            reached_buckets=list(record.reached_buckets or []),
            completed=bool(record.completed),
        )
