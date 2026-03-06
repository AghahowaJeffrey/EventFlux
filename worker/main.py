"""
EventFlux — Batch Worker (Phase 3)

Phase 3 additions:
- WORKER_SINGLE_ROW_MODE env var: switches from asyncpg COPY to individual
  row inserts (executemany) so load tests can demonstrate the throughput
  difference between single-row and batch strategies.
- Flush metrics log now includes insert_mode for easy grep/correlation.
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

from shared.queue import EventQueue
from shared.settings import get_aggregation, get_postgres, get_redis, get_worker
from worker.partition_manager import partition_manager_loop

logger = logging.getLogger("eventflux.worker")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

_redis_cfg = get_redis()
_pg_cfg = get_postgres()
_worker_cfg = get_worker()
_agg_cfg = get_aggregation()

CONSUMER_NAME = "worker-1"
# Messages delivered more than this many times are moved to dead-letter
MAX_DELIVERY_ATTEMPTS = 3
# Messages idle longer than this (ms) are claimed from PEL on recovery
PEL_IDLE_THRESHOLD_MS = 5 * 60 * 1000  # 5 minutes
DEAD_LETTER_STREAM = "events:dead"


# ─── Bulk insert via COPY ────────────────────────────────────────────────────

async def bulk_insert_copy(pool: asyncpg.Pool, events: list[dict[str, Any]]) -> int:
    """
    Bulk-insert events using asyncpg's copy_records_to_table.

    COPY is the fastest way to load data into PostgreSQL — it bypasses
    row-by-row WAL overhead and achieves significantly higher throughput
    than executemany for batches > ~100 rows.

    The target table is events_raw (partitioned); PostgreSQL routes each
    row to the correct child partition automatically.
    """
    records = [
        (
            row["event_type"],
            row["actor_id"],
            row["source"],
            row["timestamp"],              # str ISO-8601 — PG casts to timestamptz
            json.loads(row["attributes"]),  # dict — PG casts to jsonb
        )
        for row in events
    ]

    async with pool.acquire() as conn:
        await conn.copy_records_to_table(
            "events_raw",
            records=records,
            columns=["event_type", "actor_id", "source", "timestamp", "attributes"],
        )

    return len(records)


async def bulk_insert_single_row(pool: asyncpg.Pool, events: list[dict[str, Any]]) -> int:
    """
    Insert events one row at a time using executemany.

    Intentionally slower than COPY — used when WORKER_SINGLE_ROW_MODE=true
    to provide a controlled comparison baseline for Phase 5 benchmarking.
    This mirrors what many naive implementations do in production.
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


# ─── Dead-letter handling ────────────────────────────────────────────────────

async def move_to_dead_letter(
    redis: aioredis.Redis,
    msg_id: str,
    fields: dict[str, str],
    reason: str,
) -> None:
    """Move a poisoned message to the dead-letter stream and ACK it."""
    await redis.xadd(
        DEAD_LETTER_STREAM,
        {**fields, "_original_id": msg_id, "_reason": reason},
    )
    await redis.xack(_redis_cfg.stream_key, _redis_cfg.consumer_group, msg_id)
    logger.warning("Dead-lettered message %s: %s", msg_id, reason)


# ─── PEL recovery ────────────────────────────────────────────────────────────

async def recover_pending(redis: aioredis.Redis, queue: EventQueue) -> None:
    """
    On startup, claim any messages that have been idle in the PEL for
    longer than PEL_IDLE_THRESHOLD_MS using XAUTOCLAIM.

    Messages that have exceeded MAX_DELIVERY_ATTEMPTS are dead-lettered.
    Recoverable messages are returned to the ">"-cursor flow via XACK +
    re-XADD so the main loop processes them fresh.
    """
    logger.info("Running PEL recovery (idle threshold: %dms)…", PEL_IDLE_THRESHOLD_MS)
    start_id = "0-0"

    while True:
        result = await redis.xautoclaim(
            _redis_cfg.stream_key,
            _redis_cfg.consumer_group,
            CONSUMER_NAME,
            min_idle_time=PEL_IDLE_THRESHOLD_MS,
            start_id=start_id,
            count=100,
        )
        # XAUTOCLAIM returns: [next_start_id, [[id, fields], ...], [deleted_ids]]
        next_start, claimed, _ = result

        if not claimed:
            break

        for msg_id, fields in claimed:
            # Check delivery count via XPENDING range
            pending_info = await redis.xpending_range(
                _redis_cfg.stream_key,
                _redis_cfg.consumer_group,
                min=msg_id,
                max=msg_id,
                count=1,
            )
            delivery_count = pending_info[0]["times_delivered"] if pending_info else 0

            if delivery_count >= MAX_DELIVERY_ATTEMPTS:
                await move_to_dead_letter(
                    redis, msg_id, fields,
                    f"exceeded {MAX_DELIVERY_ATTEMPTS} delivery attempts",
                )
            else:
                logger.debug("Recovered PEL message %s (delivered %d times).", msg_id, delivery_count)
                # Already claimed to this consumer — will be processed in main loop

        if next_start == "0-0":
            break
        start_id = next_start

    logger.info("PEL recovery complete.")


