-- Add current, identity-checked workload facts; preserve existing telemetry and reports.
CREATE TABLE workload_resource_snapshot (
 cluster text PRIMARY KEY CHECK(cluster IN ('yz','zw')),
 evaluated_at bigint NOT NULL CHECK(evaluated_at>0 AND evaluated_at%60=0),
 attempted_at timestamptz NOT NULL DEFAULT now(),
 payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object')
);
ALTER TABLE host_history_coverage ADD COLUMN attempt_status text NOT NULL DEFAULT 'unknown'
 CHECK(attempt_status IN ('unknown','ready','partial','empty','error'));
ALTER TABLE host_history_coverage ADD COLUMN failure_code text
 CHECK(failure_code IN ('identity_unavailable','metric_unavailable','missing_metrics','no_nodes'));
UPDATE host_history_coverage SET attempt_status='ready' WHERE complete;
