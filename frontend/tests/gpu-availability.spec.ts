import { test, expect } from "@playwright/test";
import { gpuAvailability } from "../src/components/workbenchMetrics";
import type { Overview } from "../src/types/resource";
import type { ResourceStatistics } from "../src/types/statistics";

function fixture() {
  const gpu = (id: string) => ({
    cluster: "yz",
    node: "node-a",
    device_uuid: id,
  });
  const overview = {
    totals: { gpu_count_observed: 4 },
    clusters: [
      {
        id: "yz",
        status: "healthy",
        gpu_count_observed: 4,
        gpus: ["a", "b", "c", "d"].map(gpu),
      },
      { id: "zw", status: "healthy", gpu_count_observed: 0, gpus: [] },
    ],
  } as unknown as Overview;
  const statistics = {
    sources: [{ cluster: "yz", status: "healthy" }],
    devices: [
      { ...gpu("a"), physical_used_mib: 0 },
      { ...gpu("b"), physical_used_mib: 0.00001 },
      { ...gpu("c"), physical_used_mib: null },
      { ...gpu("d"), physical_used_mib: 20 },
    ],
  } as unknown as ResourceStatistics;
  return { overview, statistics };
}

test("physical idle excludes missing samples and tiny positive values", () => {
  const { overview, statistics } = fixture();
  expect(gpuAvailability(overview, statistics)).toMatchObject({
    total: 4,
    idle: 1,
    occupied: 2,
    unknown: 1,
  });
  expect(gpuAvailability(overview, statistics, "yz").idle).toBe(1);
});

test("identity deduplication rejects conflicts and foreign devices", () => {
  const { overview, statistics } = fixture();
  statistics.devices.push(
    { ...statistics.devices[0] },
    { ...statistics.devices[1], physical_used_mib: 0 },
    { ...statistics.devices[0], node: "foreign-node" },
  );
  expect(gpuAvailability(overview, statistics)).toMatchObject({
    total: 4,
    idle: 1,
    occupied: 1,
    unknown: 2,
  });
});

test("unavailable inventories and stale statistics never become zero idle", () => {
  const { overview, statistics } = fixture();
  statistics.sources[0].status = "stale";
  expect(gpuAvailability(overview, statistics)).toMatchObject({
    total: 4,
    idle: null,
    unknown: 4,
  });
  overview.clusters[1].status =
    "unavailable" as (typeof overview.clusters)[1]["status"];
  expect(gpuAvailability(overview, statistics)).toMatchObject({
    total: null,
    idle: null,
    unknown: null,
  });
});
