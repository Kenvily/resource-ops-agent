import { useEffect, useRef, useState } from "react";
import { operationsRequest, type Infrastructure } from "../api/operations";
import { memoryValue, type DisplayUnit } from "./memory";
export function InfrastructurePanel({
  displayUnit,
}: {
  displayUnit: DisplayUnit;
}) {
  const [infra, setInfra] = useState<Infrastructure | null>(null);
  const [infraError, setInfraError] = useState("");
  const [infraBusy, setInfraBusy] = useState(false);
  const infraActive = useRef<AbortController | null>(null);
  useEffect(() => () => infraActive.current?.abort(), []);
  async function loadInfra() {
    infraActive.current?.abort();
    const c = new AbortController();
    infraActive.current = c;
    setInfraBusy(true);
    setInfraError("");
    try {
      const data = await operationsRequest<Infrastructure>(
        "infrastructure",
        c.signal,
      );
      if (!c.signal.aborted) setInfra(data);
    } catch (e) {
      if (!c.signal.aborted) {
        setInfra(null);
        setInfraError(e instanceof Error ? e.message : "运行评估查询失败");
      }
    } finally {
      if (infraActive.current === c) setInfraBusy(false);
    }
  }
  const bytes = (value: number) => {
    const v = memoryValue(value, "bytes", displayUnit);
    return `${v.value} ${v.unit}`;
  };

  return (
    <section className="card operations-panel" aria-label="运行评估">
      <div className="panel-heading">
        <h2>运行评估</h2>
        <button
          type="button"
          disabled={infraBusy}
          onClick={() => void loadInfra()}
        >
          {infraBusy ? "读取中…" : "获取运行评估"}
        </button>
      </div>
      {infraError && (
        <p role="alert" className="error">
          {infraError}
        </p>
      )}
      {!infra && !infraBusy && !infraError && (
        <p className="empty">
          按当前请求延迟、并发、上游压力和数据库容量评估。
        </p>
      )}
      {infra && (
        <>
          <div className="efficiency-evidence">
            <div>
              数据库占用
              <strong>
                {infra.database
                  ? bytes(infra.database.database_bytes)
                  : "未配置"}
              </strong>
            </div>
            <div>
              数据库连接 / 锁等待
              <strong>
                {infra.database
                  ? `${infra.database.connections} / ${infra.database.lock_waiters}`
                  : "—"}
              </strong>
            </div>
            <div>
              上游峰值并发
              <strong>
                {infra.telemetry.concurrency.upstream_active?.peak ??
                  "尚无样本"}
              </strong>
            </div>
            <div>
              上游累计响应
              <strong>{bytes(infra.telemetry.upstream_response_bytes)}</strong>
            </div>
          </div>
          {infra.recommendations.map((r) => (
            <article className="operations-section" key={r.component}>
              <h3>
                {r.component} ·{" "}
                {r.status === "investigate"
                  ? "需进一步核验"
                  : r.status === "insufficient"
                    ? "样本不足"
                    : "维持现有架构"}
              </h3>
              <p>{r.reason}</p>
            </article>
          ))}
          <details>
            <summary>延迟、上游压力与数据库明细</summary>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>路径 / 操作</th>
                    <th>样本数</th>
                    <th>p50</th>
                    <th>p95</th>
                    <th>错误率</th>
                  </tr>
                </thead>
                <tbody>
                  {infra.telemetry.metrics.map((m) => (
                    <tr key={`${m.domain}-${m.label}-${m.operation}`}>
                      <td>
                        {m.domain} / {m.label} / {m.operation}
                      </td>
                      <td>{m.recent_samples}</td>
                      <td>{m.p50_ms} ms</td>
                      <td>{m.p95_ms} ms</td>
                      <td>{(m.error_rate * 100).toFixed(1)}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>表</th>
                    <th>估计行数</th>
                    <th>估计死元组</th>
                    <th>表及索引</th>
                  </tr>
                </thead>
                <tbody>
                  {infra.database?.tables.map((t) => (
                    <tr key={t.name}>
                      <td>{t.name}</td>
                      <td>{t.estimated_rows}</td>
                      <td>{t.estimated_dead_rows}</td>
                      <td>{bytes(t.total_bytes)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {infra.limitations.map((v) => (
              <p key={v}>{v}</p>
            ))}
          </details>
        </>
      )}
    </section>
  );
}
