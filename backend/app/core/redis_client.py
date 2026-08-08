"""Redis connection, shared by the rate limiter and the cache.

Redis is treated as optional at runtime. Every helper here degrades to a no-op
when it is unreachable, because none of what it holds is authoritative: rate
limit counters and cached calendars are both reconstructible, and a Redis
outage should slow the product down, not take it off the air. The one thing
that genuinely needs Redis is the Celery broker, and Celery has its own
opinion about being unable to reach it.

Keys follow section 2.3's convention: ``complipilot:{orgId}:{resource}:{id}``.
"""
from __future__ import annotations

import contextlib
import logging
from typing import Any

import redis

from app.core.config import settings

logger = logging.getLogger(__name__)

_client: redis.Redis | None = None
# Set once a connection attempt has failed, so a Redis that is down costs one
# failed connect rather than one per request. Cleared by :func:`reset_client`,
# which the health check calls on its own schedule.
_unavailable = False


def key(org_id: int | str, resource: str, ident: Any = None) -> str:
    """Build a namespaced key. ``complipilot:42:calendar:2026-07``."""
    parts = ["complipilot", str(org_id), resource]
    if ident is not None:
        parts.append(str(ident))
    return ":".join(parts)


def get_client() -> redis.Redis | None:
    """The shared client, or ``None`` when Redis is not reachable."""
    global _client, _unavailable

    if _unavailable:
        return None
    if _client is not None:
        return _client

    try:
        client = redis.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            # Short, because every caller here is on a request path and would
            # rather skip the cache than wait on a dead socket.
            socket_connect_timeout=2,
            socket_timeout=2,
            health_check_interval=30,
        )
        client.ping()
    except Exception as exc:  # noqa: BLE001 - any failure means "no Redis"
        logger.warning("Redis unavailable (%s); continuing without it", type(exc).__name__)
        _unavailable = True
        return None

    _client = client
    return _client


def reset_client() -> None:
    """Forget the cached client and the unavailable flag.

    Called by the health check and by tests. Without it, a Redis that comes
    back after a restart stays marked unavailable until the process is
    restarted too.
    """
    global _client, _unavailable
    if _client is not None:
        # Closing a dead socket is not news, and this is called from the health
        # check — a raise here would report Redis as the reason the API is
        # unhealthy when the only failure was tidying up after it.
        with contextlib.suppress(Exception):
            _client.close()
    _client = None
    _unavailable = False


def check_redis() -> tuple[bool, str | None]:
    """``(reachable, error_type)`` for the readiness endpoint."""
    try:
        client = redis.Redis.from_url(
            settings.redis_url, decode_responses=True, socket_connect_timeout=2
        )
        client.ping()
        return True, None
    except Exception as exc:  # noqa: BLE001 - the caller reports this
        return False, type(exc).__name__


def cache_get(name: str) -> str | None:
    client = get_client()
    if client is None:
        return None
    try:
        return client.get(name)
    except Exception:  # noqa: BLE001 - a cache miss is the correct fallback
        return None


def cache_set(name: str, value: str, ttl_seconds: int = 300) -> bool:
    client = get_client()
    if client is None:
        return False
    try:
        client.set(name, value, ex=ttl_seconds)
        return True
    except Exception:  # noqa: BLE001 - failing to cache is not failing
        return False


def cache_delete_prefix(prefix: str) -> int:
    """Delete every key under *prefix*. Returns how many went.

    Uses ``scan_iter`` rather than ``KEYS``: this runs on a shared Redis, and
    ``KEYS`` blocks the whole server for the duration of the scan.
    """
    client = get_client()
    if client is None:
        return 0
    removed = 0
    try:
        for name in client.scan_iter(match=f"{prefix}*", count=500):
            client.delete(name)
            removed += 1
    except Exception:  # noqa: BLE001
        return removed
    return removed
