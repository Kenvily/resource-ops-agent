import { useEffect, useState } from "react";
import { DetailDrawer } from "./DetailDrawer";
import { DataDetails } from "./DataDetails";
import { dateTime } from "./Metrics";
import { memoryValue, type DisplayUnit } from "./memory";
import { usagePercent } from "./EfficiencyEvidence";
import type { HostMetrics } from "../api/operations";
import { timeRanges } from "../types/windows";
import type { WindowSize } from "../types/trend";

type Point = {
  timestamp: number;
  mean: number | null;
  sampled_value?: number | null;
  min: number | null;
  max: number | null;
  valid_points: number;
  expected_points: number;
  coverage: number;
};
type HostReport = {
  current: HostMetrics | null;
  database_read_at: number;
  collection_enabled: boolean;
  history: {
    start: number;
    end: number;
    grain: number;
    source_grain: number;
    sampling?: "hourly_point" | "minute_statistics";
    series: { metric: string; label: string; unit: string; points: Point[] }[];
  };
  definitions: Record<string, { source: string }>;
};
const statuses: Record<string, string> = {
  ready: "已接入",
  unverified: "未通过核验",
  partial: "部分覆盖",
  missing: "暂无序列",
  unmapped: "未关联或冲突",
  error: "采集失败",
  stale: "已过期",
};
export function HostResourceDetail({
  cluster,
  node,
  displayUnit,
  close,
}: {
  cluster: string;
  node: string;
  displayUnit: DisplayUnit;
  close: () => void;
}) {
  const [windowSize, setWindow] = useState<WindowSize>("1h"),
    [metric, setMetric] = useState("cpu_utilization_pct");
  const [report, setReport] = useState<HostReport | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [loadedWindow, setLoadedWindow] = useState<WindowSize | null>(null);
  useEffect(() => {
    setReport(null);
  }, [cluster, node]);
  useEffect(() => {
    const c = new AbortController();
    setBusy(true);
    setError("");
    fetch("/api/v1/resource-statistics/host", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      signal: c.signal,
      body: JSON.stringify({
        cluster,
        node,
        window: windowSize,
        history_mode: "auto",
      }),
    })
      .then(async (r) => {
        const data = await r.json();
        if (!r.ok)
          throw Error(
            typeof data.detail === "string" ? data.detail : "主机资源查询失败",
          );
        return data;
      })
      .then((v) => {
        if (!c.signal.aborted) {
          setReport(v);
          setLoadedWindow(windowSize);
        }
      })
      .catch((e) => {
        if (!c.signal.aborted) {
          setError(e.message);
          setReport(null);
        }
      })
      .finally(() => {
        if (!c.signal.aborted) setBusy(false);
      });
    return () => c.abort();
  }, [cluster, node, windowSize, revision]);
  const amount = (raw: number | null | undefined) => {
    const x = memoryValue(raw, "bytes", displayUnit);
    return x.value === "—" ? "—" : `${x.value} ${x.unit}`;
  };
  const rate = (raw: number | null | undefined) => {
    if (raw == null) return "—";
    const unit = raw >= 1e9 ? "GB/s" : "MB/s";
    return `${(raw / (raw >= 1e9 ? 1e9 : 1e6)).toLocaleString("zh-CN", { maximumSignificantDigits: 3 })} ${unit}`;
  };
  const host = report?.current;
  const series =
    loadedWindow === windowSize
      ? report?.history.series.find((s) => s.metric === metric)
      : undefined;
  const points = series?.points ?? [];
  const value = (v: number | null) =>
    v == null
      ? "—"
      : series?.unit === "bytes"
        ? amount(v)
        : series?.unit === "%"
          ? usagePercent(v)
          : v.toLocaleString("zh-CN", { maximumSignificantDigits: 4 });
  const pointValue = (p: Point) => p.sampled_value ?? p.mean;
  const upper =
    metric === "cpu_utilization_pct"
      ? 100
      : Math.max(
          1,
          ...points.flatMap((p) =>
            pointValue(p) == null ? [] : [pointValue(p)!],
          ),
        );
  const segments: string[] = [];
  let part: string[] = [];
  points.forEach((p, i) => {
    if (pointValue(p) == null) {
      if (part.length) segments.push(part.join(" "));
      part = [];
    } else
      part.push(
        `${48 + (i * 552) / Math.max(1, points.length - 1)},${150 - (130 * pointValue(p)!) / upper}`,
      );
  });
  if (part.length) segments.push(part.join(" "));
  return (
    <DetailDrawer title={node} label="机器资源详情" close={close}>
      <div className="host-detail-toolbar">
        <strong>{cluster === "yz" ? "亦庄" : "兆维"} · 主机资源</strong>
        <button disabled={busy} onClick={() => setRevision((v) => v + 1)}>
          刷新数据库数据
        </button>
      </div>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {busy && (
        <p role="status">
          {report ? "正在读取主机资源，暂显示上次结果…" : "正在读取主机资源…"}
        </p>
      )}
      {report && (
        <>
          {(!report.collection_enabled || !host || host.status === "stale") && (
            <p className="notice">
              {!report.collection_enabled
                ? "主机采集未启用"
                : !host
                  ? "暂无主机快照"
                  : "主机快照已过期"}
            </p>
          )}
          <div className="host-resource-summary">
            <div>
              CPU 利用率
              <strong>{usagePercent(host?.cpu_utilization_pct)}</strong>
              <span>{host?.cpu_cores ?? "—"} 逻辑核</span>
            </div>
            <div>
              系统内存已用<strong>{amount(host?.memory_used_bytes)}</strong>
              <span>容量 {amount(host?.memory_total_bytes)}</span>
            </div>
            <div>
              1 分钟负载
              <strong>
                {host?.load1?.toLocaleString("zh-CN", {
                  maximumFractionDigits: 2,
                }) ?? "—"}
              </strong>
              <span>{host?.server_model ?? "整机型号未知"}</span>
            </div>
          </div>
          <section className="operations-section">
            <div className="host-history-controls">
              <h3>主机历史</h3>
              <label>
                <span className="sr-only">主机历史指标</span>
                <select
                  aria-label="主机历史指标"
                  value={metric}
                  onChange={(e) => setMetric(e.target.value)}
                >
                  {report.history.series.map((s) => (
                    <option key={s.metric} value={s.metric}>
                      {s.label}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                <span className="sr-only">主机历史时间范围</span>
                <select
                  aria-label="主机历史时间范围"
                  value={windowSize}
                  onChange={(e) => setWindow(e.target.value as WindowSize)}
                >
                  {timeRanges.map((r) => (
                    <option key={r.value} value={r.value}>
                      {r.value === "all" ? "全部已存历史" : r.label}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <p className="query-stamp">
              {loadedWindow === windowSize ? (
                <>
                  {dateTime(report.history.start)} —{" "}
                  {dateTime(report.history.end)} · 步长{" "}
                  {report.history.grain >= 3600
                    ? `${report.history.grain / 3600} 小时`
                    : `${report.history.grain / 60} 分钟`}
                  {report.history.sampling === "hourly_point" && " · 时点采样"}
                  {` · 有效 ${points.filter((p) => pointValue(p) != null).length} / ${points.length} 点`}
                </>
              ) : (
                "正在查询所选时间范围…"
              )}
            </p>
            {segments.length ? (
              <svg
                viewBox="0 0 620 180"
                className="host-history-chart"
                role="img"
                aria-label="主机资源历史；缺测处断开"
              >
                <text x="0" y="18">
                  {value(upper)}
                </text>
                <text x="16" y="154">
                  0
                </text>
                <line x1="48" y1="150" x2="600" y2="150" stroke="#dce4e9" />
                {segments.map((s, i) => {
                  if (!s.includes(" ")) {
                    const [cx, cy] = s.split(",").map(Number);
                    return (
                      <circle
                        key={i}
                        cx={cx}
                        cy={cy}
                        r={3}
                        fill="var(--chart-yz)"
                      />
                    );
                  }
                  return (
                    <polyline
                      key={i}
                      points={s}
                      fill="none"
                      stroke="var(--chart-yz)"
                      strokeWidth="2.5"
                      strokeLinecap="round"
                    />
                  );
                })}
              </svg>
            ) : (
              <p className="empty">
                {busy
                  ? "正在加载主机历史…"
                  : "当前范围尚未回填或存在缺测，缺口不补零。"}
              </p>
            )}
            <details>
              <summary>历史采样与覆盖</summary>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>时间</th>
                      <th>
                        {report.history.sampling === "hourly_point"
                          ? "采样值"
                          : "均值"}
                      </th>
                      <th>已观测极值</th>
                      <th>有效采样</th>
                    </tr>
                  </thead>
                  <tbody>
                    {points.map((p) => (
                      <tr key={p.timestamp}>
                        <td>{dateTime(p.timestamp)}</td>
                        <td>{value(pointValue(p))}</td>
                        <td>
                          {value(p.min)} / {value(p.max)}
                        </td>
                        <td>
                          {p.valid_points}/{p.expected_points} ·{" "}
                          {(p.coverage * 100).toFixed(1)}%
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          </section>
          <section className="operations-section">
            <h3>文件系统</h3>
            {host?.filesystems?.length ? (
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>挂载点 / 设备</th>
                      <th>容量</th>
                      <th>普通用户可用</th>
                    </tr>
                  </thead>
                  <tbody>
                    {host.filesystems.map((f) => (
                      <tr
                        key={JSON.stringify([
                          f.device,
                          f.mountpoint,
                          f.filesystem,
                        ])}
                      >
                        <td>
                          {f.mountpoint}
                          <small>
                            {f.device} · {f.filesystem}
                          </small>
                        </td>
                        <td>{amount(f.total_bytes)}</td>
                        <td>
                          {amount(f.available_bytes)}
                          {f.availability_ratio != null
                            ? ` · ${(f.availability_ratio * 100).toFixed(1)}%`
                            : ""}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="empty">暂无有效文件系统数据</p>
            )}
            {host?.filesystems_truncated && (
              <p className="notice">仅显示前 128 个文件系统。</p>
            )}
          </section>
          <section className="operations-section">
            <h3>网络接口</h3>
            {host?.networks?.length ? (
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>接口</th>
                      <th>接收</th>
                      <th>发送</th>
                    </tr>
                  </thead>
                  <tbody>
                    {host.networks.map((n) => (
                      <tr key={n.interface}>
                        <td>{n.interface}</td>
                        <td>{rate(n.receive_bytes_per_second)}</td>
                        <td>{rate(n.transmit_bytes_per_second)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="empty">暂无有效网络接口数据</p>
            )}
            {host?.networks_truncated && (
              <p className="notice">仅显示前 64 个接口。</p>
            )}
          </section>
          <section className="operations-section">
            <h3>K8s 节点</h3>
            <dl className="host-kube-facts">
              <div>
                <dt>可分配 CPU 上限</dt>
                <dd>{host?.kubernetes?.allocatable_cpu_cores ?? "—"} 核</dd>
              </div>
              <div>
                <dt>可分配内存上限</dt>
                <dd>{amount(host?.kubernetes?.allocatable_memory_bytes)}</dd>
              </div>
              <div>
                <dt>调度开关</dt>
                <dd>
                  {host?.kubernetes?.unschedulable == null
                    ? "未知"
                    : host.kubernetes.unschedulable
                      ? "已禁止调度"
                      : "未禁止调度"}
                </dd>
              </div>
              {Object.entries(
                host?.kubernetes?.pressure ?? {
                  MemoryPressure: "unknown",
                  DiskPressure: "unknown",
                  PIDPressure: "unknown",
                },
              ).map(([k, v]) => (
                <div key={k}>
                  <dt>
                    {{
                      MemoryPressure: "内存压力",
                      DiskPressure: "磁盘压力",
                      PIDPressure: "进程数压力",
                    }[k] ?? k}
                  </dt>
                  <dd>
                    {v === "true"
                      ? "存在压力"
                      : v === "false"
                        ? "未触发"
                        : "未知"}
                  </dd>
                </div>
              ))}
            </dl>
          </section>
          <DataDetails label="数据来源与口径">
            <p>
              数据库查询 {dateTime(report.database_read_at)} · 采集{" "}
              {dateTime(host?.evaluated_at)}
            </p>
            {Object.entries(report.definitions).map(([k, d]) => (
              <p key={k}>{d.source}</p>
            ))}
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>固定指标</th>
                    <th>状态</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries({
                    ...host?.metric_statuses,
                    ...host?.extended_metric_statuses,
                  }).map(([k, v]) => (
                    <tr key={k}>
                      <td>{k}</td>
                      <td>
                        {host?.status === "stale"
                          ? "已过期"
                          : (statuses[v] ?? v)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </DataDetails>
        </>
      )}
    </DetailDrawer>
  );
}
