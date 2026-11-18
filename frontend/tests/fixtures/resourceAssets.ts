export const aggregate = {
  gpu_count: 2,
  machine_count: 1,
  hami_capacity_raw: 48000,
  allocated_raw: 20000,
  unallocated_raw: 28000,
  physical_used_mib: 2048,
  physical_free_mib: 44000,
  physical_observed_total_mib: 46048,
  physical_usage_ratio: 2048 / 46048,
  utilization_pct: 12,
  usage_observed_gpus: 2,
};
export const device = {
  cluster: "yz",
  node: "mock-machine",
  device_uuid: "GPU-MOCK-1",
  gpu_model: "MOCK-GPU",
  driver_version: "MOCK-DRIVER",
  temperature_c: 48,
  utilization_pct: 12,
  physical_used_mib: 1024,
  physical_free_mib: 22000,
  physical_observed_total_mib: 23024,
  hami_capacity_raw: 24000,
  allocated_raw: 10000,
  unallocated_raw: 14000,
  usage_sampled_at: 1793235600,
  quota_sampled_at: 1793235600,
};
export const statistics = {
  mode: "mock",
  database_read_at: 1793235700,
  complete: true,
  units: { capacity: "MB", allocation: "MB" },
  sources: [{ cluster: "yz", status: "healthy", quota_sampled_at: 1793235600 }],
  summary: aggregate,
  devices: [device, { ...device, device_uuid: "GPU-MOCK-2" }],
  machines: [{ cluster: "yz", name: "mock-machine", ...aggregate }],
  gpu_models: [{ name: "MOCK-GPU", ...aggregate }],
  business_models: [
    {
      model_key: null,
      name: "归属未知",
      allocated_raw: 20000,
      gpu_count: 2,
      machine_count: 1,
      shared_gpu_count: 0,
      container_count: 2,
      attribution: "unknown",
      associated_devices: [
        { cluster: "yz", node: "mock-machine", device_uuid: "GPU-MOCK-1" },
      ],
    },
  ],
  unmapped_workload_count: 2,
  unmapped_workloads_truncated: false,
  unmapped_workloads: [1, 2].map((i) => ({
    cluster: "yz",
    namespace: `demo-team-${i}`,
    pod: `demo-pod-${i}`,
    model_key: null,
    attribution: "unknown",
    allocated_raw: 10000,
    gpu_count: 1,
    machine_count: 1,
    container_count: 1,
    associated_devices: [
      { cluster: "yz", node: "mock-machine", device_uuid: `GPU-MOCK-${i}` },
    ],
    devices_truncated: false,
  })),
  servers: [
    {
      cluster: "yz",
      node: device.node,
      node_ready: true,
      host: {
        status: "observed",
        cpu_cores: 64,
        cpu_utilization_pct: 18,
        memory_total_bytes: 128e9,
        memory_used_bytes: 48e9,
        load1: 2.4,
        server_model: "MOCK-SERVER",
      },
    },
  ],
  host_collection_enabled: true,
  limitations: [],
};
const points = [12, null, 20].map((v, i) => ({
  timestamp: 1793235480 + i * 60,
  mean: v,
  min: v,
  max: v,
  valid_points: v == null ? 0 : 1,
  expected_points: 1,
  coverage: v == null ? 0 : 1,
}));
export const history = {
  start: 1793235480,
  end: 1793235600,
  grain: 60,
  series: ["gpu_util", "gpu_memory", "gpu_memory_free"].map((metric) => ({
    metric,
    unit: metric === "gpu_util" ? "%" : "MiB",
    points,
  })),
};
export async function mockAssets(page: import("@playwright/test").Page) {
  await page.route("**/api/v1/resource-statistics", (r) =>
    r.fulfill({ json: statistics }),
  );
  await page.route("**/api/v1/resource-statistics/history", (r) =>
    r.fulfill({ json: history }),
  );
}
