"""
EventFlux — Detailed health check router.

GET /healthz/detailed
  Probes all downstream dependencies (Redis + PostgreSQL) and returns
  a structured report. Useful for load balancer deep health checks and
  alerting dashboards.
"""
from __future__ import annotations

import logging

import asyncpg
from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from shared.queue import EventQueue
from shared.settings import get_postgres

logger = logging.getLogger("eventflux.api.health")

router = APIRouter(tags=["ops"])

_pg_cfg = get_postgres()


@router.get("/healthz/detailed", summary="Deep dependency health check")
async def detailed_health(request: Request) -> JSONResponse:
    """
    Returns the health of all downstream services.

    - `api`: always `ok` if this handler runs
    - `redis.ping`: live TCP check against the Redis pool
    - `redis.stream_len`: number of entries currently in the event stream
    - `redis.consumer_groups`: lag per consumer group
    - `postgres`: simple `SELECT 1` to verify connectivity

    HTTP 200 = all healthy; HTTP 503 = at least one dependency degraded.
    """
    queue = EventQueue(request.app.state.redis)
    report: dict = {"api": "ok", "redis": {}, "postgres": "unknown"}
    healthy = True

    # ── Redis ─────────────────────────────────────────────────────────────
    redis_ok = await queue.ping()
    report["redis"]["ping"] = "ok" if redis_ok else "error"
    if not redis_ok:
        healthy = False

    if redis_ok:
        try:
            info = await queue.stream_info()
            report["redis"]["stream_len"] = info.length
            report["redis"]["consumer_groups"] = info.groups
        except Exception as exc:  # noqa: BLE001
            # Stream may not exist yet if no events have been ingested
            report["redis"]["stream_len"] = 0
            report["redis"]["stream_note"] = str(exc)

        try:
            lags = await queue.group_lags()
            report["redis"]["lags"] = [
                {"group": g.name, "pending": g.pending, "lag": g.lag}
                for g in lags
            ]
        except Exception:  # noqa: BLE001
            report["redis"]["lags"] = []

    # ── PostgreSQL ────────────────────────────────────────────────────────
    try:
        conn = await asyncpg.connect(_pg_cfg.dsn, timeout=3)
        await conn.fetchval("SELECT 1")
        await conn.close()
        report["postgres"] = "ok"
    except Exception as exc:  # noqa: BLE001
        report["postgres"] = f"error: {exc}"
        healthy = False

    http_status = status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(content=report, status_code=http_status)
