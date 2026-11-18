-- Additive migration. No existing samples are dropped or reinterpreted.
CREATE TABLE history_fill_guard (
    cluster text NOT NULL CHECK (cluster IN ('yz','zw')),
    grain integer NOT NULL CHECK (grain IN (60,300,3600)),
    attempted_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(cluster,grain)
);
CREATE TABLE history_source_state (
    cluster text PRIMARY KEY CHECK (cluster IN ('yz','zw')),
    earliest_ts bigint NOT NULL CHECK(earliest_ts>0),
    discovery_complete boolean NOT NULL DEFAULT true,
    discovered_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE history_rollup (
    entity_id bigint NOT NULL REFERENCES history_entity(id),
    source_grain integer NOT NULL CHECK(source_grain IN (60,300,3600)),
    bucket bigint NOT NULL,
    bucket_seconds integer NOT NULL CHECK(bucket_seconds IN (3600,86400)),
    expected_count integer NOT NULL CHECK(expected_count>0),
    covered_count integer NOT NULL CHECK(covered_count>=0 AND covered_count<=expected_count),
    valid_count integer NOT NULL CHECK(valid_count>=0 AND valid_count<=expected_count),
    metrics jsonb NOT NULL CHECK(jsonb_typeof(metrics)='object'),
    finalized boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(entity_id,source_grain,bucket),
    CHECK(bucket>0 AND (bucket+CASE WHEN bucket_seconds=86400 THEN 28800 ELSE 0 END)%bucket_seconds=0)
);
CREATE INDEX history_rollup_time_idx ON history_rollup(source_grain,bucket,entity_id);
