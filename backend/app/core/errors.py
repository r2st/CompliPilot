"""Error types and the handlers that turn them into responses.

Every error response has the same shape::

    {"error": {"code": "not_found", "message": "...", "details": {...}}}

One shape rather than several because the frontend has one error renderer, and
because a client that has to guess whether a failure is ``detail`` or
``message`` or ``errors`` will guess wrong on the path it tests least.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.core.config import settings

logger = logging.getLogger(__name__)


class AppError(Exception):
    """Base for errors this application raises deliberately.

    Carries the HTTP status with it so a service can refuse an operation
    without importing FastAPI or knowing it is being called from a route — the
    same exception is meaningful when raised inside a Celery task.
    """

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class ConflictError(AppError):
    """The request contradicts existing state — a duplicate, or a lost update."""

    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class ValidationError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "validation_error"


class AuthError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthorized"


class ForbiddenError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"


class RateLimitError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"


class InvalidTransitionError(ConflictError):
    """A filing (or breach, or request) was pushed into a state it cannot reach.

    A subclass rather than a bare ConflictError because the frontend renders it
    differently: it can name the states that *are* reachable, which it takes
    from ``details["allowed"]``.
    """

    code = "invalid_transition"


class UpstreamError(AppError):
    """A third party — OpenRouter, WhatsApp, SMTP — failed or timed out."""

    status_code = status.HTTP_502_BAD_GATEWAY
    code = "upstream_error"


def error_body(code: str, message: str, details: dict | None = None) -> dict:
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    return body


def register_error_handlers(app: FastAPI) -> None:
    """Attach the handlers. Called once from :mod:`app.main`."""

    @app.exception_handler(AppError)
    async def _app_error(_request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(HTTPException)
    async def _http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        # FastAPI's own raises — including the ones its security dependencies
        # raise — reshaped into the envelope above so a 401 from the bearer
        # scheme looks like every other 401.
        code = {
            401: "unauthorized",
            403: "forbidden",
            404: "not_found",
            409: "conflict",
            429: "rate_limited",
        }.get(exc.status_code, "error")
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(code, str(exc.detail)),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Pydantic's error list is passed through under ``fields`` because the
        # filing editor uses it to mark the offending inputs. ``str`` on the
        # context, since a ValueError in a validator is not JSON-serialisable
        # and would turn a 422 into a 500.
        fields = [
            {
                "loc": [str(p) for p in err.get("loc", [])],
                "msg": err.get("msg", ""),
                "type": err.get("type", ""),
            }
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=error_body("validation_error", "Request validation failed", {"fields": fields}),
        )

    @app.exception_handler(IntegrityError)
    async def _integrity_error(_request: Request, exc: IntegrityError) -> JSONResponse:
        # A unique or foreign-key violation that reached the database is a
        # conflict, not a server fault: two users submitted the same filing at
        # once, or a client was offboarded between the check and the write.
        # The driver's message can name column values, so it is logged and not
        # returned.
        logger.warning("Integrity error: %s", exc.orig)
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=error_body(
                "conflict", "That record conflicts with one that already exists"
            ),
        )

    @app.exception_handler(SQLAlchemyError)
    async def _db_error(_request: Request, exc: SQLAlchemyError) -> JSONResponse:
        logger.exception("Database error", exc_info=exc)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=error_body("database_unavailable", "The database is not available"),
        )

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error", exc_info=exc)
        # The message is echoed only outside production. A stack-adjacent
        # string on a compliance platform is an information leak, and the
        # request id in the log is how support finds the real one.
        message = str(exc) if settings.debug and not settings.is_production else (
            "An unexpected error occurred"
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=error_body("internal_error", message),
        )
