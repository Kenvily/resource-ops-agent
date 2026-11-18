import { useEffect, useState } from "react";
import { dateTime } from "./Metrics";
import { modules, views } from "../navigation";

type Daily = {
  date: string;
  pv: number | null;
  dau: number | null;
  feature_uses: number | null;
};
type Report = {
  enabled: boolean;
  tracking_started_at: number | null;
  pv: number;
  uv: number;
  dau: number | null;
  feature_uses: number;
  daily: Daily[];
  pages: { page: string; pv: number; feature_uses: number }[];
  features: { feature: string; count: number }[];
  agents: {
    tasks: number;
    terminal_tasks: number;
    model_tasks: number;
    unknown_model_tasks: number;
    success_rate: number | null;
    avg_duration_ms: number | null;
    duration_samples: number;
    statuses: Record<string, number>;
    kinds: Record<string, number>;
  };
  definitions: string[];
};
const moduleNames: Record<string, string> = {
  ...modules,
  settings: "数据与运行",
};
const featureNames: Record<string, string> = {
  assistant_query: "资源助手查询",
  diagnosis_run: "资源变化诊断",
  efficiency_run: "占用核查",
  planning_assess: "部署评估",
  report_generate: "生成运营报告",
  report_export: "导出运营报告",
  history_change: "历史指标/范围切换",
  manual_refresh: "手动刷新数据",
};
const states: Record<string, string> = {
  complete: "完整完成",
  limited: "结论受限",
  running: "运行中",
  waiting_input: "等待补充",
  failed: "失败",
  cancelled: "取消",
  interrupted: "中断",
  refused: "拒绝",
};
const taskNames: Record<string, string> = {
  resource_investigation: "资源助手",
  allocation_change: "资源诊断",
  gpu_usage: "占用调查",
};
function pageName(page: string) {
  const [module, view] = page.split(".");
  const choices = views[module as keyof typeof views] ?? [];
  return `${moduleNames[module] ?? module}${view ? ` / ${choices.find(([key]) => key === view)?.[1] ?? view}` : ""}`;
}
function VisitChart({ rows }: { rows: Daily[] }) {
  const topValue = Math.max(
    1,
    ...rows.map((r) => r.pv ?? 0),
    ...rows.map((r) => r.dau ?? 0),
  );
  const top = Math.max(2, Math.ceil(topValue / 2) * 2);
  const x = (index: number) =>
    rows.length === 1 ? 450 : 40 + (index * 820) / (rows.length - 1);
  const line = (key: "pv" | "dau") =>
    rows
      .map((row, index) =>
        row[key] === null
          ? null
          : `${x(index)},${160 - (row[key]! / top) * 140}`,
      )
      .filter(Boolean)
      .join(" ");
  return (
    <div className="usage-chart">
      <div className="usage-legend">
        <span>● 页面访问 PV</span>
        <span>● 活跃浏览器 DAU</span>
      </div>
      <div className="usage-chart-plot">
        <div className="usage-chart-y">
          <span>{top}</span>
          <span>{top / 2}</span>
          <span>0</span>
        </div>
        <svg
          preserveAspectRatio="none"
          viewBox="0 0 900 180"
          role="img"
          aria-label="页面访问与活跃浏览器趋势"
        >
          {[0, 0.5, 1].map((ratio) => (
            <g key={ratio}>
              <line
                x1="40"
                x2="860"
                y1={160 - ratio * 140}
                y2={160 - ratio * 140}
                stroke="var(--border)"
              />
            </g>
          ))}
          <polyline
            points={line("pv")}
            fill="none"
            stroke="var(--accent)"
            strokeWidth="3"
            vectorEffect="non-scaling-stroke"
          />
          <polyline
            points={line("dau")}
            fill="none"
            stroke="#b45309"
            strokeWidth="3"
            vectorEffect="non-scaling-stroke"
          />
          {rows.map((row, index) => (
            <g key={row.date}>
              {row.pv !== null && (
                <circle
                  cx={x(index)}
                  cy={160 - (row.pv / top) * 140}
                  r="2.5"
                  fill="var(--accent)"
                />
              )}
              {row.dau !== null && (
                <circle
                  cx={x(index)}
                  cy={160 - (row.dau / top) * 140}
                  r="2.5"
                  fill="#b45309"
                />
              )}
            </g>
          ))}
        </svg>
      </div>
      <div className="usage-chart-x">
        <span>{rows[0]?.date}</span>
        <span>{rows.at(-1)?.date}</span>
      </div>
    </div>
  );
}
export function UsageAnalytics() {
  const [window, setWindow] = useState("7d");
  const [revision, setRevision] = useState(0);
  const [data, setData] = useState<Report | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    setBusy(true);
    setData(null);
    setError("");
    fetch(`/api/v1/analytics/summary?window=${window}`, {
      signal: controller.signal,
    })
      .then(async (response) => {
        const body = await response.json();
        if (!response.ok)
          throw Error(
            typeof body.detail === "string" ? body.detail : "使用统计暂不可用",
          );
        return body;
      })
      .then((value) => {
        if (!controller.signal.aborted) setData(value);
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(e.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setBusy(false);
      });
    return () => controller.abort();
  }, [window, revision]);
  const perModule = Object.entries(moduleNames).map(([key, name]) => ({
    key,
    name,
    pv:
      data?.pages
        ?.filter((p) => p.page.split(".")[0] === key)
        .reduce((sum, p) => sum + p.pv, 0) ?? 0,
    uses:
      data?.pages
        ?.filter((p) => p.page.split(".")[0] === key)
        .reduce((sum, p) => sum + p.feature_uses, 0) ?? 0,
  }));
  return (
    <div className="usage-analytics">
      <section className="panel">
        <div className="panel-head">
          <h2>使用统计</h2>
          <div className="usage-controls">
            <label>
              统计范围{" "}
              <select
                value={window}
                onChange={(e) => setWindow(e.target.value)}
              >
                <option value="1d">今天</option>
                <option value="7d">近7天</option>
                <option value="30d">近30天</option>
                <option value="90d">近90天</option>
              </select>
            </label>
            <button
              className="button-secondary"
              disabled={busy}
              onClick={() => setRevision((v) => v + 1)}
            >
              刷新统计
            </button>
          </div>
        </div>
        {busy && <p role="status">正在读取统计…</p>}
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        {data && !data.enabled && <p className="notice">使用统计已关闭</p>}
        {data?.enabled && (
          <>
            <div className="usage-kpis">
              {[
                ["页面访问 PV", data.pv],
                ["独立浏览器 UV", data.uv],
                ["今日活跃 DAU", data.dau],
                ["功能操作", data.feature_uses],
              ].map(([label, value]) => (
                <div key={label}>
                  <span>{label}</span>
                  <strong>{value?.toLocaleString("zh-CN") ?? "—"}</strong>
                </div>
              ))}
            </div>
            <VisitChart rows={data.daily} />
            <details>
              <summary>每日访问明细</summary>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>日期</th>
                      <th>PV</th>
                      <th>DAU</th>
                      <th>功能操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.daily.map((day) => (
                      <tr key={day.date}>
                        <td>{day.date}</td>
                        <td>{day.pv ?? "—"}</td>
                        <td>{day.dau ?? "—"}</td>
                        <td>{day.feature_uses ?? "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          </>
        )}
      </section>
      {data?.enabled && (
        <>
          <section className="panel">
            <h2>功能模块</h2>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>模块</th>
                    <th>页面访问</th>
                    <th>关键操作触发</th>
                  </tr>
                </thead>
                <tbody>
                  {perModule.map((module) => (
                    <tr key={module.key}>
                      <td>{module.name}</td>
                      <td>{module.pv}</td>
                      <td>{module.uses}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <details>
              <summary>页面与功能明细</summary>
              <div className="usage-columns">
                <div>
                  {data.pages.map((page) => (
                    <p key={page.page}>
                      {pageName(page.page)} <strong>{page.pv} 次访问</strong>
                    </p>
                  ))}
                </div>
                <div>
                  {data.features.map((feature) => (
                    <p key={feature.feature}>
                      {featureNames[feature.feature] ?? feature.feature}{" "}
                      <strong>{feature.count} 次</strong>
                    </p>
                  ))}
                  {!data.features.length && <p>尚无关键操作记录</p>}
                </div>
              </div>
            </details>
          </section>
          <section className="panel">
            <h2>Agent 任务</h2>
            <div className="usage-kpis">
              {[
                ["任务总量", data.agents.tasks],
                ["已记录 LLM 任务", data.agents.model_tasks],
                [
                  "完整完成率",
                  data.agents.success_rate === null
                    ? "—"
                    : `${(data.agents.success_rate * 100).toFixed(1)}%`,
                ],
                [
                  "平均耗时",
                  data.agents.avg_duration_ms === null
                    ? "—"
                    : `${(data.agents.avg_duration_ms / 1000).toFixed(2)} 秒`,
                ],
              ].map(([label, value]) => (
                <div key={label}>
                  <span>{label}</span>
                  <strong>{value}</strong>
                </div>
              ))}
            </div>
            <div className="usage-states">
              {Object.entries(data.agents.statuses).map(([status, count]) => (
                <span key={status}>
                  {states[status] ?? status} <strong>{count}</strong>
                </span>
              ))}
            </div>
            <details>
              <summary>任务类型与耗时样本</summary>
              {Object.entries(data.agents.kinds).map(([kind, count]) => (
                <p key={kind}>
                  {taskNames[kind] ?? kind}：{count} 个
                </p>
              ))}
              <p>
                调用情况未知 {data.agents.unknown_model_tasks ?? 0} 个。耗时样本{" "}
                {data.agents.duration_samples} 个，已结束任务{" "}
                {data.agents.terminal_tasks} 个
              </p>
            </details>
          </section>
          <details className="panel usage-definitions">
            <summary>统计口径与采集范围</summary>
            <p>
              {data.tracking_started_at
                ? `访问统计始于 ${dateTime(data.tracking_started_at)}`
                : "尚未收到访问事件"}
            </p>
            <ul>
              {data.definitions.map((text) => (
                <li key={text}>{text}</li>
              ))}
            </ul>
          </details>
        </>
      )}
    </div>
  );
}
