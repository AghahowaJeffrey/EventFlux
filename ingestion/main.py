"""EventFlux Ingestion API — Application entry point."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ingestion.routers import events, analytics
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
    version="0.1.0",
    description="High-throughput telemetry ingestion and analytics query service.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(events.router, prefix="/v1")
app.include_router(analytics.router, prefix="/v1")


@app.get("/healthz", tags=["ops"])
async def health() -> dict[str, str]:
    return {"status": "ok"}
