"""FastAPI application entrypoint for CLIPAÍ API."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import Settings, get_settings
from app.core.database import dispose_engine
from app.core.exceptions import register_exception_handlers
from app.routers.auth import router as auth_router
from app.routers.health import router as health_router
from app.routers.users import router as users_router


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage startup and graceful shutdown of shared async resources."""
    yield
    await dispose_engine()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application instance."""
    resolved_settings = settings or get_settings()

    application = FastAPI(
        title="CLIPAÍ API",
        version=resolved_settings.app_version,
        debug=resolved_settings.app_debug,
        docs_url=f"{resolved_settings.api_v1_prefix}/docs"
        if resolved_settings.app_env != "production"
        else None,
        redoc_url=f"{resolved_settings.api_v1_prefix}/redoc"
        if resolved_settings.app_env != "production"
        else None,
        openapi_url=f"{resolved_settings.api_v1_prefix}/openapi.json"
        if resolved_settings.app_env != "production"
        else None,
        lifespan=lifespan,
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept", "X-Request-ID"],
    )

    register_exception_handlers(application)
    application.include_router(health_router, prefix=resolved_settings.api_v1_prefix)
    application.include_router(auth_router, prefix=resolved_settings.api_v1_prefix)
    application.include_router(users_router, prefix=resolved_settings.api_v1_prefix)

    return application


app = create_app()
