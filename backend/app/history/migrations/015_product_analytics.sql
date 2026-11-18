-- Additive, anonymous browser analytics. Never stores raw URLs, questions or user identities.
CREATE TABLE IF NOT EXISTS product_event (
 id uuid PRIMARY KEY,
 visitor_hash text NOT NULL CHECK(length(visitor_hash)=64),
 occurred_at timestamptz NOT NULL DEFAULT now(),
 page text NOT NULL CHECK(page IN ('workbench','assets.servers','assets.gpus','workloads.business_models','analysis.history','analysis.assistant','analysis.diagnosis','analysis.efficiency','analysis.records','planning','operations.reports','operations.monitoring','operations.issues','settings.status','settings.infrastructure','settings.usage')),
 kind text NOT NULL CHECK(kind IN ('page_view','feature_use')),
 feature text CHECK(feature IN ('assistant_query','diagnosis_run','efficiency_run','planning_assess','report_generate','report_export','history_change','manual_refresh')),
 CHECK((kind='page_view' AND feature IS NULL) OR (kind='feature_use' AND feature IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS product_event_time_idx ON product_event(occurred_at);
CREATE TABLE IF NOT EXISTS product_analytics_metadata (
 id integer PRIMARY KEY CHECK(id=1), first_event_at timestamptz
);
INSERT INTO product_analytics_metadata(id) VALUES(1) ON CONFLICT DO NOTHING;
