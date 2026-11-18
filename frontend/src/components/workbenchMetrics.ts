import type { Monitoring } from "../api/operations";
import type { ClusterId, Overview } from "../types/resource";
import type { ResourceStatistics } from "../types/statistics";

/** Read each ledger independently; absent or failed measurements remain unknown. */
export function clusterSignals(
  cluster: ClusterId,
  resources: Overview | null,
  statistics: ResourceStatistics | null,
  monitoring: Monitoring | null,
) {
  const source = resources?.clusters.find((c) => c.id === cluster);
  const nodes =
    source && ["healthy", "degraded"].includes(source.status)
      ? (resources?.nodes.filter((n) => n.cluster === cluster) ?? [])
      : [];
  const nodeKeys = new Set(nodes.map((n) => n.node));
  const metrics =
    monitoring?.metrics.filter((m) => m.cluster === cluster) ?? [];
  const values = (key: string) =>
    metrics
      .filter((m) => m.key === key && ["ready", "partial"].includes(m.status))
      .flatMap((m) => m.values);
  const readyMap = new Map(
    values("node_ready")
      .filter((v) => nodeKeys.has(v.node) && [0, 1].includes(v.value))
      .map((v) => [v.node, v.value]),
  );
  const temperatures = values("gpu_temp")
    .map((v) => v.value)
    .filter((v) => Number.isFinite(v) && v >= -20 && v <= 150);
  const devices =
    statistics?.devices.filter((d) => d.cluster === cluster) ?? [];
  const utilization = devices.map((d) => d.utilization_pct);
  const models = new Map<string, number>();
  for (const d of devices)
    if (d.gpu_model)
      models.set(d.gpu_model, (models.get(d.gpu_model) ?? 0) + 1);
  const knownUtil = utilization.filter(
    (v): v is number => v !== null && Number.isFinite(v),
  );
  const temperatureMetric = metrics.find((m) => m.key === "gpu_temp");
  const hosts = (monitoring?.hosts ?? []).filter(
    (h) =>
      h.cluster === cluster && nodeKeys.has(h.node) && h.status !== "stale",
  );
  const cpu = hosts.filter(
    (h) => h.cpu_cores && h.cpu_utilization_pct !== null,
  );
  const memory = hosts.filter(
    (h) => h.memory_total_bytes && h.memory_used_bytes !== null,
  );
  const cores = cpu.reduce((v, h) => v + h.cpu_cores!, 0);
  const memoryTotal = memory.reduce((v, h) => v + h.memory_total_bytes!, 0);
  const memoryUsed = memory.reduce((v, h) => v + h.memory_used_bytes!, 0);
  return {
    cpuUsage:
      cpu.length === nodes.length && cores
        ? cpu.reduce((v, h) => v + h.cpu_utilization_pct! * h.cpu_cores!, 0) /
          cores
        : null,
    cpuCoverage: cpu.length,
    memoryRatio:
      memory.length === nodes.length && memoryTotal
        ? memoryUsed / memoryTotal
        : null,
    memoryUsed:
      memory.length > 0 && memory.length === nodes.length ? memoryUsed : null,
    memoryTotal:
      memory.length > 0 && memory.length === nodes.length ? memoryTotal : null,
    hostModels: [...new Set(hosts.map((h) => h.server_model).filter(Boolean))],
    nodes: nodes.length,
    ready: [...readyMap.values()].filter((v) => v === 1).length,
    notReady: [...readyMap.values()].filter((v) => v === 0).length,
    unknownNodes: nodes.length - readyMap.size,
    readyCoverage: readyMap.size,
    maxTemperature: temperatures.length ? Math.max(...temperatures) : null,
    temperatureCoverage: temperatures.length,
    temperatureExpected: temperatureMetric?.expected_entities,
    temperatureComplete: temperatureMetric?.status === "ready",
    utilization:
      utilization.length && knownUtil.length === utilization.length
        ? knownUtil.reduce((a, b) => a + b, 0) / knownUtil.length
        : null,
    usageCoverage: knownUtil.length,
    models: [...models.entries()],
  };
}

/** Count physical memory states against the same current inventory, never quota amounts. */
export function gpuAvailability(
  overview: Overview | null,
  statistics: ResourceStatistics | null,
  cluster: "all" | "yz" | "zw" = "all",
) {
  const selected = cluster === "all" ? ["yz", "zw"] : [cluster];
  const clusters =
    overview?.clusters.filter((c) => selected.includes(c.id)) ?? [];
  const current = clusters.filter(
    (c) => c.status === "healthy" || c.status === "degraded",
  );
  const key = (d: { cluster: string; node: string; device_uuid: string }) =>
    JSON.stringify([d.cluster, d.node, d.device_uuid]);
  const inventory = new Set(current.flatMap((c) => c.gpus.map(key)));
  const reported =
    cluster === "all"
      ? overview?.totals.gpu_count_observed
      : clusters[0]?.gpu_count_observed;
  const complete =
    current.length === selected.length &&
    reported != null &&
    inventory.size === reported;
  if (!complete)
    return {
      total: null,
      idle: null,
      occupied: null,
      unknown: null,
      observed: 0,
    };
  const sources = new Set(
    (statistics?.sources ?? [])
      .filter((s) => ["healthy", "degraded"].includes(s.status))
      .map((s) => s.cluster),
  );
  const values = new Map<string, Set<number>>();
  for (const d of statistics?.devices ?? []) {
    const value = d.physical_used_mib;
    if (
      !inventory.has(key(d)) ||
      !sources.has(d.cluster) ||
      value == null ||
      !Number.isFinite(value) ||
      value < 0
    )
      continue;
    const entries = values.get(key(d)) ?? new Set<number>();
    entries.add(value);
    values.set(key(d), entries);
  }
  const known = [...values.values()]
    .filter((v) => v.size === 1)
    .map((v) => [...v][0]);
  return {
    total: reported,
    idle: known.length ? known.filter((v) => v === 0).length : null,
    occupied: known.length ? known.filter((v) => v > 0).length : null,
    unknown: reported - known.length,
    observed: known.length,
  };
}
