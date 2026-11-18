-- Historical instant observations are separate from mergeable minute statistics.
CREATE TABLE IF NOT EXISTS host_history_point (
  cluster text NOT NULL CHECK (cluster IN ('yz','zw')),
  node text NOT NULL CHECK (length(node) BETWEEN 1 AND 256),
  metric text NOT NULL CHECK (metric IN ('cpu_utilization_pct','cpu_cores','memory_total_bytes','memory_used_bytes','memory_available_bytes','load1')),
  ts bigint NOT NULL CHECK (ts % 3600 = 0),
  value double precision CHECK (value >= 0 AND value < 'Infinity'::double precision),
  PRIMARY KEY(cluster,node,metric,ts),
  CHECK (metric <> 'cpu_utilization_pct' OR value <= 100)
);
CREATE INDEX IF NOT EXISTS host_point_time_idx ON host_history_point(cluster,ts,metric);
CREATE TABLE IF NOT EXISTS host_point_coverage (
  cluster text NOT NULL CHECK (cluster IN ('yz','zw')),
  ts bigint NOT NULL CHECK (ts % 3600 = 0),
  complete boolean NOT NULL,
  attempted_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(cluster,ts)
);
