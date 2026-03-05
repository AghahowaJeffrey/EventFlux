"""
EventFlux — Event ingestion router.

POST /v1/events/bulk
  - Validates payload (Pydantic)
  - Enforces MAX_BATCH_SIZE
  - Serialises each event as a JSON field in a Redis Stream entry (XADD)
  - Returns 202 Accepted with a count of queued events

The endpoint is async and non-blocking; the actual DB write happens
in the separate batch worker process.
"""
from __future__ import annotations

import json
import logging
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import APIRouter, HTTPException, Request, status

from shared.models import BulkEventRequest
from shared.settings import get_api, get_redis

logger = logging.getLogger("eventflux.api.events")

router = APIRouter(tags=["events"])

_api_cfg = get_api()
_redis_cfg = get_redis()


@router.post(
    "/events/bulk",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest a batch of telemetry events",
)
async def bulk_ingest(
    payload: BulkEventRequest,
    request: Request,
) -> dict[str, int]:
    """
    Accept a batch of telemetry events and push each one onto the Redis Stream.

    Limits:
    - Minimum 1 event (enforced by Pydantic schema).
    - Maximum MAX_BATCH_SIZE events (configurable via env var, default 5 000).

    Returns the number of events that were successfully enqueued.
    """
    if len(payload.events) > _api_cfg.max_batch_size:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"Batch too large. Maximum allowed: {_api_cfg.max_batch_size} events, "
                f"received: {len(payload.events)}."
            ),
        )

    redis: aioredis.Redis = request.app.state.redis
    stream_key = _redis_cfg.stream_key

    pipe = redis.pipeline(transaction=False)
    for event in payload.events:
        pipe.xadd(
            stream_key,
            {
                "event_type": event.event_type,
                "actor_id": event.actor_id,
                "source": event.source,
                "timestamp": event.timestamp.isoformat(),
                "attributes": json.dumps(event.attributes),
            },
        )

    message_ids = await pipe.execute()
    queued = len(message_ids)

    logger.info(
        "Enqueued %d events onto stream %s (first id: %s)",
        queued,
        stream_key,
        message_ids[0] if message_ids else "N/A",
    )

    return {"queued": queued}
