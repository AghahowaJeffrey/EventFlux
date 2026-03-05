"""
EventFlux — Batch Worker

Responsibilities:
1. Read events from the Redis Stream (XREADGROUP with consumer group).
2. Accumulate a buffer until WORKER_BATCH_SIZE is reached or
   WORKER_FLUSH_INTERVAL_S seconds have elapsed since the last flush.
3. Bulk-insert rows into PostgreSQL using executemany (COPY in Phase 3).
4. Acknowledge processed messages (XACK) so they are removed from the PEL.
5. Run the daily aggregation job every AGGREGATION_INTERVAL_S seconds.

Design notes:
- A single asyncio event loop runs both the ingest loop and the
  aggregation scheduler to keep the worker lightweight.
- On first start the worker creates the consumer group if it does not exist.
- If the worker crashes between XACK and flush, the messages will be
  re-delivered from the PEL on restart (at-least-once semantics).
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from typing import Any

import asyncpg
import redis.asyncio as aioredis

from shared.settings import get_aggregation, get_postgres, get_redis, get_worker

logger = logging.getLogger("eventflux.worker")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

_redis_cfg = get_redis()
_pg_cfg = get_postgres()
_worker_cfg = get_worker()
_agg_cfg = get_aggregation()

# Consumer name (unique per replica; in prod parameterise from env)
CONSUMER_NAME = "worker-1"


# ─── Database helpers ────────────────────────────────────────────────────────

async def ensure_consumer_group(redis: aioredis.Redis) -> None:
    """Create the consumer group if it does not already exist."""
    try:
        await redis.xgroup_create(
            _redis_cfg.stream_key,
            _redis_cfg.consumer_group,
            id="0",         # start from oldest unread message
            mkstream=True,  # create stream if not present
        )
        logger.info(
            "Created consumer group '%s' on stream '%s'.",
            _redis_cfg.consumer_group,
            _redis_cfg.stream_key,
        )
    except aioredis.ResponseError as exc:
        if "BUSYGROUP" in str(exc):
            logger.debug("Consumer group already exists — skipping creation.")
        else:
            raise


async def bulk_insert(pool: asyncpg.Pool, events: list[dict[str, Any]]) -> int:
    """
    Bulk-insert a list of event dicts into events_raw.

    Uses executemany for Phase 1.  Phase 3 will replace this with
    asyncpg's COPY FROM STDIN for maximum throughput.
    """
    records = [
        (
            row["event_type"],
            row["actor_id"],
            row["source"],
            row["timestamp"],
            json.loads(row["attributes"]),
        )
        for row in events
    ]

    sql = """
        INSERT INTO events_raw (event_type, actor_id, source, timestamp, attributes)
        VALUES ($1, $2, $3, $4::timestamptz, $5::jsonb)
    """

    async with pool.acquire() as conn:
        await conn.executemany(sql, records)

    return len(records)


async def run_aggregation(pool: asyncpg.Pool) -> None:
    """
    Compute and upsert daily event counts into the analytics table.

    Uses INSERT ... ON CONFLICT DO UPDATE for idempotent upserts.
    """
    sql = """
        INSERT INTO analytics_daily_event_counts (day, event_type, source, count, updated_at)
        SELECT
            date(timestamp)   AS day,
            event_type,
            source,
            COUNT(*)          AS count,
            now()             AS updated_at
        FROM events_raw
        WHERE timestamp >= now() - INTERVAL '2 days'
        GROUP BY 1, 2, 3
        ON CONFLICT (day, event_type, source)
        DO UPDATE SET
            count      = EXCLUDED.count,
            updated_at = EXCLUDED.updated_at
    """
    async with pool.acquire() as conn:
        result = await conn.execute(sql)
    logger.info("Aggregation job ran. Result: %s", result)


# ─── Main ingest loop ────────────────────────────────────────────────────────

async def ingest_loop(
    redis: aioredis.Redis,
    pool: asyncpg.Pool,
    stop_event: asyncio.Event,
) -> None:
    buffer: list[dict[str, Any]] = []
    last_flush = time.monotonic()
    pending_ids: list[str] = []

    logger.info(
        "Ingest loop started. batch_size=%d flush_interval=%.1fs",
        _worker_cfg.batch_size,
        _worker_cfg.flush_interval_s,
    )

    while not stop_event.is_set():
        # ── Read a block of messages from the stream ──────────────────────
        messages = await redis.xreadgroup(
            groupname=_redis_cfg.consumer_group,
            consumername=CONSUMER_NAME,
            streams={_redis_cfg.stream_key: ">"},   # ">" = only new messages
            count=_worker_cfg.batch_size,
            block=_worker_cfg.max_block_ms,
        )

        if messages:
            for _stream_name, entries in messages:
                for msg_id, fields in entries:
                    buffer.append(fields)
                    pending_ids.append(msg_id)

        elapsed = time.monotonic() - last_flush
        should_flush = (
            len(buffer) >= _worker_cfg.batch_size
            or elapsed >= _worker_cfg.flush_interval_s
        )

        if should_flush and buffer:
            try:
                inserted = await bulk_insert(pool, buffer)
                await redis.xack(
                    _redis_cfg.stream_key,
                    _redis_cfg.consumer_group,
                    *pending_ids,
                )
                logger.info("Flushed %d events to PostgreSQL.", inserted)
                buffer.clear()
                pending_ids.clear()
                last_flush = time.monotonic()
            except Exception as exc:  # noqa: BLE001
                logger.error("Flush failed: %s — retrying next cycle.", exc)

    # Final flush on graceful shutdown
    if buffer:
        try:
            inserted = await bulk_insert(pool, buffer)
            await redis.xack(
                _redis_cfg.stream_key,
                _redis_cfg.consumer_group,
                *pending_ids,
            )
            logger.info("Final flush: %d events.", inserted)
        except Exception as exc:  # noqa: BLE001
            logger.error("Final flush failed: %s", exc)


# ─── Aggregation scheduler ───────────────────────────────────────────────────

async def aggregation_loop(pool: asyncpg.Pool, stop_event: asyncio.Event) -> None:
    logger.info(
        "Aggregation scheduler started. interval=%ds", _agg_cfg.interval_s
    )
    while not stop_event.is_set():
        await asyncio.sleep(_agg_cfg.interval_s)
        if stop_event.is_set():
            break
        try:
            await run_aggregation(pool)
        except Exception as exc:  # noqa: BLE001
            logger.error("Aggregation job failed: %s", exc)


# ─── Entry point ─────────────────────────────────────────────────────────────

async def main() -> None:
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    redis = aioredis.from_url(
        _redis_cfg.url,
        encoding="utf-8",
        decode_responses=True,
    )
    pool = await asyncpg.create_pool(
        _pg_cfg.dsn,
        min_size=2,
        max_size=10,
        command_timeout=30,
    )

    await ensure_consumer_group(redis)

    async with asyncio.TaskGroup() as tg:
        tg.create_task(ingest_loop(redis, pool, stop_event))
        tg.create_task(aggregation_loop(pool, stop_event))

    await pool.close()
    await redis.aclose()
    logger.info("Worker shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