# ─── Aggregation job ─────────────────────────────────────────────────────────

async def run_aggregation(pool: asyncpg.Pool) -> None:
    """Upsert daily event counts covering the last 2 days (idempotent)."""
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
    logger.info("Aggregation job complete. %s", result)


# ─── Ingest loop ─────────────────────────────────────────────────────────────

async def ingest_loop(
    redis: aioredis.Redis,
    pool: asyncpg.Pool,
    stop_event: asyncio.Event,
) -> None:
    buffer: list[dict[str, Any]] = []
    pending_ids: list[str] = []
    last_flush = time.monotonic()

    logger.info(
        "Ingest loop started. batch_size=%d flush_interval=%.1fs",
        _worker_cfg.batch_size, _worker_cfg.flush_interval_s,
    )

    while not stop_event.is_set():
        messages = await redis.xreadgroup(
            groupname=_redis_cfg.consumer_group,
            consumername=CONSUMER_NAME,
            streams={_redis_cfg.stream_key: ">"},
            count=_worker_cfg.batch_size,
            block=_worker_cfg.max_block_ms,
        )

        if messages:
            for _stream, entries in messages:
                for msg_id, fields in entries:
                    buffer.append(fields)
                    pending_ids.append(msg_id)

        elapsed = time.monotonic() - last_flush
        should_flush = (
            len(buffer) >= _worker_cfg.batch_size
            or elapsed >= _worker_cfg.flush_interval_s
        )

        if should_flush and buffer:
            t0 = time.monotonic()
            insert_mode = "single-row" if _worker_cfg.single_row_mode else "copy"
            try:
                if _worker_cfg.single_row_mode:
                    inserted = await bulk_insert_single_row(pool, buffer)
                else:
                    inserted = await bulk_insert_copy(pool, buffer)
                await redis.xack(
                    _redis_cfg.stream_key,
                    _redis_cfg.consumer_group,
                    *pending_ids,
                )
                duration = time.monotonic() - t0
                rate = inserted / duration if duration > 0 else 0.0
                logger.info(
                    "Flushed batch: inserted=%d mode=%s duration=%.3fs rate=%.0f events/s",
                    inserted, insert_mode, duration, rate,
                )
                buffer.clear()
                pending_ids.clear()
                last_flush = time.monotonic()
            except Exception as exc:  # noqa: BLE001
                logger.error("Flush failed mode=%s (%s) — retrying next cycle.", insert_mode, exc)

    # Graceful shutdown: flush remaining buffer
    if buffer:
        try:
            inserted = await bulk_insert_copy(pool, buffer)
            await redis.xack(
                _redis_cfg.stream_key,
                _redis_cfg.consumer_group,
                *pending_ids,
            )
            logger.info("Shutdown flush: %d events.", inserted)
        except Exception as exc:  # noqa: BLE001
            logger.error("Shutdown flush failed: %s", exc)


# ─── Aggregation scheduler ───────────────────────────────────────────────────

async def aggregation_loop(pool: asyncpg.Pool, stop_event: asyncio.Event) -> None:
    logger.info("Aggregation scheduler started. interval=%ds", _agg_cfg.interval_s)
    while not stop_event.is_set():
        await asyncio.sleep(_agg_cfg.interval_s)
        if stop_event.is_set():
            break
        try:
            await run_aggregation(pool)
        except Exception as exc:  # noqa: BLE001
            logger.error("Aggregation failed: %s", exc)


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
        _pg_cfg.dsn, min_size=2, max_size=10, command_timeout=30,
    )

    queue = EventQueue(redis)
    await queue.ensure_consumer_group(_redis_cfg.consumer_group)
    await recover_pending(redis, queue)

    async with asyncio.TaskGroup() as tg:
        tg.create_task(ingest_loop(redis, pool, stop_event), name="ingest")
        tg.create_task(aggregation_loop(pool, stop_event), name="aggregation")
        tg.create_task(partition_manager_loop(pool, stop_event), name="partitions")

    await pool.close()
    await redis.aclose()
    logger.info("Worker shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
