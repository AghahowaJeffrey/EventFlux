"""
Integration tests for POST /v1/events/bulk and GET /healthz endpoints.

Uses fakeredis (via conftest fixtures) — no real Redis or PostgreSQL needed.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone


def event_payload(**overrides) -> dict:
    base = {
        "event_type": "api_call",
        "actor_id": "user_00042",
        "source": "web",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "attributes": {"plan": "pro", "region": "eu-west"},
    }
    base.update(overrides)
    return base


def bulk_body(n: int = 3, **overrides) -> dict:
    return {"events": [event_payload(**overrides) for _ in range(n)]}


class TestBulkIngest:
    async def test_happy_path_returns_202(self, test_client):
        resp = await test_client.post("/v1/events/bulk", json=bulk_body(5))
        assert resp.status_code == 202

    async def test_queued_count_matches_input(self, test_client):
        n = 7
        resp = await test_client.post("/v1/events/bulk", json=bulk_body(n))
        body = resp.json()
        assert body["queued"] == n

    async def test_response_contains_request_id(self, test_client):
        resp = await test_client.post("/v1/events/bulk", json=bulk_body(1))
        body = resp.json()
        assert "request_id" in body
        assert len(body["request_id"]) == 36  # UUID4 string length

    async def test_request_id_header_echoed(self, test_client):
        resp = await test_client.post("/v1/events/bulk", json=bulk_body(1))
        assert "x-request-id" in resp.headers

    async def test_client_supplied_request_id_propagated(self, test_client):
        custom_id = "550e8400-e29b-41d4-a716-446655440000"
        payload = bulk_body(1)
        payload["request_id"] = custom_id
        resp = await test_client.post("/v1/events/bulk", json=payload)
        body = resp.json()
        assert body["request_id"] == custom_id
        assert resp.headers["x-request-id"] == custom_id

    async def test_oversized_batch_returns_413(self, test_client, monkeypatch):
        # Temporarily lower the max_batch_size limit for this test
        from shared import settings as s
        original = s.get_api()
        monkeypatch.setattr(original, "max_batch_size", 2, raising=False)

        # patch what the router sees
        import ingestion.routers.events as ev_module
        monkeypatch.setattr(ev_module, "_api_cfg", original)

        resp = await test_client.post("/v1/events/bulk", json=bulk_body(3))
        assert resp.status_code == 413

    async def test_empty_events_list_returns_422(self, test_client):
        resp = await test_client.post("/v1/events/bulk", json={"events": []})
        assert resp.status_code == 422

    async def test_missing_event_type_returns_422(self, test_client):
        bad_event = event_payload()
        del bad_event["event_type"]
        resp = await test_client.post("/v1/events/bulk", json={"events": [bad_event]})
        assert resp.status_code == 422

    async def test_missing_actor_id_returns_422(self, test_client):
        bad_event = event_payload()
        del bad_event["actor_id"]
        resp = await test_client.post("/v1/events/bulk", json={"events": [bad_event]})
        assert resp.status_code == 422

    async def test_events_appear_in_redis_stream(self, test_client, fake_redis):
        n = 4
        await test_client.post("/v1/events/bulk", json=bulk_body(n))
        stream_len = await fake_redis.xlen("events:raw")
        assert stream_len == n

    async def test_stream_fields_are_correct(self, test_client, fake_redis):
        await test_client.post(
            "/v1/events/bulk",
            json={"events": [event_payload(event_type="checkout", source="mobile")]},
        )
        messages = await fake_redis.xrange("events:raw", "-", "+")
        assert len(messages) == 1
        _msg_id, fields = messages[0]
        assert fields["event_type"] == "checkout"
        assert fields["source"] == "mobile"
        assert json.loads(fields["attributes"])["plan"] == "pro"


class TestLiveness:
    async def test_liveness_returns_200(self, test_client):
        resp = await test_client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
