"""Unified error format (SPEC §1.4.1): every error body is {"error": {"code", "message", "details"}}."""

import re
from http import HTTPStatus
from typing import Any

import structlog
from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = {}


class ErrorResponse(BaseModel):
    error: ErrorDetail


class DomainException(Exception):
    status_code: int = status.HTTP_400_BAD_REQUEST

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        status_code: int | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.details = details or {}
        if status_code is not None:
            self.status_code = status_code
        super().__init__(message)


class NotFoundException(DomainException):
    status_code = status.HTTP_404_NOT_FOUND

    def __init__(self, message: str = "Resource not found", details: dict[str, Any] | None = None) -> None:
        super().__init__(code="not_found", message=message, details=details)


class ConflictException(DomainException):
    status_code = status.HTTP_409_CONFLICT

    def __init__(
        self, message: str = "Resource already exists", details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(code="conflict", message=message, details=details)


class PayloadTooLargeException(DomainException):
    status_code = status.HTTP_413_CONTENT_TOO_LARGE

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(code="payload_too_large", message=message, details=details)


class UnprocessableException(DomainException):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(code=code, message=message, details=details)


# Status → code used when an HTTPException carries only a human-readable message
_STATUS_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    503: "service_unavailable",
    504: "timeout",
}
_CODE_LIKE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")


def _code_for_status(status_code: int) -> str:
    if status_code in _STATUS_CODES:
        return _STATUS_CODES[status_code]
    try:
        return HTTPStatus(status_code).phrase.lower().replace(" ", "_").replace("-", "_")
    except ValueError:
        return "error"


def error_body(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return ErrorResponse(error=ErrorDetail(code=code, message=message, details=details or {})).model_dump()


def http_error_body(status_code: int, detail: Any) -> dict[str, Any]:
    """Map an HTTPException detail (str | dict | list | None) to the unified error body."""
    code = _code_for_status(status_code)
    details: dict[str, Any] = {}
    if isinstance(detail, str) and detail:
        if _CODE_LIKE.match(detail):  # e.g. detail="invalid_credentials" → machine-readable code
            code = detail
            message = detail.replace("_", " ").capitalize()
        else:
            message = detail
    elif isinstance(detail, dict):
        code = str(detail.get("code") or code)
        message = str(detail.get("message") or _default_message(status_code))
        extra = detail.get("details")
        details = (
            extra
            if isinstance(extra, dict)
            else {k: v for k, v in detail.items() if k not in {"code", "message"}}
        )
    elif isinstance(detail, list):
        message = _default_message(status_code)
        details = {"errors": detail}
    else:
        message = _default_message(status_code)
    return error_body(code, message, jsonable_encoder(details))


def _default_message(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "Error"


def register_exception_handlers(app: FastAPI) -> None:
    log = structlog.get_logger(__name__)

    @app.exception_handler(DomainException)
    async def domain_exception_handler(request: Request, exc: DomainException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.message, jsonable_encoder(exc.details)),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=http_error_body(exc.status_code, exc.detail),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=error_body(
                "validation_error", "Request validation failed", jsonable_encoder({"errors": errors})
            ),
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_exception", path=request.url.path, error=str(exc))
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=error_body("internal_error", "Internal server error"),
        )


# OpenAPI: error responses documented with the unified shape (replaces FastAPI's default HTTPValidationError)
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    422: {"model": ErrorResponse, "description": "Validation Error"},
}
