-- ============================================================
-- EventFlux — Database Initialisation
-- Partitioned raw event storage + analytics tables
-- ============================================================

-- ── Enable useful extensions ──────────────────────────────────
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- ============================================================
-- Raw event storage — partitioned by day on `timestamp`
-- ============================================================
CREATE TABLE IF NOT EXISTS events_raw (
    id            BIGSERIAL,
    event_type    TEXT        NOT NULL,
    actor_id      TEXT        NOT NULL,
    source        TEXT        NOT NULL,
    timestamp     TIMESTAMPTZ NOT NULL,
    attributes    JSONB       NOT NULL DEFAULT '{}',
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now()
) PARTITION BY RANGE (timestamp);

-- Default partition — catches any day without an explicit partition.
-- Phase 3 will add a cron/management job to create daily partitions ahead of time.
CREATE TABLE IF NOT EXISTS events_raw_default
    PARTITION OF events_raw DEFAULT;

-- Seed the first few day partitions so Phase 1 smoke tests pass.
-- The partition-creation job (Phase 3) will take over from here.
DO $$
DECLARE
    d DATE;
BEGIN
    FOR d IN
        SELECT generate_series(
            current_date - INTERVAL '1 day',
            current_date + INTERVAL '7 days',
            INTERVAL '1 day'
        )::date
    LOOP
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS events_raw_%s
             PARTITION OF events_raw
             FOR VALUES FROM (%L) TO (%L)',
            to_char(d, 'YYYY_MM_DD'),
            d::timestamptz,
            (d + INTERVAL '1 day')::timestamptz
        );
    END LOOP;
END;
$$;

-- ── Indexes on events_raw_default (inherited by all partitions) ──
-- Partition-wise index creation is done per-partition; create on the
-- default and on the initial day partitions.
CREATE INDEX IF NOT EXISTS idx_events_ts
    ON events_raw (timestamp);

CREATE INDEX IF NOT EXISTS idx_events_type_ts
    ON events_raw (event_type, timestamp);

CREATE INDEX IF NOT EXISTS idx_events_actor_ts
    ON events_raw (actor_id, timestamp);

-- ============================================================
-- Analytics — daily event counts (aggregation target)
-- ============================================================
CREATE TABLE IF NOT EXISTS analytics_daily_event_counts (
    day        DATE  NOT NULL,
    event_type TEXT  NOT NULL,
    source     TEXT  NOT NULL,
    count      BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (day, event_type, source)
);

CREATE INDEX IF NOT EXISTS idx_adec_day
    ON analytics_daily_event_counts (day);

CREATE INDEX IF NOT EXISTS idx_adec_event_type
    ON analytics_daily_event_counts (event_type, day);

-- ============================================================
-- Helper view — last 24 h event summary (useful for queries)
-- ============================================================
CREATE OR REPLACE VIEW v_events_last_24h AS
SELECT
    event_type,
    source,
    COUNT(*)            AS event_count,
    MIN(timestamp)      AS earliest,
    MAX(timestamp)      AS latest
FROM events_raw
WHERE timestamp >= now() - INTERVAL '24 hours'
GROUP BY event_type, source;
