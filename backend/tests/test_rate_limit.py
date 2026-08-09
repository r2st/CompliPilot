"""The per-organization rate limiter.

Every other test in the suite runs with ``RATE_LIMIT_ENABLED=false`` (see
``conftest``'s module docstring), which means this module has never actually
been exercised by the rest of the suite — a limiter that always says "allowed"
is indistinguishable from one that is broken. These tests turn it on and drive
it directly, against a fake Redis client rather than a real one, since the
suite has no Redis to talk to.
"""
from __future__ import annotations

import time

import pytest

from app.core import rate_limit
from app.core.config import settings


class FakePipeline:
    def __init__(self, store: dict[str, int]):
        self._store = store
        self._ops: list[tuple[str, tuple]] = []

    def incr(self, key: str):
        self._ops.append(("incr", (key,)))
        return self

    def expire(self, key: str, seconds: int):
        self._ops.append(("expire", (key, seconds)))
        return self

    def execute(self) -> list[int]:
        results = []
        for op, args in self._ops:
            if op == "incr":
                key = args[0]
                self._store[key] = self._store.get(key, 0) + 1
                results.append(self._store[key])
            else:
                results.append(True)
        return results


class FakeRedis:
    """Enough of the client surface for :func:`rate_limit.check` to run."""

    def __init__(self):
        self.store: dict[str, int] = {}

    def pipeline(self):
        return FakePipeline(self.store)


class BrokenRedis:
    """A client whose pipeline blows up, as a flaky Redis would mid-outage."""

    def pipeline(self):
        raise ConnectionError("connection refused")


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", True)


class TestDisabled:
    def test_disabled_allows_without_touching_redis(self, monkeypatch):
        monkeypatch.setattr(settings, "rate_limit_enabled", False)
        monkeypatch.setattr(rate_limit, "get_client", lambda: (_ for _ in ()).throw(
            AssertionError("Redis should not be consulted when the limiter is off")
        ))

        result = rate_limit.check("org:1")

        assert result.allowed is True


class TestNoRedis:
    def test_a_missing_client_fails_open(self, monkeypatch):
        monkeypatch.setattr(rate_limit, "get_client", lambda: None)

        result = rate_limit.check("org:1", limit=5)

        assert result.allowed is True
        assert result.remaining == 5

    def test_a_broken_pipeline_fails_open_rather_than_500ing_every_request(
        self, monkeypatch
    ):
        monkeypatch.setattr(rate_limit, "get_client", lambda: BrokenRedis())

        result = rate_limit.check("org:1", limit=5)

        assert result.allowed is True


class TestCounting:
    def test_the_first_hit_in_a_window_is_allowed(self, monkeypatch):
        monkeypatch.setattr(rate_limit, "get_client", lambda: FakeRedis())

        result = rate_limit.check("org:1", limit=3)

        assert result.allowed is True
        assert result.remaining == 2

    def test_a_hit_at_the_ceiling_is_still_allowed(self, monkeypatch):
        fake = FakeRedis()
        monkeypatch.setattr(rate_limit, "get_client", lambda: fake)

        for _ in range(3):
            result = rate_limit.check("org:1", limit=3)

        assert result.allowed is True
        assert result.remaining == 0

    def test_the_hit_past_the_ceiling_is_refused(self, monkeypatch):
        fake = FakeRedis()
        monkeypatch.setattr(rate_limit, "get_client", lambda: fake)

        for _ in range(3):
            rate_limit.check("org:1", limit=3)
        result = rate_limit.check("org:1", limit=3)

        assert result.allowed is False
        assert result.remaining == -1

    def test_two_buckets_are_independent(self, monkeypatch):
        fake = FakeRedis()
        monkeypatch.setattr(rate_limit, "get_client", lambda: fake)

        for _ in range(3):
            rate_limit.check("org:1", limit=3)
        result = rate_limit.check("org:2", limit=3)

        assert result.allowed is True

    def test_the_default_ceiling_is_the_configured_setting(self, monkeypatch):
        monkeypatch.setattr(settings, "rate_limit_per_minute", 7)
        monkeypatch.setattr(rate_limit, "get_client", lambda: FakeRedis())

        result = rate_limit.check("org:1")

        assert result.limit == 7


class TestResultHeaders:
    def test_an_allowed_result_carries_no_retry_after(self):
        result = rate_limit.RateLimitResult(
            allowed=True, limit=10, remaining=4, reset_at=int(time.time()) + 30
        )

        assert "Retry-After" not in result.headers()
        assert result.headers()["X-RateLimit-Remaining"] == "4"

    def test_a_refused_result_carries_a_positive_retry_after(self):
        result = rate_limit.RateLimitResult(
            allowed=False, limit=10, remaining=-2, reset_at=int(time.time()) + 30
        )

        assert "Retry-After" in result.headers()
        assert int(result.headers()["Retry-After"]) >= 1

    def test_remaining_never_reports_negative_in_the_header(self):
        """The dataclass field can go negative; the header a client reads must not."""
        result = rate_limit.RateLimitResult(
            allowed=False, limit=10, remaining=-5, reset_at=int(time.time()) + 30
        )

        assert result.headers()["X-RateLimit-Remaining"] == "0"

    def test_retry_after_is_never_less_than_one(self):
        """Zero would read to a client as "retry immediately" — a busy loop."""
        result = rate_limit.RateLimitResult(
            allowed=False, limit=10, remaining=-1, reset_at=int(time.time())
        )

        assert result.retry_after >= 1
