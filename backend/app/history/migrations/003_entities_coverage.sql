-- Additive data migration: values and original warnings are preserved through entity references.
CREATE TABLE history_entity (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    cluster text NOT NULL CHECK (cluster IN ('yz','zw')),
    dimension text NOT NULL CHECK (dimension IN ('cluster','node','gpu','namespace','pod','workload')),
    entity_key text[] NOT NULL,
    node_name text GENERATED ALWAYS AS
        (CASE WHEN dimension IN ('node','gpu','workload') THEN entity_key[1] END) STORED,
    device_uuid text GENERATED ALWAYS AS
        (CASE WHEN dimension IN ('gpu','workload') THEN entity_key[2] END) STORED,
    namespace_name text GENERATED ALWAYS AS
        (CASE WHEN dimension IN ('namespace','pod') THEN entity_key[1]
              WHEN dimension='workload' THEN entity_key[3] END) STORED,
    pod_name text GENERATED ALWAYS AS
        (CASE WHEN dimension='pod' THEN entity_key[2] WHEN dimension='workload' THEN entity_key[4] END) STORED,
    UNIQUE (cluster,dimension,entity_key),
    UNIQUE (id,cluster,dimension)
);
INSERT INTO history_entity (cluster,dimension,entity_key)
SELECT DISTINCT cluster,dimension,entity FROM history_sample;
-- NOT VALID preserves legacy anomalies; new writes are checked immediately.
ALTER TABLE history_entity ADD CONSTRAINT history_entity_shape CHECK (
    cardinality(entity_key) = CASE dimension WHEN 'cluster' THEN 0 WHEN 'node' THEN 1
        WHEN 'gpu' THEN 2 WHEN 'namespace' THEN 1 WHEN 'pod' THEN 2 ELSE 5 END
    AND (cardinality(entity_key)=0 OR (array_ndims(entity_key)=1 AND array_lower(entity_key,1)=1))
    AND array_position(entity_key,NULL) IS NULL AND NOT (''=ANY(entity_key))
    AND coalesce(octet_length(entity_key[1]),0)<=256 AND coalesce(octet_length(entity_key[2]),0)<=256
    AND coalesce(octet_length(entity_key[3]),0)<=256 AND coalesce(octet_length(entity_key[4]),0)<=256
    AND coalesce(octet_length(entity_key[5]),0)<=256
) NOT VALID;
CREATE INDEX history_entity_scope_idx ON history_entity (cluster,dimension,node_name,namespace_name);
CREATE INDEX history_entity_business_idx ON history_entity (cluster,dimension,namespace_name,pod_name);
CREATE TABLE history_coverage (
    cluster text NOT NULL CHECK (cluster IN ('yz','zw')),
    grain integer NOT NULL CHECK (grain IN (60,300,3600)),
    ts bigint NOT NULL CHECK (ts>0 AND ts%grain=0),
    status text NOT NULL CHECK (status IN ('healthy','degraded','empty')),
    warnings jsonb NOT NULL CHECK (jsonb_typeof(warnings)='array'),
    source_age_seconds double precision CHECK (source_age_seconds>=0 AND source_age_seconds<'Infinity'::float8),
    last_attempt_warnings jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(last_attempt_warnings)='array'),
    last_attempt_at timestamptz NOT NULL DEFAULT now(),
    last_attempt_status text NOT NULL DEFAULT 'healthy' CHECK (last_attempt_status IN ('healthy','degraded','empty')),
    quality_version smallint NOT NULL DEFAULT 1 CHECK (quality_version IN (1,2)),
    PRIMARY KEY (cluster,grain,ts)
);
INSERT INTO history_coverage (cluster,grain,ts,status,warnings,last_attempt_status)
SELECT s.cluster,s.grain,s.ts,coalesce(c.status,'degraded'),coalesce(c.warnings,'["旧数据缺少覆盖标记"]'::jsonb),coalesce(c.status,'degraded')
FROM (SELECT DISTINCT cluster,grain,ts FROM history_sample) s
LEFT JOIN history_sample c ON c.cluster=s.cluster AND c.grain=s.grain AND c.ts=s.ts
AND c.dimension='cluster' AND c.entity='{}';
CREATE INDEX history_coverage_retention_idx ON history_coverage (grain,ts);
ALTER TABLE history_sample ADD COLUMN entity_id bigint;
UPDATE history_sample s SET entity_id=e.id FROM history_entity e
WHERE e.cluster=s.cluster AND e.dimension=s.dimension AND e.entity_key=s.entity;
ALTER TABLE history_sample ALTER COLUMN entity_id SET NOT NULL;
ALTER TABLE history_sample DROP CONSTRAINT history_sample_pkey;
DROP INDEX IF EXISTS history_dimension_idx;
DROP INDEX IF EXISTS history_retention_idx;
ALTER TABLE history_sample DROP COLUMN entity;
ALTER TABLE history_sample ADD PRIMARY KEY (entity_id,grain,ts);
ALTER TABLE history_sample ADD FOREIGN KEY (entity_id,cluster,dimension)
    REFERENCES history_entity(id,cluster,dimension);
ALTER TABLE history_sample ADD FOREIGN KEY (cluster,grain,ts)
    REFERENCES history_coverage(cluster,grain,ts) ON DELETE CASCADE;
ALTER TABLE history_sample ADD CONSTRAINT history_sample_semantics CHECK (
    ts>0 AND jsonb_typeof(warnings)='array'
    AND (dimension NOT IN ('namespace','pod','workload') OR capacity IS NULL)
    AND (status<>'healthy' OR (allocated IS NOT NULL AND
        (dimension IN ('namespace','pod','workload') OR capacity IS NOT NULL)))
) NOT VALID;
CREATE INDEX history_sample_scope_idx ON history_sample (cluster,grain,ts);
ALTER TABLE history_progress ADD COLUMN quality_version smallint NOT NULL DEFAULT 1;
ALTER TABLE history_progress ADD CONSTRAINT history_progress_scope CHECK (
    cluster IN ('yz','zw') AND grain IN (60,300,3600) AND next_ts>0 AND next_ts%grain=0
    AND quality_version IN (1,2)) NOT VALID;
ALTER TABLE resource_snapshot ADD CONSTRAINT resource_snapshot_contract CHECK (
    jsonb_typeof(payload)='object' AND coalesce(payload->>'id','')=cluster
    AND jsonb_typeof(payload->'fetched_at')='number'
    AND (last_good IS NULL OR (jsonb_typeof(last_good)='object' AND coalesce(last_good->>'id','')=cluster))
    AND updated_at>0 AND updated_at<'Infinity'::float8
) NOT VALID;

-- The successful current payload already IS the latest good snapshot; retain its values once.
UPDATE resource_snapshot SET last_good=NULL
WHERE payload->>'status'='healthy' AND last_good=payload;
