import { createRequestId } from "../api/requestId";
import { useEffect, useRef, useState } from "react";
import type { ClusterFilter } from "../types/resource";
import { dateTime } from "./Metrics";

export type Delivery<T = unknown> = {
  id: string;
  kind: "assessment" | "report";
  clusters: string[];
  created_at: number;
  result: T;
  request: unknown;
  decisions: {
    request_id: string;
    decision: string;
    candidates: number[];
    note: string;
    created_at: number;
  }[];
};
export async function workspace<T>(
  path: string,
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(`/api/v1/operations/workspace/${path}`, {
    method: body === undefined ? "GET" : "POST",
    signal,
    headers:
      body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw Error(
      typeof data.detail === "string" ? data.detail : "运营记录暂不可用",
    );
  }
  return response.json();
}
export function useWorkspace() {
  const [state, setState] = useState({
    available: false,
    write_enabled: false,
  });
  useEffect(() => {
    const active = new AbortController();
    workspace<typeof state>("capabilities", undefined, active.signal)
      .then(setState)
      .catch(() => {});
    return () => active.abort();
  }, []);
  return state;
}
const decisions: Record<string, string> = {
  accepted: "已确认",
  needs_changes: "需补充",
  rejected: "未采纳",
};
export function DeliveryReview({
  record,
  selected = [],
  onSaved,
  enabled,
}: {
  record: Delivery;
  selected?: number[];
  onSaved: () => void;
  enabled: boolean;
}) {
  const [decision, setDecision] = useState("accepted"),
    [note, setNote] = useState("");
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const active = useRef<AbortController | null>(null);
  useEffect(() => {
    const c = new AbortController();
    active.current = c;
    setBusy(false);
    setNote("");
    setError("");
    setDecision("accepted");
    return () => c.abort();
  }, [record.id]);
  const latest = record.decisions.at(-1);
  return (
    <section className="operations-section delivery-review">
      <h3>{record.kind === "assessment" ? "评估复核" : "报告复核"}</h3>
      <p>
        保存于 {dateTime(record.created_at)} ·{" "}
        {latest ? decisions[latest.decision] : "待复核"}
      </p>
      {record.decisions.map((d) => (
        <p key={d.request_id}>
          {dateTime(d.created_at)} · {decisions[d.decision]}
          {d.candidates.length
            ? ` · 候选 ${d.candidates.map((i) => i + 1).join("、")}`
            : ""}
          {d.note ? ` · ${d.note}` : ""}
        </p>
      ))}
      {enabled && (
        <form
          className="operations-form"
          onSubmit={async (e) => {
            e.preventDefault();
            const c = active.current;
            setBusy(true);
            setError("");
            try {
              await workspace(
                `records/${record.id}/review`,
                {
                  request_id: createRequestId(),
                  decision,
                  candidates: record.kind === "assessment" ? selected : [],
                  note,
                },
                c?.signal,
              );
              if (!c?.signal.aborted) onSaved();
            } catch (e) {
              if (!c?.signal.aborted)
                setError(e instanceof Error ? e.message : "复核保存失败");
            } finally {
              if (!c?.signal.aborted) setBusy(false);
            }
          }}
        >
          <label>
            复核结论
            <select
              disabled={busy}
              value={decision}
              onChange={(e) => setDecision(e.target.value)}
            >
              <option value="accepted">确认方案 / 报告</option>
              <option value="needs_changes">需要补充</option>
              <option value="rejected">不采纳</option>
            </select>
          </label>
          <label>
            补充说明
            <textarea
              maxLength={1000}
              disabled={busy}
              value={note}
              onChange={(e) => setNote(e.target.value)}
            />
          </label>
          <button
            disabled={
              busy ||
              (record.kind === "assessment" &&
                decision === "accepted" &&
                !selected.length)
            }
            type="submit"
            className="primary"
          >
            {busy ? "保存中…" : "保存复核"}
          </button>
        </form>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
export function DeliveryHistory<T>({
  kind,
  cluster,
  revision,
  onLoad,
}: {
  kind: "assessment" | "report";
  cluster: ClusterFilter;
  revision: string;
  onLoad: (v: Delivery<T>) => void;
}) {
  const active = useRef<AbortController | null>(null);
  const [items, setItems] = useState<Delivery<T>[]>([]),
    [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    active.current = controller;
    setBusy(false);
    setItems([]);
    setError("");
    workspace<{ records: Delivery<T>[] }>(
      `records?kind=${kind}&cluster=${cluster}`,
      undefined,
      controller.signal,
    )
      .then((v) => setItems(v.records))
      .catch((e) => {
        if (!controller.signal.aborted) setError(e.message);
      });
    return () => controller.abort();
  }, [kind, cluster, revision]);
  return (
    <details className="operations-section delivery-history">
      <summary>
        已保存的{kind === "assessment" ? "评估" : "报告"}（最近 50 条）
      </summary>
      {error && <p role="alert">{error}</p>}
      {!items.length && !error && <p className="empty">暂无记录</p>}
      {items.map((item) => (
        <div className="delivery-history-row" key={item.id}>
          <span>
            {dateTime(item.created_at)} ·{" "}
            {item.clusters
              .map((c) => (c === "yz" ? "亦庄" : "兆维"))
              .join("、")}
          </span>
          <button
            type="button"
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              setError("");
              try {
                const c = active.current;
                const saved = await workspace<Delivery<T>>(
                  `records/${item.id}?cluster=${cluster}`,
                  undefined,
                  c?.signal,
                );
                if (!c?.signal.aborted) onLoad(saved);
              } catch (e) {
                setError(e instanceof Error ? e.message : "读取失败");
              } finally {
                setBusy(false);
              }
            }}
          >
            查看记录
          </button>
        </div>
      ))}
    </details>
  );
}
