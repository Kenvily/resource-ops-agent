import { useEffect, useRef, useState } from "react";
import type { ResourceStatistics, ServerStatistics } from "../types/statistics";
import type { ClusterFilter } from "../types/resource";
import { memoryText, type DisplayUnit } from "./memory";
import { usagePercent } from "./EfficiencyEvidence";
import { AssetSummary } from "./AssetSummary";
import { WorkloadResources } from "./WorkloadResources";
const names: Record<string, string> = { yz: "亦庄", zw: "兆维" };
const host = (row: ServerStatistics) =>
  row.host?.status === "stale" ? null : row.host;
export type ServerFilters = { query: string; status: string; sort: string };
export function ServerAssets({
  data,
  cluster,
  displayUnit,
  onInspect,
  containers = false,
  filters,
  onFiltersChange,
}: {
  data: ResourceStatistics;
  cluster: ClusterFilter;
  displayUnit: DisplayUnit;
  onInspect: (row: ServerStatistics) => void;
  containers?: boolean;
  filters?: ServerFilters;
  onFiltersChange?: (filters: ServerFilters) => void;
}) {
  const rows =
    data.servers ??
    data.machines.map((m) => ({
      cluster: m.cluster,
      node: m.name,
      host: m.host ?? null,
      node_ready: null,
    }));
  const [localFilters, setLocalFilters] = useState<ServerFilters>({
    query: "",
    status: "all",
    sort: "name",
  });
  const currentFilters = filters ?? localFilters;
  const { query, status, sort } = currentFilters;
  const changeFilters = (patch: Partial<ServerFilters>) =>
    (onFiltersChange ?? setLocalFilters)({ ...currentFilters, ...patch });
  const [showContainers, setShowContainers] = useState(containers);
  const [selectedNode, setSelectedNode] = useState<ServerStatistics | null>(
    null,
  );
  const containerSection = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    if (!filters) setLocalFilters({ query: "", status: "all", sort: "name" });
    setSelectedNode(null);
    setShowContainers(containers);
  }, [cluster, containers]);
  useEffect(() => {
    if (showContainers) {
      containerSection.current?.querySelector("summary")?.focus();
      if (selectedNode || containers)
        containerSection.current?.scrollIntoView({
          block: "start",
          behavior: window.matchMedia("(prefers-reduced-motion: reduce)")
            .matches
            ? "auto"
            : "smooth",
        });
    }
  }, [showContainers, selectedNode]);
  const sum = (field: "cpu_cores" | "memory_total_bytes") =>
    rows.length && rows.every((r) => host(r)?.[field] != null)
      ? rows.reduce((n, r) => n + host(r)![field]!, 0)
      : null;
  const cores = sum("cpu_cores"),
    memory = sum("memory_total_bytes");
  const cpu =
    cores && rows.every((r) => host(r)?.cpu_utilization_pct != null)
      ? rows.reduce(
          (n, r) => n + host(r)!.cpu_utilization_pct! * host(r)!.cpu_cores!,
          0,
        ) / cores
      : null;
  const amount = (n: number | null | undefined) =>
    memoryText(n, "bytes", displayUnit);
  const memoryPct = (r: ServerStatistics) => {
    const h = host(r);
    return h?.memory_used_bytes != null &&
      h.memory_total_bytes != null &&
      h.memory_total_bytes > 0
      ? (h.memory_used_bytes / h.memory_total_bytes) * 100
      : null;
  };
  const shown = rows
    .filter(
      (r) =>
        `${names[r.cluster]} ${r.node} ${host(r)?.server_model ?? ""}`
          .toLowerCase()
          .includes(query.toLowerCase()) &&
        (status === "all" ||
          (status === "ready" && r.node_ready === true) ||
          (status === "attention" && r.node_ready !== true)),
    )
    .sort((a, b) => {
      if (sort === "cpu")
        return (
          (host(b)?.cpu_utilization_pct ?? -1) -
          (host(a)?.cpu_utilization_pct ?? -1)
        );
      if (sort === "memory") return (memoryPct(b) ?? -1) - (memoryPct(a) ?? -1);
      return a.cluster.localeCompare(b.cluster) || a.node.localeCompare(b.node);
    });
  return (
    <>
      {data.host_collection_enabled === false && (
        <p className="notice">主机指标采集未启用</p>
      )}
      <AssetSummary
        items={[
          { label: "已记录服务器", value: `${rows.length} 台` },
          {
            label: "CPU 总核数",
            value: cores == null ? "—" : `${cores.toLocaleString()} 核`,
          },
          { label: "系统内存总量", value: amount(memory) },
          {
            label: "CPU 平均利用率",
            value: usagePercent(cpu),
            note: cpu == null ? "采样不完整" : undefined,
          },
        ]}
      />
      <div className="asset-filters">
        <label className="search">
          <span className="sr-only">搜索服务器</span>
          <input
            aria-label="搜索服务器"
            placeholder="搜索服务器、型号"
            value={query}
            onChange={(e) => changeFilters({ query: e.target.value })}
          />
        </label>
        <label>
          节点状态
          <select
            aria-label="筛选服务器状态"
            value={status}
            onChange={(e) => changeFilters({ status: e.target.value })}
          >
            <option value="all">全部状态</option>
            <option value="ready">Ready</option>
            <option value="attention">未就绪 / 未知</option>
          </select>
        </label>
        <label>
          排序
          <select
            aria-label="服务器排序"
            value={sort}
            onChange={(e) => changeFilters({ sort: e.target.value })}
          >
            <option value="name">名称</option>
            <option value="cpu">CPU 利用率优先</option>
            <option value="memory">内存利用率优先</option>
          </select>
        </label>
        <span className="count">
          {shown.length} / {rows.length} 台
        </span>
      </div>
      <div className="table-scroll">
        <table className="asset-table">
          <thead>
            <tr>
              <th>服务器 / 集群</th>
              <th>整机型号</th>
              <th>CPU 配置 / 利用率</th>
              <th>系统内存已用 / 总量</th>
              <th>内存利用率</th>
              <th>负载（1分钟）</th>
              <th>节点状态</th>
              <th>详情</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((r) => {
              const h = host(r);
              return (
                <tr key={JSON.stringify([r.cluster, r.node])}>
                  <td>
                    <strong>{r.node}</strong>
                    <small>{names[r.cluster]}</small>
                  </td>
                  <td>{h?.server_model ?? "型号未知"}</td>
                  <td>
                    {h?.cpu_cores == null ? "—" : `${h.cpu_cores} 核`}
                    <small>{usagePercent(h?.cpu_utilization_pct)}</small>
                  </td>
                  <td>
                    {amount(h?.memory_used_bytes)} /{" "}
                    {amount(h?.memory_total_bytes)}
                  </td>
                  <td>{usagePercent(memoryPct(r))}</td>
                  <td>
                    {h?.load1 == null
                      ? "—"
                      : h.load1.toLocaleString("zh-CN", {
                          maximumFractionDigits: 2,
                        })}
                  </td>
                  <td>
                    <span
                      className={`asset-state ${r.node_ready === true ? "ready" : r.node_ready === false ? "attention" : "unknown"}`}
                    >
                      {r.node_ready === true
                        ? "Ready"
                        : r.node_ready === false
                          ? "未就绪"
                          : "状态未知"}
                    </span>
                  </td>
                  <td>
                    <div className="resource-row-actions">
                      <button
                        type="button"
                        className="link"
                        onClick={() => onInspect(r)}
                      >
                        服务器详情
                      </button>
                      <button
                        type="button"
                        className="link"
                        onClick={() => {
                          setSelectedNode(r);
                          setShowContainers(true);
                        }}
                      >
                        容器资源
                      </button>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {!shown.length && <p className="empty">没有匹配的服务器</p>}
      <details
        ref={containerSection}
        className="asset-containers"
        open={showContainers}
        onToggle={(e) => setShowContainers(e.currentTarget.open)}
      >
        <summary>
          容器 CPU 与内存
          {selectedNode
            ? ` · ${names[selectedNode.cluster]} / ${selectedNode.node}`
            : ""}
        </summary>
        {showContainers && (
          <>
            {selectedNode && (
              <button className="link" onClick={() => setSelectedNode(null)}>
                查看全部服务器容器
              </button>
            )}
            <WorkloadResources
              key={
                selectedNode
                  ? JSON.stringify([selectedNode.cluster, selectedNode.node])
                  : cluster
              }
              cluster={(selectedNode?.cluster ?? cluster) as ClusterFilter}
              displayUnit={displayUnit}
              node={selectedNode?.node}
            />
          </>
        )}
      </details>
    </>
  );
}
