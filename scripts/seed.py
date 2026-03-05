#!/usr/bin/env python3
"""
EventFlux — Seed Script

Generates and POSTs synthetic telemetry events to the ingestion API
for smoke testing and development.

Usage:
    python scripts/seed.py [--url http://localhost:8000] [--events 500] [--batch 100]
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

EVENT_TYPES = [
    "project_created",
    "api_call",
    "user_signup",
    "feature_enabled",
    "page_viewed",
    "error_raised",
    "payment_completed",
]
SOURCES = ["web", "mobile", "api", "cli", "sdk"]
REGIONS = ["us-east", "eu-west", "ap-south", "us-west", "eu-central"]
PLANS = ["free", "pro", "enterprise"]


def make_event() -> dict:
    return {
        "event_type": random.choice(EVENT_TYPES),
        "actor_id": f"user_{random.randint(1, 10_000):05d}",
        "source": random.choice(SOURCES),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "attributes": {
            "plan": random.choice(PLANS),
            "region": random.choice(REGIONS),
            "latency_ms": random.randint(1, 500),
        },
    }


def post_batch(url: str, events: list[dict]) -> dict:
    payload = json.dumps({"events": events}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed EventFlux with synthetic events")
    parser.add_argument("--url", default="http://localhost:8000", help="API base URL")
    parser.add_argument("--events", type=int, default=500, help="Total events to send")
    parser.add_argument("--batch", type=int, default=100, help="Events per request")
    args = parser.parse_args()

    endpoint = f"{args.url.rstrip('/')}/v1/events/bulk"
    total_sent = 0
    total_queued = 0

    print(f"Seeding {args.events} events → {endpoint} (batch={args.batch})")

    while total_sent < args.events:
        batch_size = min(args.batch, args.events - total_sent)
        events = [make_event() for _ in range(batch_size)]
        try:
            result = post_batch(endpoint, events)
            total_queued += result.get("queued", 0)
            total_sent += batch_size
            print(f"  Sent {total_sent}/{args.events}  queued_total={total_queued}")
        except urllib.error.URLError as exc:
            print(f"  ERROR: {exc}. Is the API running?", file=sys.stderr)
            sys.exit(1)

    print(f"\nDone. Total sent={total_sent}, total queued={total_queued}")


if __name__ == "__main__":
    main()
