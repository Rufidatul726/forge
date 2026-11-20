import logging
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("forge.api")


class ErrorBody(BaseModel):
    code: str
    message: str


class ValidationIssue(BaseModel):
    location: str
    message: str
    type: str


class ErrorResponse(BaseModel):
    error: ErrorBody
    request_id: str
    details: list[ValidationIssue] | None = None


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "Resource not found"},
    422: {"model": ErrorResponse, "description": "Request validation failed"},
    500: {"model": ErrorResponse, "description": "Unexpected server error"},
}


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "unavailable")


def _error_response(
    request_id: str,
    status_code: int,
    code: str,
    message: str,
    details: list[dict[str, str]] | None = None,
) -> JSONResponse:
    payload = ErrorResponse(
        error=ErrorBody(code=code, message=message),
        request_id=request_id,
        details=[ValidationIssue(**detail) for detail in details] if details is not None else None,
    )
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(exclude_none=True),
        headers={"X-Request-ID": request_id},
    )


async def handle_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    if exc.status_code == 404:
        code = "not_found"
        message = "Resource not found."
    else:
        code = "http_error"
        message = "Request could not be completed."
    return _error_response(_request_id(request), exc.status_code, code, message)


async def handle_validation_error(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    details = [
        {
            "location": ".".join(str(part) for part in error["loc"]),
            "message": str(error["msg"]),
            "type": str(error["type"]),
        }
        for error in exc.errors()
    ]
    return _error_response(
        _request_id(request),
        422,
        "validation_error",
        "Request validation failed.",
        details,
    )


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    request_id = _request_id(request)
    logger.error(
        "request_failed request_id=%s method=%s path=%s exception_type=%s",
        request_id,
        request.method,
        request.url.path,
        type(exc).__name__,
    )
    return _error_response(
        request_id,
        500,
        "internal_error",
        "An unexpected error occurred.",
    )
