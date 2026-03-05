# EventFlux

> High-throughput telemetry and event analytics pipeline — a backend infrastructure showcase.

## Architecture

```
Applications
     │  HTTP POST /v1/events/bulk
     ▼
Ingestion API (FastAPI)   ← validates, rejects oversized batches
     │  XADD
     ▼
Redis Streams             ← absorbs burst traffic, decouples write path
     │  XREADGROUP
     ▼
Batch Worker              ← accumulates events, bulk-inserts to PG
     │  COPY / bulk INSERT
     ▼
PostgreSQL (partitioned)  ← events_raw partitioned by day
     │
     ▼
Aggregation Jobs          ← periodic INSERT INTO analytics_daily_event_counts
     │
     ▼
Query API                 ← GET /v1/analytics/events/daily
```

## Services

| Service    | Port  | Description                          |
|------------|-------|--------------------------------------|
| api        | 8000  | FastAPI ingestion + query API        |
| worker     | —     | Batch insert worker (Redis → PG)     |
| postgres   | 5432  | Partitioned event storage            |
| redis      | 6379  | Redis Streams event queue            |

## Quick Start

```bash
cp .env.example .env
make up        # docker compose up --build -d
make seed      # insert sample events via the API
make logs      # tail all service logs
```

## Development Phases

| Phase | Description |
|-------|-------------|
| 1     | Project initialization, Docker Compose, scaffolding |
| 2     | Ingestion API, Redis Streams integration            |
| 3     | Batch worker, partitioned PG storage                |
| 4     | Aggregation jobs, analytics query API               |
| 5     | k6 load tests, performance experiments              |

## Load Testing

```bash
make load-test-scenario1   # 100 events × 100 VUs
make load-test-scenario2   # 1000 events × 50 VUs
```
