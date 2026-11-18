export type AssessmentRisk =
  "unknown" | "historical_shortfall" | "within_observed";
export interface AssessmentDevice {
  cluster: string;
  node: string;
  device_uuid: string;
  capacity_bytes: number;
  allocated_bytes: number;
  unallocated_bytes: number;
  historical_min_bytes: number | null;
  coverage: number;
  expected_points: number;
  observed_points: number;
}
export interface AssessmentReport {
  request?: {
    gpu_count: number;
    memory_per_gpu: number;
    memory_unit: string;
    same_node: boolean;
    node?: string | null;
    duration_hours: number;
    history_hours: number;
  };
  required_bytes_per_gpu?: number;
  mode: string;
  database_read_at: number;
  candidate_count: number;
  truncated: boolean;
  history_window: { start: number; end: number; grain: number };
  clusters: {
    cluster: string;
    status: string;
    warnings: string[];
    eligible_gpu_count: number;
    source_sampled_at: number | null;
    snapshot_stored_at: number | null;
  }[];
  candidates: {
    cluster: string;
    nodes: string[];
    gpus: AssessmentDevice[];
    risk: AssessmentRisk;
    minimum_headroom_bytes?: number;
  }[];
  limitations: string[];
}
