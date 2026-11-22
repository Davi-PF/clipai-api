"""SQLAlchemy 2.0 async repository for RefreshToken rotation and revocation."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import RefreshToken


class RefreshTokenRepository:
    """Data access layer for the refresh_tokens table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        user_id: UUID,
        token_hash: str,
        family_id: UUID,
        expires_at: datetime,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> RefreshToken:
        """Insert a new hashed refresh token record."""
        record = RefreshToken(
            user_id=user_id,
            token_hash=token_hash,
            family_id=family_id,
            expires_at=expires_at,
            user_agent=user_agent[:512] if user_agent else None,
            ip_address=ip_address[:45] if ip_address else None,
        )
        self._session.add(record)
        await self._session.flush()
        return record

    async def get_by_token_hash(self, token_hash: str) -> RefreshToken | None:
        """Fetch a refresh token record by its SHA-256 hash."""
        stmt = select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def revoke_and_replace(
        self,
        token: RefreshToken,
        *,
        revoked_at: datetime,
        replaced_by_id: UUID,
    ) -> None:
        """Mark an existing token as rotated and link to its replacement."""
        token.revoked_at = revoked_at
        token.replaced_by_id = replaced_by_id
        await self._session.flush()

    async def revoke_by_token_hash(self, token_hash: str, *, revoked_at: datetime) -> None:
        """Revoke an active refresh token matching the given SHA-256 hash."""
        stmt = (
            update(RefreshToken)
            .where(RefreshToken.token_hash == token_hash, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=revoked_at)
        )
        await self._session.execute(stmt)
        await self._session.flush()

    async def revoke_family(self, family_id: UUID, *, revoked_at: datetime) -> None:
        """Revoke all active tokens belonging to a compromised rotation family."""
        stmt = (
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=revoked_at)
        )
        await self._session.execute(stmt)
        await self._session.flush()

    async def revoke_all_for_user(self, user_id: UUID, *, revoked_at: datetime) -> None:
        """Revoke all active refresh tokens across every session of a user."""
        stmt = (
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=revoked_at)
        )
        await self._session.execute(stmt)
        await self._session.flush()
