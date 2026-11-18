-- Additive resource identities and mergeable measurements. Existing quota history is untouched.
CREATE TABLE IF NOT EXISTS resource_device (
 cluster text NOT NULL CHECK(cluster IN ('yz','zw')), node text NOT NULL, device_uuid text NOT NULL,
 gpu_model text, driver_version text, first_seen bigint NOT NULL, last_seen bigint NOT NULL,
 PRIMARY KEY(cluster,node,device_uuid), CHECK(first_seen<=last_seen)
);
CREATE TABLE IF NOT EXISTS resource_metric_bucket (
 cluster text NOT NULL, node text NOT NULL, device_uuid text NOT NULL,
 metric text NOT NULL CHECK(metric IN ('gpu_util','gpu_memory','gpu_memory_free')),
 grain integer NOT NULL CHECK(grain IN (60,300,3600)), ts bigint NOT NULL CHECK(ts>0),
 sum_value double precision, min_value double precision, max_value double precision,
 valid_points integer NOT NULL CHECK(valid_points>=0),
 PRIMARY KEY(cluster,node,device_uuid,metric,grain,ts),
 FOREIGN KEY(cluster,node,device_uuid) REFERENCES resource_device(cluster,node,device_uuid),
 CHECK(ts%grain=0), CHECK(valid_points<=grain/60),
 CHECK((valid_points=0 AND sum_value IS NULL AND min_value IS NULL AND max_value IS NULL)
    OR (valid_points>0 AND sum_value IS NOT NULL AND min_value IS NOT NULL AND max_value IS NOT NULL AND min_value<=max_value)),
 CHECK(sum_value IS NULL OR (sum_value>=0 AND sum_value<'Infinity'::double precision)),
 CHECK(min_value IS NULL OR (min_value>=0 AND max_value<'Infinity'::double precision))
);
CREATE INDEX IF NOT EXISTS resource_metric_window_idx ON resource_metric_bucket(cluster,grain,ts,metric);
CREATE TABLE IF NOT EXISTS resource_business_model (
 model_key text PRIMARY KEY CHECK(length(model_key) BETWEEN 1 AND 128),
 display_name text NOT NULL CHECK(length(display_name) BETWEEN 1 AND 200)
);
CREATE TABLE IF NOT EXISTS resource_model_binding (
 cluster text NOT NULL CHECK(cluster IN ('yz','zw')), namespace_name text NOT NULL, pod_name text NOT NULL,
 container_name text NOT NULL, model_key text NOT NULL REFERENCES resource_business_model(model_key),
 effective_from bigint NOT NULL, effective_until bigint,
 PRIMARY KEY(cluster,namespace_name,pod_name,container_name,effective_from),
 CHECK(effective_until IS NULL OR effective_until>=effective_from)
);
CREATE UNIQUE INDEX IF NOT EXISTS resource_current_binding_idx
 ON resource_model_binding(cluster,namespace_name,pod_name,container_name) WHERE effective_until IS NULL;

ALTER TABLE operations_metric_snapshot DROP CONSTRAINT IF EXISTS operations_metric_snapshot_metric_check;
ALTER TABLE operations_metric_snapshot ADD CONSTRAINT operations_metric_snapshot_metric_check
 CHECK(metric IN ('gpu_util','gpu_memory','gpu_memory_free','gpu_temp','gpu_xid','gpu_ecc','node_ready'));
CREATE TABLE IF NOT EXISTS resource_history_coverage (
 cluster text NOT NULL CHECK(cluster IN ('yz','zw')), ts bigint NOT NULL CHECK(ts>0 AND ts%60=0),
 expected_devices integer NOT NULL CHECK(expected_devices>=0), complete boolean NOT NULL,
 attempted_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(cluster,ts)
);
CREATE TABLE IF NOT EXISTS resource_coarse_coverage (
 cluster text NOT NULL CHECK(cluster IN ('yz','zw')), ts bigint NOT NULL CHECK(ts>0 AND ts%3600=0),
 complete boolean NOT NULL, attempted_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(cluster,ts)
);
