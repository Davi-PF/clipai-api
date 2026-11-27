"""Invite link HTTP router (/api/v1/invites)."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.invite import (
    InviteAcceptResponse,
    InvitePreviewResponse,
    MyInviteResponse,
)
from app.services.invite_service import InviteService

router = APIRouter(prefix="/invites", tags=["Invites"])


def get_invite_service(
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> InviteService:
    """Provide InviteService dependency."""
    return InviteService(session=session, settings=settings)


@router.get(
    "/me",
    response_model=MyInviteResponse,
    summary="Retorna o código e a URL completa de convite do usuário autenticado",
)
async def get_my_invite(
    current_user: User = Depends(get_current_user),
    invite_service: InviteService = Depends(get_invite_service),
) -> MyInviteResponse:
    """Return authenticated user's personal invite code and share URL."""
    return invite_service.get_my_invite(current_user)


@router.get(
    "/{code}",
    response_model=InvitePreviewResponse,
    summary="Retorna preview público seguro de quem está convidando",
)
async def get_invite_preview(
    code: str,
    invite_service: InviteService = Depends(get_invite_service),
) -> InvitePreviewResponse:
    """Return safe public preview of an invite link owner."""
    return await invite_service.get_invite_preview(code)


@router.post(
    "/{code}/accept",
    response_model=InviteAcceptResponse,
    summary="Aceita link de convite e cria amizade aceita automaticamente",
)
async def accept_invite(
    code: str,
    current_user: User = Depends(get_current_user),
    invite_service: InviteService = Depends(get_invite_service),
) -> InviteAcceptResponse:
    """Accept an invite link and establish an accepted friendship idempotently."""
    return await invite_service.accept_invite(code, current_user)
