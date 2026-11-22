"""Unit tests for SQLAlchemy 2.0 UserRepository and RefreshTokenRepository."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import RefreshToken, User
from app.repositories.refresh_token_repository import RefreshTokenRepository
from app.repositories.user_repository import UserRepository


async def test_user_repository_queries_and_mutations() -> None:
    """Verify UserRepository executes queries and flushes without committing."""
    session = AsyncMock(spec=AsyncSession)
    session.add = MagicMock()
    repo = UserRepository(session)

    sample_user = User(
        id=uuid4(),
        username="fallen_br",
        email="fallen@clipai.com.br",
        password_hash="hashed",
    )
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = sample_user
    session.execute.return_value = result_mock

    assert await repo.get_by_id(sample_user.id) is sample_user
    assert await repo.get_by_email("FALLEN@clipai.com.br") is sample_user
    assert await repo.get_by_username("FALLEN_BR") is sample_user

    created = await repo.create(
        username="new_user",
        email="NEW@clipai.com.br",
        password_hash="hash123",
        display_name="New User",
    )
    assert created.email == "new@clipai.com.br"
    session.add.assert_called_once()

    await repo.mark_email_verified(created)
    assert created.is_email_verified is True

    await repo.update_password_hash(created, "new_hash")
    assert created.password_hash == "new_hash"
    assert created.hashed_password == "new_hash"
    assert created.has_password is True
    created.hashed_password = None
    assert created.has_password is False
    assert created.discord_linked is False

    assert await repo.get_by_discord_id("123456") is sample_user
    assert await repo.save(created) is created

    # Test get_public_stats
    clips_result = MagicMock()
    clips_result.one.return_value = (4, 150, 32)
    colls_result = MagicMock()
    colls_result.scalar_one.return_value = 2
    session.execute.side_effect = [clips_result, colls_result]
    stats = await repo.get_public_stats(sample_user.id)
    assert stats == {
        "clips_count": 4,
        "total_views": 150,
        "total_likes": 32,
        "collections_count": 2,
    }

    session.commit.assert_not_awaited()


async def test_refresh_token_repository_queries_and_revocations() -> None:
    """Verify RefreshTokenRepository creates, rotates, and revokes token records."""
    session = AsyncMock(spec=AsyncSession)
    session.add = MagicMock()
    repo = RefreshTokenRepository(session)

    now = datetime.now(UTC)
    token = RefreshToken(
        id=uuid4(),
        user_id=uuid4(),
        token_hash="abc" * 21 + "a",
        family_id=uuid4(),
        expires_at=now + timedelta(days=7),
    )
    assert token.is_revoked is False

    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = token
    session.execute.return_value = result_mock

    assert await repo.get_by_token_hash(token.token_hash) is token

    created = await repo.create(
        user_id=token.user_id,
        token_hash="def" * 21 + "d",
        family_id=token.family_id,
        expires_at=now + timedelta(days=7),
        user_agent="pytest",
        ip_address="127.0.0.1",
    )
    assert created.ip_address == "127.0.0.1"

    await repo.revoke_and_replace(token, revoked_at=now, replaced_by_id=uuid4())
    assert token.is_revoked is True
    assert token.replaced_by_id is not None

    await repo.revoke_by_token_hash("hash", revoked_at=now)
    await repo.revoke_family(token.family_id, revoked_at=now)
    await repo.revoke_all_for_user(token.user_id, revoked_at=now)

    session.commit.assert_not_awaited()
