"""Shared caching helpers.

- ``get_redis()``: lazy, process-wide ``redis.asyncio`` client (one connection
  pool instead of a new client per call). Modules that currently build their
  own client (e.g. forecast.py) can switch to it.
- ``TTLCache``: tiny in-process TTL cache for hot read paths (rain cumulative
  totals, "backfill already attempted" markers...). Values never leave the
  process, so it needs no Redis and survives Redis outages.
- ``CircuitBreaker``: negative cache for an external API. After a failure the
  upstream is skipped for a cool-down, so a dead API costs one short timeout
  per cool-down instead of one per request.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Hashable, Optional

from app.config import get_settings

logger = logging.getLogger(__name__)

# Short timeouts: a cache must never be the reason a request hangs.
REDIS_SOCKET_TIMEOUT_S = 2.0

# httpx timeout of the realtime external calls (collectors refreshed by the
# scheduler, cache misses of request paths). Historical backfills keep longer
# timeouts: they download large windows and never block a dashboard read.
EXTERNAL_TIMEOUT_S = 10.0
# Default cool-down of a CircuitBreaker after a failure
BREAKER_COOLDOWN_S = 15 * 60

_redis_client = None


def get_redis():
    """Return the shared async Redis client (created lazily).

    ``redis.asyncio.from_url`` does not connect until the first command, so this
    is cheap and never raises because Redis is down: errors surface on use and
    callers are expected to treat the cache as optional.
    """
    global _redis_client
    if _redis_client is None:
        import redis.asyncio as redis

        settings = get_settings()
        _redis_client = redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=REDIS_SOCKET_TIMEOUT_S,
            socket_connect_timeout=REDIS_SOCKET_TIMEOUT_S,
        )
    return _redis_client


async def close_redis() -> None:
    """Close the shared Redis client (call on application shutdown)."""
    global _redis_client
    client = _redis_client
    _redis_client = None
    if client is not None:
        try:
            await client.aclose()
        except AttributeError:  # redis-py < 5.0.1
            await client.close()
        except Exception as e:  # pragma: no cover - best effort
            logger.debug("Error closing Redis client: %s", e)


class TTLCache:
    """Minimal in-process TTL cache (monotonic clock, bounded size).

    Not thread-safe by design: it is only used from the asyncio event loop.
    """

    def __init__(self, ttl_seconds: float, max_entries: int = 512):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._data: dict[Hashable, tuple[float, Any]] = {}

    def get(self, key: Hashable, default: Any = None) -> Any:
        item = self._data.get(key)
        if item is None:
            return default
        expires_at, value = item
        if time.monotonic() >= expires_at:
            self._data.pop(key, None)
            return default
        return value

    def __contains__(self, key: Hashable) -> bool:
        sentinel = object()
        return self.get(key, sentinel) is not sentinel

    def set(self, key: Hashable, value: Any, ttl_seconds: Optional[float] = None) -> None:
        if len(self._data) >= self.max_entries:
            self._evict()
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        self._data[key] = (time.monotonic() + ttl, value)

    def delete(self, key: Hashable) -> None:
        self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)

    def _evict(self) -> None:
        now = time.monotonic()
        for k in [k for k, (exp, _) in self._data.items() if exp <= now]:
            del self._data[k]
        # Still full: drop the entries closest to expiry.
        while len(self._data) >= self.max_entries:
            oldest = min(self._data, key=lambda k: self._data[k][0])
            del self._data[oldest]


def is_upstream_failure(exc: BaseException) -> bool:
    """True when the upstream is down or slow (timeout, connection error,
    HTTP 5xx), as opposed to a request it rejected (4xx)."""
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, (httpx.TransportError, TimeoutError))


def describe_error(exc: BaseException) -> str:
    """Log-friendly error: the repr (``str(ReadTimeout())`` is empty), or the
    status and body start for an HTTP error (its repr would carry the URL and
    its query-string token)."""
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code} {exc.response.text[:200]}"
    return repr(exc)


class CircuitBreaker:
    """Skip an external API for ``cooldown_s`` after a failure.

    ``allow()`` is False while open. Only the first failure of a streak is
    logged (WARNING, with the exception repr: ``str(ReadTimeout())`` is empty);
    the next ones go to DEBUG until ``record_success()`` closes the breaker.
    Once the cool-down is over the next call is a normal try (half-open).
    """

    def __init__(self, name: str, cooldown_s: float = BREAKER_COOLDOWN_S):
        self.name = name
        self.cooldown_s = cooldown_s
        self._open_until = 0.0
        self._failing = False
        _breakers.append(self)

    def allow(self) -> bool:
        return time.monotonic() >= self._open_until

    def record_failure(self, error: Any) -> None:
        self._open_until = time.monotonic() + self.cooldown_s
        reason = error if isinstance(error, str) else describe_error(error)
        if self._failing:
            logger.debug("%s still unavailable: %s", self.name, reason)
        else:
            logger.warning(
                "%s unavailable, calls skipped for %.0f min: %s",
                self.name, self.cooldown_s / 60, reason,
            )
        self._failing = True

    def record_success(self) -> None:
        if self._failing:
            logger.info("%s available again", self.name)
        self._failing = False
        self._open_until = 0.0

    def reset(self) -> None:
        """Forget any failure (tests)."""
        self._failing = False
        self._open_until = 0.0


# Every breaker (module-level singletons), for reset_breakers()
_breakers: list[CircuitBreaker] = []


def reset_breakers() -> None:
    """Close every breaker (test isolation)."""
    for breaker in _breakers:
        breaker.reset()
