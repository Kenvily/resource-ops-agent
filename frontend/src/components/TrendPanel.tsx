import { DataDetails } from "./DataDetails";
import { useEffect, useRef, useState } from "react";
import { RefreshCw } from "lucide-react";
import type { ClusterFilter, Gpu } from "../types/resource";
import type { TrendPoint, TrendResponse, WindowSize } from "../types/trend";
import { dateTime, percent, StatusBadge } from "./Metrics";
import { rangeLabel, timeRanges } from "../types/windows";
import type { MemoryFormatter } from "./memory";
import { memoryValue, type DisplayUnit } from "./memory";
import { durationLabel } from "./duration";

type Metric =
  | "allocated_raw"
  | "unallocated_raw"
  | "allocation_ratio"
  | "gpu_util"
  | "gpu_memory"
  | "cpu_utilization_pct"
  | "memory_used_bytes"
  | "load1";
const extraMetrics: Record<string, string> = {
  gpu_util: "GPU 利用率",
  gpu_memory: "物理显存已用",
  cpu_utilization_pct: "CPU 利用率",
  memory_used_bytes: "系统内存已用",
  load1: "节点总负载",
};
const date = (value: number) =>
  new Date(value * 1000).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
export function TrendPanel({
  cluster,
  memory: fallbackMemory,
  displayUnit,
  gpu,
  initialWindow = "1d",
  onWindowChange,
  onMetricSourceChange,
}: {
  cluster: ClusterFilter;
  memory: MemoryFormatter;
  displayUnit?: DisplayUnit;
  gpu?: Gpu;
  initialWindow?: WindowSize;
  onWindowChange?: (window: WindowSize) => void;
  onMetricSourceChange?: (actual: boolean) => void;
}) {
  const [windowSize, setWindow] = useState<WindowSize>(initialWindow);
  useEffect(() => setWindow(initialWindow), [initialWindow]);
  const [metric, setMetric] = useState<Metric>("allocated_raw");
  const actual = Object.hasOwn(extraMetrics, metric);
  const measurement = actual ? metric : "quota";
  useEffect(
    () => onMetricSourceChange?.(actual),
    [actual, onMetricSourceChange],
  );
  const pointValue = (p?: TrendPoint) =>
    p
      ? actual
        ? (p.value ?? null)
        : p[metric as "allocated_raw" | "unallocated_raw" | "allocation_ratio"]
      : null;
  const [data, setData] = useState<TrendResponse | null>(null);
  const memory: MemoryFormatter = (value, kind = "allocation") => {
    if (!displayUnit) return fallbackMemory(value, kind);
    const source =
      kind === "unallocated"
        ? data?.units.capacity === data?.units.allocation
          ? data?.units.capacity
          : "unverified"
        : data?.units[kind];
    const formatted = memoryValue(value, source, displayUnit);
    return formatted.value === "—"
      ? "—"
      : `${formatted.value} ${formatted.unit}`;
  };
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const [revision, setRevision] = useState(0),
    [selected, setSelected] = useState<number | null>(null);
  const [tableOpen, setTableOpen] = useState(false),
    [page, setPage] = useState(0);
  const chartRef = useRef<SVGSVGElement>(null);
  const [chartWidth, setChartWidth] = useState(1000);
  const node = gpu?.node,
    uuid = gpu?.device_uuid;
  useEffect(() => {
    setData(null);
    setError("");
    setSelected(null);
    setPage(0);
  }, [cluster, node, uuid, measurement]);
  useEffect(() => {
    const controller = new AbortController();
    let active = true,
      timedOut = false;
    setBusy(true);
    setError("");
    const timeout = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, 35000);
    const query = new URLSearchParams({ cluster, window: windowSize });
    if (node && uuid) {
      query.set("node", node);
      query.set("uuid", uuid);
    }
    if (revision > 0) query.set("refresh", "true");
    fetch(
      actual ? "/api/v1/resource-statistics/trends" : `/api/v1/trends?${query}`,
      {
        ...(actual
          ? {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                cluster,
                window: windowSize,
                metric,
                history_mode: "auto",
              }),
            }
          : {}),
        signal: controller.signal,
        cache: "no-store",
      },
    )
      .then(async (response) => {
        if (!response.ok) throw new Error("历史接口暂时不可用");
        return response.json() as Promise<TrendResponse>;
      })
      .then((result) => {
        if (active) {
          setData(result);
          setSelected(null);
          setPage(0);
        }
      })
      .catch((reason) => {
        if (active && (!controller.signal.aborted || timedOut))
          setError(
            `${timedOut ? "历史查询超时" : reason.message}；如有旧曲线，仅供查看上次结果。`,
          );
      })
      .finally(() => {
        window.clearTimeout(timeout);
        if (active) setBusy(false);
      });
    return () => {
      active = false;
      controller.abort();
      window.clearTimeout(timeout);
    };
  }, [cluster, windowSize, node, uuid, revision, measurement]);
  useEffect(() => {
    const timer = window.setInterval(() => setRevision((n) => n + 1), 60000);
    return () => window.clearInterval(timer);
  }, [windowSize]);
  const count = data?.series[0]?.points.length ?? 0;
  const cursor = Math.min(
    selected ?? Math.max(0, count - 1),
    Math.max(0, count - 1),
  );
  const format = (value: number | null) => {
    if (actual) {
      if (value == null) return "—";
      if (metric === "gpu_util" || metric === "cpu_utilization_pct")
        return `${value.toFixed(1)}%`;
      if (metric === "load1") return value.toFixed(2);
      const v = memoryValue(
        value,
        metric === "gpu_memory" ? "MiB" : "bytes",
        displayUnit ?? "GB",
      );
      return `${v.value} ${v.unit}`;
    }
    return metric === "allocation_ratio"
      ? percent(value)
      : memory(
          value,
          metric === "unallocated_raw" ? "unallocated" : "allocation",
        );
  };
  const values =
    data?.series.flatMap((s) =>
      s.points.flatMap((p) =>
        pointValue(p) == null ? [] : [pointValue(p) as number],
      ),
    ) ?? [];
  const min = Math.min(0, ...values);
  let max = Math.max(0, ...values);
  if (max === min)
    max =
      metric === "allocation_ratio"
        ? 1
        : Math.max(
            1,
            ...(data?.series.flatMap((s) =>
              s.points.map((p) => p.capacity_raw ?? 0),
            ) ?? []),
          );
  const x = (index: number) =>
    16 + (index / Math.max(1, count - 1)) * (chartWidth - 32);
  const y = (value: number) =>
    190 - ((value - min) / Math.max(1, max - min)) * 155;
  const records =
    data?.series.flatMap((s) =>
      s.points.map((p) => ({ label: s.label, ...p })),
    ) ?? [];
  useEffect(() => {
    if (!chartRef.current) return;
    const observer = new ResizeObserver((entries) =>
      setChartWidth(Math.max(250, entries[0].contentRect.width)),
    );
    observer.observe(chartRef.current);
    return () => observer.disconnect();
  }, [values.length > 0]);
  function segments(points: TrendPoint[]) {
    const result: { x: number; y: number }[][] = [];
    let current: { x: number; y: number }[] = [];
    points.forEach((p, i) => {
      if (pointValue(p) == null) {
        if (current.length) result.push(current);
        current = [];
      } else current.push({ x: x(i), y: y(pointValue(p) as number) });
    });
    if (current.length) result.push(current);
    return result;
  }
  return (
    <>
      <section
        className={`card trend-panel ${gpu ? "gpu-trend" : ""}`}
        aria-label={gpu ? "单卡历史趋势" : "集群历史趋势"}
        aria-busy={busy}
      >
        <div className="panel-heading">
          <div>
            <h2>
              {gpu
                ? "单卡分配变化"
                : actual
                  ? `${extraMetrics[metric]}趋势`
                  : "分配变化趋势"}
            </h2>
          </div>
          <button
            className="icon-button"
            disabled={busy}
            onClick={() => setRevision((n) => n + 1)}
            aria-label={gpu ? "刷新单卡历史" : "刷新集群历史"}
          >
            <RefreshCw
              aria-hidden="true"
              size={16}
              className={busy ? "spin" : ""}
            />
          </button>
        </div>
        <div className="trend-tools">
          <label className="range-select">
            时间范围
            <select
              data-analytics-feature="history_change"
              aria-label="历史时间范围"
              value={windowSize}
              onChange={(e) => {
                setWindow(e.target.value as WindowSize);
                onWindowChange?.(e.target.value as WindowSize);
              }}
            >
              {timeRanges.map((r) => (
                <option key={r.value} value={r.value}>
                  {r.label}
                </option>
              ))}
            </select>
          </label>
          <select
            data-analytics-feature="history_change"
            aria-label="历史趋势指标"
            value={metric}
            onChange={(e) => setMetric(e.target.value as Metric)}
          >
            <option value="allocated_raw">已分配额度</option>
            <option value="unallocated_raw">未分配额度</option>
            <option value="allocation_ratio">虚拟显存分配率</option>
            {!gpu &&
              Object.entries(extraMetrics).map(([key, label]) => (
                <option key={key} value={key}>
                  {label}
                </option>
              ))}
          </select>
        </div>
        {error && (
          <p className="notice" role="alert">
            {error}
          </p>
        )}
        {busy && data && (
          <p className="trend-note">
            后台查询中，当前仍显示 {rangeLabel(data.window)} 的上次结果。
          </p>
        )}
        {!data ? (
          <div className="trend-placeholder">
            {busy ? "正在加载聚合历史…" : "暂无历史数据，请重试"}
          </div>
        ) : (
          <>
            <div className="trend-legends">
              {data.series.map((series) => (
                <div key={series.id}>
                  <span className={`series-line series-${series.id}`} />
                  <b>
                    {series.label}
                    {gpu ? " · 当前 GPU" : ""}
                  </b>
                  <StatusBadge status={series.status} />
                  {series.missing_points || series.invalid_points ? (
                    <span className="coverage-warning">
                      有效 {series.observed_points}/{series.expected_points} 点
                    </span>
                  ) : null}
                </div>
              ))}
            </div>
            <DataDetails label="历史数据详情">
              {data.series.map((series) => (
                <section key={series.id}>
                  <strong>{series.label}</strong>{" "}
                  {series.source_start_at && (
                    <small>已发现起点 {dateTime(series.source_start_at)}</small>
                  )}
                  {series.database_read_at && (
                    <small>
                      数据库查询 {dateTime(series.database_read_at)} ·
                      历史最近同步记录 {dateTime(series.history_collected_at)}
                    </small>
                  )}
                  {series.expected_points != null && (
                    <small>
                      有效 {series.observed_points}/{series.expected_points} 点
                      · 待同步 {series.missing_points ?? 0} 点 · 质量待核查{" "}
                      {series.invalid_points ?? 0} 点
                    </small>
                  )}
                </section>
              ))}
            </DataDetails>
            {values.length ? (
              <>
                <svg
                  className={`trend-chart ${error ? "old-history" : ""}`}
                  ref={chartRef}
                  viewBox={`0 0 ${chartWidth} 230`}
                  role="img"
                  aria-label="历史趋势折线图；缺口不连线，精确采样值可通过下方滑块和数据表查看"
                  onPointerMove={(event) => {
                    const box = event.currentTarget.getBoundingClientRect();
                    const point =
                      ((event.clientX - box.left) / box.width) * chartWidth;
                    setSelected(
                      Math.max(
                        0,
                        Math.min(
                          count - 1,
                          Math.round(
                            ((point - 16) / (chartWidth - 32)) * (count - 1),
                          ),
                        ),
                      ),
                    );
                  }}
                >
                  {[0, 0.5, 1].map((t) => (
                    <g key={t}>
                      <line
                        x1="16"
                        x2={chartWidth - 16}
                        y1={35 + t * 155}
                        y2={35 + t * 155}
                        className="chart-grid"
                      />
                      <text x="16" y={27 + t * 155} className="chart-label">
                        {format(max - (max - min) * t)}
                      </text>
                    </g>
                  ))}
                  {data.series.map((series) => (
                    <g
                      key={series.id}
                      className={`chart-series chart-series-${series.id}`}
                    >
                      {segments(series.points).map((segment, i) =>
                        segment.length === 1 ? (
                          <circle
                            key={i}
                            cx={segment[0].x}
                            cy={segment[0].y}
                            r="3"
                          />
                        ) : (
                          <polyline
                            key={i}
                            points={segment
                              .map((p) => `${p.x},${p.y}`)
                              .join(" ")}
                            fill="none"
                            strokeWidth="2.5"
                            vectorEffect="non-scaling-stroke"
                          />
                        ),
                      )}
                      {pointValue(series.points[cursor]) != null && (
                        <circle
                          cx={x(cursor)}
                          cy={y(pointValue(series.points[cursor]) as number)}
                          r="4"
                        />
                      )}
                    </g>
                  ))}
                  <line
                    x1={x(cursor)}
                    x2={x(cursor)}
                    y1="35"
                    y2="190"
                    className="chart-cursor"
                  />
                  <text x="16" y="220" className="chart-label">
                    {date(data.start)}
                  </text>
                  <text
                    x={chartWidth - 16}
                    y="220"
                    textAnchor="end"
                    className="chart-label"
                  >
                    {date(data.end)}
                  </text>
                </svg>
                <label className="trend-slider">
                  查看采样点
                  <input
                    type="range"
                    aria-label="历史采样点"
                    aria-valuetext={data.series
                      .map(
                        (s) =>
                          `${date(data.start + cursor * data.step)} ${s.label} ${format(pointValue(s.points[cursor]))}`,
                      )
                      .join("；")}
                    min="0"
                    max={Math.max(0, count - 1)}
                    value={cursor}
                    onChange={(e) => setSelected(Number(e.target.value))}
                  />
                </label>
                <output className="trend-readout" aria-live="off">
                  {date(data.start + cursor * data.step)}
                  {data.series.map((s) => (
                    <span key={s.id}>
                      {s.label}：<b>{format(pointValue(s.points[cursor]))}</b>
                    </span>
                  ))}
                </output>
              </>
            ) : (
              <div className="trend-placeholder">
                当前范围没有有效历史指标；请查看数据源提示。
              </div>
            )}
            {data.series.flatMap((s) =>
              s.warnings.map((w) => (
                <p className="trend-note warning" key={s.id + w}>
                  {s.label}：{w}
                </p>
              )),
            )}
            <details
              className="trend-table"
              onToggle={(e) => setTableOpen(e.currentTarget.open)}
            >
              <summary>查看采样数据</summary>
              {tableOpen && (
                <>
                  <div className="table-scroll">
                    <table>
                      <thead>
                        <tr>
                          <th>集群</th>
                          <th>采样时间</th>
                          <th>{actual ? extraMetrics[metric] : "已分配"}</th>
                          {!actual && <th>未分配</th>}
                        </tr>
                      </thead>
                      <tbody>
                        {records.slice(page * 40, (page + 1) * 40).map((p) => (
                          <tr key={p.label + p.timestamp}>
                            <td>{p.label}</td>
                            <td>{date(p.timestamp)}</td>
                            <td>
                              {actual
                                ? format(pointValue(p))
                                : memory(p.allocated_raw)}
                            </td>
                            {!actual && (
                              <td>
                                {memory(p.unallocated_raw, "unallocated")}
                              </td>
                            )}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <div className="trend-pagination">
                    <button
                      disabled={page === 0}
                      onClick={() => setPage((p) => p - 1)}
                    >
                      上一页
                    </button>
                    <span>
                      {page + 1} / {Math.max(1, Math.ceil(records.length / 40))}
                    </span>
                    <button
                      disabled={(page + 1) * 40 >= records.length}
                      onClick={() => setPage((p) => p + 1)}
                    >
                      下一页
                    </button>
                  </div>
                </>
              )}
            </details>
            <div className="panel-foot">
              步长 {durationLabel(data.step)}
              {data.sampling === "hourly_point" && " · 时点采样"}
            </div>
          </>
        )}
      </section>
    </>
  );
}
