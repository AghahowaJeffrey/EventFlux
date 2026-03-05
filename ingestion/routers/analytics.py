"""
EventFlux — Analytics query router.

GET /v1/analytics/events/daily
  - Queries analytics_daily_event_counts
  - Supports optional ?start_date, ?end_date, ?event_type filters
  - Returns pre-aggregated daily counts

Phase 1: stub that returns a placeholder until the aggregation job
         (Phase 4) populates the analytics tables.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Optional

import asyncpg
from fastapi import APIRouter, Query

from shared.settings import get_postgres

logger = logging.getLogger("eventflux.api.analytics")

router = APIRouter(tags=["analytics"])

_pg_cfg = get_postgres()


@router.get(
    "/analytics/events/daily",
    summary="Query pre-aggregated daily event counts",
)
async def daily_event_counts(
    start_date: Optional[date] = Query(None, description="Inclusive start date (YYYY-MM-DD)"),
    end_date: Optional[date] = Query(None, description="Inclusive end date (YYYY-MM-DD)"),
    event_type: Optional[str] = Query(None, description="Filter by event_type"),
    limit: int = Query(1000, ge=1, le=10_000),
) -> list[dict]:
    """
    Return daily aggregated event counts from the analytics table.

    Supports optional date-range and event_type filters.
    Results are ordered newest-first.

    Note: Returns an empty list until the aggregation job (Phase 4)
    has run and populated `analytics_daily_event_counts`.
    """
    conn: asyncpg.Connection = await asyncpg.connect(_pg_cfg.dsn)
    try:
        filters = []
        params: list = []

        if start_date:
            params.append(start_date)
            filters.append(f"day >= ${len(params)}")
        if end_date:
            params.append(end_date)
            filters.append(f"day <= ${len(params)}")
        if event_type:
            params.append(event_type)
            filters.append(f"event_type = ${len(params)}")

        where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
        params.append(limit)

        sql = f"""
            SELECT
                day::text,
                event_type,
                source,
                count
            FROM analytics_daily_event_counts
            {where_clause}
            ORDER BY day DESC, count DESC
            LIMIT ${len(params)}
        """

        rows = await conn.fetch(sql, *params)
        return [dict(row) for row in rows]
    finally:
        await conn.close()
