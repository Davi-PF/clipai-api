"""Unit tests for async database engine, session dependency, and health probe."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database
from app.core.config import Settings
from app.models import Base, TimestampMixin
from app.models.base import NAMING_CONVENTION


async def test_check_database_connection_returns_true_on_success() -> None:
    """Return True when SELECT 1 succeeds."""
    session = AsyncMock(spec=AsyncSession)
    result_mock = MagicMock()
    result_mock.scalar_one.return_value = 1
    session.execute.return_value = result_mock

    assert await database.check_database_connection(session) is True
    session.execute.assert_awaited_once()


async def test_check_database_connection_returns_false_on_sqlalchemy_error() -> None:
    """Return False when SQLAlchemy raises an OperationalError."""
    session = AsyncMock(spec=AsyncSession)
    session.execute.side_effect = OperationalError("SELECT 1", {}, Exception("connection lost"))

    assert await database.check_database_connection(session) is False


async def test_check_database_connection_returns_false_on_os_error() -> None:
    """Return False when a network/socket OSError occurs."""
    session = AsyncMock(spec=AsyncSession)
    session.execute.side_effect = OSError("network unreachable")

    assert await database.check_database_connection(session) is False


def test_build_async_engine_applies_statement_cache_size(test_settings: Settings) -> None:
    """Pass statement_cache_size to connect_args when configured for PgBouncer."""
    custom_settings = test_settings.model_copy(update={"db_statement_cache_size": 0})
    with patch("app.core.database.create_async_engine") as mock_create:
        database.build_async_engine(custom_settings)

    mock_create.assert_called_once()
    _, kwargs = mock_create.call_args
    assert kwargs["pool_pre_ping"] is True
    assert kwargs["connect_args"] == {"statement_cache_size": 0}


async def test_get_db_yields_session_and_rolls_back_on_error() -> None:
    """Verify get_db yields an AsyncSession and rolls back when an error bubbles up."""
    mock_session = AsyncMock(spec=AsyncSession)
    mock_context = AsyncMock()
    mock_context.__aenter__.return_value = mock_session
    mock_context.__aexit__.return_value = None
    mock_factory = MagicMock(return_value=mock_context)

    with patch("app.core.database.get_session_factory", return_value=mock_factory):
        generator = database.get_db()
        yielded = await anext(generator)
        assert yielded is mock_session

        with pytest.raises(RuntimeError, match="boom"):
            await generator.athrow(RuntimeError("boom"))

    mock_session.rollback.assert_awaited_once()


async def test_get_engine_session_factory_and_dispose() -> None:
    """Verify lazy engine creation, session factory reuse, and clean disposal."""
    await database.dispose_engine()
    mock_engine = AsyncMock()

    with patch("app.core.database.build_async_engine", return_value=mock_engine) as mock_build:
        engine_first = database.get_engine()
        engine_second = database.get_engine()
        factory_first = database.get_session_factory()
        factory_second = database.get_session_factory()

        assert engine_first is mock_engine
        assert engine_second is mock_engine
        assert factory_first is factory_second
        mock_build.assert_called_once()

        await database.dispose_engine()

    mock_engine.dispose.assert_awaited_once()


def test_build_async_engine_without_statement_cache_size(test_settings: Settings) -> None:
    """Leave connect_args empty when statement_cache_size is not set."""
    custom_settings = test_settings.model_copy(update={"db_statement_cache_size": None})
    with patch("app.core.database.create_async_engine") as mock_create:
        database.build_async_engine(custom_settings)

    _, kwargs = mock_create.call_args
    assert kwargs["connect_args"] == {}


def test_declarative_base_uses_deterministic_naming_convention() -> None:
    """Ensure Alembic-compatible naming conventions and TimestampMixin are configured."""
    assert Base.metadata.naming_convention == NAMING_CONVENTION
    assert hasattr(TimestampMixin, "created_at")
    assert hasattr(TimestampMixin, "updated_at")
