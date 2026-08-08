"""Per-organization rate limiting (section 6.1).

A fixed-window counter in Redis, keyed on the organization rather than the IP.
That is the requirement and it is also the right unit: a CA firm's whole office
shares one NAT address, so an IP limit would throttle a hundred staff as if
they were one attacker, while doing nothing about a single API key hammering
from a hundred addresses.

Unauthenticated requests have no organization, so those fall back to the client
IP — the login endpoint is exactly where a per-IP limit is the useful one.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from app.core.config import settings
from app.core.redis_client import get_client

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    # Unix seconds at which the current window rolls over.
    reset_at: int

    @property
    def retry_after(self) -> int:
        """Seconds a client should wait, never less than one.

        Zero would be read by a client library as "retry immediately", which
        turns a rate limit into a busy loop.
        """
        return max(1, self.reset_at - int(time.time()))

    def headers(self) -> dict[str, str]:
        h = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(0, self.remaining)),
            "X-RateLimit-Reset": str(self.reset_at),
        }
        if not self.allowed:
            h["Retry-After"] = str(self.retry_after)
        return h


def check(bucket: str, *, limit: int | None = None, window_seconds: int = 60) -> RateLimitResult:
    """Count one hit against *bucket* and say whether it is allowed.

    Fails open. If Redis is down the request proceeds: refusing every request
    because the counter is unreachable converts a cache outage into a total
    outage, and the limiter exists to protect against load, not to be a second
    authentication check.
    """
    ceiling = limit if limit is not None else settings.rate_limit_per_minute

    if not settings.rate_limit_enabled:
        return RateLimitResult(True, ceiling, ceiling, int(time.time()) + window_seconds)

    client = get_client()
    if client is None:
        return RateLimitResult(True, ceiling, ceiling, int(time.time()) + window_seconds)

    now = int(time.time())
    window_start = now - (now % window_seconds)
    reset_at = window_start + window_seconds
    # The window start is in the key, so a new window is a new key and the old
    # one expires on its own. That is what makes this correct without a
    # separate reset step, and what stops a long-lived key drifting.
    redis_key = f"complipilot:ratelimit:{bucket}:{window_start}"

    try:
        pipe = client.pipeline()
        pipe.incr(redis_key)
        # TTL is set on every hit rather than only on creation. Setting it only
        # when the counter is 1 leaves a key immortal if the process dies
        # between the INCR and the EXPIRE, and an immortal counter locks a
        # tenant out permanently.
        pipe.expire(redis_key, window_seconds + 10)
        count = int(pipe.execute()[0])
    except Exception as exc:  # noqa: BLE001 - fail open, see docstring
        logger.warning("Rate limit check failed (%s); allowing", type(exc).__name__)
        return RateLimitResult(True, ceiling, ceiling, reset_at)

    return RateLimitResult(
        allowed=count <= ceiling,
        limit=ceiling,
        remaining=ceiling - count,
        reset_at=reset_at,
    )
