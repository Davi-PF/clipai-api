"""Healthcheck response schema."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class HealthResponse(BaseModel):
    """Response schema for GET /api/v1/health."""

    model_config = ConfigDict(from_attributes=True)

    status: Literal["ok", "degraded"]
    version: str
    database: Literal["connected", "disconnected"]
