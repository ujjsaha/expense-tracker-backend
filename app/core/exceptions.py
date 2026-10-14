"""Application error hierarchy and the global handlers (SRS sections 6 and 7).

Every error leaving the API uses the SRS 6.2 failure envelope:

    {"status": "failure", "message": "...", "errors": []}

Internal detail - stack traces, driver messages - never reaches a client.
"""

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class AppError(Exception):
    """Base for every error the application raises deliberately."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    default_message: str = "An unexpected error occurred"

    def __init__(self, message: str | None = None, errors: list[Any] | None = None) -> None:
        self.message = message or self.default_message
        self.errors = errors or []
        super().__init__(self.message)


class BadRequestError(AppError):
    """Business rule violation - SRS 6.3 maps this to 400."""

    status_code = status.HTTP_400_BAD_REQUEST
    default_message = "Bad request"


class UnauthorizedError(AppError):
    """Missing, expired, or invalid token / credentials."""

    status_code = status.HTTP_401_UNAUTHORIZED
    default_message = "Not authenticated"


class ForbiddenError(AppError):
    """Authenticated, but the record belongs to someone else (SRS 4.4)."""

    status_code = status.HTTP_403_FORBIDDEN
    default_message = "You are not permitted to access this resource"


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    default_message = "Resource not found"


class ConflictError(AppError):
    """Duplicate email or mobile."""

    status_code = status.HTTP_409_CONFLICT
    default_message = "Resource already exists"


def failure_response(
    status_code: int, message: str, errors: list[Any] | None = None
) -> JSONResponse:
    """Build the SRS 6.2 envelope, adding the auth challenge header on 401."""
    headers = None
    if status_code == status.HTTP_401_UNAUTHORIZED:
        headers = {"WWW-Authenticate": "Bearer"}
    return JSONResponse(
        status_code=status_code,
        content={"status": "failure", "message": message, "errors": errors or []},
        headers=headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        logger.info("Handled %s on %s: %s", type(exc).__name__, request.url.path, exc.message)
        return failure_response(exc.status_code, exc.message, exc.errors)

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Schema failures - SRS 6.3 keeps FastAPI's 422, wrapped in our envelope.

        The raw pydantic details go in `errors` so a client can still see which
        field failed and why.
        """
        return failure_response(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Validation failed",
            jsonable_encoder(exc.errors()),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        message = exc.detail if isinstance(exc.detail, str) else "Request failed"
        return failure_response(exc.status_code, message)

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        # Full detail to the log, generic message to the client (SRS section 7).
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return failure_response(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "An internal server error occurred",
        )
