#!/usr/bin/env python3
"""
EventFlux — Query Benchmark Script (Phase 3)

Measures and compares query performance across two axes:

  1. Partitioned vs Non-Partitioned:
     Same query run against events_raw (partitioned) and events_raw_flat.
     Demonstrates partition pruning benefits.

  2. Indexed vs Non-Indexed:
     Runs EXPLAIN ANALYZE on queries that hit different indexes to show
     how composite index selection changes with filter combinations.

Prerequisites:
  - PostgreSQL running and accessible (via .env or env vars)
  - Both tables populated: use `python scripts/seed.py` first
  - Run migrations/experiments.sql to create events_raw_flat:
      psql -U eventflux -d eventflux -f migrations/experiments.sql

Usage:
  python scripts/benchmark_queries.py [--rows 50000] [--runs 3]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import time
from datetime import datetime, timedelta, timezone


# ── Config from environment (mirrors shared/settings.py without the dep) ─────
PG_DSN = (
    f"postgresql://{os.getenv('POSTGRES_USER','eventflux')}"
    f":{os.getenv('POSTGRES_PASSWORD','eventflux_secret')}"
    f"@{os.getenv('POSTGRES_HOST','localhost')}"
    f":{os.getenv('POSTGRES_PORT','5432')}"
    f"/{os.getenv('POSTGRES_DB','eventflux')}"
)

EVENT_TYPES = ["user_signup", "api_call", "page_viewed", "payment_completed", "feature_enabled"]
SOURCES = ["web", "mobile", "api", "cli"]


# ── Helpers ───────────────────────────────────────────────────────────────────

async def populate_flat_table(conn, rows: int) -> None:
    """Copy existing rows from events_raw into events_raw_flat for fair comparison."""
    result = await conn.fetchval("SELECT count(*) FROM events_raw_flat")
    if result >= rows:
        print(f"  events_raw_flat already has {result} rows — skipping populate.")
        return

    print(f"  Populating events_raw_flat with {rows} rows from events_raw…")
    await conn.execute(f"""
        INSERT INTO events_raw_flat (event_type, actor_id, source, timestamp, attributes, ingested_at)
        SELECT event_type, actor_id, source, timestamp, attributes, ingested_at
        FROM events_raw
        LIMIT {rows}
        ON CONFLICT DO NOTHING
    """)
    print("  Done.")


async def timed_query(conn, sql: str, *args) -> tuple[list, float]:
    """Run a query and return (rows, elapsed_ms)."""
    t0 = time.perf_counter()
    rows = await conn.fetch(sql, *args)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    return rows, elapsed_ms


async def explain_query(conn, sql: str, *args) -> str:
    """Return EXPLAIN ANALYZE output (first node line only)."""
    rows = await conn.fetch(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) {sql}", *args)
    return "\n".join(r[0] for r in rows[:8])


# ── Benchmark cases ───────────────────────────────────────────────────────────

BENCHMARKS = [
    {
        "name": "Daily count by event_type (timestamp range)",
        "partitioned": """
            SELECT date(timestamp) AS day, event_type, count(*)
            FROM events_raw
            WHERE timestamp >= $1 AND timestamp < $2
            GROUP BY 1, 2
            ORDER BY 1
        """,
        "flat": """
            SELECT date(timestamp) AS day, event_type, count(*)
            FROM events_raw_flat
            WHERE timestamp >= $1 AND timestamp < $2
            GROUP BY 1, 2
            ORDER BY 1
        """,
        "args_factory": lambda: (
            datetime.now(timezone.utc) - timedelta(days=3),
            datetime.now(timezone.utc),
        ),
    },
    {
        "name": "Actor event history (actor_id, timestamp index)",
        "partitioned": """
            SELECT event_type, timestamp
            FROM events_raw
            WHERE actor_id = $1
              AND timestamp >= $2
            ORDER BY timestamp DESC
            LIMIT 100
        """,
        "flat": """
            SELECT event_type, timestamp
            FROM events_raw_flat
            WHERE actor_id = $1
              AND timestamp >= $2
            ORDER BY timestamp DESC
            LIMIT 100
        """,
        "args_factory": lambda: (
            f"user_{random.randint(1, 10000):05d}",
            datetime.now(timezone.utc) - timedelta(days=7),
        ),
    },
    {
        "name": "Event type filter (event_type, timestamp index)",
        "partitioned": """
            SELECT actor_id, source, timestamp
            FROM events_raw
            WHERE event_type = $1
              AND timestamp >= $2
            LIMIT 500
        """,
        "flat": """
            SELECT actor_id, source, timestamp
            FROM events_raw_flat
            WHERE event_type = $1
              AND timestamp >= $2
            LIMIT 500
        """,
        "args_factory": lambda: (
            random.choice(EVENT_TYPES),
            datetime.now(timezone.utc) - timedelta(days=1),
        ),
    },
]


# ── Runner ────────────────────────────────────────────────────────────────────

async def run_benchmarks(runs: int, seed_rows: int) -> None:
    try:
        import asyncpg
    except ImportError:
        print("ERROR: asyncpg not installed. Run: pip install asyncpg")
        return

    print(f"\nConnecting to: {PG_DSN.split('@')[1]}")
    conn = await asyncpg.connect(PG_DSN)

    try:
        raw_count = await conn.fetchval("SELECT count(*) FROM events_raw")
        flat_count = await conn.fetchval("SELECT count(*) FROM events_raw_flat")
        print(f"\n  events_raw       rows: {raw_count:,}")
        print(f"  events_raw_flat  rows: {flat_count:,}")

        if flat_count < seed_rows // 2:
            await populate_flat_table(conn, seed_rows)

        print(f"\n{'='*70}")
        print(f"  Benchmark: {runs} run(s) per query")
        print(f"{'='*70}\n")

        results = []
        for benchmark in BENCHMARKS:
            name = benchmark["name"]
            print(f"▶  {name}")

            p_times, f_times = [], []
            for _ in range(runs):
                args = benchmark["args_factory"]()
                _, p_ms = await timed_query(conn, benchmark["partitioned"], *args)
                _, f_ms = await timed_query(conn, benchmark["flat"], *args)
                p_times.append(p_ms)
                f_times.append(f_ms)

            p_med = statistics.median(p_times)
            f_med = statistics.median(f_times)
            speedup = f_med / p_med if p_med > 0 else 0
            winner = "partition" if p_med <= f_med else "flat (unexpected!)"

            print(f"   Partitioned  median={p_med:7.2f}ms  min={min(p_times):.2f}ms  max={max(p_times):.2f}ms")
            print(f"   Flat         median={f_med:7.2f}ms  min={min(f_times):.2f}ms  max={max(f_times):.2f}ms")
            print(f"   Speedup: {speedup:.1f}x  →  winner: {winner}\n")
            results.append({"name": name, "partitioned_ms": p_med, "flat_ms": f_med, "speedup": speedup})

        # EXPLAIN ANALYZE for last benchmark so partition pruning is visible
        print(f"{'─'*70}")
        print("EXPLAIN ANALYZE — partitioned (timestamp range):")
        args = BENCHMARKS[0]["args_factory"]()
        plan = await explain_query(conn, BENCHMARKS[0]["partitioned"], *args)
        print(plan)

    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="EventFlux query benchmark")
    parser.add_argument("--runs", type=int, default=3, help="Runs per benchmark (default: 3)")
    parser.add_argument("--rows", type=int, default=50_000, help="Seed rows for flat table (default: 50000)")
    args = parser.parse_args()
    asyncio.run(run_benchmarks(args.runs, args.rows))


if __name__ == "__main__":
    main()
