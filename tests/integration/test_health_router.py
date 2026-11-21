"""Integration tests for GET /api/v1/health, CORS, and error contract."""

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

from fastapi import status
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.database import get_db
from app.core.exceptions import AppError
from app.main import create_app, lifespan


async def test_health_returns_200_when_database_is_connected(async_client: AsyncClient) -> None:
    """Verify GET /api/v1/health matches docs/API_CONTRACT.md when DB is reachable."""
    response = await async_client.get("/api/v1/health")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {
        "status": "ok",
        "version": "1.0.0",
        "database": "connected",
    }


async def test_health_returns_503_when_database_is_disconnected(test_settings: Settings) -> None:
    """Verify GET /api/v1/health returns 503 degraded when PostgreSQL fails."""
    application = create_app(settings=test_settings)
    failing_session = AsyncMock(spec=AsyncSession)
    failing_session.execute.side_effect = OperationalError(
        "SELECT 1", {}, Exception("connection refused")
    )

    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield failing_session

    application.dependency_overrides[get_db] = _override_get_db

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/api/v1/health")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert response.json() == {
        "status": "degraded",
        "version": "1.0.0",
        "database": "disconnected",
    }


async def test_cors_allows_configured_frontend_origin(async_client: AsyncClient) -> None:
    """Ensure CORS middleware allows the configured clipai-web origin."""
    response = await async_client.get(
        "/api/v1/health",
        headers={"Origin": "http://localhost:3000"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.headers.get("access-control-allow-origin") == "http://localhost:3000"
    assert response.headers.get("access-control-allow-credentials") == "true"


async def test_not_found_returns_standardized_pt_br_error(async_client: AsyncClient) -> None:
    """Ensure 404 responses conform to the standardized PT-BR error contract."""
    response = await async_client.get(
        "/api/v1/non-existent-route",
        headers={"X-Request-ID": "req_custom_123"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    body = response.json()
    assert body["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert body["error"]["message"] == "O recurso solicitado não foi encontrado."
    assert body["error"]["details"] == []
    assert body["error"]["request_id"] == "req_custom_123"


async def test_app_error_and_validation_handlers(test_settings: Settings) -> None:
    """Validate AppError and Pydantic RequestValidationError response format."""
    application = create_app(settings=test_settings)

    class _SamplePayload(BaseModel):
        count: int

    @application.get("/_test/domain-error")
    async def _raise_domain_error() -> None:
        raise AppError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="EMAIL_NOT_VERIFIED",
            message="Confirme seu endereço de e-mail antes de realizar esta ação.",
        )

    @application.post("/_test/validate")
    async def _validate_payload(payload: _SamplePayload) -> dict[str, int]:
        return {"count": payload.count}

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        domain_resp = await client.get("/_test/domain-error")
        validation_resp = await client.post("/_test/validate", json={"count": "invalid"})

    assert domain_resp.status_code == status.HTTP_403_FORBIDDEN
    assert domain_resp.json()["error"]["code"] == "EMAIL_NOT_VERIFIED"
    assert domain_resp.json()["error"]["request_id"].startswith("req_")

    assert validation_resp.status_code == 422
    val_body = validation_resp.json()
    assert val_body["error"]["code"] == "VALIDATION_ERROR"
    assert (
        val_body["error"]["message"]
        == "Os dados enviados são inválidos. Verifique os campos e tente novamente."
    )
    assert val_body["error"]["details"][0]["field"] == "count"


def test_production_app_disables_openapi_docs(test_settings: Settings) -> None:
    """Disable Swagger/ReDoc docs endpoints when running in production."""
    prod_settings = test_settings.model_copy(update={"app_env": "production"})
    prod_app = create_app(settings=prod_settings)

    assert prod_app.docs_url is None
    assert prod_app.redoc_url is None
    assert prod_app.openapi_url is None


async def test_lifespan_disposes_engine_on_shutdown(test_settings: Settings) -> None:
    """Ensure FastAPI lifespan cleanly disposes the database engine on shutdown."""
    application = create_app(settings=test_settings)
    with patch("app.main.dispose_engine", new_callable=AsyncMock) as mock_dispose:
        async with lifespan(application):
            mock_dispose.assert_not_awaited()

    mock_dispose.assert_awaited_once()
