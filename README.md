# EventFlux

> **A production-grade, high-throughput event ingestion and analytics pipeline.**
> Designed to demonstrate the architectural patterns, tradeoffs, and operational constraints that apply at FAANG scale — built from first principles.

---

## Table of Contents

1. [What Is EventFlux?](#1-what-is-eventflux)
2. [Architecture Overview](#2-architecture-overview)
3. [Service Breakdown](#3-service-breakdown)
4. [Database Design](#4-database-design)
5. [Redis Streams Design](#5-redis-streams-design)
6. [Ingestion API Design](#6-ingestion-api-design)
7. [Batch Worker Design](#7-batch-worker-design)
8. [Aggregation Strategy](#8-aggregation-strategy)
9. [Query API Design](#9-query-api-design)
10. [Caching Layer](#10-caching-layer)
11. [Performance Experiments](#11-performance-experiments)
12. [Observability](#12-observability)
13. [Operational Concerns](#13-operational-concerns)
14. [Getting Started](#14-getting-started)
15. [Configuration Reference](#15-configuration-reference)
16. [Development Workflow](#16-development-workflow)
17. [Load Testing](#17-load-testing)
18. [Study Guide: Key Concepts](#18-study-guide-key-concepts)

---

## 1. What Is EventFlux?

EventFlux is an **event ingestion and analytics system** designed to receive millions of telemetry events per day, store them durably in a partitioned PostgreSQL table, aggregate them into daily rollup tables, and expose a query API for analytics dashboards.

**Problem statement:** You have hundreds of client applications emitting structured events (API calls, user signups, payments, feature usage) at high velocity. You need to:

1. Accept events with sub-10ms API latency at high concurrency.
2. Persist every event reliably without data loss.
3. Serve aggregated analytics queries in under 50ms.
4. Scale writes and reads independently.

**Scale targets:**

| Metric | Target |
|---|---|
| Ingestion throughput | 50,000+ events/second (burst) |
| API p99 latency | < 15ms (write path) |
| Query latency | < 50ms (pre-aggregated), < 500ms (raw) |
| Data retention | Configurable (default: indefinite with partition-drop policy) |
| Daily unique events | 10M–1B |

---

## 2. Architecture Overview

```
                           ┌─────────────────────────────────────────────────┐
                           │                  Clients                        │
                           │  (mobile · web · server SDKs · IoT devices)     │
                           └───────────────────┬─────────────────────────────┘
                                               │ HTTPS POST /v1/events/bulk
                                               ▼
                           ┌─────────────────────────────────────────────────┐
                           │             Ingestion API (FastAPI)             │
                           │                                                 │
                           │  • Pydantic validation (schema enforcement)     │
                           │  • X-Request-ID middleware (end-to-end tracing) │
                           │  • Batch size enforcement (max 5,000 events)    │
                           │  • EventQueue.enqueue_batch() → XADD pipeline   │
                           │  • /healthz/detailed (Redis + PG dependency)    │
                           └──────────────────┬──────────────────────────────┘
                                              │ Redis XADD (pipelined)
                                              ▼
                           ┌─────────────────────────────────────────────────┐
                           │          Redis Streams  (events:raw)            │
                           │                                                 │
                           │  • MAXLEN ~1,000,000 (backpressure safety)      │
                           │  • Consumer group: batch_workers                │
                           │  • Dead-letter stream: events:dead              │
                           └──────────────────┬──────────────────────────────┘
                                              │ XREADGROUP
                                              ▼
                           ┌─────────────────────────────────────────────────┐
                           │            Batch Worker (asyncio)               │
                           │                                                 │
                           │  • PEL recovery (XAUTOCLAIM) on startup         │
                           │  • Buffer flush: 500 events OR 2 seconds        │
                           │  • asyncpg COPY → PostgreSQL (fastest path)     │
                           │  • Aggregation scheduler (dual-strategy)        │
                           │  • Partition manager (creates daily partitions) │
                           └──────────────────┬──────────────────────────────┘
                                              │ COPY / INSERT
                                              ▼
                           ┌─────────────────────────────────────────────────┐
                           │              PostgreSQL 16                      │
                           │                                                 │
                           │  events_raw              (partitioned by day)   │
                           │  analytics_daily_event_counts  (rollup table)   │
                           │  events_raw_flat          (experiment baseline)  │
                           └──────────────────────────────────────────────────┘
                                              ▲
                                         SQL queries
                           ┌──────────────────┴──────────────────────────────┐
                           │      Analytics Query API  (FastAPI)             │
                           │                                                 │
                           │  GET /v1/analytics/events/daily                 │
                           │  GET /v1/analytics/events/summary               │
                           │  GET /v1/analytics/event-types                  │
                           │  GET /v1/analytics/events/raw                   │
                           │         ↕ Redis cache (TTL-based)               │
                           └─────────────────────────────────────────────────┘
```

**Core pattern:** Write path and read path are fully decoupled.

- **Write path:** Clients → API → Redis Stream → Worker → PostgreSQL
- **Read path:** Clients → API → Redis Cache → PostgreSQL

---

## 3. Service Breakdown

### 3.1 Ingestion API (`ingestion/`)

A **FastAPI** service running under Uvicorn. Stateless — can be horizontally scaled behind a load balancer.

**Responsibilities:**
- Validate incoming events against the Pydantic schema
- Push events to Redis Streams via a pipelined `XADD`
- Return a 202 Accepted immediately (non-blocking write path)
- Expose `/healthz/detailed` for load balancer health checks

**Why FastAPI?**
- Native async/await reduces thread overhead compared to Django or Flask.
- Pydantic v2 validation runs in Rust — schema enforcement at negligible latency cost.
- Automatic OpenAPI doc generation (visit `/docs` when running).

### 3.2 Batch Worker (`worker/`)

A **Python asyncio** daemon consuming from Redis Streams. Single-instance by default; can be scaled horizontally with multiple consumer group members.

**Responsibilities:**
- Pull messages from `events:raw` consumer group
- Buffer events and flush to PostgreSQL via `asyncpg COPY`
- Run incremental + daily aggregation on a schedule
- Manage daily partitions (create next 7 days ahead of time)
- Recover stale messages from the PEL on startup

### 3.3 Shared Layer (`shared/`)

Python package shared by both services:

| Module | Purpose |
|---|---|
| `shared/models.py` | Pydantic models: `Event`, `BulkEventRequest`, `EventBulkResponse` |
| `shared/settings.py` | Typed config from environment variables (`pydantic-settings`) |
| `shared/queue.py` | `EventQueue` class — Redis Streams abstraction |
| `shared/cache.py` | Redis cache primitives + `@cached` async decorator |

---

## 4. Database Design

### 4.1 Partitioned Event Storage

```sql
CREATE TABLE events_raw (
    id            BIGSERIAL,
    event_type    TEXT        NOT NULL,
    actor_id      TEXT        NOT NULL,
    source        TEXT        NOT NULL,
    timestamp     TIMESTAMPTZ NOT NULL,   -- event time (client-reported)
    attributes    JSONB       NOT NULL DEFAULT '{}',
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now()  -- server arrival time
) PARTITION BY RANGE (timestamp);
```

**Why time-based partitioning?**

Events have a natural temporal access pattern: queries almost always include a `WHERE timestamp >= X` predicate. PostgreSQL uses **partition pruning** — it reads only the child table(s) whose range intersects the query window. For a 1-day query against a 2-year dataset:

```
Without partitioning: scans index over ~730M rows
With partitioning:    scans index over ~2M rows (single daily partition)
```

**Constraint: no global UNIQUE**

Declarative partitioning restricts `UNIQUE`/`PRIMARY KEY` constraints to include the partition key. We use `BIGSERIAL` for a global monotonic ID, but uniqueness is not enforced across partitions. This is an acceptable tradeoff — we treat events as append-only immutable facts (idempotency is handled at the producer level via `request_id`).

**Partition lifecycle:**

The `partition_manager` coroutine runs on startup and hourly, pre-creating the next 7 days of partitions:

```sql
CREATE TABLE IF NOT EXISTS events_raw_2026_03_08
    PARTITION OF events_raw
    FOR VALUES FROM ('2026-03-08 00:00:00+00') TO ('2026-03-09 00:00:00+00');
```

A `DEFAULT` partition catches any events whose timestamp falls outside existing partitions (e.g., from clients with badly synced clocks). Monitor its size — a large default partition is a warning sign.

**Old partition retention:**

To implement data retention (e.g., keep 90 days), drop partitions:

```sql
-- Safe: detach first (instant), then drop asynchronously
ALTER TABLE events_raw DETACH PARTITION events_raw_2025_12_01;
DROP TABLE events_raw_2025_12_01;
```

This is why partitioning is preferred over a `DELETE WHERE timestamp < X` — dropping a partition is O(1), a DELETE is O(rows).

### 4.2 Index Design

Per partition, three composite indexes are created:

```sql
CREATE INDEX ON events_raw_<date> (timestamp);
CREATE INDEX ON events_raw_<date> (event_type, timestamp);
CREATE INDEX ON events_raw_<date> (actor_id,   timestamp);
```

**Why composite indexes, not single-column?**

The index `(event_type, timestamp)` satisfies queries like:

```sql
WHERE event_type = 'api_call' AND timestamp >= '2026-03-07'
```

PostgreSQL can use an **Index Scan** rather than filtering a timestamp index post-scan. The leading column (`event_type`) handles equality, the trailing column (`timestamp`) handles range — the query planner exploits both.

**Why not `(timestamp, event_type)`?** The range predicate is on `timestamp`. If `timestamp` leads, the planner must scan all rows in the range and then filter for `event_type` — losing the composite benefit for equality lookups.

### 4.3 Analytics Rollup Table

```sql
CREATE TABLE analytics_daily_event_counts (
    day        DATE   NOT NULL,
    event_type TEXT   NOT NULL,
    source     TEXT   NOT NULL,
    count      BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (day, event_type, source)
);
```

**Motivation:** Raw events cannot be queried efficiently for dashboard use at scale. A query like "how many `api_call` events happened last month, grouped by source?" would scan tens of millions of rows. The rollup table stores pre-computed totals — queries return in < 5ms regardless of underlying data volume.

**Tradeoff:** Eventual consistency. The rollup is updated every `AGGREGATION_INTERVAL_S` seconds (default: 60). Dashboard data is at most 60 seconds stale. This is acceptable for analytics (not acceptable for billing or audit logs).

### 4.4 JSONB `attributes` Field

```json
{"plan": "pro", "region": "eu-west", "ab_variant": "control"}
```

**Why JSONB over structured columns?**

Events from different sources have heterogeneous metadata. Adding a column for every possible attribute leads to wide, sparse tables. JSONB:

- Stores arbitrary key-value pairs without schema migrations
- Supports GIN indexes for efficient key/value lookups
- Is stored as decomposed binary (not text) — fast for reads

**Constraint:** JSONB `attributes` is not indexed by default. If you frequently filter by a specific attribute key (e.g., `attributes->>'region'`), add a targeted expression index:

```sql
CREATE INDEX ON events_raw ((attributes->>'region'));
```

---

## 5. Redis Streams Design

### 5.1 Why Redis Streams?

**Alternatives considered:**

| Option | Pros | Cons |
|---|---|---|
| **Redis Streams** | Low ops overhead, built into existing Redis, persistent, consumer groups | Single-node bottleneck, limited replay window |
| **Apache Kafka** | Horizontally scalable, long retention, exactly-once semantics | High operational complexity, requires ZooKeeper/KRaft |
| **RabbitMQ** | Mature, flexible routing, good client support | Fan-out semantics; at-scale partitioning is complex |
| **Direct DB insert** | Simplest path | Blocks the API on every write; no buffering; at high concurrency locks become a bottleneck |

**Decision:** Redis Streams at this scale (< 1M events/hour sustained) avoids Kafka's operational burden while providing durable consumer groups, message acknowledgement, and replay capability.

**When to migrate to Kafka:** If you need multi-datacenter replication, > 10M events/second sustained, or retention longer than Redis allows. The `EventQueue` abstraction in `shared/queue.py` is the seam — swap out the implementation without touching the API.

### 5.2 Stream Design Choices

**`MAXLEN ~1,000,000` (approximate trim):**

```python
await redis.xadd(stream_key, fields, maxlen=1_000_000, approximate=True)
```

Approximate trim (`~`) performs amortized pruning instead of exact pruning every insert, which avoids the O(n) cost of precise eviction on every write. The `~` flag means the stream will be trimmed to *approximately* MAXLEN — within a radix tree node boundary — which is acceptable.

This also acts as **backpressure**: if the worker falls behind (e.g., PG is slow), the stream stops growing indefinitely. When it hits MAXLEN, new entries evict old unread ones. This is a **best-effort** guarantee — for **exactly-once** semantics you'd need persistent Kafka consumer offsets.

**Consumer Groups:**

```python
XREADGROUP GROUP batch_workers worker-1 COUNT 500 BLOCK 2000 STREAMS events:raw >
```

`">"` means "only give me new messages not yet delivered to this group." This enables horizontal scaling: add `worker-2`, `worker-3` and each pulls a distinct subset of messages.

### 5.3 Pending Entry List (PEL) Recovery

Every message read via `XREADGROUP` enters the **PEL** — a per-consumer ledger of "delivered but not yet ACKed" messages. If the worker crashes mid-batch, the PEL retains those messages indefinitely.

On startup, the worker calls `XAUTOCLAIM` to reclaim messages idle in the PEL for > 5 minutes:

```python
redis.xautoclaim(stream_key, group, consumer, min_idle_time=300_000, start_id="0-0")
```

Messages that have been delivered `> MAX_DELIVERY_ATTEMPTS` (3) times are moved to `events:dead` (the dead-letter stream) and ACKed from the main stream — breaking the poison-pill loop.

**PEL failure modes if you skip this:**
- Crash during a flush → those events are never inserted → silent data loss
- Or they stay in PEL indefinitely → memory leak in Redis

### 5.4 Dead-Letter Stream

```
events:dead  → stores the original message + {_original_id, _reason}
```

Operators can inspect dead-lettered messages with:

```bash
docker compose exec redis redis-cli XRANGE events:dead - +
```

And replay them after a fix:

```bash
# Manual replay: re-add to events:raw
redis-cli XRANGE events:dead - + | # parse and XADD back to events:raw
```

---

## 6. Ingestion API Design

### 6.1 The 202 Accepted Pattern

The API returns `202 Accepted` immediately after enqueuing to Redis — it does **not** wait for the database write. This is the correct pattern for write-heavy pipelines:

```
Client → API (< 5ms) → Redis (~1ms) → 202 returned
                           ↓
                        Worker → PostgreSQL (~20ms per batch)
```

**Why not 201 Created?** `201` implies the resource now exists in the database. It doesn't — it's in the queue. `202` is the correct semantic: "received and will be processed."

**Tradeoff:** If Redis crashes between `202` and the worker ACKing, those events are lost. Mitigations:
- Redis AOF persistence (fsync per operation — increases latency)
- Redis RDB + AOF hybrid (fsync every second — reduces durability window to 1 second)
- Double-write to Redis + local disk (complex, rarely worth it at this scale)

### 6.2 Request-ID Tracing

```
Client sends:    X-Request-ID: 550e8400-e29b-41d4-a716-446655440000
API echoes:      X-Request-ID: 550e8400-e29b-41d4-a716-446655440000  (in response)
Response body:   {"request_id": "550e8400-...", "queued": 50}
```

If the client doesn't provide an ID, the `RequestIDMiddleware` generates a UUID4. The same ID flows through:

1. API request logs
2. Redis stream message fields
3. Response body and header

This enables cross-service log correlation: grep `request_id=550e8400` across `api`, `worker`, and PostgreSQL query logs.

### 6.3 Batch Size Enforcement

```python
if len(payload.events) > _api_cfg.max_batch_size:
    raise HTTPException(status_code=413, ...)
```

**Why 413 (Payload Too Large)?** This is the semantically correct HTTP status — the request body exceeds a server-defined limit. The client should split into smaller batches.

**Why enforce a max batch size?** Un-capped batches can:
- Consume all Redis pipeline memory
- Cause the worker's flush to time out
- Allow a single client to monopolize the stream

Default is 5,000 events per request. A client sending 100,000 events should send 20 requests of 5,000.

### 6.4 Health Checks

```
GET /healthz            → 200 {"status": "ok"}  (used by Docker, load balancers)
GET /healthz/detailed   → 200/503 with per-dependency status
```

The detailed endpoint checks:
- Redis PING
- Redis stream length (backpressure warning)
- Consumer group lag
- PostgreSQL connectivity

Return `503` if any dependency is unhealthy — this causes the load balancer to remove this instance from rotation, preventing traffic to a degraded pod.

---

## 7. Batch Worker Design

### 7.1 Why asyncio (not threads)?

The worker's bottlenecks are I/O: reading from Redis, writing to PostgreSQL. asyncio cooperative multitasking handles thousands of concurrent I/O operations on a single thread more efficiently than OS threads (no GIL contention, no context-switch overhead, no lock management).

**Concurrency model:**

```
asyncio.TaskGroup (Python 3.11+)
  ├── ingest_loop      (read Redis → buffer → flush to PG)
  ├── aggregation_loop (incremental + daily aggregation)
  └── partition_manager_loop (ensure future partitions exist)
```

`TaskGroup` ensures all tasks are cancelled cleanly on shutdown — no zombie coroutines.

### 7.2 COPY vs executemany vs single-row INSERT

| Strategy | Throughput | Latency | Use case |
|---|---|---|---|
| `COPY` via `asyncpg` | ~50,000 rows/s | Low (bulk) | **Default — production path** |
| `executemany` | ~5,000 rows/s | Medium | Experiment toggle (`WORKER_SINGLE_ROW_MODE=true`) |
| Single-row INSERT | ~500 rows/s | High | API bypass-queue mode (benchmarking only) |

**Why COPY is fastest:**

PostgreSQL's `COPY` protocol bypasses the query planner and row-by-row WAL logging. Data is streamed directly into the storage layer in binary format. For batches of 500+ rows, it's 5–10× faster than `executemany`.

**asyncpg `copy_records_to_table`:**

```python
await conn.copy_records_to_table(
    "events_raw",
    records=records,   # list of Python tuples
    columns=["event_type", "actor_id", "source", "timestamp", "attributes"],
)
```

This sends data over the wire once per batch, not once per row.

**Constraint:** COPY cannot be easily rolled back on partial failure (unlike a transaction with multiple INSERTs). If COPY fails mid-batch, the messages remain in the PEL and will be retried — potentially causing duplicate inserts. To make inserts idempotent, you'd need a unique constraint on `(actor_id, event_type, timestamp)` which is expensive on a partitioned table.

**Decision:** Accept rare duplicates (best-effort delivery). For exact-once semantics, add a deduplication step or use a unique event ID in `attributes`.

### 7.3 Flush Strategy

```python
should_flush = (
    len(buffer) >= batch_size      # size-based: 500 events
    or elapsed  >= flush_interval  # time-based: 2 seconds
)
```

The **size + time dual trigger** ensures:
- At high throughput: flush triggers on batch fill (minimize COPY overhead per row)
- At low throughput: flush triggers on timeout (minimize latency for infrequent events)

**Why not flush-on-every-read?** A Redis `XREADGROUP` pulling 500 events followed by an immediate flush creates one COPY call per Redis read — good. But at low volume, `XREADGROUP` returns 1 event after a 2-second block timeout. Without time-based flush, that 1 event would wait until the buffer fills to 500 — adding up to `500 × 2s = 1000s` of latency.

---

## 8. Aggregation Strategy

### 8.1 The Bug: Replace vs Accumulate

A common mistake in UPSERT-based aggregation:

```sql
-- ❌ WRONG — replaces existing count
ON CONFLICT DO UPDATE SET count = EXCLUDED.count

-- ✅ CORRECT (full recompute — authoritative window)
ON CONFLICT DO UPDATE SET count = EXCLUDED.count  -- OK when scanning the full day

-- ✅ CORRECT (incremental — adds the delta)
ON CONFLICT DO UPDATE SET count = analytics_daily_event_counts.count + EXCLUDED.count
```

The wrong version causes silent data loss: if you aggregate at 8:00am and get `count=500`, then re-aggregate at 9:00am on only the last hour and get `count=100` — the upsert *replaces* 500 with 100. You've lost 500 events from your count.

### 8.2 Dual-Strategy Scheduler

```
Every AGGREGATION_INTERVAL_S (60s):
  → run_incremental_aggregation(pool, watermark)
    - Scans only events with ingested_at > watermark
    - Adds deltas: count = existing + new_count
    - Updates watermark to now()

Every ~1 hour (3600 / interval ticks):
  → run_daily_aggregation(pool)
    - Full recompute for today and yesterday
    - Uses REPLACE semantics (safe for a full window)
    - Heals any gaps from missed incremental runs
```

**Why both?** The incremental path is cheap and keeps data fresh (< 60s stale). The daily recompute is authoritative — it corrects any drift caused by late-arriving events (events with `timestamp` in the past but `ingested_at` now), backfills, or bugs in the incremental path.

**Late-arriving events:** If a client is offline and batches events from 3 hours ago, those events will have old `timestamp` but current `ingested_at`. The incremental job picks them up (it scans by `ingested_at`), but adds them to today's `day` bucket if the `timestamp` maps to today — which is correct unless the client's event is also timestamped in the past.

**Solution for true late arrivals:** The hourly full recompute covers this case — it recomputes the last 2 days from `events_raw`, which will include late-arriving events regardless of when they were ingested.

---

## 9. Query API Design

### 9.1 Pre-aggregated vs Raw

| Endpoint | Source table | Typical latency | When to use |
|---|---|---|---|
| `/events/daily` | `analytics_daily_event_counts` | < 5ms | Dashboards, reporting |
| `/events/summary` | `analytics_daily_event_counts` | < 10ms | KPI widgets |
| `/events/raw` | `events_raw` | 10–500ms | Debugging, ad-hoc |

**Never expose unbounded raw queries to end users.** The `/events/raw` endpoint enforces a 1-hour window — returning 400 if the client requests a wider range. The `LIMIT 5000` further caps result size. Without these guards, a single query can scan billions of rows and take minutes.

### 9.2 Cursor-based Pagination

```json
// Response
{"data": [...500 rows...], "count": 500, "next_cursor": "2026-03-05"}

// Next page request
GET /v1/analytics/events/daily?after_day=2026-03-05&limit=500
```

**Why cursor-based instead of offset?**

- `OFFSET 500` forces PostgreSQL to **scan and discard the first 500 rows** before returning results. At page 100 (`OFFSET 50000`), performance degrades significantly.
- Cursor-based pagination uses a `WHERE day < cursor` predicate — the index makes this O(log n) regardless of how deep in the result set you are.

**Tradeoff:** Cursor pagination doesn't support jumping to an arbitrary page number (page 42 of 200). This is acceptable for analytics dashboards that scroll chronologically.

---

## 10. Caching Layer

### 10.1 Cache Architecture

```
Request → Check Redis cache (key: cache:{namespace}:{sha256(params)[:12]})
              ↓ HIT                           ↓ MISS
          Return JSON                  Query PostgreSQL
                                       Store in Redis (TTL)
                                       Return JSON
```

**Key design:** `cache:daily:a3f9c2e1b8d4` — deterministic, short, collision-resistant.

The SHA-256 digest of sorted query params ensures:
- Same params → same key (cache hit)
- Different params → different key (no cross-contamination)
- Params are sorted before hashing — `?event_type=foo&source=web` and `?source=web&event_type=foo` produce the same cache key

### 10.2 TTL Strategy

| Endpoint | TTL | Rationale |
|---|---|---|
| `/events/daily` | 300s | Aggregation runs every 60s; 5min is 5× the freshness cycle |
| `/events/summary` | 120s | Heavier computation; 2min acceptable for KPI widgets |
| `/event-types` | 600s | New event types appear rarely; 10min is safe |
| `/events/raw` | 30s | Near-realtime debugging; short TTL to avoid stale data |

### 10.3 Graceful Degradation

If Redis is unavailable, the `@cached` decorator swallows the exception, executes the function normally, and logs a warning. The API returns correct data — just slower (direct PG query every time).

```python
async def cache_get(redis, key):
    try:
        ...
    except Exception as exc:
        logger.warning("Cache GET error: %s", exc)  # Don't crash
        return None
```

This is the correct production pattern: **never let the cache layer take down the data layer**. The cache is a performance optimization, not a correctness requirement.

### 10.4 Cache Invalidation

EventFlux uses **TTL-based expiration only** — no active invalidation on write.

**When is this a problem?** If you run a manual backfill and want the dashboard to reflect updated counts immediately, the old cached response will serve for up to 300 seconds.

**Manual invalidation:**

```python
from shared.cache import cache_delete
await cache_delete(redis, "cache:daily:*")
```

Or via Redis CLI:

```bash
redis-cli KEYS "cache:daily:*" | xargs redis-cli DEL
```

---

## 11. Performance Experiments

### 11.1 Partitioned vs Non-Partitioned

`migrations/experiments.sql` creates `events_raw_flat` — an identical non-partitioned table. Run `scripts/benchmark_queries.py` to compare:

```bash
python scripts/benchmark_queries.py --rows 1000000 --runs 5
```

**Expected result:**

| Query type | Partitioned | Flat | Speedup |
|---|---|---|---|
| 1-day range scan | ~3ms | ~40ms | ~13× |
| Actor history | ~1ms | ~8ms | ~8× |
| Event type filter | ~2ms | ~15ms | ~7× |

The speedup comes entirely from partition pruning — PostgreSQL reads 1 partition instead of scanning the entire table.

### 11.2 Batch vs Single-Row Write

```bash
# Compare COPY (default) vs single-row INSERT (experiment)

# 1. Run with COPY (default)
WORKER_SINGLE_ROW_MODE=false docker compose up worker

# 2. Run with single-row
WORKER_SINGLE_ROW_MODE=true docker compose up worker

# Observe in worker logs:
# mode=copy     → ~25,000 events/s
# mode=single-row → ~2,500 events/s
```

### 11.3 API Bypass vs Queue Path

```bash
# Standard path (Redis queue → async COPY)
time curl -s -X POST http://localhost:8000/v1/events/bulk \
  -H "Content-Type: application/json" \
  -d '{"events": [<1000 events>]}'
# → ~3ms total

# Bypass path (synchronous single-row PG writes)
time curl -s -X POST http://localhost:8000/v1/events/bulk \
  -H "X-Experiment-Mode: bypass-queue" \
  -H "Content-Type: application/json" \
  -d '{"events": [<1000 events>]}'
# → ~800ms total (1000 round trips to PG)
```

This experiment demonstrates why the queue exists — the API latency at high event counts is unacceptable without buffering.

---

## 12. Observability

### 12.1 Structured Logging

All services use Python's `logging` module with a structured format:

```
2026-03-07 04:00:00 INFO eventflux.worker — Flushed batch: inserted=500 mode=copy duration=0.021s rate=23809 events/s
2026-03-07 04:00:02 INFO eventflux.worker.aggregation — Incremental aggregation: upserted 12 rows
2026-03-07 04:00:02 INFO eventflux.api.events — request_id=550e8400 queued=50 stream_len=1242 elapsed_ms=4.2
```

**Log fields to alert on:**
- `mode=single-row` in worker logs (unexpected if experiment is off)
- `Flush failed` — indicates PG connectivity issue
- `Dead-lettered message` — indicates a poison-pill event
- `Cache GET error` — indicates Redis connectivity issue

### 12.2 Health Endpoint

```bash
curl http://localhost:8000/healthz/detailed
```

```json
{
  "api": "ok",
  "redis": {
    "ping": "ok",
    "stream_len": 1242,
    "consumer_lag": 0
  },
  "postgres": "ok"
}
```

`HTTP 503` is returned if any dependency is unhealthy. This integrates directly with:
- Docker health checks (`HEALTHCHECK` in Dockerfile)
- Kubernetes liveness/readiness probes
- Load balancer target group health checks (AWS ALB, GCP GLB)

### 12.3 Key Metrics to Track (Production)

| Metric | Source | Alert condition |
|---|---|---|
| `events:raw` stream length | Redis XLEN | > 500,000 (worker falling behind) |
| Consumer group lag | XINFO GROUPS | > 10,000 (risk of data loss from MAXLEN trim) |
| Worker flush rate (events/s) | Worker logs | Drop > 50% from baseline |
| Aggregation staleness | `analytics_daily_event_counts.updated_at` | > 5 min from now |
| API p99 latency | Application metrics | > 50ms |
| PG connection pool wait | asyncpg pool | > 100ms |

---

## 13. Operational Concerns

### 13.1 Partition Management

The `partition_manager` ensures partitions exist 7 days ahead. If it fails (or the worker stops running), partitions won't be pre-created. Events will land in the `DEFAULT` partition, which:

1. Has no targeted indexes → slower queries
2. Cannot be cleanly dropped for retention without migrating rows

**Detection:** Run `SELECT count(*) FROM events_raw_default;` and alert if > 0.

### 13.2 Scaling the Worker

Multiple worker instances can consume from the same consumer group — Redis delivers each message to exactly one consumer:

```yaml
# docker-compose.yml — scale worker to 3 instances
worker:
  deploy:
    replicas: 3
```

**Constraint:** Each worker instance must have a unique `CONSUMER_NAME`. Currently hardcoded to `"worker-1"`. In production, use `hostname` or inject via environment:

```python
CONSUMER_NAME = os.getenv("CONSUMER_NAME", socket.gethostname())
```

### 13.3 Scaling the API

The API is fully stateless (Redis connection is managed per-request). Scale horizontally without configuration changes:

```bash
docker compose up --scale api=4
```

Place an Nginx or AWS ALB in front for load distribution.

### 13.4 Data Retention

Drop old partitions to reclaim disk space:

```sql
-- Safe two-step: detach then drop
ALTER TABLE events_raw DETACH PARTITION events_raw_2025_01_01;
DROP TABLE events_raw_2025_01_01;
```

Automate this with a cron job or extend `partition_manager.py` to drop partitions older than N days.

### 13.5 Redis Memory Management

With `MAXLEN ~1,000,000`, the stream can consume up to ~500MB of Redis memory. Monitor with:

```bash
redis-cli INFO memory | grep used_memory_human
redis-cli XLEN events:raw
```

If Redis memory pressure is high, reduce `MAXLEN` or increase the worker's flush speed.

---

## 14. Getting Started

### Prerequisites

- Docker + Docker Compose v2
- Python 3.11+ (for running scripts/tests locally)
- k6 (for load testing)

### Quick Start

```bash
# 1. Clone and configure
git clone https://github.com/AghahowaJeffrey/EventFlux.git
cd eventflux
cp .env.example .env

# 2. Start all services
make up
# Equivalent: docker compose up --build -d

# 3. Wait for health checks (PostgreSQL takes ~5s to initialise)
make logs   # Watch until you see "Application startup complete"

# 4. Verify the API is healthy
curl http://localhost:8000/healthz
# → {"status": "ok"}

curl http://localhost:8000/healthz/detailed
# → {"api": "ok", "redis": {...}, "postgres": "ok"}

# 5. Seed 1,000 test events
make seed   # Calls: python scripts/seed.py --events 1000 --batch 100

# 6. Check events arrived in Redis
docker compose exec redis redis-cli XLEN events:raw

# 7. Wait ~5 seconds for the worker to flush, then check PG
docker compose exec postgres psql -U eventflux -d eventflux \
  -c "SELECT count(*) FROM events_raw;"

# 8. Check aggregation (runs every 60s)
curl "http://localhost:8000/v1/analytics/events/daily"
```

### Apply Experiment Schema

```bash
# Required for benchmark_queries.py
docker compose exec postgres psql -U eventflux -d eventflux \
  -f /docker-entrypoint-initdb.d/experiments.sql
```

---

## 15. Configuration Reference

All settings are read from environment variables. Defaults are set in `shared/settings.py`.

### PostgreSQL

| Variable | Default | Description |
|---|---|---|
| `POSTGRES_HOST` | `postgres` | Host (service name in Docker) |
| `POSTGRES_PORT` | `5432` | Port |
| `POSTGRES_DB` | `eventflux` | Database name |
| `POSTGRES_USER` | `eventflux` | Username |
| `POSTGRES_PASSWORD` | `eventflux_secret` | Password |

### Redis

| Variable | Default | Description |
|---|---|---|
| `REDIS_HOST` | `redis` | Host |
| `REDIS_PORT` | `6379` | Port |
| `REDIS_STREAM_KEY` | `events:raw` | Main stream name |
| `REDIS_CONSUMER_GROUP` | `batch_workers` | Consumer group name |

### Ingestion API

| Variable | Default | Description |
|---|---|---|
| `API_HOST` | `0.0.0.0` | Listen address |
| `API_PORT` | `8000` | Listen port |
| `API_MAX_BATCH_SIZE` | `5000` | Max events per bulk request (413 if exceeded) |

### Batch Worker

| Variable | Default | Description |
|---|---|---|
| `WORKER_BATCH_SIZE` | `500` | Max events to buffer before flushing |
| `WORKER_FLUSH_INTERVAL_S` | `2.0` | Max seconds between flushes |
| `WORKER_MAX_BLOCK_MS` | `2000` | XREADGROUP block timeout (milliseconds) |
| `WORKER_SINGLE_ROW_MODE` | `false` | **Experiment only:** use executemany instead of COPY |

### Aggregation

| Variable | Default | Description |
|---|---|---|
| `AGGREGATION_INTERVAL_S` | `60` | Seconds between incremental aggregation runs |

### Experiment Flags

| Flag | How to activate | Effect |
|---|---|---|
| Single-row inserts | `WORKER_SINGLE_ROW_MODE=true` | Worker uses `executemany` instead of COPY |
| Bypass queue | `X-Experiment-Mode: bypass-queue` header | API writes directly to PG synchronously |

---

## 16. Development Workflow

```bash
make up          # Start all services
make down        # Stop and remove containers
make build       # Rebuild images
make logs        # Tail all service logs

make psql        # Open psql shell in the Postgres container
make migrate     # Run migrations/init.sql

make seed        # Seed 1,000 events (configurable in Makefile)

make load-test-scenario1   # k6 sustained load
make load-test-scenario2   # k6 spike load

# Run tests locally (requires Python env with test deps)
pip install -r tests/requirements.txt
pytest tests/ -v
```

---

## 17. Load Testing

**Tool:** [k6](https://k6.io) — a modern load testing tool written in Go.

**Scenarios** (`load_tests/scenarios/bulk_ingest.js`):

| Scenario | Pattern | VUs | Duration |
|---|---|---|---|
| Scenario 1: Sustained | Ramp up → hold | 50 | 5 min |
| Scenario 2: Spike | Sudden burst | 200 | 30 sec |

**Metrics tracked:**
- `ingest_error_rate` — custom counter for non-202 responses
- `ingest_duration` — custom histogram for response time
- Standard k6: `http_req_duration`, `http_reqs`, `data_received`

**Running:**

```bash
# Ensure services are running
make up

# Scenario 1: sustained 50 VUs for 5 minutes
make load-test-scenario1

# Scenario 2: spike to 200 VUs
make load-test-scenario2
```

**Interpreting results:**

- `http_req_duration p(95) < 15ms` → ingestion API is healthy
- `http_req_duration p(95) > 50ms` → check Redis pipeline latency and worker lag
- `ingest_error_rate > 0.01` → investigate 4xx/5xx errors in API logs

---

## 18. Study Guide: Key Concepts

This section connects the implementation to the foundational computer science and systems concepts it demonstrates.

### Database Concepts

| Concept | Where in EventFlux |
|---|---|
| **Table partitioning** | `events_raw` partitioned by `timestamp` (RANGE) |
| **Partition pruning** | Automatic with `WHERE timestamp >= X` queries |
| **Composite indexes** | `(event_type, timestamp)` covers equality + range |
| **UPSERT semantics** | `ON CONFLICT DO UPDATE` in aggregation |
| **Incremental accumulation** | `count + EXCLUDED.count` (not `= EXCLUDED.count`) |
| **EXPLAIN ANALYZE** | Used in `benchmark_queries.py` to verify pruning |
| **Connection pooling** | `asyncpg.create_pool(min_size=2, max_size=10)` |
| **COPY protocol** | Fastest PostgreSQL bulk load — bypasses row-level WAL |

### Distributed Systems Concepts

| Concept | Where in EventFlux |
|---|---|
| **Write buffering / batching** | Worker buffers 500 events before COPY flush |
| **Backpressure** | Redis MAXLEN ~1M; stream growth is bounded |
| **At-least-once delivery** | PEL + XACK ensures no silent message loss |
| **Dead-letter queues** | `events:dead` for poison-pill message isolation |
| **Idempotent writes** | `request_id` for deduplication; partition manager `IF NOT EXISTS` |
| **Eventual consistency** | Aggregation is 60s stale; raw data is authoritative |
| **Circuit breaker pattern** | `@cached` degrades gracefully if Redis is down |
| **Watermark-based streaming** | `ingested_at` watermark in incremental aggregation |

### API Design Concepts

| Concept | Where in EventFlux |
|---|---|
| **202 Accepted** | Non-blocking write acknowledgement |
| **Cursor pagination** | `after_day` replaces OFFSET for O(log n) paging |
| **Rate limiting (implicit)** | MAX_BATCH_SIZE per request enforced via 413 |
| **Health checks** | `/healthz` (liveness) + `/healthz/detailed` (readiness) |
| **Request tracing** | `X-Request-ID` propagated end-to-end |
| **Content negotiation** | JSON responses; error shapes consistent |

### Python-Specific Concepts

| Concept | Where in EventFlux |
|---|---|
| **asyncio TaskGroup** | Worker runs 3 coroutines concurrently (Python 3.11+) |
| **async context managers** | `asyncpg.Pool.acquire()`, Redis connection lifecycle |
| **Pydantic v2** | Schema validation, `model_validator`, `field_validator` |
| **`functools.wraps`** | Cache decorator preserves wrapped function metadata |
| **`lru_cache`** | Settings objects cached in memory (singleton pattern) |

### Production Readiness Checklist

Before deploying to production, address:

- [ ] **Secrets management** — Replace `.env` with Vault, AWS Secrets Manager, or K8s Secrets
- [ ] **TLS everywhere** — API behind HTTPS; Redis with TLS (`rediss://`); PG with SSL
- [ ] **Auth + rate limiting** — Add API key validation or JWT; Nginx rate limit (`limit_req`)
- [ ] **Redis persistence** — Enable AOF (`appendonly yes`) for durability
- [ ] **Dead-letter alerting** — Alert when `XLEN events:dead > 0`
- [ ] **Partition cron job** — Ensure `partition_manager` is running; alert if `events_raw_default` grows
- [ ] **Connection pool tuning** — Set `max_size` based on PG's `max_connections`
- [ ] **Structured log aggregation** — Ship to ELK / Datadog / Loki
- [ ] **Metrics** — Instrument with Prometheus + expose `/metrics` endpoint
- [ ] **Horizontal scaling** — Unique `CONSUMER_NAME` per worker instance (use hostname)
- [ ] **Graceful shutdown** — SIGTERM handling (implemented) verified under load
- [ ] **Backup strategy** — PG WAL archiving or logical replication to read replica
- [ ] **Load test before go-live** — k6 at 2× expected peak traffic; observe p99 and error rate

---

*EventFlux — Built to learn, designed to scale.*
