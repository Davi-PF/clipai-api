"""Shared Pydantic v2 schemas for errors and pagination."""

from pydantic import BaseModel, ConfigDict


class ErrorDetailItem(BaseModel):
    """Field-level validation error item."""

    model_config = ConfigDict(from_attributes=True)

    field: str
    message: str


class ErrorBody(BaseModel):
    """Structured error object returned by all 4xx/5xx responses."""

    model_config = ConfigDict(from_attributes=True)

    code: str
    message: str
    details: list[ErrorDetailItem]
    request_id: str


class ErrorResponse(BaseModel):
    """Top-level error envelope compliant with docs/API_CONTRACT.md."""

    model_config = ConfigDict(from_attributes=True)

    error: ErrorBody
