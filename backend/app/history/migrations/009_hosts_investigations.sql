-- Add current host facts and investigation metadata; existing telemetry and reports remain intact.
CREATE TABLE node_metric_snapshot (
 cluster text NOT NULL CHECK(cluster IN ('yz','zw')), node text NOT NULL,
 evaluated_at bigint NOT NULL CHECK(evaluated_at>0), attempted_at timestamptz NOT NULL DEFAULT now(),
 payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object'), PRIMARY KEY(cluster,node)
);
CREATE TABLE investigation_task (
 id uuid PRIMARY KEY, kind text NOT NULL CHECK(kind IN ('allocation_change','gpu_usage')),
 status text NOT NULL CHECK(status IN ('running','complete','limited','refused','cancelled','failed','interrupted')),
 clusters text[] NOT NULL CHECK(cardinality(clusters) BETWEEN 1 AND 2 AND clusters<@ARRAY['yz','zw']::text[]),
 request jsonb NOT NULL CHECK(jsonb_typeof(request)='object'), result jsonb,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 deadline timestamptz NOT NULL, finished_at timestamptz, failure_reason text,
 legacy_run_id uuid, legacy boolean NOT NULL DEFAULT false,
 CHECK(result IS NULL OR jsonb_typeof(result)='object'), CHECK(finished_at IS NULL OR finished_at>=created_at)
);
CREATE INDEX investigation_recent_idx ON investigation_task(created_at DESC,id);
CREATE INDEX investigation_running_idx ON investigation_task(deadline) WHERE status='running';
CREATE TABLE investigation_event (
 task_id uuid NOT NULL REFERENCES investigation_task(id) ON DELETE CASCADE,
 sequence integer NOT NULL CHECK(sequence BETWEEN 1 AND 96), type text NOT NULL CHECK(type IN ('progress','evidence')),
 data jsonb NOT NULL CHECK(jsonb_typeof(data)='object'), created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(task_id,sequence)
);
CREATE TABLE investigation_feedback (
 request_id uuid PRIMARY KEY, task_id uuid NOT NULL REFERENCES investigation_task(id) ON DELETE CASCADE,
 decision text NOT NULL CHECK(decision IN ('accepted','follow_up','insufficient')),
 note text NOT NULL DEFAULT '' CHECK(length(note)<=1000), created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX investigation_feedback_task_idx ON investigation_feedback(task_id,created_at DESC);
-- Old reports keep their original IDs and evidence; absent execution metadata remains unknown.
INSERT INTO investigation_task(id,kind,status,clusters,request,result,created_at,updated_at,deadline,finished_at,legacy_run_id,legacy)
 SELECT id,'gpu_usage',CASE WHEN result->'agent'->>'status'='complete' THEN 'complete' ELSE 'limited' END,
 clusters,request,result,created_at,created_at,created_at,created_at,id,true FROM efficiency_investigation
 ON CONFLICT(id) DO NOTHING;
