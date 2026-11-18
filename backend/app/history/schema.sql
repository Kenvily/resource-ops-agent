-- Frozen v2 bootstrap for empty/v1 databases. Run app.history.cli init, not this file directly.
-- The current schema is produced by numbered, transactional migrations.
CREATE TABLE IF NOT EXISTS history_metadata (
    id integer PRIMARY KEY CHECK (id = 1), version integer NOT NULL,
    contract jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS history_sample (
    cluster text NOT NULL CHECK (cluster IN ('yz','zw')),
    grain integer NOT NULL CHECK (grain IN (60,300,3600)),
    ts bigint NOT NULL CHECK (ts % grain = 0),
    dimension text NOT NULL CHECK (dimension IN ('cluster','node','gpu','namespace','pod','workload')),
    entity text[] NOT NULL,
    capacity double precision, allocated double precision,
    status text NOT NULL CHECK (status IN ('healthy','degraded','empty')),
    warnings jsonb NOT NULL DEFAULT '[]',
    PRIMARY KEY (cluster,grain,dimension,entity,ts),
    CHECK (capacity IS NULL OR (capacity >= 0 AND capacity < 'Infinity'::float8)),
    CHECK (allocated IS NULL OR (allocated >= 0 AND allocated < 'Infinity'::float8))
);
CREATE INDEX IF NOT EXISTS history_retention_idx ON history_sample (grain,ts);
CREATE INDEX IF NOT EXISTS history_dimension_idx ON history_sample (cluster,grain,dimension,ts);
CREATE TABLE IF NOT EXISTS history_progress (
    cluster text NOT NULL, grain integer NOT NULL,
    next_ts bigint NOT NULL, updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (cluster,grain)
);

-- Version 2: bounded current snapshots. Payloads preserve the complete API contract.
CREATE TABLE IF NOT EXISTS resource_snapshot (
    cluster text PRIMARY KEY CHECK (cluster IN ('yz','zw')),
    payload jsonb NOT NULL,
    last_good jsonb,
    updated_at double precision NOT NULL
);
