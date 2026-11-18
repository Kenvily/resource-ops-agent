import { DataDetails } from "../components/DataDetails";
import { useMonitoring } from "../hooks/useMonitoring";
import { useWorkbenchStatistics } from "../hooks/useWorkbenchStatistics";
import { GpuAvailability } from "../components/GpuAvailability";
import {
  clusterSignals,
  gpuAvailability,
} from "../components/workbenchMetrics";
import { MonitoringPanel } from "../components/MonitoringPanel";
import { usagePercent } from "../components/EfficiencyEvidence";
import { ArrowRight, Server } from "lucide-react";
import {
  Bar,
  number,
  percent,
  StatusBadge,
  time,
  dateTime,
} from "../components/Metrics";
import {
  memoryValue,
  type DisplayUnit,
  type MemoryFormatter,
} from "../components/memory";
import type { ClusterFilter, Overview } from "../types/resource";
export function Workbench({
  data,
  loading,
  refreshing,
  memory,
  displayUnit,
  refresh,
  onAssets,
  onCluster,
  cluster,
  signalRevision,
}: {
  data: Overview | null;
  loading: boolean;
  refreshing: ClusterFilter[];
  memory: MemoryFormatter;
  displayUnit: DisplayUnit;
  refresh: (cluster?: ClusterFilter) => Promise<void>;
  onAssets: () => void;
  onCluster: (cluster: ClusterFilter) => void;
  cluster: ClusterFilter;
  signalRevision: number;
}) {
  const statistics = useWorkbenchStatistics(cluster, signalRevision);
  const monitoring = useMonitoring(cluster, true, 30000, signalRevision);
  const selected = cluster === "all" ? (["yz", "zw"] as const) : [cluster];
  const signals = selected.map((id) => ({
    id,
    ...clusterSignals(id, data, statistics.data, monitoring.data),
  }));
  const nodes = signals.reduce((n, s) => n + s.nodes, 0);
  const ready = signals.reduce((n, s) => n + s.ready, 0);
  const unknown = signals.reduce((n, s) => n + s.unknownNodes, 0);
  const availability = gpuAvailability(data, statistics.data, cluster);
  const summary = statistics.data?.summary;
  const physical = (value: number | null | undefined) => {
    const result = memoryValue(value, "MiB", displayUnit);
    return result.value === "—" ? "—" : `${result.value} ${result.unit}`;
  };
  return (
    <div className="workbench-dashboard">
      <section className="workbench-overview" aria-label="资源概况">
        <GpuAvailability state={availability} />
        {loading ? (
          <div className="kpis" aria-label="正在加载资源">
            <div className="skeleton" />
            <div className="skeleton" />
            <div className="skeleton" />
            <div className="skeleton" />
          </div>
        ) : (
          <div className="kpis">
            {[
              [
                "GPU 总数",
                number(data?.totals.gpu_count_observed),
                "张",
                `${nodes} 台服务器`,
              ],
              [
                "平均 GPU 利用率",
                usagePercent(summary?.utilization_pct),
                "",
                summary &&
                summary.usage_observed_gpus === data?.totals.gpu_count_observed
                  ? ""
                  : `已观测 ${summary?.usage_observed_gpus ?? 0} / ${data?.totals.gpu_count_observed ?? "—"} 张`,
              ],
              [
                "物理显存使用率",
                percent(summary?.physical_usage_ratio ?? null),
                "",
                `${physical(summary?.physical_used_mib)} / ${physical(summary?.physical_observed_total_mib)}`,
              ],
              [
                "服务器就绪",
                nodes ? `${ready} / ${nodes}` : "—",
                "台",
                unknown ? `${unknown} 台状态未知` : nodes ? "" : "等待节点数据",
              ],
            ].map(([label, value, unit, hint], i) => (
              <div className={`kpi kpi-${i}`} key={label}>
                <div className="kpi-title">{label}</div>
                <strong>
                  {value}
                  <small>{unit}</small>
                </strong>
                {hint && <p>{hint}</p>}
              </div>
            ))}
          </div>
        )}
      </section>
      <section>
        <div className="section-title">
          <h2>集群概览</h2>
        </div>
        <div className="clusters">
          {data?.clusters
            .filter((c) => cluster === "all" || c.id === cluster)
            .map((c) => (
              <article
                className="card cluster-card"
                key={c.id}
                aria-busy={
                  refreshing.includes("all") || refreshing.includes(c.id)
                }
              >
                <div className="cluster-heading">
                  <div className={`cluster-icon ${c.id}`}>
                    <Server aria-hidden="true" size={22} />
                  </div>
                  <div>
                    <h3>
                      {c.label}集群 <span>{c.id.toUpperCase()}</span>
                    </h3>
                    <p>采集 {time(c.source_sampled_at ?? c.sampled_at)}</p>
                  </div>
                  <StatusBadge status={c.status} />
                </div>
                {(() => {
                  const state = signals.find((s) => s.id === c.id)!;
                  const gpuState = gpuAvailability(data, statistics.data, c.id);
                  return (
                    <div className="cluster-health-grid">
                      <div>
                        <span>GPU 配置</span>
                        <strong>
                          {state.nodes} 台 / {number(c.gpu_count_observed)} 张
                        </strong>
                        <span className="cluster-idle">
                          空闲 {gpuState.idle ?? "—"} / {gpuState.total ?? "—"}{" "}
                          张
                        </span>
                        <small>
                          {state.models.length
                            ? state.models
                                .slice(0, 2)
                                .map(([name, n]) => `${name} × ${n}`)
                                .join(" · ")
                            : "GPU 型号未知"}
                        </small>
                      </div>
                      <div>
                        <span>平均 GPU 利用率</span>
                        <strong>{usagePercent(state.utilization)}</strong>
                        {state.usageCoverage !== c.gpu_count_observed && (
                          <small className="coverage-warning">
                            覆盖 {state.usageCoverage} /{" "}
                            {number(c.gpu_count_observed)} 张
                          </small>
                        )}
                      </div>
                      <div>
                        <span>
                          {state.temperatureComplete
                            ? "最高 GPU 温度"
                            : "已观测最高温度"}
                        </span>
                        <strong>
                          {state.maxTemperature === null
                            ? "—"
                            : `${state.maxTemperature} °C`}
                        </strong>
                        {!state.temperatureComplete && (
                          <small className="coverage-warning">
                            覆盖 {state.temperatureCoverage} /{" "}
                            {state.temperatureExpected ?? "—"} 张
                          </small>
                        )}
                      </div>
                      <div>
                        <span>CPU 利用率</span>
                        <strong>{usagePercent(state.cpuUsage)}</strong>
                        {state.cpuCoverage < state.nodes && (
                          <small className="coverage-warning">
                            覆盖 {state.cpuCoverage} / {state.nodes} 台
                          </small>
                        )}
                      </div>
                      <div>
                        <span>系统内存使用率</span>
                        <strong>{percent(state.memoryRatio)}</strong>
                        <small>
                          {(() => {
                            const used = memoryValue(
                              state.memoryUsed,
                              "bytes",
                              displayUnit,
                            );
                            const total = memoryValue(
                              state.memoryTotal,
                              "bytes",
                              displayUnit,
                            );
                            return `${used.value} / ${total.value} ${total.unit}`;
                          })()}
                        </small>
                      </div>
                      <div>
                        <span>节点 Ready</span>
                        <strong>
                          {state.nodes
                            ? `${state.ready} / ${state.nodes}`
                            : "—"}
                        </strong>
                        <small>
                          {state.notReady
                            ? `${state.notReady} 台未就绪`
                            : state.unknownNodes
                              ? `${state.unknownNodes} 台未知`
                              : state.nodes
                                ? ""
                                : "节点未知"}
                        </small>
                      </div>
                    </div>
                  );
                })()}
                <div className="quota-summary">
                  <span>HAMi 额度分配率</span>
                  <strong>{percent(c.allocation_ratio)}</strong>
                </div>
                <Bar ratio={c.allocation_ratio} />
                <div className="legend">
                  <span>
                    <i />
                    {c.allocated_raw == null ? "已观测分配" : "已分配"}{" "}
                    {memory(c.allocated_raw ?? c.allocated_observed_raw)}
                  </span>
                  <span>
                    <i />
                    {c.unallocated_raw == null ? "已观测未分配" : "未分配"}{" "}
                    {memory(
                      c.unallocated_raw ?? c.unallocated_observed_raw,
                      "unallocated",
                    )}
                  </span>
                </div>
                {c.warnings.length > 0 && (
                  <details className="warnings">
                    <summary>{c.warnings.length} 条数据提示</summary>
                    {c.warnings.map((w) => (
                      <p key={w}>{w}</p>
                    ))}
                  </details>
                )}
                {c.collection_attempted_at != null &&
                  (c.collection_succeeded_at == null ||
                    c.collection_attempted_at > c.collection_succeeded_at) && (
                    <p className="notice collection-failure">
                      最近采集未成功：{dateTime(c.collection_attempted_at)}
                    </p>
                  )}
                <DataDetails label="采集与主机信息">
                  <p className="host-model-summary">
                    整机型号：
                    {signals
                      .find((s) => s.id === c.id)
                      ?.hostModels.join("、") || "型号未知"}
                  </p>
                  {c.storage_backend === "postgresql" && (
                    <div className="collection-times">
                      <span>数据库查询：{dateTime(c.database_read_at)}</span>
                      <span>
                        最近成功采集：{dateTime(c.collection_succeeded_at)}
                      </span>
                    </div>
                  )}
                </DataDetails>
                <div className="cluster-footer">
                  <span>容量 {memory(c.capacity_raw, "capacity")} </span>
                  <button
                    className="link"
                    onClick={() => {
                      onCluster(c.id);
                      onAssets();
                    }}
                  >
                    查看资源 <ArrowRight aria-hidden="true" size={14} />
                  </button>
                  <button
                    className="link"
                    disabled={
                      refreshing.includes("all") || refreshing.includes(c.id)
                    }
                    onClick={() => void refresh(c.id)}
                    aria-label={`刷新${c.label}数据源`}
                  >
                    {refreshing.includes("all") || refreshing.includes(c.id)
                      ? "更新中…"
                      : "刷新"}
                  </button>
                </div>
              </article>
            )) ?? (
            <div className="empty">
              {loading ? "正在连接数据源…" : "暂无集群数据"}
            </div>
          )}
        </div>
      </section>
      {(statistics.error || monitoring.error) && (
        <p className="notice" role="status">
          {[statistics.error, monitoring.error].filter(Boolean).join("；")}
          。未取得的指标显示“—”。
        </p>
      )}
      <MonitoringPanel
        cluster={cluster}
        displayUnit={displayUnit}
        compact
        state={monitoring}
      />
    </div>
  );
}
