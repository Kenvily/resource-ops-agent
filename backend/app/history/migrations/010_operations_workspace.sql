-- Immutable planning/report deliveries, append-only human decisions, versioned issue state.
-- No metric tables or legacy reports are rewritten.
CREATE TABLE operations_record (
 id uuid PRIMARY KEY, kind text NOT NULL CHECK(kind IN ('assessment','report')),
 clusters text[] NOT NULL CHECK(cardinality(clusters) BETWEEN 1 AND 2 AND clusters<@ARRAY['yz','zw']::text[]),
 request jsonb NOT NULL CHECK(jsonb_typeof(request)='object'),
 result jsonb NOT NULL CHECK(jsonb_typeof(result)='object'),
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX operations_record_recent_idx ON operations_record(kind,created_at DESC,id);
CREATE TABLE operations_decision (
 request_id uuid PRIMARY KEY, record_id uuid NOT NULL REFERENCES operations_record(id) ON DELETE CASCADE,
 decision text NOT NULL CHECK(decision IN ('accepted','needs_changes','rejected')),
 candidates integer[] NOT NULL DEFAULT '{}' CHECK(cardinality(candidates)<=3),
 note text NOT NULL DEFAULT '' CHECK(length(note)<=1000), created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX operations_decision_record_idx ON operations_decision(record_id,created_at DESC,request_id);
CREATE TABLE operations_issue (
 id uuid PRIMARY KEY, cluster text NOT NULL CHECK(cluster IN ('yz','zw')), fingerprint text NOT NULL,
 title text NOT NULL CHECK(length(title)<=300), evidence jsonb NOT NULL CHECK(jsonb_typeof(evidence)='object'),
 status text NOT NULL DEFAULT 'open' CHECK(status IN ('open','investigating','resolved','dismissed')),
 revision integer NOT NULL DEFAULT 1 CHECK(revision>0),
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
-- Repeated capture reuses an open issue; a later recurrence may create a new issue after closure.
CREATE UNIQUE INDEX operations_issue_active_idx ON operations_issue(cluster,fingerprint)
 WHERE status IN ('open','investigating');
CREATE INDEX operations_issue_recent_idx ON operations_issue(updated_at DESC,id);
CREATE TABLE operations_issue_event (
 request_id uuid PRIMARY KEY, issue_id uuid NOT NULL REFERENCES operations_issue(id) ON DELETE CASCADE,
 revision integer NOT NULL CHECK(revision>1), status text NOT NULL CHECK(status IN ('open','investigating','resolved','dismissed')),
 note text NOT NULL DEFAULT '' CHECK(length(note)<=1000),
 investigation jsonb CHECK(investigation IS NULL OR jsonb_typeof(investigation)='object'),
 created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(issue_id,revision)
);
