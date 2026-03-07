"""
EventFlux — Analytics query router (Phase 6 + Phase 7 caching).

Endpoints:
  GET /v1/analytics/events/daily    — pre-aggregated daily counts (cached 5 min)
  GET /v1/analytics/events/summary  — aggregate summary over a window (cached 2 min)
  GET /v1/analytics/event-types     — distinct event_type list (cached 10 min)
  GET /v1/analytics/events/raw      — direct events_raw query (max 1h window, cached 30s)
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Optional

import asyncpg
from fastapi import APIRouter, HTTPException, Query, Request, status

from shared.cache import cached
from shared.settings import get_postgres

logger = logging.getLogger("eventflux.api.analytics")

router = APIRouter(tags=["analytics"])

_pg_cfg = get_postgres()


async def _get_conn() -> asyncpg.Connection:
    return await asyncpg.connect(_pg_cfg.dsn)


# ─── /analytics/events/daily ─────────────────────────────────────────────────

@router.get("/analytics/events/daily", summary="Pre-aggregated daily event counts")
@cached(ttl=300, namespace="daily")
async def daily_event_counts(
    request: Request,
    start_date: Annotated[Optional[date], Query(description="Inclusive start date (YYYY-MM-DD)")] = None,
    end_date: Annotated[Optional[date], Query(description="Inclusive end date (YYYY-MM-DD)")] = None,
    event_type: Annotated[Optional[str], Query(description="Filter by event_type")] = None,
    source: Annotated[Optional[str], Query(description="Filter by source")] = None,
    after_day: Annotated[Optional[date], Query(description="Cursor: return rows with day < after_day")] = None,
    limit: Annotated[int, Query(ge=1, le=10_000)] = 500,
) -> dict:
    """
    Return daily aggregated event counts.
    Supports date-range, event_type, source filters, and cursor-based pagination.
    """
    conn = await _get_conn()
    try:
        filters: list[str] = []
        params: list = []

        def add(clause: str, value) -> None:
            params.append(value)
            filters.append(clause.replace("?", f"${len(params)}"))

        if start_date:
            add("day >= ?", start_date)
        if end_date:
            add("day <= ?", end_date)
        if after_day:
            add("day < ?", after_day)
        if event_type:
            add("event_type = ?", event_type)
        if source:
            add("source = ?", source)

        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        params.append(limit)

        sql = f"""
            SELECT day::text, event_type, source, count, updated_at::text
            FROM analytics_daily_event_counts
            {where}
            ORDER BY day DESC, count DESC
            LIMIT ${len(params)}
        """
        rows = await conn.fetch(sql, *params)
        data = [dict(r) for r in rows]
        next_cursor = data[-1]["day"] if len(data) == limit else None
        return {"data": data, "count": len(data), "next_cursor": next_cursor}
    finally:
        await conn.close()


# ─── /analytics/events/summary ───────────────────────────────────────────────

@router.get("/analytics/events/summary", summary="Aggregate summary over a date window")
@cached(ttl=120, namespace="summary")
async def events_summary(
    request: Request,
    start_date: Annotated[Optional[date], Query(description="Start date")] = None,
    end_date: Annotated[Optional[date], Query(description="End date")] = None,
    top_n: Annotated[int, Query(ge=1, le=100)] = 10,
) -> dict:
    """
    Aggregated stats: total events, unique types/sources, top-N breakdown.
    """
    conn = await _get_conn()
    try:
        filters: list[str] = []
        params: list = []

        def add(clause: str, value) -> None:
            params.append(value)
            filters.append(clause.replace("?", f"${len(params)}"))

        if start_date:
            add("day >= ?", start_date)
        if end_date:
            add("day <= ?", end_date)

        where = f"WHERE {' AND '.join(filters)}" if filters else ""

        totals_sql = f"""
            SELECT
                COALESCE(SUM(count), 0)    AS total_events,
                COUNT(DISTINCT event_type) AS unique_event_types,
                COUNT(DISTINCT source)     AS unique_sources
            FROM analytics_daily_event_counts {where}
        """
        totals = dict(await conn.fetchrow(totals_sql, *params))

        params.append(top_n)
        top_types = [
            {"event_type": r["event_type"], "total": int(r["total"])}
            for r in await conn.fetch(
                f"""
                SELECT event_type, SUM(count) AS total
                FROM analytics_daily_event_counts {where}
                GROUP BY event_type ORDER BY total DESC LIMIT ${len(params)}
                """,
                *params,
            )
        ]

        top_sources = [
            {"source": r["source"], "total": int(r["total"])}
            for r in await conn.fetch(
                f"""
                SELECT source, SUM(count) AS total
                FROM analytics_daily_event_counts {where}
                GROUP BY source ORDER BY total DESC
                """,
                *params[:-1],
            )
        ]

        return {
            **{k: int(v) for k, v in totals.items()},
            "top_event_types": top_types,
            "top_sources": top_sources,
        }
    finally:
        await conn.close()


# ─── /analytics/event-types ──────────────────────────────────────────────────

@router.get("/analytics/event-types", summary="List all distinct event types")
@cached(ttl=600, namespace="event_types")
async def list_event_types(request: Request) -> list[str]:
    """Returns all distinct event_type values (for filter dropdowns)."""
    conn = await _get_conn()
    try:
        rows = await conn.fetch(
            "SELECT DISTINCT event_type FROM analytics_daily_event_counts ORDER BY 1"
        )
        return [r["event_type"] for r in rows]
    finally:
        await conn.close()


# ─── /analytics/events/raw ───────────────────────────────────────────────────

@router.get("/analytics/events/raw", summary="Query raw events (max 1-hour window)")
@cached(ttl=30, namespace="raw")
async def raw_events(
    request: Request,
    event_type: Annotated[Optional[str], Query(description="Filter by event_type")] = None,
    actor_id: Annotated[Optional[str], Query(description="Filter by actor_id")] = None,
    source: Annotated[Optional[str], Query(description="Filter by source")] = None,
    since: Annotated[Optional[datetime], Query(description="Start of window (ISO-8601)")] = None,
    until: Annotated[Optional[datetime], Query(description="End of window (ISO-8601)")] = None,
    limit: Annotated[int, Query(ge=1, le=5_000)] = 100,
) -> list[dict]:
    """
    Direct query on events_raw. Hard limit: time window ≤ 1 hour.
    Use /analytics/events/daily for larger windows.
    """
    now = datetime.now(timezone.utc)
    window_start = since or (now - timedelta(hours=1))
    window_end = until or now

    if (window_end - window_start).total_seconds() > 3600:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Time window exceeds 1 hour. Use /analytics/events/daily for larger windows.",
        )

    conn = await _get_conn()
    try:
        filters = ["timestamp >= $1", "timestamp < $2"]
        params: list = [window_start, window_end]

        def add(clause: str, value) -> None:
            params.append(value)
            filters.append(clause.replace("?", f"${len(params)}"))

        if event_type:
            add("event_type = ?", event_type)
        if actor_id:
            add("actor_id = ?", actor_id)
        if source:
            add("source = ?", source)

        params.append(limit)
        sql = f"""
            SELECT event_type, actor_id, source,
                   timestamp::text, attributes, ingested_at::text
            FROM events_raw
            WHERE {' AND '.join(filters)}
            ORDER BY timestamp DESC
            LIMIT ${len(params)}
        """
        rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]
    finally:
        await conn.close()
