"""EventFlux Ingestion API — Application entry point."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ingestion.middleware import RequestIDMiddleware
from ingestion.routers import analytics, events, health
from shared.settings import get_redis

logger = logging.getLogger("eventflux.api")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage Redis connection pool lifecycle."""
    cfg = get_redis()
    redis_pool = aioredis.from_url(
        cfg.url,
        encoding="utf-8",
        decode_responses=True,
        max_connections=20,
    )
    app.state.redis = redis_pool
    logger.info("Redis pool connected: %s", cfg.url)
    yield
    await redis_pool.aclose()
    logger.info("Redis pool closed.")


app = FastAPI(
    title="EventFlux Ingestion API",
    version="0.2.0",
    description="High-throughput telemetry ingestion and analytics query service.",
    lifespan=lifespan,
)

# Middleware — order matters: RequestID runs closest to the handler
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestIDMiddleware)

# Routers
app.include_router(events.router, prefix="/v1")
app.include_router(analytics.router, prefix="/v1")
app.include_router(health.router)


@app.get("/healthz", tags=["ops"], summary="Liveness probe")
async def health_liveness() -> dict[str, str]:
    """Always returns 200 OK if the process is alive."""
    return {"status": "ok"}
