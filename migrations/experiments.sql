-- ============================================================
-- EventFlux — Performance Experiment Tables
-- Non-partitioned baseline for partition pruning comparison
-- ============================================================

-- events_raw_flat mirrors events_raw exactly but is a plain heap table.
-- Used by scripts/benchmark_queries.py to compare:
--   - Query latency: partitioned vs non-partitioned
--   - Write throughput: single-row vs batch (COPY)
CREATE TABLE IF NOT EXISTS events_raw_flat (
    id            BIGSERIAL PRIMARY KEY,
    event_type    TEXT        NOT NULL,
    actor_id      TEXT        NOT NULL,
    source        TEXT        NOT NULL,
    timestamp     TIMESTAMPTZ NOT NULL,
    attributes    JSONB       NOT NULL DEFAULT '{}',
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Same indexes as events_raw partitions so comparison is fair.
CREATE INDEX IF NOT EXISTS idx_flat_ts
    ON events_raw_flat (timestamp);

CREATE INDEX IF NOT EXISTS idx_flat_type_ts
    ON events_raw_flat (event_type, timestamp);

CREATE INDEX IF NOT EXISTS idx_flat_actor_ts
    ON events_raw_flat (actor_id, timestamp);
