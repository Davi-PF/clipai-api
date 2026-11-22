"""Domain exceptions and standardized PT-BR error handlers for FastAPI."""

from uuid import uuid4

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.schemas.common import ErrorBody, ErrorDetailItem, ErrorResponse

_HTTP_STATUS_DEFAULTS: dict[int, tuple[str, str]] = {
    status.HTTP_400_BAD_REQUEST: (
        "BAD_REQUEST",
        "Não foi possível processar a requisição.",
    ),
    status.HTTP_401_UNAUTHORIZED: (
        "INVALID_TOKEN",
        "Token de autenticação inválido ou revogado.",
    ),
    status.HTTP_403_FORBIDDEN: (
        "FORBIDDEN",
        "Você não tem permissão para acessar ou alterar este recurso.",
    ),
    status.HTTP_404_NOT_FOUND: (
        "RESOURCE_NOT_FOUND",
        "O recurso solicitado não foi encontrado.",
    ),
    status.HTTP_405_METHOD_NOT_ALLOWED: (
        "METHOD_NOT_ALLOWED",
        "Método HTTP não permitido para este endereço.",
    ),
    status.HTTP_429_TOO_MANY_REQUESTS: (
        "RATE_LIMIT_EXCEEDED",
        "Muitas tentativas em sequência. Aguarde alguns instantes e tente novamente.",
    ),
}


class AppError(Exception):
    """Base domain exception mapped to the standardized API error contract."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        details: list[ErrorDetailItem] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or []
        self.headers = headers


def _resolve_request_id(request: Request) -> str:
    """Extract X-Request-ID header or generate a traceable request identifier."""
    header_value = request.headers.get("X-Request-ID")
    return header_value if header_value else f"req_{uuid4().hex}"


def _build_error_response(
    *,
    request: Request,
    status_code: int,
    code: str,
    message: str,
    details: list[ErrorDetailItem] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Serialize an error payload compliant with docs/API_CONTRACT.md."""
    payload = ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message,
            details=details or [],
            request_id=_resolve_request_id(request),
        )
    )
    return JSONResponse(status_code=status_code, content=payload.model_dump(), headers=headers)


def register_exception_handlers(app: FastAPI) -> None:
    """Attach global standardized exception handlers to the FastAPI app."""

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return _build_error_response(
            request=request,
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            details=exc.details,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_exception(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        details = [
            ErrorDetailItem(
                field=".".join(str(part) for part in err["loc"] if part != "body"),
                message=str(err["msg"]).removeprefix("Value error, "),
            )
            for err in exc.errors()
        ]
        return _build_error_response(
            request=request,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="VALIDATION_ERROR",
            message="Os dados enviados são inválidos. Verifique os campos e tente novamente.",
            details=details,
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request,
        exc: StarletteHTTPException,
    ) -> JSONResponse:
        code, message = _HTTP_STATUS_DEFAULTS.get(
            exc.status_code,
            (
                "INTERNAL_SERVER_ERROR",
                "Ocorreu um erro interno inesperado. Tente novamente mais tarde.",
            ),
        )
        return _build_error_response(
            request=request,
            status_code=exc.status_code,
            code=code,
            message=message,
        )
