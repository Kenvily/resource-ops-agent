import { useEffect, useRef, useState } from "react";
import { ArrowUpRight, Plus, Square } from "lucide-react";
import { dateTime } from "./Metrics";
import { MarkdownAnswer } from "./MarkdownAnswer";
import { AgentReport } from "./AgentReport";
import { createRequestId } from "../api/requestId";
import { routeHref } from "../navigation";
import { timeRanges } from "../types/windows";
import type { ClusterFilter } from "../types/resource";
import type { WindowSize } from "../types/trend";
import { memoryValue, type DisplayUnit } from "./memory";

type Proof = {
  id: string;
  label: string;
  tool: string;
  status?: string;
  data: Record<string, unknown>;
  provenance?: {
    source?: string;
    data_as_of?: number | null;
    queried_at?: number;
    units?: unknown;
  };
};
type Finding = {
  title: string;
  level?: string;
  state?: string;
  cluster?: string;
  evidence_ids: string[];
  limitations?: string[];
  metric?: string;
  value?: number;
  unit?: string;
  entity_ref?: string;
};
type Result = {
  status: string;
  summary?: string;
  answer_type?: string;
  answer_markdown?: string;
  answer_status?: string;
  scope?: { window?: string };
  question?: string;
  mode?: string;
  findings: Finding[];
  evidence: Proof[];
  questions?: string[];
  next_actions?: { label: string; href: string }[];
  warnings?: string[];
  execution?: { stop_reason?: string };
  model_calls?: number;
  tool_calls?: number;
  entities?: {
    id: string;
    cluster: string;
    dimension: string;
    label?: string;
  }[];
  investigation?: { id: string; reused?: boolean };
  memory?: { evidence_refreshed?: boolean };
};
const statuses: Record<string, string> = {
  running: "调查中",
  failed: "执行失败",
  cancelled: "已取消",
  interrupted: "执行中断",
  complete: "已完成",
  limited: "结论受限",
  waiting_input: "需要补充",
  refused: "超出只读范围",
};
const modes: Record<string, string> = {
  deterministic: "确定性查询",
  deepseek: "模型调查",
  degraded: "降级核查",
};
const metricNames: Record<string, string> = {
  gpu_util: "GPU 利用率",
  gpu_memory: "物理显存已用",
  allocation: "已分配额度",
  cpu_utilization_pct: "CPU 利用率",
  memory_used_bytes: "系统内存已用",
  load1: "主机负载",
};
const examples = [
  "两个机房总体服务器配置如何？",
  "当前GPU有多少张？",
  "为什么近三个月CPU曲线不完整？",
  "分配额度变化是否由总容量下降造成？",
  "GPU分配较高但利用率很低，是否存在反例？",
];

