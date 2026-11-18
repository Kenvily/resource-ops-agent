import { useEffect, useState } from "react";
import type { ClusterFilter, Overview } from "../types/resource";
import type { WindowSize } from "../types/trend";
import { memoryValue, type DisplayUnit } from "./memory";
import { percent } from "./Metrics";

type Stat = {
  sum: number | null;
  mean: number | null;
  min: number | null;
  max: number | null;
  valid_count: number;
  coverage: number;
};
type Report = {
  source_grain: number;
  expected_points?: number;
  warnings?: string[];
  summary?: Record<string, Stat>;
  cluster: string;
  expected_buckets: number;
  truncated: boolean;
  units: Overview["units"];
  buckets: {
    expected_count: number;
    valid_count: number;
    coverage: number;
    metrics: Record<string, Stat>;
    archived: boolean;
  }[];
};
export function StatisticsPanel({
  cluster,
  windowSize,
  displayUnit,
}: {
  cluster: ClusterFilter;
  windowSize: WindowSize;
  displayUnit: DisplayUnit;
}) {
  const [reports, setReports] = useState<Report[]>([]),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  useEffect(() => {
    const c = new AbortController();
    setBusy(true);
    setError("");
    setReports([]);
    const ids = cluster === "all" ? ["yz", "zw"] : [cluster];
    Promise.allSettled(
      ids.map(async (id) => {
        const r = await fetch(
          `/api/v1/history/statistics?cluster=${id}&window=${windowSize}`,
          { signal: c.signal },
        );
        if (!r.ok) {
          const body = await r.json().catch(() => ({}));
          throw Error(
            typeof body.detail === "string" ? body.detail : "时段统计暂不可用",
          );
        }
        return r.json() as Promise<Report>;
      }),
    )
      .then((results) => {
        if (c.signal.aborted) return;
        setReports(
          results.flatMap((r) => (r.status === "fulfilled" ? [r.value] : [])),
        );
        setError(
          results
            .flatMap((r, i) =>
              r.status === "rejected"
                ? [
                    `${ids[i] === "yz" ? "亦庄" : "兆维"}：${r.reason instanceof Error ? r.reason.message : "时段统计暂不可用"}`,
                  ]
                : [],
            )
            .concat(
              results.flatMap((r) =>
                r.status === "fulfilled" ? (r.value.warnings ?? []) : [],
              ),
            )
            .join("；"),
        );
      })
      .catch((e) => {
        if (!c.signal.aborted) setError(e.message);
      })
      .finally(() => {
        if (!c.signal.aborted) setBusy(false);
      });
    return () => c.abort();
  }, [cluster, windowSize]);
  return (
    <section
      className="card statistics-panel"
      aria-label="时段统计"
      aria-busy={busy}
    >
      <div className="panel-heading">
        <h2>时段统计</h2>
      </div>
      {error && (
        <p role="alert" className="notice">
          {error}
        </p>
      )}
      {busy ? (
        <p className="statistics-caption" role="status">
          正在读取统计…
        </p>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>集群</th>
                <th>已分配均值</th>
                <th>最小值</th>
                <th>最大值</th>
                <th>有效采样数</th>
                <th>覆盖率</th>
              </tr>
            </thead>
            <tbody>
              {reports.map((r) => {
                const values = r.buckets
                    .map((b) => b.metrics.allocated)
                    .filter(Boolean),
                  count = values.reduce((s, v) => s + v.valid_count, 0),
                  sum = values.reduce((s, v) => s + (v.sum ?? 0), 0),
                  mins = values.flatMap((v) => (v.min == null ? [] : [v.min])),
                  maxs = values.flatMap((v) => (v.max == null ? [] : [v.max]));
                const summary = r.summary?.allocated;
                const memory = (raw: number | null) => {
                  const v = memoryValue(raw, r.units.allocation, displayUnit);
                  return `${v.value} ${v.unit}`;
                };
                const expected =
                  r.expected_points ??
                  r.expected_buckets *
                    (r.source_grain === 3600 ? 24 : 3600 / r.source_grain);
                return (
                  <tr key={r.cluster}>
                    <td>
                      {r.cluster === "yz" ? "亦庄" : "兆维"}
                      {r.truncated ? "（时段明细截断）" : ""}
                    </td>
                    <td>
                      {memory(summary?.mean ?? (count ? sum / count : null))}
                    </td>
                    <td>
                      {memory(
                        summary?.min ??
                          (mins.length ? Math.min(...mins) : null),
                      )}
                    </td>
                    <td>
                      {memory(
                        summary?.max ??
                          (maxs.length ? Math.max(...maxs) : null),
                      )}
                    </td>
                    <td>{(summary?.valid_count ?? count).toLocaleString()}</td>
                    <td>
                      {percent(
                        summary?.coverage ??
                          (expected ? count / expected : null),
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
