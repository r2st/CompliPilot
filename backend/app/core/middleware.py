"""HTTP middleware: request ids, access logging, security headers, body limits."""
from __future__ import annotations

import logging
import time
import uuid

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import settings
from app.core.errors import error_body
from app.core.logging import bind_context, clear_context

logger = logging.getLogger(__name__)

# Endpoints that must not appear in the access log at INFO. The health checks
# are polled every few seconds by systemd and by Caddy, and left in they are
# ~95% of the log volume — which is how a real error gets missed.
_QUIET_PATHS = frozenset({"/health", "/health/ready", "/health/live", "/metrics"})


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a request id, log the request, and time it.

    The id comes from an inbound ``X-Request-ID`` when Caddy supplied one, so a
    trace survives the hop from the proxy, and is minted here otherwise. It
    goes back out on the response, into the logging context, and — via
    :mod:`app.services.audit` — onto the audit entry, which is what lets a
    regulator's question about one action be answered from the logs.
    """

    async def dispatch(self, request: Request, call_next):
        inbound = request.headers.get("X-Request-ID", "")
        # Bounded and sanitised: it lands in log lines and in a database column,
        # and an unbounded header value is a cheap way to bloat both.
        request_id = (
            "".join(c for c in inbound if c.isalnum() or c in "-_")[:64]
            or uuid.uuid4().hex
        )

        request.state.request_id = request_id
        clear_context()
        bind_context(request_id=request_id)

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # Logged here as well as in the exception handler because the
            # duration is only known at this level, and "which slow request
            # blew up" is the question being asked when this line is read.
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.exception(
                "%s %s failed after %.1fms",
                request.method,
                request.url.path,
                elapsed_ms,
            )
            raise
        finally:
            clear_context()

        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time-ms"] = f"{elapsed_ms:.1f}"

        if request.url.path not in _QUIET_PATHS:
            logger.log(
                # A 5xx is an incident and belongs at ERROR even though the
                # request completed; a 4xx is the client's problem and only
                # matters in aggregate.
                logging.ERROR if response.status_code >= 500 else logging.INFO,
                "%s %s -> %d in %.1fms",
                request.method,
                request.url.path,
                response.status_code,
                elapsed_ms,
                extra={
                    "http_method": request.method,
                    "http_path": request.url.path,
                    "http_status": response.status_code,
                    "duration_ms": round(elapsed_ms, 1),
                    "request_id": request_id,
                },
            )

        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Response headers that cost nothing and close off whole classes of attack."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        # This is a JSON API: it needs no camera, no microphone, no geolocation.
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        # A JSON API serves no scripts and embeds nothing. The default-src
        # 'none' policy is therefore accurate rather than aspirational, and it
        # neuters an XSS payload reflected through an error message.
        response.headers.setdefault(
            "Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"
        )

        if settings.is_production:
            # Only over TLS, and only in production — sent from a local HTTP
            # dev server it would pin the developer's browser to https on
            # localhost, which breaks every other project on the machine.
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )

        return response


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject an oversized upload on the declared length, before reading it.

    FastAPI would otherwise buffer the whole body to disk before the route's
    own size check ran, which turns a 2 GB POST into 2 GB of disk write on a
    request that was always going to be refused.

    A body with no ``Content-Length`` (chunked) passes here and is caught by
    the route's own byte counter — this middleware is the cheap first line,
    not the only one.
    """

    def __init__(self, app, max_bytes: int):
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next):
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                length = int(declared)
            except ValueError:
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content=error_body("bad_request", "Malformed Content-Length header"),
                )
            if length > self.max_bytes:
                return JSONResponse(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    content=error_body(
                        "payload_too_large",
                        f"Request body exceeds {self.max_bytes // (1024 * 1024)} MB",
                    ),
                )
        return await call_next(request)


def register_middleware(app: FastAPI) -> None:
    """Attach the middleware stack.

    Order matters and is the reverse of registration: Starlette wraps each new
    middleware *around* the ones already added, so the last registered is the
    outermost. The request-context middleware is registered last so it sees
    every request first and every response last — including the ones the body
    limit rejects, which would otherwise be logged with no request id.
    """
    # A generous ceiling above the per-file limit: a multipart upload carries
    # boundaries and fields beyond the file itself, and a request refused for
    # being 40 bytes over its file limit is a confusing error.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_upload_bytes + 1_048_576)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)