export function AgentWorkspace({
  cluster,
  initialWindow = "auto",
  taskId,
  displayUnit,
}: {
  cluster: ClusterFilter;
  initialWindow?: WindowSize | "auto";
  taskId?: string;
  displayUnit: DisplayUnit;
}) {
  const [question, setQuestion] = useState("");
  const [windowSize, setWindow] = useState<WindowSize | "auto">(initialWindow);
  const [result, setResult] = useState<Result | null>(null);
  const [answer, setAnswer] = useState("");
  const [writing, setWriting] = useState(false);
  const [saved, setSaved] = useState(false);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const [parentId, setParentId] = useState<string | null>(null);
  const [entity, setEntity] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [steps, setSteps] = useState<string[]>([]);
  const [proofs, setProofs] = useState<Proof[]>([]);
  const controller = useRef<AbortController | null>(null);
  const requestKey = useRef<string | null>(null);
  const formQuestion = useRef<HTMLTextAreaElement | null>(null);
  const generation = useRef(0);
  const reset = () => {
    generation.current += 1;
    controller.current?.abort();
    setBusy(false);
    setResult(null);
    setAnswer("");
    setWriting(false);
    setSaved(false);
    setSavedAt(null);
    setParentId(null);
    setEntity("");
    setQuestion("");
    setError("");
    setSteps([]);
    setProofs([]);
    requestKey.current = null;
    window.history.replaceState(null, "", routeHref("analysis", "assistant"));
    formQuestion.current?.focus();
  };
  useEffect(() => {
    generation.current += 1;
    controller.current?.abort();
    setBusy(false);
    setResult(null);
    setAnswer("");
    setWriting(false);
    setParentId(null);
    setEntity("");
    setProofs([]);
    setSteps([]);
    requestKey.current = null;
    return () => controller.current?.abort();
  }, [cluster]);
  useEffect(() => {
    if (!taskId) return;
    const c = new AbortController();
    setBusy(true);
    setError("");
    fetch(`/api/v1/investigations/${taskId}?cluster=${cluster}`, {
      signal: c.signal,
    })
      .then(async (r) => {
        if (!r.ok) throw Error("调查记录不存在或不在当前范围");
        return r.json();
      })
      .then((data) => {
        if (c.signal.aborted) return;
        if (timeRanges.some((r) => r.value === data.request?.window))
          setWindow(data.request.window);
        const stored = data.result ?? {};
        setSaved(true);
        setSavedAt(data.created_at ?? null);
        setResult({
          ...stored,
          status: data.status,
          summary: stored.summary ?? data.failure_reason ?? "已保存的调查结果",
          findings:
            stored.findings ?? stored.agent?.findings ?? data.findings ?? [],
          evidence:
            stored.evidence ?? stored.agent?.evidence ?? data.evidence ?? [],
          warnings: stored.warnings ?? stored.agent?.warnings ?? [],
          mode: stored.mode ?? stored.agent?.mode,
          model_calls: stored.model_calls ?? stored.agent?.model_calls,
          tool_calls: stored.tool_calls ?? stored.agent?.tool_calls,
          entities:
            stored.entities ??
            (stored.results ?? []).map(
              (
                r: {
                  cluster: string;
                  node: string;
                  investigation?: { entity_id?: string };
                },
                i: number,
              ) => ({
                id: r.investigation?.entity_id ?? `legacy-gpu-${i + 1}`,
                cluster: r.cluster,
                dimension: "gpu",
                label: r.node,
              }),
            ),
        });
        setParentId(data.id);
        setProofs(data.evidence ?? []);
        setSteps(
          (data.events ?? [])
            .filter((e: { type: string }) => e.type === "progress")
            .map(
              (e: { data: { label?: string; message?: string } }) =>
                e.data.label ?? e.data.message ?? "已完成核查",
            ),
        );
      })
      .catch((e) => {
        if (!c.signal.aborted) setError(e.message);
      })
      .finally(() => {
        if (!c.signal.aborted) setBusy(false);
      });
    return () => c.abort();
  }, [taskId, cluster]);
  const submit = async () => {
    if (!question.trim() || busy) return;
    const mine = ++generation.current;
    const c = new AbortController();
    controller.current = c;
    requestKey.current ??= createRequestId();
    setBusy(true);
    setResult(null);
    setAnswer("");
    setWriting(false);
    setError("");
    setSteps([]);
    setProofs([]);
    const timeout = window.setTimeout(() => c.abort(), 135000);
    try {
      const response = await fetch("/api/v1/agents/query", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": requestKey.current,
        },
        signal: c.signal,
        body: JSON.stringify({
          question,
          ...(parentId ? {} : { cluster }),
          ...(windowSize === "auto" ? {} : { window: windowSize }),
          ...(parentId ? { parent_id: parentId } : {}),
          ...(entity ? { entity_ref: entity } : {}),
        }),
      });
      if (!response.ok) {
        const data = await response.json();
        throw Error(
          typeof data.detail === "string" ? data.detail : "调查暂不可用",
        );
      }
      if (!response.body) throw Error("调查响应为空");
      const reader = response.body.getReader(),
        decoder = new TextDecoder();
      let buffer = "",
        completed = false;
      while (true) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value, { stream: !done });
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";
        for (const line of lines) {
          if (!line.trim()) continue;
          const event = JSON.parse(line);
          if (mine !== generation.current) return;
          if (event.type === "answer_start") setWriting(true);
          if (event.type === "answer_delta")
            setAnswer((text) => text + event.data.text);
          if (event.type === "answer_reset") {
            setAnswer("");
            setWriting(false);
          }
          if (event.type === "progress")
            setSteps((s) => [
              ...s,
              event.data.label ?? event.data.message ?? "正在核查",
            ]);
          if (event.type === "evidence") setProofs((p) => [...p, event.data]);
          if (event.type === "task") setSteps((s) => [...s, "任务已建立"]);
          if (event.type === "error") throw Error(event.data.message);
          if (event.type === "result") {
            completed = true;
            const data: Result = event.data;
            setResult(data);
            setAnswer("");
            setWriting(false);
            setSaved(Boolean(data.investigation?.reused));
            setSavedAt(
              data.investigation?.reused
                ? (data.evidence?.[0]?.provenance?.queried_at ?? null)
                : null,
            );
            setProofs(data.evidence ?? []);
            setEntity("");
            setQuestion("");
            requestKey.current = null;
            if (data.investigation?.id) {
              setParentId(data.investigation.id);
              window.history.replaceState(
                null,
                "",
                routeHref(
                  "analysis",
                  "assistant",
                  undefined,
                  data.investigation.id,
                ),
              );
            }
          }
        }
        if (done) break;
      }
      if (!completed)
        throw Error("连接已结束，尚未收到完整结果；可复用本次请求重试");
    } catch (e) {
      if (mine === generation.current)
        setError(
          c.signal.aborted
            ? "调查已停止；已保存的步骤可在调查记录中查看"
            : e instanceof Error
              ? e.message
              : "调查失败",
        );
    } finally {
      window.clearTimeout(timeout);
      if (mine === generation.current) {
        setBusy(false);
        setWriting(false);
        setAnswer("");
      }
    }
  };
  const formatFinding = (f: Finding) => {
    if (f.value == null || !f.metric) return f.title;
    const amount = ["bytes", "MB", "MiB", "GB"].includes(f.unit ?? "")
      ? memoryValue(f.value, f.unit!, displayUnit)
      : null;
    return `${result?.entities?.find((e) => e.id === f.entity_ref)?.label ?? f.entity_ref ?? ""} · ${metricNames[f.metric] ?? f.metric}：${amount ? `${amount.value} ${amount.unit}` : `${f.value.toLocaleString("zh-CN", { maximumSignificantDigits: 4 })} ${f.unit ?? ""}`}`;
  };
  const visibleProofs = busy ? proofs : (result?.evidence ?? proofs);
  return (
    <div className="agent-workspace">
      <section className="panel agent-composer" aria-label="资源助手">
        <div className="panel-head">
          <div>
            <h2>资源助手</h2>
          </div>
          <button className="button-secondary" onClick={reset}>
            <Plus size={16} aria-hidden="true" />
            新建调查
          </button>
        </div>
        <form
          data-analytics-feature="assistant_query"
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
        >
          <label htmlFor="resource-agent-question">你想了解什么？</label>
          <textarea
            ref={formQuestion}
            id="resource-agent-question"
            value={question}
            maxLength={500}
            rows={3}
            onChange={(e) => {
              setQuestion(e.target.value);
              requestKey.current = null;
            }}
            placeholder="例如：为什么CPU历史不完整？GPU低利用率是否有其他解释？"
            disabled={busy}
          />
          <div className="agent-composer-actions">
            <label>
              观察范围
              <select
                aria-label="助手观察范围"
                value={windowSize}
                disabled={busy}
                onChange={(e) => {
                  setWindow(e.target.value as WindowSize);
                  requestKey.current = null;
                }}
              >
                <option value="auto">自动识别时段</option>
                {timeRanges.map((r) => (
                  <option key={r.value} value={r.value}>
                    {r.label}
                  </option>
                ))}
              </select>
            </label>
            {parentId && (result?.entities?.length ?? 0) > 0 && (
              <label>
                调查对象
                <select
                  aria-label="继续调查对象"
                  value={entity}
                  disabled={busy}
                  onChange={(e) => {
                    setEntity(e.target.value);
                    requestKey.current = null;
                  }}
                >
                  <option value="">当前范围</option>
                  {result?.entities?.map((e) => (
                    <option key={e.id} value={e.id}>
                      {e.label ?? `${e.id} · ${e.dimension}`}
                    </option>
                  ))}
                </select>
              </label>
            )}
            {busy ? (
              <button type="button" onClick={() => controller.current?.abort()}>
                <Square size={15} aria-hidden="true" />
                停止调查
              </button>
            ) : (
              <button
                className="primary"
                type="submit"
                disabled={!question.trim()}
              >
                {parentId ? "继续调查" : "开始查询"}
                <ArrowUpRight size={16} aria-hidden="true" />
              </button>
            )}
          </div>
        </form>
        {!result && !busy && (
          <div className="agent-examples">
            {examples.map((text) => (
              <button
                key={text}
                onClick={() => {
                  setQuestion(text);
                  requestKey.current = null;
                  formQuestion.current?.focus();
                }}
              >
                {text}
              </button>
            ))}
          </div>
        )}
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        {busy && (
          <p role="status" className="agent-progress">
            {writing ? "正在撰写回答…" : "正在查询与核对数据…"}
          </p>
        )}
      </section>
      {busy && answer && (
        <section className="panel agent-result" aria-label="正在生成的回答">
          <div className="agent-prose">
            <MarkdownAnswer
              text={answer}
              evidenceIds={proofs.map((p) => p.id)}
            />
          </div>
        </section>
      )}
      {result && (
        <section className="panel agent-result" aria-label="调查结论">
          {result.question && (
            <p className="agent-user-question">{result.question}</p>
          )}
          <div className="panel-head">
            <h2>{statuses[result.status] ?? result.status}</h2>
            {saved && (
              <span className="agent-mode">
                {saved
                  ? `已保存${savedAt ? ` · ${dateTime(savedAt)}` : ""}`
                  : (modes[result.mode ?? ""] ?? "已保存结果")}
              </span>
            )}
          </div>
          {result.scope?.window && (
            <span className="agent-context">
              {saved && result.answer_type !== "facts" ? "原观察范围 · " : ""}
              {result.answer_type === "facts"
                ? saved
                  ? "原快照"
                  : "当前快照"
                : result.scope.window === "all"
                  ? "全部本地历史"
                  : (timeRanges.find((r) => r.value === result.scope?.window)
                      ?.label ?? result.scope.window)}
            </span>
          )}
          {result.answer_status === "fallback" && (
            <p className="notice">回答生成未完成，以下为已查证结果。</p>
          )}
          <AgentReport
            markdown={result.answer_markdown}
            summary={result.summary}
            findings={result.findings}
            evidence={result.evidence}
            questions={result.questions}
            formatFinding={formatFinding}
            context={`${statuses[result.status] ?? result.status} · ${saved ? `已保存${savedAt ? ` · ${dateTime(savedAt)}` : ""}` : (modes[result.mode ?? ""] ?? "已保存结果")} · ${result.answer_type === "facts" ? (saved ? "原快照" : "当前快照") : result.scope?.window === "all" ? "全部本地历史" : (timeRanges.find((r) => r.value === result.scope?.window)?.label ?? "观察范围未指定")}`}
          />
          {result.next_actions?.map((action) => (
            <a className="link" href={action.href} key={action.href}>
              {action.label}
            </a>
          ))}
          <details className="agent-result-footer">
            <summary>回答信息</summary>
            <span>{modes[result.mode ?? ""] ?? "已保存结果"}</span>
            <span>
              模型 {result.model_calls ?? 0} 轮 · 工具 {result.tool_calls ?? 0}{" "}
              次
            </span>
            {parentId && (
              <a href={routeHref("analysis", "records", undefined, parentId)}>
                查看调查记录
              </a>
            )}
          </details>
        </section>
      )}
      {(visibleProofs.length > 0 || steps.length > 0) && (
        <details className="panel agent-evidence" aria-label="调查证据">
          <summary>查看依据与过程</summary>
          <div className="agent-proof-list">
            {visibleProofs.map((p) => (
              <details id={`proof-${p.id}`} key={p.id}>
                <summary>
                  <strong>{p.id}</strong>
                  <span>
                    {p.label === p.tool
                      ? ({
                          get_resource_snapshot: "当前资源快照",
                          get_resource_history: "多资源历史趋势",
                          assess_hypothesis: "假设核验",
                        }[p.tool] ?? p.tool)
                      : p.label}
                  </span>
                  <span>
                    {p.status === "error"
                      ? "查询受限"
                      : p.status === "complete"
                        ? "已核查"
                        : "部分证据"}
                  </span>
                </summary>
                <pre>{JSON.stringify(p.data, null, 2)}</pre>
              </details>
            ))}
          </div>
          <details className="agent-trace">
            <summary>执行步骤（{steps.length}）</summary>
            <ol>
              {steps.map((step, i) => (
                <li key={i}>{step}</li>
              ))}
            </ol>
          </details>
          {!!result?.warnings?.length && (
            <details>
              <summary>运行说明</summary>
              <ul>
                {result.warnings.map((s) => (
                  <li key={s}>{s}</li>
                ))}
              </ul>
            </details>
          )}
        </details>
      )}
    </div>
  );
}
