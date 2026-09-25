from typing import Any

from pydantic import BaseModel


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = {}


class ErrorResponse(BaseModel):
    error: ErrorDetail


class DomainException(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)


class NotFoundException(DomainException):
    def __init__(self, message: str = "Resource not found", details: dict[str, Any] | None = None) -> None:
        super().__init__(code="not_found", message=message, details=details)


class ConflictException(DomainException):
    def __init__(
        self, message: str = "Resource already exists", details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(code="conflict", message=message, details=details)
