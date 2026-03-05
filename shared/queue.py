"""
EventFlux — EventQueue

Thin abstraction over Redis Streams that encapsulates:
- XADD with approximate MAXLEN (backpressure) via pipelined batch enqueue
- XINFO wrappers for health checks and lag monitoring
- Consumer group creation (idempotent)
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import redis.asyncio as aioredis

from shared.models import Event
from shared.settings import get_redis

logger = logging.getLogger("eventflux.queue")

_cfg = get_redis()

# Maximum approximate stream length — protects against unbounded growth
# if workers are down. Excess oldest entries are trimmed by Redis.
STREAM_MAXLEN = 1_000_000


@dataclass
class StreamInfo:
    length: int
    groups: int
    first_entry_id: str | None
    last_entry_id: str | None


@dataclass
class GroupLag:
    name: str
    consumers: int
    pending: int
    lag: int | None   # None if Redis < 7.0


class EventQueue:
    """
    Wraps a redis.asyncio client to provide a typed, observable interface
    for the events:raw Redis Stream.
    """

    def __init__(self, redis: aioredis.Redis, stream_key: str = _cfg.stream_key) -> None:
        self._redis = redis
        self._stream_key = stream_key

    # ── Write path ────────────────────────────────────────────────────────────

    async def enqueue_batch(self, events: list[Event]) -> list[str]:
        """
        Enqueue a batch of events using a single pipelined XADD.

        Each message stores per-field strings so the worker can read
        without parsing an outer JSON envelope.

        The stream is trimmed to ~STREAM_MAXLEN oldest entries to
        provide backpressure when the worker is slow.

        Returns the list of message IDs assigned by Redis.
        """
        pipe = self._redis.pipeline(transaction=False)
        for event in events:
            pipe.xadd(
                self._stream_key,
                {
                    "event_type": event.event_type,
                    "actor_id": event.actor_id,
                    "source": event.source,
                    "timestamp": event.timestamp.isoformat(),
                    "attributes": json.dumps(event.attributes),
                },
                maxlen=STREAM_MAXLEN,
                approximate=True,
            )
        return await pipe.execute()

    # ── Consumer group ────────────────────────────────────────────────────────

    async def ensure_consumer_group(self, group_name: str) -> None:
        """Create the consumer group idempotently (BUSYGROUP is not an error)."""
        try:
            await self._redis.xgroup_create(
                self._stream_key,
                group_name,
                id="0",
                mkstream=True,
            )
            logger.info("Created consumer group '%s' on '%s'.", group_name, self._stream_key)
        except aioredis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    # ── Observability ─────────────────────────────────────────────────────────

    async def stream_info(self) -> StreamInfo:
        """Return basic stream metadata via XINFO STREAM."""
        info: dict[str, Any] = await self._redis.xinfo_stream(self._stream_key)
        return StreamInfo(
            length=info.get("length", 0),
            groups=info.get("groups", 0),
            first_entry_id=str(info["first-entry"][0]) if info.get("first-entry") else None,
            last_entry_id=str(info["last-entry"][0]) if info.get("last-entry") else None,
        )

    async def group_lags(self) -> list[GroupLag]:
        """Return lag information per consumer group via XINFO GROUPS."""
        groups: list[dict[str, Any]] = await self._redis.xinfo_groups(self._stream_key)
        return [
            GroupLag(
                name=g["name"],
                consumers=g["consumers"],
                pending=g["pending"],
                lag=g.get("lag"),  # lag field added in Redis 7.0
            )
            for g in groups
        ]

    async def ping(self) -> bool:
        """Quick liveness check — returns True if Redis responds."""
        try:
            return await self._redis.ping()
        except Exception:  # noqa: BLE001
            return False
