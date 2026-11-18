-- Evidence and human review metadata; no changes to existing resource/history values.
CREATE TABLE efficiency_container_sample (
    cluster text NOT NULL, ts bigint NOT NULL, node text NOT NULL, device_uuid text NOT NULL,
    namespace_name text NOT NULL, pod_name text NOT NULL, container_name text NOT NULL,
    pod_uid text, device_model text,
    utilization_pct double precision CHECK(utilization_pct BETWEEN 0 AND 100),
    memory_bytes double precision CHECK(memory_bytes>=0 AND memory_bytes<'Infinity'::float8),
    kernel_elapsed_seconds double precision CHECK(kernel_elapsed_seconds>=0 AND kernel_elapsed_seconds<'Infinity'::float8),
    utilization_verified boolean NOT NULL DEFAULT false,
    warnings jsonb NOT NULL CHECK(jsonb_typeof(warnings)='array'),
    PRIMARY KEY(cluster,ts,node,device_uuid,namespace_name,pod_name,container_name),
    FOREIGN KEY(cluster,ts,node,device_uuid,namespace_name,pod_name,container_name)
      REFERENCES efficiency_workload_sample(cluster,ts,node,device_uuid,namespace_name,pod_name,container_name) ON DELETE CASCADE
);
CREATE INDEX efficiency_container_uid_time_idx ON efficiency_container_sample(cluster,pod_uid,ts);
CREATE TABLE efficiency_investigation (
    id uuid PRIMARY KEY, clusters text[] NOT NULL CHECK(cardinality(clusters) BETWEEN 1 AND 2 AND clusters<@ARRAY['yz','zw']::text[]),
    request jsonb NOT NULL, result jsonb NOT NULL CHECK(jsonb_typeof(result)='object'),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX efficiency_investigation_time_idx ON efficiency_investigation(created_at DESC);
CREATE TABLE efficiency_case (
    run_id uuid NOT NULL REFERENCES efficiency_investigation(id) ON DELETE CASCADE,
    id uuid NOT NULL, cluster text NOT NULL CHECK(cluster IN ('yz','zw')), node text NOT NULL,
    device_uuid text NOT NULL, pod_uids text[] NOT NULL, classification text NOT NULL,
    PRIMARY KEY(run_id,id), UNIQUE(run_id,cluster,node,device_uuid)
);
CREATE INDEX efficiency_case_scope_idx ON efficiency_case(cluster,node,device_uuid);
CREATE TABLE efficiency_review (
    id bigserial PRIMARY KEY, request_id uuid NOT NULL UNIQUE,
    run_id uuid NOT NULL, case_id uuid NOT NULL,
    decision text NOT NULL CHECK(decision IN ('inefficient','reasonable','insufficient')),
    task_stage text NOT NULL CHECK(task_stage IN ('unknown','training','inference','loading','data_preparation','checkpoint','waiting','completed')),
    note text NOT NULL DEFAULT '' CHECK(length(note)<=1000),
    reviewer text NOT NULL DEFAULT '' CHECK(length(reviewer)<=80),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY(run_id,case_id) REFERENCES efficiency_case(run_id,id) ON DELETE CASCADE
);
CREATE INDEX efficiency_review_case_time_idx ON efficiency_review(run_id,case_id,id DESC);
