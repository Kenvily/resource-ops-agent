-- Additive, independent facts: physical usage never reuses virtual quota columns.
CREATE TABLE efficiency_coverage (
    cluster text NOT NULL CHECK(cluster IN ('yz','zw')),
    ts bigint NOT NULL CHECK(ts>0 AND ts%60=0),
    profile text NOT NULL,
    status text NOT NULL CHECK(status IN ('healthy','degraded','unavailable','empty')),
    warnings jsonb NOT NULL DEFAULT '[]'::jsonb CHECK(jsonb_typeof(warnings)='array'),
    last_attempt_status text NOT NULL CHECK(last_attempt_status IN ('healthy','degraded','unavailable','empty')),
    collected_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(cluster,ts)
);
CREATE TABLE efficiency_device_sample (
    cluster text NOT NULL,
    ts bigint NOT NULL,
    node text NOT NULL CHECK(node<>''),
    device_uuid text NOT NULL CHECK(device_uuid<>''),
    capacity_raw double precision,
    allocated_raw double precision,
    allocation_unit text NOT NULL CHECK(allocation_unit IN ('MB','MiB','bytes','unverified')),
    utilization_pct double precision CHECK(utilization_pct BETWEEN 0 AND 100),
    physical_memory_bytes double precision CHECK(physical_memory_bytes>=0 AND physical_memory_bytes<'Infinity'::float8),
    verified boolean NOT NULL,
    warnings jsonb NOT NULL DEFAULT '[]'::jsonb CHECK(jsonb_typeof(warnings)='array'),
    PRIMARY KEY(cluster,ts,node,device_uuid),
    FOREIGN KEY(cluster,ts) REFERENCES efficiency_coverage(cluster,ts) ON DELETE CASCADE
);
CREATE INDEX efficiency_device_history_idx ON efficiency_device_sample(cluster,node,device_uuid,ts);
CREATE TABLE efficiency_workload_sample (
    cluster text NOT NULL,
    ts bigint NOT NULL,
    node text NOT NULL,
    device_uuid text NOT NULL,
    namespace_name text NOT NULL CHECK(namespace_name<>''),
    pod_name text NOT NULL CHECK(pod_name<>''),
    container_name text NOT NULL CHECK(container_name<>''),
    pod_uid text,
    allocated_raw double precision NOT NULL CHECK(allocated_raw>=0 AND allocated_raw<'Infinity'::float8),
    pod_started_at double precision CHECK(pod_started_at>0 AND pod_started_at<'Infinity'::float8),
    running boolean,
    restart_count double precision CHECK(restart_count>=0 AND restart_count<'Infinity'::float8),
    verified boolean NOT NULL,
    warnings jsonb NOT NULL DEFAULT '[]'::jsonb CHECK(jsonb_typeof(warnings)='array'),
    PRIMARY KEY(cluster,ts,node,device_uuid,namespace_name,pod_name,container_name),
    FOREIGN KEY(cluster,ts,node,device_uuid) REFERENCES efficiency_device_sample(cluster,ts,node,device_uuid) ON DELETE CASCADE
);
CREATE INDEX efficiency_pod_history_idx ON efficiency_workload_sample(cluster,pod_uid,ts);
