"""FastAPI application factory and lifespan."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from app.core.config import settings, validate_startup_config
from app.core.database import check_database, get_db, pool_status
from app.core.errors import register_error_handlers
from app.core.logging import configure_logging
from app.core.middleware import register_middleware
from app.core.redis_client import check_redis
from app.routers import (
    alerts,
    audit,
    auth,
    calendar,
    clients,
    dashboard,
    documents,
    dpdp,
    filings,
    obligations,
    organizations,
    templates,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    configure_logging()

    problems = validate_startup_config()
    if problems:
        for problem in problems:
            logger.error("Startup configuration problem: %s", problem)
        # Refuse rather than warn. Every one of these is a production-only
        # check, and each represents a deployment that would run with the
        # development secret or without the mandatory second factor — states
        # a compliance platform must not serve traffic in.
        raise RuntimeError(
            f"Refusing to start: {len(problems)} configuration problem(s); see the log"
        )

    reachable, error = check_database()
    if not reachable:
        # Logged, not fatal. Postgres and the API restart independently under
        # systemd, and an API that exits because the database was slow to come
        # up turns a five-second delay into a restart loop.
        logger.warning("Database not reachable at startup: %s", error)

    logger.info(
        "%s starting (environment=%s, debug=%s)",
        settings.app_name,
        settings.environment,
        settings.debug,
    )
    yield
    logger.info("%s shutting down", settings.app_name)


def create_app() -> FastAPI:
    app = FastAPI(
        title=f"{settings.app_name} API",
        description=(
            "AI compliance copilot for Indian SMBs and CA firms. "
            "Regulatory calendar, filing generation and tracking, DPDP toolkit, "
            "and a tamper-evident audit trail across GST, Income Tax, RBI, "
            "SEBI, MCA, FEMA, labour law and the DPDP Act."
        ),
        version="0.1.0",
        lifespan=lifespan,
        # The interactive docs are useful in development and are an attack
        # surface plus a schema disclosure in production.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None if settings.is_production else "/redoc",
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # So the browser can read the request id off a response and quote it in
        # a bug report.
        expose_headers=["X-Request-ID", "X-RateLimit-Remaining", "X-RateLimit-Reset"],
    )
    register_middleware(app)
    register_error_handlers(app)

    prefix = settings.api_v1_prefix
    app.include_router(auth.router, prefix=prefix)
    app.include_router(organizations.router, prefix=prefix)
    app.include_router(clients.router, prefix=prefix)
    app.include_router(obligations.router, prefix=prefix)
    app.include_router(calendar.router, prefix=prefix)
    app.include_router(filings.router, prefix=prefix)
    app.include_router(documents.router, prefix=prefix)
    app.include_router(alerts.router, prefix=prefix)
    app.include_router(templates.router, prefix=prefix)
    app.include_router(dpdp.router, prefix=prefix)
    app.include_router(audit.router, prefix=prefix)
    app.include_router(dashboard.router, prefix=prefix)

    @app.get("/health", tags=["health"], summary="Liveness")
    def health() -> dict:
        """Is the process up. Deliberately touches nothing else.

        systemd and Caddy poll this every few seconds; making it check the
        database would restart a healthy API because Postgres was busy.
        """
        return {"status": "ok", "app": settings.app_name, "version": "0.1.0"}

    @app.get("/health/ready", tags=["health"], summary="Readiness")
    def readiness(db: Session = Depends(get_db)) -> dict:
        """Can this process actually serve a request.

        Reuses the request's own session so the check covers the pool the API
        serves from, rather than a fresh connection that proves nothing about
        it. Redis being down is reported but does not make the API unready —
        everything it holds is reconstructible.
        """
        db_ok, db_error = check_database(db)
        redis_ok, redis_error = check_redis()
        return {
            "status": "ok" if db_ok else "degraded",
            "database": {"ok": db_ok, "error": db_error, "pool": pool_status()},
            "redis": {"ok": redis_ok, "error": redis_error},
        }

    return app


app = create_app()
