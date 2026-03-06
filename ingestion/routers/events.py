"""
EventFlux — Event ingestion router.

POST /v1/events/bulk
  Standard path:   validate → EventQueue.enqueue_batch() → Redis Stream → 202
  Bypass-queue:    validate → direct single-row INSERT into PostgreSQL → 202

The `X-Experiment-Mode: bypass-queue` header activates the slow synchronous
path to demonstrate write-throughput degradation for Phase 3/5 benchmarking.
Never use bypass-queue in production.
"""
from __future__ import annotations

import json
import logging
import time

import asyncpg
from fastapi import APIRouter, Header, HTTPException, Request, Response, status

from shared.models import BulkEventRequest, EventBulkResponse
from shared.queue import EventQueue
from shared.settings import get_api, get_postgres

logger = logging.getLogger("eventflux.api.events")

router = APIRouter(tags=["events"])

_api_cfg = get_api()
_pg_cfg = get_postgres()


# ── single-row direct insert (experiment only) ────────────────────────────────

async def _direct_pg_insert(events: list) -> int:
    """
    Insert events one row at a time using individual statements.
    Intentionally slow — used to demonstrate the cost of synchronous
    per-row writes versus async batch insertion via the worker.
    """
    conn: asyncpg.Connection = await asyncpg.connect(_pg_cfg.dsn)
    try:
        sql = """
            INSERT INTO events_raw (event_type, actor_id, source, timestamp, attributes)
            VALUES ($1, $2, $3, $4::timestamptz, $5::jsonb)
        """
        for event in events:
            await conn.execute(
                sql,
                event.event_type,
                event.actor_id,
                event.source,
                event.timestamp.isoformat(),
                json.dumps(event.attributes),
            )
    finally:
        await conn.close()
    return len(events)


# ── endpoint ──────────────────────────────────────────────────────────────────

@router.post(
    "/events/bulk",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=EventBulkResponse,
    summary="Ingest a batch of telemetry events",
)
async def bulk_ingest(
    payload: BulkEventRequest,
    request: Request,
    response: Response,
    x_experiment_mode: str | None = Header(
        default=None,
        alias="X-Experiment-Mode",
        description="Set to 'bypass-queue' for direct synchronous PG writes (benchmarking only).",
    ),
) -> EventBulkResponse:
    """
    Accept a batch of telemetry events.

    **Standard path** (recommended)
    - Validates payload → enqueues to Redis Stream → returns 202 immediately.
    - Worker handles the actual DB write asynchronously in batches via COPY.

    **Bypass-queue path** (`X-Experiment-Mode: bypass-queue`)
    - Validates payload → inserts row-by-row into PostgreSQL synchronously.
    - Intentionally slow: exposes the P99 latency cost of unbatched writes.
    - Used exclusively for Phase 5 performance comparison experiments.

    **Limits**
    - Maximum `MAX_BATCH_SIZE` events (default 5 000).
    """
    if len(payload.events) > _api_cfg.max_batch_size:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"Batch too large: received {len(payload.events)}, "
                f"maximum allowed is {_api_cfg.max_batch_size}."
            ),
        )

    request_id = str(payload.request_id)
    response.headers["X-Request-ID"] = request_id

    bypass = (x_experiment_mode or "").strip().lower() == "bypass-queue"
    t0 = time.perf_counter()

    if bypass:
        # ── Slow path: direct synchronous single-row inserts ──────────────
        inserted = await _direct_pg_insert(payload.events)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        logger.info(
            "EXPERIMENT bypass-queue request_id=%s inserted=%d elapsed_ms=%.1f",
            request_id, inserted, elapsed_ms,
        )
        return EventBulkResponse(
            request_id=request_id,
            queued=inserted,
            stream_len=None,
            mode="bypass-queue",
        )

    # ── Fast path: enqueue to Redis Stream ────────────────────────────────
    queue = EventQueue(request.app.state.redis)
    message_ids = await queue.enqueue_batch(payload.events)
    queued = len(message_ids)
    elapsed_ms = (time.perf_counter() - t0) * 1000

    stream_len: int | None = None
    try:
        info = await queue.stream_info()
        stream_len = info.length
    except Exception:  # noqa: BLE001
        pass

    logger.info(
        "request_id=%s queued=%d stream_len=%s elapsed_ms=%.1f first_id=%s",
        request_id, queued, stream_len, elapsed_ms,
        message_ids[0] if message_ids else "N/A",
    )

    return EventBulkResponse(
        request_id=request_id,
        queued=queued,
        stream_len=stream_len,
        mode="queue",
    )
