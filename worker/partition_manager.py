"""
EventFlux — Partition Manager

Runs as a periodic coroutine inside the worker's asyncio event loop.
Ensures `events_raw` always has explicit day partitions for the next
N days, so no events fall into the catch-all default partition.

Partition naming convention:  events_raw_YYYY_MM_DD

Indexes created per partition (same as the parent):
  - (timestamp)
  - (event_type, timestamp)
  - (actor_id, timestamp)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

import asyncpg

logger = logging.getLogger("eventflux.worker.partitions")

# Create partitions this many days ahead
LOOKAHEAD_DAYS = 7


async def ensure_partitions(pool: asyncpg.Pool, lookahead: int = LOOKAHEAD_DAYS) -> None:
    """
    Idempotently create day partitions for today through today + `lookahead` days.
    Uses IF NOT EXISTS on both the table and the indexes so it is safe to call
    repeatedly.
    """
    today = date.today()
    created: list[str] = []

    async with pool.acquire() as conn:
        for offset in range(lookahead + 1):
            day = today + timedelta(days=offset)
            next_day = day + timedelta(days=1)
            partition_name = f"events_raw_{day.strftime('%Y_%m_%d')}"

            create_partition_sql = f"""
                CREATE TABLE IF NOT EXISTS {partition_name}
                PARTITION OF events_raw
                FOR VALUES FROM ('{day.isoformat()}') TO ('{next_day.isoformat()}')
            """
            await conn.execute(create_partition_sql)

            # Indexes — CREATE INDEX IF NOT EXISTS is idempotent
            idx_base = f"idx_{partition_name}"
            await conn.execute(
                f"CREATE INDEX IF NOT EXISTS {idx_base}_ts "
                f"ON {partition_name} (timestamp)"
            )
            await conn.execute(
                f"CREATE INDEX IF NOT EXISTS {idx_base}_type_ts "
                f"ON {partition_name} (event_type, timestamp)"
            )
            await conn.execute(
                f"CREATE INDEX IF NOT EXISTS {idx_base}_actor_ts "
                f"ON {partition_name} (actor_id, timestamp)"
            )
            created.append(partition_name)

    logger.debug("Partition check complete: ensured %d partitions.", len(created))


async def partition_manager_loop(
    pool: asyncpg.Pool,
    stop_event: asyncio.Event,
    interval_s: int = 3600,  # re-check every hour
) -> None:
    """
    Coroutine that runs `ensure_partitions` on startup and then every `interval_s`.
    Designed to run inside the worker's TaskGroup alongside the ingest loop.
    """
    logger.info("Partition manager started. interval=%ds lookahead=%dd", interval_s, LOOKAHEAD_DAYS)

    # Run immediately on startup
    try:
        await ensure_partitions(pool)
        logger.info("Initial partition check complete.")
    except Exception as exc:  # noqa: BLE001
        logger.error("Partition check failed on startup: %s", exc)

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(
                asyncio.shield(stop_event.wait()),
                timeout=float(interval_s),
            )
        except asyncio.TimeoutError:
            pass  # Normal: timeout means it's time to re-run

        if stop_event.is_set():
            break

        try:
            await ensure_partitions(pool)
        except Exception as exc:  # noqa: BLE001
            logger.error("Partition check failed: %s", exc)
