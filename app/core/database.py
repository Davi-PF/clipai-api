"""Async SQLAlchemy 2.0 engine, session factory, and connectivity helpers."""

import logging
from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def build_async_engine(settings: Settings) -> AsyncEngine:
    """Create a configured SQLAlchemy 2.0 AsyncEngine."""
    connect_args: dict[str, int] = {}
    if settings.db_statement_cache_size is not None:
        connect_args["statement_cache_size"] = settings.db_statement_cache_size

    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        echo=settings.db_echo_sql,
        connect_args=connect_args,
    )


def get_engine() -> AsyncEngine:
    """Return lazily initialized singleton AsyncEngine."""
    global _engine
    if _engine is None:
        _engine = build_async_engine(get_settings())
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return lazily initialized singleton async_sessionmaker."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield an AsyncSession per request with rollback on unhandled errors."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def check_database_connection(session: AsyncSession) -> bool:
    """Execute a lightweight query to verify PostgreSQL reachability."""
    try:
        result = await session.execute(text("SELECT 1"))
        return result.scalar_one() == 1
    except (SQLAlchemyError, OSError) as exc:
        logger.warning("Database connectivity check failed: %s", exc)
        return False


async def dispose_engine() -> None:
    """Dispose the global AsyncEngine and reset cached factories on shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
