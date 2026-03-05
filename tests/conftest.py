"""
EventFlux tests — shared pytest fixtures.
"""
from __future__ import annotations

import pytest
import fakeredis.aioredis as fake_aioredis
from httpx import ASGITransport, AsyncClient


@pytest.fixture()
async def fake_redis():
    """In-memory async Redis stub — no real Redis required."""
    server = fake_aioredis.FakeRedis(decode_responses=True)
    yield server
    await server.aclose()


@pytest.fixture()
async def test_client(fake_redis):
    """
    FastAPI test client with the Redis pool replaced by fakeredis.
    PostgreSQL calls (analytics router) are NOT mocked here — tests
    that hit PG should use monkeypatching or skip if PG is unavailable.
    """
    from ingestion.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Override the redis pool injected by lifespan
        app.state.redis = fake_redis
        yield client
