"""SQLAlchemy engine, session factory, and declarative base.

SQLAlchemy 2.0 style. The URL comes from settings so the test suite can point
the same code at an in-memory SQLite database.

The pool is configured rather than left at its defaults. A CA firm opening the
consolidated dashboard fires one query per client, so the pool needs overflow;
and the API runs as several uvicorn workers plus a Celery worker against a
stock Postgres, so ``pool_size`` has to stay small enough that the processes
together sit under ``max_connections``.
"""
from __future__ import annotations

import logging
from collections.abc import Generator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def _engine_kwargs(url: str) -> dict:
    """Engine options appropriate to the backend behind *url*."""
    common: dict = {
        # Every checkout is validated with a one-round-trip ping. Cheap next to
        # a request failing because a pooler recycled the socket underneath it.
        "pool_pre_ping": True,
        "future": True,
        "echo": settings.db_echo,
    }

    if url.startswith("sqlite"):
        sqlite_args = {"check_same_thread": False}
        # An in-memory database additionally needs StaticPool, or every
        # connection opens its own fresh, empty database.
        if ":memory:" in url or "mode=memory" in url:
            return {**common, "connect_args": sqlite_args, "poolclass": StaticPool}
        return {**common, "connect_args": sqlite_args}

    connect_args: dict = {
        # Fail fast when the server is unreachable rather than hanging the
        # worker that tried to reach it.
        "connect_timeout": 10,
        # Names the process in pg_stat_activity, which is what turns "some
        # connection is holding a lock" into an answerable question.
        "application_name": f"{settings.app_name.lower()}-{settings.environment}",
    }
    if settings.db_statement_timeout_seconds:
        connect_args["options"] = (
            f"-c statement_timeout={settings.db_statement_timeout_seconds * 1000}"
        )

    return {
        **common,
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_timeout": settings.db_pool_timeout,
        "pool_recycle": settings.db_pool_recycle,
        "connect_args": connect_args,
    }


def _make_engine(url: str) -> Engine:
    new_engine = create_engine(url, **_engine_kwargs(url))

    if url.startswith("sqlite"):

        @event.listens_for(new_engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - driver hook
            # SQLite ignores foreign keys unless asked, which would let the
            # suite pass against a schema Postgres would reject.
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return new_engine


engine = _make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a request-scoped session.

    Rolls back on the way out of a failed request. Without that, a session
    returned to the pool mid-transaction hands the next request a connection
    with an open, aborted transaction on it, and every statement after that
    fails with "current transaction is aborted" until the process restarts.
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def check_database(session: Session | None = None) -> tuple[bool, str | None]:
    """``(reachable, error_type)`` — used by the health endpoint.

    Takes an optional session so the health route can reuse the request's own,
    which makes the check cover the pool the API actually serves from rather
    than a fresh connection that proves nothing about it.
    """
    try:
        if session is not None:
            session.execute(text("SELECT 1"))
        else:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        return True, None
    except Exception as exc:  # noqa: BLE001 - the caller's job is to report this
        logger.warning("Database health check failed: %s", exc)
        return False, type(exc).__name__


def pool_status() -> dict[str, int | str]:
    """Live pool counters, for the readiness endpoint."""
    pool = engine.pool
    status: dict[str, int | str] = {"type": type(pool).__name__}
    for name, attr in (
        ("size", "size"),
        ("checked_in", "checkedin"),
        ("checked_out", "checkedout"),
        ("overflow", "overflow"),
    ):
        getter = getattr(pool, attr, None)
        if not callable(getter):
            continue
        try:
            status[name] = int(getter())  # type: ignore[call-overload]
        except Exception:  # noqa: BLE001 - a counter must not break health
            continue
    return status
