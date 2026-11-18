import { createRequestId } from "../api/requestId";
import { useEffect, useRef, useState } from "react";
import type { ClusterFilter } from "../types/resource";
import { dateTime } from "./Metrics";
import { routeHref } from "../navigation";
const states: Record<string, string> = {
  running: "调查中",
  waiting_input: "需要补充",
  complete: "已完成",
  limited: "结论受限",
  refused: "范围不支持",
  cancelled: "已取消",
  failed: "执行失败",
  interrupted: "执行中断",
};
const kinds: Record<string, string> = {
  resource_investigation: "资源助手",
  allocation_change: "资源变化诊断",
  gpu_usage: "占用深入调查",
};
export function InvestigationLink({ id }: { id?: string }) {
  return id ? (
    <a
      className="link investigation-link"
      href={routeHref("analysis", "records", undefined, id)}
    >
      查看调查记录与反馈
    </a>
  ) : null;
}
type Entry = {
  id: string;
  kind: string;
  status: string;
  created_at: number;
  clusters: string[];
  legacy: boolean;
};
type Detail = Entry & {
  request: Record<string, unknown>;
  result?: {
    mode?: string;
    model_calls?: number;
    tool_calls?: number;
    agent?: { mode?: string; model_calls?: number; tool_calls?: number };
  };
  failure_reason: string | null;
  warnings?: string[];
  evidence_window?: { start: number | null; end: number | null };
  goals: string[];
  evidence: {
    id: string;
    label: string;
    tool: string;
    arguments?: Record<string, unknown>;
    data: Record<string, unknown>;
  }[];
  events: { sequence: number; type: string; data: Record<string, unknown> }[];
  findings: {
    title: string;
    level: string;
    evidence_ids: string[];
    limitations: string[];
  }[];
  feedback_enabled: boolean;
  feedback: { decision: string; note: string; created_at: number }[];
  finished_at: number | null;
};
export function InvestigationRecords({
  cluster,
  taskId,
}: {
  cluster: ClusterFilter;
  taskId?: string;
}) {
  const [rows, setRows] = useState<Entry[]>([]),
    [detail, setDetail] = useState<Detail | null>(null),
    [error, setError] = useState(""),
    [revision, setRevision] = useState(0);
  const [decision, setDecision] = useState("follow_up"),
    [note, setNote] = useState(""),
    [saving, setSaving] = useState(false),
    [message, setMessage] = useState("");
  const feedbackRequest = useRef<AbortController | null>(null);
  const feedbackId = useRef<string | null>(null);
  useEffect(() => {
    feedbackRequest.current?.abort();
    setSaving(false);
    const c = new AbortController();
    setRows([]);
    setDetail(null);
    setError("");
    setMessage("");
    setNote("");
    feedbackId.current = null;
    async function load() {
      try {
        const r = await fetch(
          taskId
            ? `/api/v1/investigations/${taskId}?cluster=${cluster}`
            : `/api/v1/investigations?cluster=${cluster}`,
          { signal: c.signal },
        );
        if (!r.ok)
          throw Error(
            taskId ? "调查不存在、不在当前范围或暂不可用" : "调查记录暂不可用",
          );
        const value = await r.json();
        if (!c.signal.aborted) {
          if (taskId) setDetail(value);
          else setRows(value.tasks);
        }
      } catch (e) {
        if (!c.signal.aborted)
          setError(e instanceof Error ? e.message : "查询失败");
      }
    }
    void load();
    return () => {
      c.abort();
      feedbackRequest.current?.abort();
    };
  }, [cluster, taskId, revision]);
  async function save() {
    const c = new AbortController();
    feedbackRequest.current = c;
    setSaving(true);
    setMessage("");
    try {
      feedbackId.current ??= createRequestId();
      const r = await fetch(`/api/v1/investigations/${taskId}/feedback`, {
        signal: c.signal,
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          request_id: feedbackId.current,
          decision,
          note,
        }),
      });
      if (!r.ok) throw Error("反馈未保存，请检查授权或重试");
      const updated = await fetch(
        `/api/v1/investigations/${taskId}?cluster=${cluster}`,
        { signal: c.signal },
      );
      if (updated.ok && !c.signal.aborted) setDetail(await updated.json());
      if (c.signal.aborted) return;
      setMessage("反馈已保存");
      feedbackId.current = null;
    } catch (e) {
      if (!c.signal.aborted)
        setMessage(e instanceof Error ? e.message : "保存失败");
    } finally {
      if (feedbackRequest.current === c) setSaving(false);
    }
  }
  const execution = detail?.result?.agent ?? detail?.result;
  return (
    <section
      className="card operations-panel investigation-records"
      aria-label="调查记录"
    >
      <div className="panel-heading">
        <h2>{detail ? kinds[detail.kind] : "调查记录"}</h2>
        <button onClick={() => setRevision((v) => v + 1)}>刷新记录</button>
      </div>
      {taskId && (
        <a className="link" href="#analysis?view=records">
          返回记录列表
        </a>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {!taskId && (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>任务</th>
                <th>范围</th>
                <th>状态</th>
                <th>创建时间</th>
                <th>详情</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td>
                    {kinds[row.kind]}
                    {row.legacy && <small>旧版证据报告</small>}
                  </td>
                  <td>
                    {row.clusters
                      .map((c) => (c === "yz" ? "亦庄" : "兆维"))
                      .join("、")}
                  </td>
                  <td>{states[row.status]}</td>
                  <td>{dateTime(row.created_at)}</td>
                  <td>
                    <a
                      className="link"
                      href={routeHref("analysis", "records", undefined, row.id)}
                    >
                      查看证据
                    </a>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {!rows.length && !error && <p className="empty">暂无调查记录</p>}
        </div>
      )}
      {detail && (
        <div className="investigation-content">
          <details className="investigation-meta">
            <summary>
              <strong>{states[detail.status]}</strong> · 调查信息
            </summary>
            <span>
              {execution?.mode
                ? ({
                    deepseek: "模型调查",
                    deterministic: "确定性流程",
                    degraded: "模型降级",
                  }[execution.mode] ?? "执行模式未知")
                : "执行模式未知"}
            </span>
            {execution?.tool_calls !== undefined && (
              <span>
                {execution.tool_calls} 次工具 / {execution.model_calls ?? "—"}{" "}
                次模型
              </span>
            )}
            <span>任务 {detail.id}</span>
            <span>创建于 {dateTime(detail.created_at)}</span>
            {detail.finished_at && (
              <span>结束于 {dateTime(detail.finished_at)}</span>
            )}
          </details>
          {detail.evidence_window?.start && detail.evidence_window?.end && (
            <p>
              证据区间：{dateTime(detail.evidence_window.start)} —{" "}
              {dateTime(detail.evidence_window.end)}
            </p>
          )}
          {detail.warnings?.map((warning, i) => (
            <p key={i} className="notice">
              {warning}
            </p>
          ))}
          {detail.failure_reason && (
            <p className="notice">{detail.failure_reason}</p>
          )}
          <details>
            <summary>调查范围与核查目标</summary>
            <p>{JSON.stringify(detail.request)}</p>
            <ol>
              {detail.goals.map((g) => (
                <li key={g}>{g}</li>
              ))}
            </ol>
            {detail.legacy && (
              <p>旧版记录未保存的执行状态和工具输入保持未知。</p>
            )}
          </details>
          {detail.findings.map((f, i) => (
            <article className="investigation-finding" key={i}>
              <span className="rule-badge">
                {{ fact: "事实", inference: "推断", unknown: "无法判断" }[
                  f.level
                ] ?? "无法判断"}
              </span>
              <h3>{f.title}</h3>
              {f.limitations.map((l) => (
                <p key={l}>{l}</p>
              ))}
              <div className="context-links">
                {f.evidence_ids.map((id) => (
                  <a
                    key={id}
                    href={`#proof-${id}`}
                    onClick={(e) => {
                      e.preventDefault();
                      const target = document.getElementById(
                        `proof-${id}`,
                      ) as HTMLDetailsElement | null;
                      if (target) {
                        target.open = true;
                        target.scrollIntoView({ block: "center" });
                      }
                    }}
                  >
                    证据 {id}
                  </a>
                ))}
              </div>
            </article>
          ))}
          {!detail.findings.length && (
            <p className="notice">尚无足够证据形成结论；可查看已完成步骤。</p>
          )}
          <h3>证据与数据范围</h3>
          {detail.evidence.map((e) => (
            <details
              className="investigation-evidence"
              id={`proof-${e.id}`}
              key={e.id}
            >
              <summary>
                {e.label} · {e.id}
              </summary>
              <p>工具：{e.tool}</p>
              <p>
                输入：
                {e.arguments ? JSON.stringify(e.arguments) : "旧记录未保存"}
              </p>
              <pre>{JSON.stringify(e.data, null, 2)}</pre>
            </details>
          ))}
          <a
            className="link"
            href={routeHref("analysis", "assistant", undefined, detail.id)}
          >
            继续调查
          </a>
          <details>
            <summary>执行步骤（{detail.events.length}）</summary>
            <ol>
              {detail.events.map((e) => (
                <li key={e.sequence}>
                  {String(e.data.label ?? e.data.message ?? e.type)}
                </li>
              ))}
            </ol>
          </details>
          <section className="investigation-feedback" aria-label="调查反馈">
            <h3>人工反馈</h3>
            {detail.feedback.map((f, i) => (
              <p key={i}>
                {dateTime(f.created_at)} ·{" "}
                {
                  {
                    accepted: "认可结论",
                    follow_up: "继续核查",
                    insufficient: "证据不足",
                  }[f.decision]
                }
                {f.note ? `：${f.note}` : ""}
              </p>
            ))}
            {detail.feedback_enabled && detail.status !== "running" ? (
              <>
                <label>
                  反馈结果
                  <select
                    disabled={saving}
                    value={decision}
                    onChange={(e) => {
                      setDecision(e.target.value);
                      feedbackId.current = null;
                    }}
                  >
                    <option value="follow_up">继续核查</option>
                    <option value="accepted">认可结论</option>
                    <option value="insufficient">证据不足</option>
                  </select>
                </label>
                <label>
                  补充说明
                  <textarea
                    disabled={saving}
                    maxLength={1000}
                    value={note}
                    onChange={(e) => {
                      setNote(e.target.value);
                      feedbackId.current = null;
                    }}
                  />
                </label>
                <button
                  type="button"
                  disabled={saving}
                  onClick={() => void save()}
                >
                  {saving ? "保存中…" : "保存反馈"}
                </button>
              </>
            ) : (
              <p>当前未启用反馈，或调查尚未结束。</p>
            )}
            {message && <p role="status">{message}</p>}
          </section>
        </div>
      )}
    </section>
  );
}
