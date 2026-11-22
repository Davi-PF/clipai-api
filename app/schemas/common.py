"""Shared Pydantic v2 schemas for errors and pagination."""

from math import ceil

from pydantic import BaseModel, ConfigDict


class PaginatedResponse[T](BaseModel):
    """Standard pagination envelope compliant with docs/API_CONTRACT.md."""

    model_config = ConfigDict(from_attributes=True)

    items: list[T]
    page: int
    page_size: int
    total: int
    total_pages: int

    @classmethod
    def create(
        cls,
        *,
        items: list[T],
        page: int,
        page_size: int,
        total: int,
    ) -> "PaginatedResponse[T]":
        """Build a paginated envelope calculating total_pages."""
        total_pages = ceil(total / page_size) if total > 0 else 0
        return cls(
            items=items,
            page=page,
            page_size=page_size,
            total=total,
            total_pages=total_pages,
        )


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
