import type { ClusterFilter, Metrics, Overview } from "../types/resource";

export function mergeOverview(
  previous: Overview | null,
  update: Overview,
): Overview {
  if (!previous) return update;
  const ids = new Set(update.clusters.map((c) => c.id));
  const preserved = new Set<string>();
  const clusters = previous.clusters.map((c) => {
    const next = update.clusters.find((n) => n.id === c.id);
    if (next?.status === "unavailable" && c.sampled_at !== null) {
      preserved.add(c.id);
      return {
        ...c,
        status: "stale" as const,
        warnings: [
          ...next.warnings,
          "数据源刷新失败：保留上次成功数据，已过期",
        ],
      };
    }
    return next ?? c;
  });
  const eligible = clusters.map((c) =>
    ["healthy", "degraded"].includes(c.status) ? c : null,
  );
  const sum = (key: keyof Metrics) =>
    eligible.every((c) => c?.[key] != null)
      ? eligible.reduce((n, c) => n + (c?.[key] ?? 0), 0)
      : null;
  const capacity = sum("capacity_raw"),
    allocated = sum("allocated_raw");
  const observed = (key: keyof Metrics) => {
    const values = eligible.flatMap((c) =>
      c?.[key] == null ? [] : [c[key] as number],
    );
    return values.length ? values.reduce((a, b) => a + b, 0) : null;
  };
  const totals: Metrics = {
    gpu_count_observed: sum("gpu_count_observed"),
    capacity_raw: capacity,
    allocated_raw: allocated,
    unallocated_raw:
      capacity !== null && allocated !== null ? capacity - allocated : null,
    allocation_ratio:
      capacity && allocated !== null ? allocated / capacity : null,
    allocated_observed_raw: observed("allocated_observed_raw"),
    unallocated_observed_raw: observed("unallocated_observed_raw"),
    allocation_observed_gpu_count: observed("allocation_observed_gpu_count"),
  };
  const complete = clusters.every((c) => c.status === "healthy");
  return {
    ...previous,
    units: update.units,
    mode: update.mode,
    fetched_at: update.fetched_at,
    database_read_at: update.database_read_at,
    clusters,
    totals,
    complete,
    overall_status: complete
      ? "healthy"
      : clusters.every((c) => c.status === "unavailable")
        ? "unavailable"
        : "degraded",
    nodes: [
      ...previous.nodes.filter(
        (n) => !ids.has(n.cluster) || preserved.has(n.cluster),
      ),
      ...update.nodes.filter((n) => !preserved.has(n.cluster)),
    ],
    insights: [
      ...previous.insights.filter(
        (text) => !update.clusters.some((c) => text.includes(c.label)),
      ),
      ...update.insights,
    ].filter((v, i, all) => all.indexOf(v) === i),
  };
}

export function markStale(data: Overview, target: ClusterFilter): Overview {
  const failed = data.clusters
    .filter((c) => target === "all" || c.id === target)
    .map((c) => ({
      ...c,
      status: "stale" as const,
      warnings: [...c.warnings, "刷新失败：以下为上次成功数据，已过期"],
    }));
  return mergeOverview(data, {
    ...data,
    clusters: failed,
    nodes: data.nodes.filter((n) => failed.some((c) => c.id === n.cluster)),
    insights: ["刷新失败，过期数据不参与当前合计。"],
  });
}
