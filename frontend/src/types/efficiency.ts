export type EfficiencyWorkload = {
  namespace: string;
  pod: string;
  container: string;
  pod_uid: string | null;
  started_at: number | null;
  running: boolean | null;
  restart_count: number | null;
  verified: boolean;
  allocated_raw?: number;
};
export type EfficiencyDevice = {
  screening_status?:
    "low_utilization" | "above_threshold" | "unallocated" | "insufficient";
  usage_valid_points?: number;
  usage_coverage?: number;
  case_id?: string;
  task_candidates?: {
    namespace: string;
    pod: string;
    container: string;
    pod_uid: string;
    sustained_minutes: number;
    mean_utilization_pct: number;
    memory_peak_bytes: number;
    minimum_allocated_bytes: number;
    valid_points: number;
    expected_points: number;
  }[];
  investigation?: {
    complete: boolean;
    entity_id: string;
    evidence_ids: string[];
    analysis?: { title: string; outcome: string };
    signal_entities?: Record<
      string,
      { namespace: string; pod: string; container: string; pod_uid: string }
    >;
    container_usage: {
      task_utilization_available?: boolean;
      signals?: {
        workload_ref: string;
        observed_points: number;
        expected_points?: number;
        coverage?: number;
        utilization_verified: boolean;
        mean_utilization_pct: number | null;
        memory_peak_bytes: number | null;
        kernel_signal_available?: boolean;
        recent_kernel_observed: boolean;
        zero_conflicts_with_activity: boolean;
      }[];
    };
    task_stage: string;
  };
  cluster: string;
  node: string;
  device_uuid: string;
  classification: string;
  coverage: number;
  valid_points: number;
  expected_points: number;
  sustained_minutes: number;
  current_allocated_raw: number | null;
  allocation_unit: string;
  mean_utilization_pct: number | null;
  max_utilization_pct: number | null;
  mean_physical_memory_bytes: number | null;
  workloads: EfficiencyWorkload[];
  limitations: string[];
  workloads_truncated: boolean;
};
export type EfficiencyReport = {
  investigation?: { id: string };
  run_id?: string;
  is_saved_report?: boolean;
  agent?: {
    mode: "deepseek" | "deterministic" | "degraded";
    status: string;
    investigated_devices?: number;
    screened_devices?: number;
    model_calls: number;
    tool_calls: number;
    elapsed_ms: number;
    warnings: string[];
    evidence: {
      id: string;
      tool: string;
      label: string;
      cluster: string;
      data: Record<string, unknown>;
    }[];
  };
  mode: string;
  database_read_at: number;
  start: number;
  end: number;
  results: EfficiencyDevice[];
  task_candidate_count?: number;
  result_count: number;
  truncated: boolean;
  limitations: string[];
  clusters: {
    cluster: string;
    device_count: number;
    candidate_count: number;
    collected_points: number;
    expected_points: number;
    latest_collection_at: number | null;
    warnings: string[];
    steps: { check: string; status: string }[];
  }[];
};
