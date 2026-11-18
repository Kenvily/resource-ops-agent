import { useEffect, useRef, useState } from "react";
import type { ClusterFilter } from "../types/resource";
import { memoryValue, type DisplayUnit } from "./memory";
import { dateTime } from "./Metrics";
import { DataDetails } from "./DataDetails";
import { DetailDrawer } from "./DetailDrawer";
type Container = {
  cluster: string;
  node: string;
  namespace_name: string;
  pod_name: string;
  container_name: string;
  pod_uid: string;
  container_id: string | null;
  model_name: string | null;
  running: boolean | null;
  cpu_usage_cores: number | null;
  memory_working_set_bytes: number | null;
  cpu_request_cores: number | null;
  cpu_limit_cores: number | null;
  memory_request_bytes: number | null;
  memory_limit_bytes: number | null;
  metric_statuses: Record<string, string>;
};
type Report = {
  collection_enabled: boolean;
  database_read_at: number;
  mode: string;
  sources: {
    cluster: string;
    status: string;
    evaluated_at: number | null;
    row_count: number;
    identity_rejected: number;
    truncated: boolean;
    source_statuses: Record<string, string>;
  }[];
  containers: Container[];
  summary: Record<string, number | null>;
  coverage: Record<string, number>;
};
const names: Record<string, string> = { yz: "亦庄", zw: "兆维" };
const states: Record<string, string> = {
  ready: "已采集",
  partial: "部分覆盖",
  empty: "暂无容器",
  error: "采集失败",
  stale: "已过期",
  not_collected: "尚未采集",
  missing: "未报告",
  conflict: "来源冲突",
  unit_conflict: "单位冲突",
  unverified_identity: "身份未核验",
};
export function WorkloadResources({
  cluster,
  displayUnit,
  node,
}: {
  cluster: ClusterFilter;
  displayUnit: DisplayUnit;
  node?: string;
}) {
  const [data, setData] = useState<Report | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const [query, setQuery] = useState(""),
    [page, setPage] = useState(0),
    [selected, setSelected] = useState<Container | null>(null);
  const [revision, setRevision] = useState(0);
  const active = useRef<AbortController | null>(null);
  useEffect(() => {
    setData(null);
    setSelected(null);
    setPage(0);
  }, [cluster]);
  useEffect(() => {
    const c = new AbortController();
    active.current = c;
    setSelected(null);
    setBusy(true);
    setError("");
    fetch("/api/v1/resource-statistics/workloads", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      signal: c.signal,
      body: JSON.stringify({ cluster }),
    })
      .then(async (r) => {
        const b = await r.json();
        if (!r.ok)
          throw Error(
            typeof b.detail === "string" ? b.detail : "业务资源读取失败",
          );
        return b;
      })
      .then((d) => {
        if (!c.signal.aborted) {
          setData(d);
          setPage(0);
        }
      })
      .catch((e) => {
        if (!c.signal.aborted) {
          setError(e.message);
          setData(null);
        }
      })
      .finally(() => {
        if (!c.signal.aborted) setBusy(false);
      });
    return () => c.abort();
  }, [cluster, revision]);
  useEffect(() => setPage(0), [cluster, query]);
  const memory = (n: number | null | undefined) => {
    const v = memoryValue(n, "bytes", displayUnit);
    if (
      n != null &&
      n > 0 &&
      n / (displayUnit === "GB" ? 1e9 : 1e6) <
        (displayUnit === "GB" ? 0.01 : 0.1)
    )
      return `<${displayUnit === "GB" ? "0.01" : "0.1"} ${displayUnit}`;
    return v.value === "—" ? "—" : `${v.value} ${v.unit}`;
  };
  const cpu = (n: number | null | undefined) =>
    n == null
      ? "—"
      : n.toLocaleString("zh-CN", { maximumSignificantDigits: 4 });
  const rows = (data?.containers ?? []).filter(
    (r) =>
      (!node || r.node === node) &&
      [
        r.node,
        r.namespace_name,
        r.pod_name,
        r.container_name,
        r.model_name ?? "",
      ].some((s) => s.toLowerCase().includes(query.toLowerCase())),
  );
  const visible = rows.slice(page * 25, (page + 1) * 25);
  return (
    <section className="card operations-panel workload-compute">
      <div className="panel-heading">
        <h2>容器 CPU 与内存</h2>
        <button disabled={busy} onClick={() => setRevision((v) => v + 1)}>
          {busy ? "读取中…" : "刷新数据库数据"}
        </button>
      </div>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {busy && (
        <p role="status">
          {data ? "正在读取，暂显示上次结果…" : "正在读取业务资源…"}
        </p>
      )}
      {data && (
        <>
          {!data.collection_enabled && (
            <p className="notice">业务资源采集未启用</p>
          )}
          {data.sources
            .filter((s) => s.status !== "ready")
            .map((s) => (
              <p className="notice" key={s.cluster}>
                {names[s.cluster]} · {states[s.status]}
                {s.identity_rejected
                  ? ` · ${s.identity_rejected} 个容器身份无法唯一确认`
                  : ""}
              </p>
            ))}
          {!node && (
            <div className="host-resource-summary">
              <div>
                容器 CPU 使用（核）
                <strong>{cpu(data.summary.cpu_usage_cores)}</strong>
                <span>
                  覆盖 {data.coverage.cpu_usage_cores}/{data.containers.length}
                </span>
              </div>
              <div>
                容器内存工作集
                <strong>{memory(data.summary.memory_working_set_bytes)}</strong>
                <span>
                  覆盖 {data.coverage.memory_working_set_bytes}/
                  {data.containers.length}
                </span>
              </div>
              <div>
                已识别容器<strong>{data.containers.length}</strong>
                <span>
                  {data.sources.some((s) => s.truncated)
                    ? "部分结果已截断"
                    : "GPU 节点上的普通容器"}
                </span>
              </div>
            </div>
          )}
          <label className="search">
            <span className="sr-only">搜索业务资源</span>
            <input
              aria-label="搜索业务资源"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="搜索节点、Namespace、Pod 或模型"
            />
          </label>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>集群 / 节点</th>
                  <th>Pod / 容器</th>
                  <th>业务模型</th>
                  <th>CPU 近5分钟（核）</th>
                  <th>CPU requests / limits（核）</th>
                  <th>内存工作集</th>
                  <th>内存 requests / limits</th>
                  <th>详情</th>
                </tr>
              </thead>
              <tbody>
                {visible.map((r) => (
                  <tr key={`${r.cluster}-${r.pod_uid}-${r.container_name}`}>
                    <td>
                      {names[r.cluster]}
                      <small>{r.node}</small>
                    </td>
                    <td>
                      <strong>{r.pod_name}</strong>
                      <small>
                        {r.namespace_name} / {r.container_name}
                      </small>
                    </td>
                    <td>{r.model_name ?? "未映射"}</td>
                    <td>{cpu(r.cpu_usage_cores)}</td>
                    <td>
                      {r.cpu_request_cores == null &&
                      r.metric_statuses.cpu_request_cores === "missing"
                        ? "未报告"
                        : cpu(r.cpu_request_cores)}{" "}
                      /{" "}
                      {r.cpu_limit_cores == null &&
                      r.metric_statuses.cpu_limit_cores === "missing"
                        ? "未报告"
                        : cpu(r.cpu_limit_cores)}
                    </td>
                    <td>{memory(r.memory_working_set_bytes)}</td>
                    <td>
                      {r.memory_request_bytes == null &&
                      r.metric_statuses.memory_request_bytes === "missing"
                        ? "未报告"
                        : memory(r.memory_request_bytes)}{" "}
                      /{" "}
                      {r.memory_limit_bytes == null &&
                      r.metric_statuses.memory_limit_bytes === "missing"
                        ? "未报告"
                        : memory(r.memory_limit_bytes)}
                    </td>
                    <td>
                      <button className="link" onClick={() => setSelected(r)}>
                        容器详情
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!rows.length && (
            <p className="empty">
              {query ? "没有匹配的业务资源" : "暂无可核验的容器资源"}
            </p>
          )}
          {rows.length > 25 && (
            <div className="host-detail-toolbar">
              <button
                disabled={page === 0}
                onClick={() => setPage((p) => p - 1)}
              >
                上一页
              </button>
              <span>
                {page + 1} / {Math.ceil(rows.length / 25)}
              </span>
              <button
                disabled={(page + 1) * 25 >= rows.length}
                onClick={() => setPage((p) => p + 1)}
              >
                下一页
              </button>
            </div>
          )}
          <DataDetails label="采集范围与来源">
            <p>
              数据库查询 {dateTime(data.database_read_at)}
              {data.mode === "mock" ? " · Mock" : ""}
            </p>
            <p>
              CPU 为近5分钟使用核数，内存为 working set；requests/limits
              是普通容器声明值。未报告不补零，不作为 Pod
              有效调度需求或节点剩余量。
            </p>
            {data.sources.map((s) => (
              <p key={s.cluster}>
                {names[s.cluster]} · {states[s.status]} · 采集{" "}
                {dateTime(s.evaluated_at)} · 元数据识别 {s.row_count} 个容器
              </p>
            ))}
          </DataDetails>
        </>
      )}
      {selected && (
        <DetailDrawer
          title={selected.container_name}
          label="容器资源详情"
          close={() => setSelected(null)}
        >
          <dl className="host-kube-facts">
            {Object.entries({
              集群: names[selected.cluster],
              节点: selected.node,
              Namespace: selected.namespace_name,
              Pod: selected.pod_name,
              业务模型: selected.model_name ?? "未映射",
              Running:
                selected.running == null ? "未知" : String(selected.running),
              "Pod UID": selected.pod_uid,
              "容器 ID": selected.container_id ?? "未知",
            }).map(([k, v]) => (
              <div key={k}>
                <dt>{k}</dt>
                <dd>{v}</dd>
              </div>
            ))}
          </dl>
          <section className="operations-section">
            <h3>指标核验</h3>
            {Object.entries(selected.metric_statuses).map(([k, v]) => (
              <p key={k}>
                {k} · {states[v] ?? v}
              </p>
            ))}
          </section>
        </DetailDrawer>
      )}
    </section>
  );
}
