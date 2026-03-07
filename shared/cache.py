"""
EventFlux — Redis Cache Helper (Phase 7)

Provides a lightweight caching layer for analytics query endpoints.

Design
──────
• Key schema: `cache:{endpoint}:{sha256(sorted_params)[:12]}`
• Storage: JSON-serialised response → Redis STRING with TTL
• Decorator: `@cached(ttl=300)` wraps any async function returning
  a JSON-serialisable value.
• Invalidation: TTL-based only (eventual consistency is acceptable for
  read-heavy analytics data where stale-by-N-seconds is fine).
• Graceful degradation: if Redis is unavailable, the function executes
  normally and the result is not cached (no crash, just a log warning).

TTL guidance
────────────
  /analytics/events/daily  → 300s  (5 min; data updates every AGGREGATION_INTERVAL_S)
  /analytics/events/summary → 120s  (2 min; heavier query, refresh frequently)
  /analytics/event-types    → 600s  (10 min; changes rarely)
  /analytics/events/raw     → 30s   (near-real-time; short TTL acceptable)
"""
from __future__ import annotations

import functools
import hashlib
import json
import logging
from typing import Any, Callable

import redis.asyncio as aioredis

logger = logging.getLogger("eventflux.api.cache")

_CACHE_PREFIX = "cache:"


# ── Low-level get/set ─────────────────────────────────────────────────────────

async def cache_get(redis: aioredis.Redis, key: str) -> Any | None:
    """Return the cached value or None on a miss or error."""
    try:
        raw = await redis.get(key)
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cache GET error key=%s: %s", key, exc)
        return None


async def cache_set(
    redis: aioredis.Redis, key: str, value: Any, ttl: int
) -> None:
    """Store value as JSON with a TTL. Silently skips on error."""
    try:
        await redis.set(key, json.dumps(value, default=str), ex=ttl)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cache SET error key=%s: %s", key, exc)


async def cache_delete(redis: aioredis.Redis, pattern: str) -> int:
    """
    Delete all keys matching a glob pattern.
    Returns the number of keys deleted.
    Use for manual invalidation (e.g., after a backfill operation).
    """
    try:
        keys = await redis.keys(pattern)
        if keys:
            return await redis.delete(*keys)
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cache DELETE error pattern=%s: %s", pattern, exc)
        return 0


# ── Cache key construction ────────────────────────────────────────────────────

def _make_key(namespace: str, kwargs: dict) -> str:
    """
    Deterministic cache key from namespace + sorted kwargs.
    Uses a 12-char hex digest so keys stay short.
    """
    param_str = json.dumps(
        {k: str(v) for k, v in sorted(kwargs.items()) if v is not None},
        sort_keys=True,
    )
    digest = hashlib.sha256(param_str.encode()).hexdigest()[:12]
    return f"{_CACHE_PREFIX}{namespace}:{digest}"


# ── Decorator ─────────────────────────────────────────────────────────────────

def cached(ttl: int = 300, namespace: str | None = None):
    """
    Async function decorator for Redis-backed caching.

    The decorated function MUST:
    1. Have a FastAPI `Request` as its first or keyword argument, OR
    2. Accept a `_redis` keyword argument of type `aioredis.Redis`.

    Usage (FastAPI endpoint):

        from ingestion.routers.cache import cached

        @router.get("/analytics/events/daily")
        @cached(ttl=300)
        async def daily_event_counts(request: Request, ...) -> dict:
            ...

    The decorator reads `request.app.state.redis` for the connection.
    All query-parameter kwargs (excluding `request`) are used to build the key.
    """

    def decorator(fn: Callable):
        ns = namespace or fn.__name__

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            # Extract redis client from request.app.state or explicit kwarg
            redis_client: aioredis.Redis | None = kwargs.pop("_redis", None)
            if redis_client is None:
                # Try to find a `request` argument
                from fastapi import Request as _Request
                request = kwargs.get("request") or next(
                    (a for a in args if isinstance(a, _Request)), None
                )
                if request is not None:
                    redis_client = getattr(request.app.state, "redis", None)

            # Build cache key from non-request kwargs
            cache_kwargs = {k: v for k, v in kwargs.items() if k != "request"}
            key = _make_key(ns, cache_kwargs)

            # Try cache hit
            if redis_client is not None:
                cached_value = await cache_get(redis_client, key)
                if cached_value is not None:
                    logger.debug("Cache HIT key=%s", key)
                    return cached_value

            # Cache miss — execute the function
            result = await fn(*args, **kwargs)

            # Store result
            if redis_client is not None:
                await cache_set(redis_client, key, result, ttl)
                logger.debug("Cache SET key=%s ttl=%d", key, ttl)

            return result

        return wrapper
    return decorator
