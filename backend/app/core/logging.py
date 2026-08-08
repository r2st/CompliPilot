"""Logging setup, and the request-scoped context every log line carries.

Two formatters: a readable one for a terminal and JSON for the journal on the
VPS. Both pull the request id, organization and user from :data:`request_ctx`,
which is a ``ContextVar`` rather than a parameter threaded through every call —
a service three layers down should not need a logging argument to produce a
line that can be correlated with the request that caused it.
"""
from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from typing import Any

from app.core.config import settings

# Set by the middleware on the way in, and by the Celery hooks at task start.
# The default is a plain dict so a log line emitted outside any request — at
# import, or from a management command — formats rather than raising.
request_ctx: ContextVar[dict[str, Any]] = ContextVar("request_ctx", default={})

# Attributes present on every LogRecord. Anything not in here was put on the
# record by the caller with ``extra=`` and is worth emitting.
_STANDARD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()
) | {"message", "asctime", "taskName"}


def _context_fields() -> dict[str, Any]:
    try:
        return dict(request_ctx.get() or {})
    except LookupError:  # pragma: no cover - only if the var is reset oddly
        return {}


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for ``journalctl -o cat | jq``."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(_context_fields())

        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        # ``default=str`` because a caller will eventually log a Decimal, a
        # date or a model instance, and a logging call must never be the thing
        # that raises.
        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """Human-readable, with the request id appended when there is one."""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        ctx = _context_fields()
        parts = [f"{k}={v}" for k, v in ctx.items() if v is not None]
        return f"{base}  [{' '.join(parts)}]" if parts else base


def configure_logging() -> None:
    """Install the root handler. Idempotent — a second call replaces the first."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    if settings.log_format.lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            ConsoleFormatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
        )

    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())

    # uvicorn installs its own handlers on these; left in place they duplicate
    # every access line, once in our format and once in theirs.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        log = logging.getLogger(name)
        log.handlers = []
        log.propagate = True

    # SQLAlchemy's engine logger is chatty at INFO and says nothing useful
    # unless ``db_echo`` was asked for explicitly.
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.db_echo else logging.WARNING
    )


def bind_context(**fields: Any) -> None:
    """Merge fields into the current logging context."""
    request_ctx.set({**_context_fields(), **fields})


def clear_context() -> None:
    request_ctx.set({})
