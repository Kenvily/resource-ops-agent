-- Additive task-memory migration. Existing investigations and telemetry remain intact.
ALTER TABLE investigation_task DROP CONSTRAINT IF EXISTS investigation_task_kind_check;
ALTER TABLE investigation_task ADD CONSTRAINT investigation_task_kind_check CHECK(kind IN ('allocation_change','gpu_usage','resource_investigation'));
ALTER TABLE investigation_task DROP CONSTRAINT IF EXISTS investigation_task_status_check;
ALTER TABLE investigation_task ADD CONSTRAINT investigation_task_status_check CHECK(status IN ('running','complete','limited','refused','cancelled','failed','interrupted','waiting_input'));
CREATE TABLE IF NOT EXISTS agent_task_memory (
 task_id uuid PRIMARY KEY REFERENCES investigation_task(id) ON DELETE CASCADE,
 root_id uuid NOT NULL,
 parent_id uuid,
 scope_fingerprint text NOT NULL,
 turns_reserved integer NOT NULL DEFAULT 1 CHECK(turns_reserved BETWEEN 1 AND 4),
 memory jsonb NOT NULL DEFAULT '{}' CHECK(jsonb_typeof(memory)='object'),
 bindings jsonb NOT NULL DEFAULT '{}' CHECK(jsonb_typeof(bindings)='object'),
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent_memory_root_idx ON agent_task_memory(root_id);
