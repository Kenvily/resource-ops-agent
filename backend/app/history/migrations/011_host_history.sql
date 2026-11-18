-- Host facts are keyed by real node identity, never by a fabricated GPU UUID.
CREATE TABLE host_metric_bucket (
 cluster text NOT NULL CHECK(cluster IN ('yz','zw')), node text NOT NULL CHECK(length(node) BETWEEN 1 AND 256),
 metric text NOT NULL CHECK(metric IN ('cpu_utilization_pct','cpu_cores','memory_total_bytes','memory_used_bytes','memory_available_bytes','load1')),
 grain integer NOT NULL CHECK(grain IN (60,300,3600)), ts bigint NOT NULL CHECK(ts>0 AND ts%grain=0),
 sum_value double precision, min_value double precision, max_value double precision,
 valid_points integer NOT NULL CHECK(valid_points BETWEEN 0 AND grain/60),
 PRIMARY KEY(cluster,node,metric,grain,ts),
 CHECK((valid_points=0 AND sum_value IS NULL AND min_value IS NULL AND max_value IS NULL)
 OR (valid_points>0 AND min_value>=0 AND min_value<='Infinity'::float8 AND max_value<'Infinity'::float8 AND min_value<=max_value AND sum_value>=0 AND sum_value<'Infinity'::float8)),
 CHECK(metric<>'cpu_utilization_pct' OR max_value<=100)
);
CREATE INDEX host_metric_prune_idx ON host_metric_bucket(grain,ts);
CREATE TABLE host_history_coverage (
 cluster text NOT NULL CHECK(cluster IN ('yz','zw')), ts bigint NOT NULL CHECK(ts>0 AND ts%60=0),
 expected_nodes integer NOT NULL CHECK(expected_nodes BETWEEN 0 AND 1000), complete boolean NOT NULL,
 attempted_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(cluster,ts)
);
