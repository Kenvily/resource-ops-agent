import { createRequestId } from "../api/requestId";
import { useEffect, useRef, useState } from "react";
import { useWorkspace, workspace } from "./OperationsWorkspace";
import { useMonitoring, type MonitoringState } from "../hooks/useMonitoring";
import type { ClusterFilter } from "../types/resource";
import { dateTime } from "./Metrics";
import { memoryValue, type DisplayUnit } from "./memory";
const names: Record<string, string> = { yz: "亦庄", zw: "兆维" };
const states: Record<string, string> = {
  ready: "已接入",
  partial: "部分覆盖",
  missing: "无新鲜序列",
  unmapped: "身份未关联",
  error: "采集失败",
  stale: "已过期",
  not_collected: "尚未采集",
};
export function MonitoringPanel({
  cluster,
  displayUnit,
  compact = false,
  state,
}: {
  cluster: ClusterFilter;
  displayUnit: DisplayUnit;
  compact?: boolean;
  state?: MonitoringState;
}) {
  const caps = useWorkspace();
  const [captureError, setCaptureError] = useState("");
  const [captured, setCaptured] = useState<Record<string, string>>({});
  const [capturing, setCapturing] = useState("");
  const captureActive = useRef<AbortController | null>(null);
  useEffect(() => {
    captureActive.current?.abort();
    setCaptured({});
    setCapturing("");
    setCaptureError("");
    return () => captureActive.current?.abort();
  }, [cluster]);
  const own = useMonitoring(cluster, !state);
  const { data: monitoring, error, busy, refresh: load } = state ?? own;
  return (
    <>
      <section
        className={`card operations-panel ${compact ? "attention-panel" : ""}`}
        aria-label="指标与告警"
      >
        <div className="panel-heading">
          <h2>{compact ? "待关注问题" : "指标与告警"}</h2>
          <button type="button" onClick={() => void load()} disabled={busy}>
            {busy ? "读取中…" : "刷新数据库数据"}
          </button>
        </div>
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        {monitoring && (
          <>
            {!monitoring.collection_enabled && (
              <p className="notice">扩展指标后台采集未启用。</p>
            )}
            <div className="assessment-summary">
              <h3>
                {compact
                  ? `${monitoring.alert_count} 条待关注`
                  : `关注项 ${monitoring.alert_count} 条`}
              </h3>
              <span>
                {monitoring.mode === "mock" ? "Mock · " : ""}
                {monitoring.health === "attention"
                  ? "需要核查"
                  : monitoring.health === "unknown"
                    ? "部分指标未知"
                    : "未触发已配置规则"}
              </span>
            </div>
            {!compact && (
              <p className="query-stamp">
                数据库查询：{dateTime(monitoring.database_read_at)}
              </p>
            )}
            {captureError && (
              <p role="alert" className="error">
                {captureError}
              </p>
            )}
            {(compact ? monitoring.alerts.slice(0, 3) : monitoring.alerts).map(
              (alert, i) => (
                <article
                  className="operations-alert"
                  key={`${alert.cluster}-${alert.metric}-${i}`}
                >
                  <strong>
                    {names[alert.cluster]} · {alert.message}
                  </strong>
                  <p>
                    {alert.node}
                    {alert.device_uuid ? ` / ${alert.device_uuid}` : ""}
                  </p>
                  <span>采集评估时间：{dateTime(alert.evaluated_at)}</span>
                  {!compact && caps.write_enabled && (
                    <div className="context-links">
                      {captured[
                        `${alert.cluster}-${alert.metric}-${alert.node}-${alert.device_uuid}`
                      ] ? (
                        <a href="#operations?view=issues">查看待核查问题</a>
                      ) : (
                        <button
                          type="button"
                          disabled={!!capturing}
                          onClick={async () => {
                            const key = `${alert.cluster}-${alert.metric}-${alert.node}-${alert.device_uuid}`;
                            const c = new AbortController();
                            captureActive.current = c;
                            setCapturing(key);
                            setCaptureError("");
                            try {
                              const result = await workspace<{ id: string }>(
                                "issues",
                                {
                                  request_id: createRequestId(),
                                  cluster: alert.cluster,
                                  metric: alert.metric,
                                  node: alert.node,
                                  device_uuid: alert.device_uuid,
                                },
                                c.signal,
                              );
                              if (!c.signal.aborted)
                                setCaptured((v) => ({
                                  ...v,
                                  [key]: result.id,
                                }));
                            } catch (e) {
                              if (!c.signal.aborted)
                                setCaptureError(
                                  e instanceof Error ? e.message : "加入失败",
                                );
                            } finally {
                              if (!c.signal.aborted) setCapturing("");
                            }
                          }}
                        >
                          加入待核查
                        </button>
                      )}
                    </div>
                  )}
                </article>
              ),
            )}
            {monitoring.truncated && (
              <p className="notice">仅展示前 200 条关注项。</p>
            )}
            {compact ? (
              <a className="link" href="#operations?view=monitoring">
                查看全部指标与关注项
              </a>
            ) : (
              <details className="operations-section">
                <summary>指标接入与覆盖</summary>
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>集群 / 指标</th>
                        <th>状态</th>
                        <th>有效实体</th>
                        <th>来源单位</th>
                        <th>评估时间</th>
                      </tr>
                    </thead>
                    <tbody>
                      {monitoring.metrics.map((m) => (
                        <tr key={`${m.cluster}-${m.key}`}>
                          <td>
                            {names[m.cluster]} / {m.label}
                            <details>
                              <summary>查看来源与数值</summary>
                              <p>{m.source}</p>
                              {m.values.map((v, i) => (
                                <p key={i}>
                                  {v.node}
                                  {v.device_uuid ? ` / ${v.device_uuid}` : ""}：
                                  {m.unit === "MiB"
                                    ? (() => {
                                        const x = memoryValue(
                                          v.value,
                                          "MiB",
                                          displayUnit,
                                        );
                                        return `${x.value} ${x.unit}`;
                                      })()
                                    : `${v.value} ${m.unit}`}
                                </p>
                              ))}
                            </details>
                          </td>
                          <td>{states[m.status] ?? m.status}</td>
                          <td>
                            {m.values.length} / {m.expected_entities ?? "—"}
                          </td>
                          <td>{m.unit}</td>
                          <td>{dateTime(m.evaluated_at)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </details>
            )}
          </>
        )}
      </section>
    </>
  );
}
