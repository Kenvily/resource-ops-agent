import { useEffect, useState } from "react";
import type { ClusterFilter } from "../types/resource";
import { dateTime } from "./Metrics";
import { workspace } from "./OperationsWorkspace";
type Task = {
  id: string;
  clusters: string[];
  status: string;
  kind: string;
  created_at: number;
};
type Issue = { id: string; title: string; status: string };
export function ReportSources({
  cluster,
  tasks,
  issues,
  setTasks,
  setIssues,
  disabled,
}: {
  cluster: ClusterFilter;
  tasks: string[];
  issues: string[];
  setTasks: (v: string[]) => void;
  setIssues: (v: string[]) => void;
  disabled: boolean;
}) {
  const [available, setAvailable] = useState<Task[]>([]),
    [problems, setProblems] = useState<Issue[]>([]),
    [error, setError] = useState("");
  useEffect(() => {
    const c = new AbortController();
    setAvailable([]);
    setProblems([]);
    setError("");
    Promise.all([
      fetch(`/api/v1/investigations?cluster=${cluster}`, {
        signal: c.signal,
      }).then(async (r) => {
        if (!r.ok) throw Error("调查列表暂不可用");
        return r.json();
      }),
      workspace<{ issues: Issue[] }>(
        `issues?cluster=${cluster}`,
        undefined,
        c.signal,
      ),
    ])
      .then(([a, b]) => {
        setAvailable(
          a.tasks.filter((t: Task) =>
            ["complete", "limited", "refused"].includes(t.status),
          ),
        );
        setProblems(b.issues);
      })
      .catch((e) => {
        if (!c.signal.aborted) setError(e.message);
      });
    return () => c.abort();
  }, [cluster]);
  return (
    <details className="operations-section report-sources">
      <summary>关联调查与问题（可选）</summary>
      {error && <p role="alert">{error}</p>}
      {!available.length && !problems.length && !error && (
        <p className="empty">暂无可关联记录</p>
      )}
      {available.map((t) => (
        <label className="efficiency-filter" key={t.id}>
          <input
            type="checkbox"
            disabled={disabled || (!tasks.includes(t.id) && tasks.length >= 5)}
            checked={tasks.includes(t.id)}
            onChange={(e) =>
              setTasks(
                e.target.checked
                  ? [...tasks, t.id]
                  : tasks.filter((i) => i !== t.id),
              )
            }
          />
          {dateTime(t.created_at)} ·{" "}
          {t.kind === "gpu_usage" ? "占用调查" : "资源变化"} · {t.status}
        </label>
      ))}
      {problems.map((t) => (
        <label className="efficiency-filter" key={t.id}>
          <input
            type="checkbox"
            disabled={
              disabled || (!issues.includes(t.id) && issues.length >= 5)
            }
            checked={issues.includes(t.id)}
            onChange={(e) =>
              setIssues(
                e.target.checked
                  ? [...issues, t.id]
                  : issues.filter((i) => i !== t.id),
              )
            }
          />
          {t.title}
        </label>
      ))}
    </details>
  );
}
