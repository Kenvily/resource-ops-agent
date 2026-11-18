-- Bounded current capability/alert facts. No report scheduling, audit or report retention table.
CREATE TABLE IF NOT EXISTS operations_metric_snapshot (
    cluster text NOT NULL CHECK (cluster IN ('yz','zw')),
    metric text NOT NULL CHECK (metric IN ('gpu_util','gpu_memory','gpu_temp','gpu_xid','gpu_ecc','node_ready')),
    attempted_at timestamptz NOT NULL DEFAULT now(),
    evaluated_at bigint NOT NULL CHECK (evaluated_at>0),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload)='object'),
    PRIMARY KEY(cluster,metric)
);
