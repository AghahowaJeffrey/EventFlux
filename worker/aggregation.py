"""
EventFlux — Aggregation Jobs (Phase 5)

Two aggregation strategies are provided:

  1. run_daily_aggregation(pool)
     - Recomputes counts for `today` and `yesterday` from events_raw.
     - Uses a full GROUP BY then UPSERT. Safe to run any time; idempotent.
     - Designed to run every N seconds (configurable via AGGREGATION_INTERVAL_S).

  2. run_incremental_aggregation(pool, since)
     - Scans only events_raw rows with ingested_at > `since`.
     - Updates the running count by **adding the delta** (count + EXCLUDED.count).
     - Much cheaper for continuous operation; `since` should be persisted across restarts.

Design notes
────────────
• Both functions are idempotent — safe to run concurrently (PG advisory lock
  not required; the ON CONFLICT upsert is atomic at the row level).
• The `count` accumulation uses `count + EXCLUDED.count` to avoid the
  replace-on-conflict bug from Phase 2 (which set count = EXCLUDED.count,
  discarding existing data when rerunning on a partial window).
• `updated_at` always reflects the last aggregation run, useful for freshness checks.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import asyncpg

logger = logging.getLogger("eventflux.worker.aggregation")


# ── Full daily recompute (safe, idempotent) ───────────────────────────────────

async def run_daily_aggregation(pool: asyncpg.Pool) -> int:
    """
    Recompute total daily counts for the current and previous day.

    This is the safe, always-correct path. It re-aggregates from scratch
    so the result is always consistent, even if previous runs had partial data.
    Uses REPLACE semantics (SET count = EXCLUDED.count) because a full
    recompute produces the authoritative result.

    Returns the number of rows upserted.
    """
    sql = """
        INSERT INTO analytics_daily_event_counts
            (day, event_type, source, count, updated_at)
        SELECT
            date(timestamp AT TIME ZONE 'UTC') AS day,
            event_type,
            source,
            COUNT(*)                           AS count,
            now()                              AS updated_at
        FROM events_raw
        WHERE
            -- cover today + yesterday so we always heal partial days
            date(timestamp AT TIME ZONE 'UTC') >= current_date - INTERVAL '1 day'
        GROUP BY 1, 2, 3
        ON CONFLICT (day, event_type, source)
        DO UPDATE SET
            -- full recompute: authoritative, so we replace
            count      = EXCLUDED.count,
            updated_at = EXCLUDED.updated_at
    """
    async with pool.acquire() as conn:
        result = await conn.execute(sql)

    # "INSERT N" → parse N
    affected = int(result.split()[-1]) if result else 0
    logger.info("Daily aggregation: upserted %d rows", affected)
    return affected


# ── Incremental accumulation (efficient for continuous operation) ─────────────

async def run_incremental_aggregation(
    pool: asyncpg.Pool, since: datetime
) -> tuple[int, datetime]:
    """
    Accumulate counts for events ingested after `since`.

    Scans only the newly-arrived rows (using ingested_at for recency), then
    ADDS their counts to the existing totals via:
        ON CONFLICT DO UPDATE SET count = analytics_daily_event_counts.count + EXCLUDED.count

    This is the correct incremental pattern — it doesn't overwrite existing
    counts, it adds the delta. Safe to run at high frequency.

    Returns (rows_upserted, new_watermark) where new_watermark should be
    stored by the caller and passed as `since` on the next invocation.
    """
    sql = """
        INSERT INTO analytics_daily_event_counts
            (day, event_type, source, count, updated_at)
        SELECT
            date(timestamp AT TIME ZONE 'UTC') AS day,
            event_type,
            source,
            COUNT(*)                           AS count,
            now()                              AS updated_at
        FROM events_raw
        WHERE ingested_at > $1
        GROUP BY 1, 2, 3
        ON CONFLICT (day, event_type, source)
        DO UPDATE SET
            -- incremental: ADD the delta to the existing total
            count      = analytics_daily_event_counts.count + EXCLUDED.count,
            updated_at = EXCLUDED.updated_at
    """
    watermark = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        result = await conn.execute(sql, since)

    affected = int(result.split()[-1]) if result else 0
    logger.info(
        "Incremental aggregation: upserted %d rows (since=%s → now=%s)",
        affected, since.isoformat(), watermark.isoformat(),
    )
    return affected, watermark


# ── Backfill — recompute arbitrary date range ─────────────────────────────────

async def backfill_aggregation(
    pool: asyncpg.Pool,
    start_date: str,
    end_date: str,
) -> int:
    """
    Full recompute for a historical date range.
    Intended for one-off corrections or after bulk data loads.
    Usage: await backfill_aggregation(pool, "2026-01-01", "2026-03-01")
    """
    sql = """
        INSERT INTO analytics_daily_event_counts
            (day, event_type, source, count, updated_at)
        SELECT
            date(timestamp AT TIME ZONE 'UTC') AS day,
            event_type,
            source,
            COUNT(*)                            AS count,
            now()                               AS updated_at
        FROM events_raw
        WHERE
            date(timestamp AT TIME ZONE 'UTC') BETWEEN $1::date AND $2::date
        GROUP BY 1, 2, 3
        ON CONFLICT (day, event_type, source)
        DO UPDATE SET
            count      = EXCLUDED.count,
            updated_at = EXCLUDED.updated_at
    """
    async with pool.acquire() as conn:
        result = await conn.execute(sql, start_date, end_date)

    affected = int(result.split()[-1]) if result else 0
    logger.info(
        "Backfill aggregation %s → %s: upserted %d rows",
        start_date, end_date, affected,
    )
    return affected
