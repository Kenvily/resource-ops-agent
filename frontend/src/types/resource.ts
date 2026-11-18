export type ClusterId = "yz" | "zw";
export type ClusterFilter = ClusterId | "all";
export type Status = "healthy" | "degraded" | "unavailable" | "empty" | "stale";
export interface Metrics {
  gpu_count_observed: number | null;
  capacity_raw: number | null;
  allocated_raw: number | null;
  unallocated_raw: number | null;
  allocation_ratio: number | null;
  allocated_observed_raw: number | null;
  unallocated_observed_raw: number | null;
  allocation_observed_gpu_count: number | null;
}
export interface Workload {
  cluster: ClusterId;
  node: string;
  device_uuid: string;
  namespace: string;
  pod: string;
  container: string;
  allocated_raw: number | null;
  sampled_at: number;
}
export interface Gpu extends Metrics {
  cluster: ClusterId;
  node: string;
  device_uuid: string;
  sampled_at: number | null;
  warnings: string[];
}
export interface Node extends Metrics {
  cluster: ClusterId;
  node: string;
}
export interface Cluster extends Metrics {
  database_read_at?: number | null;
  collection_attempted_at?: number | null;
  collection_succeeded_at?: number | null;
  snapshot_stored_at?: number | null;
  storage_backend?: "prometheus" | "postgresql";
  id: ClusterId;
  label: string;
  status: Status;
  sampled_at: number | null;
  fetched_at: number;
  source_sampled_at?: number | null;
  source_age_seconds?: number | null;
  cache_status?: "miss" | "hit" | "stale";
  cache_age_seconds: number;
  warnings: string[];
  gpus: Gpu[];
  workloads: Workload[];
}
export interface Overview {
  database_read_at?: number | null;
  mode: "mock" | "live";
  overall_status: Status;
  fetched_at: number;
  complete: boolean;
  totals: Metrics;
  clusters: Cluster[];
  nodes: Node[];
  insights: string[];
  units: {
    memory: string;
    unit_verified: boolean;
    capacity: string;
    allocation: string;
  };
}
