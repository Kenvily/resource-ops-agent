import { createRequestId } from "../api/requestId";
import { useEffect, useRef, useState } from "react";
import type { ClusterFilter } from "../types/resource";
import { dateTime } from "./Metrics";
import { workspace, useWorkspace } from "./OperationsWorkspace";
import { routeHref } from "../navigation";
type Issue = {
  id: string;
  cluster: string;
  title: string;
  status: string;
  revision: number;
  created_at: number;
  evidence: { node: string; device_uuid: string | null; evaluated_at: number };
  events: {
    request_id: string;
    status: string;
    note: string;
    created_at: number;
    investigation: { id: string; status: string } | null;
  }[];
};
type Task = {
  id: string;
  clusters: string[];
  status: string;
  created_at: number;
  kind: string;
};
const labels: Record<string, string> = {
  open: "待核查",
  investigating: "核查中",
  resolved: "已处理",
  dismissed: "无需处理",
};
export function OperationsIssues({ cluster }: { cluster: ClusterFilter }) {
  const caps = useWorkspace();
  const [items, setItems] = useState<Issue[]>([]),
    [detail, setDetail] = useState<Issue | null>(null);
  const [status, setStatus] = useState("investigating"),
    [note, setNote] = useState(""),
    [task, setTask] = useState("");
  const [tasks, setTasks] = useState<Task[]>([]),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const active = useRef<AbortController | null>(null);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const c = new AbortController();
    active.current = c;
    setDetail(null);
    setError("");
    setItems([]);
    setTasks([]);
    setBusy(false);
    workspace<{ issues: Issue[] }>(
      `issues?cluster=${cluster}`,
      undefined,
      c.signal,
    )
      .then((v) => setItems(v.issues))
      .catch((e) => {
        if (!c.signal.aborted) setError(e.message);
      });
    fetch(`/api/v1/investigations?cluster=${cluster}`, { signal: c.signal })
      .then((r) => (r.ok ? r.json() : { tasks: [] }))
      .then((v) => setTasks(v.tasks))
      .catch(() => {});
    return () => c.abort();
  }, [cluster, revision]);
  async function load(id: string) {
    const c = active.current;
    setBusy(true);
    setError("");
    try {
      const d = await workspace<Issue>(
        `issues/${id}?cluster=${cluster}`,
        undefined,
        c?.signal,
      );
      if (c?.signal.aborted) return;
      setDetail(d);
      setStatus(d.status === "open" ? "investigating" : d.status);
      setNote("");
      setTask("");
    } catch (e) {
      if (!c?.signal.aborted)
        setError(e instanceof Error ? e.message : "读取失败");
    } finally {
      if (!c?.signal.aborted) setBusy(false);
    }
  }
  return (
    <section className="card operations-panel">
      <div className="panel-heading">
        <h2>问题核查</h2>
        <button disabled={busy} onClick={() => setRevision((v) => v + 1)}>
          刷新记录
        </button>
      </div>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {!items.length && !error && (
        <p className="empty">暂无问题，可从指标告警加入待核查。</p>
      )}
      <div className="issue-layout">
        <div>
          {items.map((i) => (
            <button
              className="issue-list-row"
              disabled={busy}
              aria-pressed={detail?.id === i.id}
              key={i.id}
              onClick={() => void load(i.id)}
            >
              <strong>
                {i.cluster === "yz" ? "亦庄" : "兆维"} · {i.title}
              </strong>
              <span>{labels[i.status]}</span>
            </button>
          ))}
        </div>
        {detail && (
          <section className="operations-section">
            <h3>{detail.title}</h3>
            <p>
              {detail.evidence.node}
              {detail.evidence.device_uuid
                ? ` / ${detail.evidence.device_uuid}`
                : ""}
            </p>
            <p>
              捕获时间 {dateTime(detail.evidence.evaluated_at)} ·{" "}
              {labels[detail.status]}
            </p>
            <a href={routeHref("analysis", "efficiency")}>进入占用调查</a>
            {detail.events.map((e) => (
              <div className="operations-section" key={e.request_id}>
                <p>
                  {dateTime(e.created_at)} · {labels[e.status]} · {e.note}
                </p>
                {e.investigation && (
                  <a
                    href={routeHref(
                      "analysis",
                      "records",
                      undefined,
                      e.investigation.id,
                    )}
                  >
                    查看关联调查 · {e.investigation.status}
                  </a>
                )}
              </div>
            ))}
            {caps.write_enabled && (
              <form
                className="issue-form"
                onSubmit={async (e) => {
                  e.preventDefault();
                  setBusy(true);
                  setError("");
                  const c = active.current;
                  try {
                    await workspace(
                      `issues/${detail.id}/review`,
                      {
                        request_id: createRequestId(),
                        revision: detail.revision,
                        status,
                        note,
                        investigation_id: task || null,
                      },
                      c?.signal,
                    );
                    if (!c?.signal.aborted) {
                      await load(detail.id);
                      setItems((v) =>
                        v.map((i) =>
                          i.id === detail.id ? { ...i, status } : i,
                        ),
                      );
                    }
                  } catch (e) {
                    if (!c?.signal.aborted)
                      setError(e instanceof Error ? e.message : "保存失败");
                  } finally {
                    if (!c?.signal.aborted) setBusy(false);
                  }
                }}
              >
                <label>
                  核查状态
                  <select
                    value={status}
                    disabled={busy}
                    onChange={(e) => setStatus(e.target.value)}
                  >
                    {Object.entries(labels).map(([k, v]) => (
                      <option key={k} value={k}>
                        {v}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  关联调查（可选）
                  <select
                    value={task}
                    disabled={busy}
                    onChange={(e) => setTask(e.target.value)}
                  >
                    <option value="">不关联</option>
                    {tasks
                      .filter(
                        (t) =>
                          t.clusters.length === 1 &&
                          t.clusters[0] === detail.cluster &&
                          ["complete", "limited", "refused"].includes(t.status),
                      )
                      .map((t) => (
                        <option key={t.id} value={t.id}>
                          {dateTime(t.created_at)} ·{" "}
                          {t.kind === "gpu_usage" ? "占用调查" : "资源变化"} ·{" "}
                          {t.status}
                        </option>
                      ))}
                  </select>
                </label>
                <label>
                  核查结论
                  <textarea
                    value={note}
                    required={["resolved", "dismissed"].includes(status)}
                    maxLength={1000}
                    disabled={busy}
                    onChange={(e) => setNote(e.target.value)}
                  />
                </label>
                <button type="submit" className="primary" disabled={busy}>
                  {busy ? "保存中…" : "保存核查"}
                </button>
              </form>
            )}
          </section>
        )}
      </div>
    </section>
  );
}
