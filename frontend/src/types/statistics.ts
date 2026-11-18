import type { HostMetrics } from "../api/operations";
export type ResourceAggregate = {
  gpu_count: number;
  machine_count: number;
  hami_capacity_raw: number | null;
  allocated_raw: number | null;
  unallocated_raw: number | null;
  physical_used_mib: number | null;
  physical_free_mib: number | null;
  physical_observed_total_mib: number | null;
  physical_usage_ratio: number | null;
  utilization_pct: number | null;
  usage_observed_gpus: number;
};
export type DeviceStatistics = {
  cluster: string;
  node: string;
  device_uuid: string;
  gpu_model: string | null;
  driver_version: string | null;
  utilization_pct: number | null;
  temperature_c?: number | null;
  temperature_sampled_at?: number | null;
  physical_used_mib: number | null;
  physical_free_mib: number | null;
  physical_observed_total_mib: number | null;
  hami_capacity_raw: number | null;
  allocated_raw: number | null;
  unallocated_raw: number | null;
  usage_sampled_at: number | null;
  quota_sampled_at: number | null;
};
export type ServerStatistics = {
  cluster: string;
  node: string;
  host: HostMetrics | null;
  node_ready: boolean | null;
};
export type ResourceStatistics = {
  servers?: ServerStatistics[];
  host_collection_enabled?: boolean;
  temperature_threshold_c?: number;
  mode: string;
  database_read_at: number;
  complete: boolean;
  units: { capacity: string; allocation: string };
  sources: {
    cluster: string;
    status: string;
    quota_sampled_at: number | null;
  }[];
  summary: ResourceAggregate;
  devices: DeviceStatistics[];
  machines: ({
    cluster: string;
    name: string;
    host?: HostMetrics | null;
  } & ResourceAggregate)[];
  gpu_models: ({ name: string; host?: never } & ResourceAggregate)[];
  business_models: {
    model_key: string | null;
    name: string;
    category?: string;
    attributes?: Record<string, string>;
    description?: string;
    model_names?: string[];
    workloads?: { cluster: string; namespace: string; pod: string }[];
    workloads_truncated?: boolean;
    allocated_raw: number | null;
    gpu_count: number;
    machine_count: number;
    shared_gpu_count: number;
    container_count: number;
    associated_devices?: {
      cluster: string;
      node: string;
      device_uuid: string;
    }[];
    devices_truncated?: boolean;
    attribution: string;
  }[];
  unmapped_workloads?: {
    cluster: string;
    namespace: string;
    pod: string;
    model_key: null;
    attribution: "unknown";
    allocated_raw: number | null;
    gpu_count: number;
    machine_count: number;
    container_count: number;
    associated_devices: {
      cluster: string;
      node: string;
      device_uuid: string;
    }[];
    devices_truncated: boolean;
  }[];
  unmapped_workload_count?: number;
  unmapped_workloads_truncated?: boolean;
  limitations: string[];
};
export type ResourceHistory = {
  start: number;
  end: number;
  grain: number;
  series: {
    metric: string;
    unit: string;
    points: {
      timestamp: number;
      mean: number | null;
      min: number | null;
      max: number | null;
      valid_points: number;
      expected_points: number;
      coverage: number;
    }[];
  }[];
};
