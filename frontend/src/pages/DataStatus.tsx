import { useEffect, useState } from "react";
import type { ClusterFilter } from "../types/resource";
import { durationLabel } from "../components/duration";
import { dateTime } from "../components/Metrics";

type HostSync = {
  available: boolean;
  clusters: {
    cluster: string;
    state: string;
    expected_minutes: number;
    complete_minutes: number;
    pending_minutes: number;
    error_minutes: number;
    partial_minutes: number;
    empty_minutes: number;
    unknown_minutes: number;
    latest_attempt_at: number | null;
    latest_complete_at: number | null;
  }[];
};
type Capability = {
  available: boolean;
  model_ready?: boolean;
  sync_status?: string;
};
type Diagnostics = {
  snapshots?: { sync_status: string };
  history?: {
    sync_status: string;
    progress?: {
      cluster: string;
      grain: number;
      next_ts: number;
      quality_version: number;
    }[];
  };
};
const states: Record<string, string> = {
  idle: "等待采集",
  healthy: "最近同步成功",
  ok: "最近同步成功",
  running: "同步中",
  disabled: "未启用",
  error: "同步失败",
  partial: "部分成功",
  database_unavailable: "数据库不可用",
};
export function DataStatus({ cluster }: { cluster: ClusterFilter }) {
  const [hostSync, setHostSync] = useState<HostSync | null>(null);
  const [data, setData] = useState<Diagnostics | null>(null);
  const [diagnosis, setDiagnosis] = useState<Capability | null>(null);
  const [usage, setUsage] = useState<Capability | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [busy, setBusy] = useState(false),
    [revision, setRevision] = useState(0);
  useEffect(() => {
    const c = new AbortController();
    setBusy(true);
    setData(null);
    setDiagnosis(null);
    setUsage(null);
    setHostSync(null);
    setErrors([]);
    const targets = [
      ["diagnostics", "同步状态", setData],
      ["insights/capabilities", "资源诊断", setDiagnosis],
      ["efficiency/capabilities", "占用核查", setUsage],
      [`operations/host-sync?cluster=${cluster}`, "主机历史", setHostSync],
    ] as const;
    Promise.all(
      targets.map(async ([path, label, save]) => {
        try {
          const r = await fetch(`/api/v1/${path}`, {
            signal: c.signal,
            cache: "no-store",
          });
          if (!r.ok) throw Error(label);
          const result = await r.json();
          if (!c.signal.aborted) save(result);
        } catch {
          if (!c.signal.aborted) setErrors((v) => [...v, `${label}暂不可用`]);
        }
      }),
    ).finally(() => {
      if (!c.signal.aborted) setBusy(false);
    });
    return () => c.abort();
  }, [revision, cluster]);
  return (
    <section className="card operations-panel" aria-label="数据与同步">
      <div className="panel-heading">
        <h2>数据与同步</h2>
        <button disabled={busy} onClick={() => setRevision((v) => v + 1)}>
          {busy ? "读取中…" : "刷新状态"}
        </button>
      </div>
      {errors.map((e) => (
        <p className="error" role="alert" key={e}>
          {e}
        </p>
      ))}
      <dl className="status-grid">
        <div>
          <dt>资源快照</dt>
          <dd>
            {data
              ? data.snapshots
                ? (states[data.snapshots.sync_status] ?? "等待核验")
                : "未配置数据库"
              : "—"}
          </dd>
        </div>
        <div>
          <dt>额度历史</dt>
          <dd>
            {data
              ? data.history
                ? (states[data.history.sync_status] ?? "等待核验")
                : "未配置数据库"
              : "—"}
          </dd>
        </div>
        <div>
          <dt>资源诊断</dt>
          <dd>
            {diagnosis
              ? diagnosis.available
                ? diagnosis.model_ready
                  ? "模型已就绪"
                  : "确定性核查"
                : "未配置数据库"
              : "—"}
          </dd>
        </div>
        <div>
          <dt>占用核查</dt>
          <dd>
            {usage
              ? usage.available
                ? usage.model_ready
                  ? "模型已就绪"
                  : "确定性核查"
                : "采集未启用"
              : "—"}
          </dd>
        </div>
      </dl>
      {data?.history?.progress && (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>集群</th>
                <th>粒度</th>
                <th>下一回填时间</th>
                <th>质量版本</th>
              </tr>
            </thead>
            <tbody>
              {data.history.progress
                .filter((p) => cluster === "all" || p.cluster === cluster)
                .map((p) => (
                  <tr key={`${p.cluster}-${p.grain}`}>
                    <td>{p.cluster === "yz" ? "亦庄" : "兆维"}</td>
                    <td>{durationLabel(p.grain)}</td>
                    <td>{dateTime(p.next_ts)}</td>
                    <td>{p.quality_version}</td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      )}
      {hostSync?.available && (
        <section className="operations-section">
          <h3>主机分钟覆盖</h3>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>集群</th>
                  <th>状态</th>
                  <th>完整 / 应有分钟</th>
                  <th>最近完整采样</th>
                  <th>最近同步尝试</th>
                </tr>
              </thead>
              <tbody>
                {hostSync.clusters.map((s) => (
                  <tr key={s.cluster}>
                    <td>{s.cluster === "yz" ? "亦庄" : "兆维"}</td>
                    <td>
                      {{
                        ready: "覆盖完整",
                        attention: "需要核查",
                        backfilling: "积累与补齐中",
                        disabled: "未启用",
                        not_started: "尚未开始",
                      }[s.state] ?? "未知"}
                    </td>
                    <td>
                      {s.complete_minutes}/{s.expected_minutes}
                    </td>
                    <td>{dateTime(s.latest_complete_at)}</td>
                    <td>{dateTime(s.latest_attempt_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <details>
            <summary>缺口与失败分类</summary>
            {hostSync.clusters.map((s) => (
              <p key={s.cluster}>
                {s.cluster === "yz" ? "亦庄" : "兆维"} · 未完整{" "}
                {s.pending_minutes} 分钟 · 身份查询失败 {s.error_minutes} ·
                指标部分缺测 {s.partial_minutes} · 无节点记录 {s.empty_minutes}{" "}
                · 旧记录原因未知 {s.unknown_minutes}
              </p>
            ))}
          </details>
        </section>
      )}
      <details className="operations-section">
        <summary>数据与归属配置</summary>
        <p>
          数据源、采集策略和业务模型归属由服务端配置管理。未映射业务保留“归属未知”。
        </p>
      </details>
    </section>
  );
}
