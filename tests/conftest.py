"""Shared pytest fixtures and test environment bootstrap."""

import os
import secrets
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession


def _bootstrap_test_environment() -> None:
    """Populate non-secret runtime defaults before application modules load."""
    defaults = {
        "APP_ENV": "development",
        "DATABASE_URL": "postgresql+asyncpg://localhost:5432/clipai_test",
        "JWT_SECRET_KEY": secrets.token_hex(32),
        "R2_ACCOUNT_ID": "test_account",
        "R2_ACCESS_KEY_ID": secrets.token_hex(16),
        "R2_SECRET_ACCESS_KEY": secrets.token_hex(32),
        "R2_BUCKET_NAME": "clipai-test",
        "R2_PUBLIC_BASE_URL": "https://cdn.clipai.test",
        "RESEND_API_KEY": f"re_{secrets.token_hex(16)}",
        "CORS_ALLOWED_ORIGINS": "http://localhost:3000",
    }
    for key, value in defaults.items():
        os.environ.setdefault(key, value)


_bootstrap_test_environment()

from app.core.config import Settings, get_settings  # noqa: E402
from app.core.database import get_db  # noqa: E402
from app.core.rate_limit import rate_limiter  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture(autouse=True)
def reset_rate_limiter() -> None:
    """Clear in-memory rate-limit buckets before each test."""
    rate_limiter.reset()


@pytest.fixture
def test_settings() -> Settings:
    """Return fresh Settings instance for isolated tests."""
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def healthy_db_session() -> AsyncMock:
    """Return mocked AsyncSession simulating a healthy PostgreSQL connection."""
    session = AsyncMock(spec=AsyncSession)
    result_mock = MagicMock()
    result_mock.scalar_one.return_value = 1
    session.execute.return_value = result_mock
    return session


@pytest.fixture
async def async_client(
    test_settings: Settings,
    healthy_db_session: AsyncMock,
) -> AsyncGenerator[AsyncClient, None]:
    """Yield httpx AsyncClient backed by ASGITransport with mocked DB session."""
    application = create_app(settings=test_settings)

    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield healthy_db_session

    application.dependency_overrides[get_db] = _override_get_db

    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client

    application.dependency_overrides.clear()
