export type HostMetrics = {
  load1?: number | null;
  filesystems?: {
    device: string;
    mountpoint: string;
    filesystem: string;
    total_bytes: number | null;
    available_bytes: number | null;
    availability_ratio: number | null;
  }[];
  filesystems_truncated?: boolean;
  networks?: {
    interface: string;
    receive_bytes_per_second: number | null;
    transmit_bytes_per_second: number | null;
  }[];
  networks_truncated?: boolean;
  kubernetes?: {
    allocatable_cpu_cores: number | null;
    allocatable_memory_bytes: number | null;
    unschedulable: boolean | null;
    pressure: Record<string, string>;
  } | null;
  metric_statuses?: Record<string, string>;
  extended_metric_statuses?: Record<string, string>;
  cluster: string;
  node: string;
  status: string;
  evaluated_at: number;
  cpu_cores: number | null;
  cpu_utilization_pct: number | null;
  memory_total_bytes: number | null;
  memory_available_bytes: number | null;
  memory_used_bytes: number | null;
  server_model: string | null;
  server_vendor: string | null;
};
export type Period = { start: number; end: number; grain: number };
export type PeriodMetrics = {
  complete: boolean;
  expected_points: number;
  verified_coverage_points: number;
  allocated_mean_raw: number | null;
  capacity_mean_raw: number | null;
  allocated_max_raw: number | null;
  ratio_mean: number | null;
};
export type Change = {
  entity: string[];
  baseline: PeriodMetrics;
  target: PeriodMetrics;
  allocated_change_raw: number | null;
  capacity_change_raw: number | null;
};
export type Comparison = {
  complete: boolean;
  changes: Change[];
  warnings: string[];
  omitted_entities: number | null;
};
export type Metric = {
  cluster: string;
  key: string;
  source: string;
  label: string;
  unit: string;
  status: string;
  evaluated_at: number | null;
  expected_entities: number | null;
  values: { node: string; device_uuid: string | null; value: number }[];
};
export type OperationReport = {
  linked_investigations?: {
    id: string;
    status: string;
    evidence_window?: { start: number; end: number };
    warnings?: string[];
    evidence: unknown[];
    findings: { title: string; level: string; evidence_ids: string[] }[];
  }[];
  linked_issues?: { id: string; title: string; status: string }[];
  trace_warnings?: string[];
  alerts: {
    cluster: string;
    node: string;
    device_uuid: string | null;
    message: string;
    evaluated_at: number;
  }[];
  alert_count: number;
  mode: string;
  generated_at: number;
  window: string;
  baseline: Period;
  target: Period;
  units: { allocation: string; capacity: string };
  clusters: {
    cluster: string;
    current: {
      status: string;
      sampled_at: number | null;
      gpu_count: number | null;
      allocated_raw: number | null;
      capacity_raw: number | null;
    } | null;
    comparison: Comparison | null;
    namespace_changes: Comparison | null;
    warnings: string[];
  }[];
  metrics: Metric[];
  review_status: string;
  notes: string[];
};
export type Monitoring = {
  hosts?: HostMetrics[];
  host_collection_enabled?: boolean;
  mode: string;
  database_read_at: number;
  collection_enabled: boolean;
  metrics: Metric[];
  alerts: {
    cluster: string;
    metric: string;
    node: string;
    device_uuid: string | null;
    message: string;
    evaluated_at: number;
  }[];
  alert_count: number;
  health: string;
  truncated: boolean;
  temperature_threshold_c: number;
};
export type Infrastructure = {
  observed_at: number;
  telemetry: {
    uptime_seconds: number;
    upstream_response_bytes: number;
    concurrency: Record<string, { current: number; peak: number }>;
    metrics: {
      domain: string;
      label: string;
      operation: string;
      count: number;
      recent_samples: number;
      errors: number;
      p95_ms: number;
      p50_ms: number;
      error_rate: number;
    }[];
  };
  database: {
    database_bytes: number;
    connections: number;
    lock_waiters: number;
    active_queries: number;
    tables: {
      name: string;
      estimated_rows: number;
      estimated_dead_rows: number;
      total_bytes: number;
    }[];
  } | null;
  recommendations: { component: string; status: string; reason: string }[];
  limitations: string[];
};
export async function operationsRequest<T>(
  path: string,
  signal: AbortSignal,
  body?: unknown,
): Promise<T> {
  const response = await fetch(`/api/v1/operations/${path}`, {
    method: body === undefined ? "GET" : "POST",
    signal,
    headers:
      body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw Error(
      typeof data.detail === "string" ? data.detail : "运营服务暂不可用",
    );
  }
  return response.json();
}
