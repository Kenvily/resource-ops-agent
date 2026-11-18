import { createRequestId } from "../api/requestId";
import { InvestigationLink } from "./InvestigationRecords";
import { timeRanges } from "../types/windows";
import { useEffect, useRef, useState } from "react";
import { SearchCheck, Square } from "lucide-react";
import type { ClusterFilter, Overview } from "../types/resource";
import type { WindowSize } from "../types/trend";
import { memoryValue, type DisplayUnit } from "./memory";
import { percent } from "./Metrics";

type Evidence = {
  id: string;
  tool: string;
  label: string;
  cluster: string | null;
  status: string;
  data: Record<string, unknown>;
  elapsed_ms: number;
};
type Finding = {
  cluster: string;
  title: string;
  kind: string;
  evidence_ids: string[];
  capacity_change_raw: number | null;
  allocated_change_raw: number | null;
  baseline_ratio: number | null;
  target_ratio: number | null;
  limitations: string[];
};
type Result = {
  investigation?: { id: string };
  id: string;
  mode: "deepseek" | "deterministic" | "degraded";
  status: string;
  findings: Finding[];
  evidence: Evidence[];
  entities: { id: string; label: string; dimension: string }[];
  warnings: string[];
  tool_calls: number;
  model_calls: number;
  elapsed_ms: number;
  units: Overview["units"];
  windows: Record<
    string,
    Record<string, { start: number; end: number; grain: number }>
  >;
};
type Capability = { available: boolean; model_ready: boolean };
// Generic metric definitions remain available in evidence and the indicator guide.
const generalExplanations = new Set([
  "统计描述虚拟显存额度，不表示实际GPU利用率或调度能力",
  "名称关联与同步指标不能证明任务生命周期或因果关系",
]);
const questionExamples = [
  {
    label: "分配率变化",
    question:
      "分析所选时段的分配率变化，区分已分配额度变化、容量变化和数据异常。",
  },
  {
    label: "容量变化",
    question:
      "核查所选时段总容量是否变化，定位相关节点和 GPU，并检查是否存在采样缺口。",
  },
  {
    label: "业务增长贡献",
    question:
      "所选时段哪些 Namespace 的已分配额度增长较多？继续核查主要贡献者的 Pod 变化。",
  },
  {
    label: "额度下降来源",
    question:
      "核查所选时段已分配额度是否下降，定位主要节点和业务贡献，不将额度减少直接解释为任务结束。",
  },
  {
    label: "节点变化对比",
    question:
      "比较所选时段各节点的容量和已分配额度变化，找出变化较明显的节点并下钻相关 GPU。",
  },
  {
    label: "变化发生时段",
    question:
      "定位所选范围内分配变化较明显的时段，核查同期容量变化和主要业务贡献。",
  },
  {
    label: "历史数据完整性",
    question:
      "检查所选时段的数据覆盖、缺测和新鲜度，说明哪些变化结论有证据支持、哪些暂不能判断。",
  },
  {
    label: "集群分别核查",
    question:
      "分别核查所选集群的容量、已分配额度和分配率变化，说明各自的主要贡献与证据缺口。",
  },
];
const clusterName = (id: string | null) =>
  id === "yz" ? "亦庄" : id === "zw" ? "兆维" : "指标口径";
const statusName: Record<string, string> = {
  complete: "核查完成",
  limited: "证据有限",
  error: "查询失败",
  budget_exhausted: "已达预算",
  refused: "范围不支持",
  unavailable: "暂不可用",
};
const date = (ts: number) =>
  new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false });

export function DiagnosisPanel({
  cluster,
  windowSize,
  displayUnit,
}: {
  cluster: ClusterFilter;
  windowSize: WindowSize;
  displayUnit: DisplayUnit;
}) {
  const [diagnosisWindow, setDiagnosisWindow] = useState<WindowSize | "custom">(
    windowSize,
  );
  const localInput = (ts: number) => {
    const d = new Date(ts);
    return new Date(ts - d.getTimezoneOffset() * 60000)
      .toISOString()
      .slice(0, 16);
  };
  const [customStart, setCustomStart] = useState(() =>
    localInput(Date.now() - 86400000),
  );
  const [customEnd, setCustomEnd] = useState(() =>
    localInput(Date.now() - 180000),
  );
  useEffect(() => setDiagnosisWindow(windowSize), [windowSize]);
  const [capability, setCapability] = useState<Capability | null>(null);
  const [question, setQuestion] = useState(
    "分析分配率变化，核查容量变化和主要业务贡献。",
  );
  const [steps, setSteps] = useState<Evidence[]>([]);
  const [taskId, setTaskId] = useState<string>();
  const [result, setResult] = useState<Result | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const controller = useRef<AbortController | null>(null);
  const generation = useRef(0);
  useEffect(() => {
    const c = new AbortController();
    fetch("/api/v1/insights/capabilities", { signal: c.signal })
      .then((r) => {
        if (!r.ok) throw Error();
        return r.json();
      })
      .then(setCapability)
      .catch(() => {
        if (!c.signal.aborted) setMessage("诊断服务暂不可用");
      });
    return () => c.abort();
  }, []);
  useEffect(() => {
    generation.current++;
    controller.current?.abort();
    setSteps([]);
    setResult(null);
    setTaskId(undefined);
    setBusy(false);
    setMessage("");
    return () => {
      generation.current++;
      controller.current?.abort();
    };
  }, [cluster, windowSize, diagnosisWindow, customStart, customEnd]);
  async function start() {
    const token = ++generation.current;
    const c = new AbortController();
    controller.current = c;
    setResult(null);
    setTaskId(undefined);
    setSteps([]);
    setBusy(true);
    setMessage("正在准备历史核查…");
    const timeout = window.setTimeout(() => c.abort("timeout"), 125000);
    try {
      if (
        diagnosisWindow === "custom" &&
        (!Number.isFinite(new Date(customStart).getTime()) ||
          !Number.isFinite(new Date(customEnd).getTime()) ||
          new Date(customEnd).getTime() <= new Date(customStart).getTime())
      )
        throw Error("请输入有效的自定义起止时间，结束时间须晚于开始时间");
      const response = await fetch("/api/v1/insights/diagnose", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": createRequestId(),
        },
        body: JSON.stringify({
          cluster,
          window: diagnosisWindow === "custom" ? "1d" : diagnosisWindow,
          question,
          ...(diagnosisWindow === "custom"
            ? {
                start: Math.floor(new Date(customStart).getTime() / 1000),
                end: Math.floor(new Date(customEnd).getTime() / 1000),
              }
            : {}),
        }),
        signal: c.signal,
      });
      if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        throw Error(error.detail || "诊断请求失败，请重试");
      }
      if (!response.body) throw Error("浏览器不支持流式响应");
      const reader = response.body.getReader(),
        decoder = new TextDecoder();
      let buffer = "",
        received = 0,
        completed = false;
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        received += value.byteLength;
        if (received > 500000) {
          await reader.cancel();
          throw Error("诊断结果超过显示预算");
        }
        buffer += decoder.decode(value, { stream: true });
        let newline;
        while ((newline = buffer.indexOf("\n")) >= 0) {
          const line = buffer.slice(0, newline);
          buffer = buffer.slice(newline + 1);
          if (!line.trim()) continue;
          const event = JSON.parse(line);
          if (generation.current !== token) return;
          if (event.type === "task") {
            setTaskId(event.data.id);
            setMessage("检查范围和证据覆盖 → 定位贡献 → 核查替代解释");
          }
          if (event.type === "progress") setMessage(event.data.label);
          if (event.type === "evidence") {
            setSteps((s) => [...s, event.data]);
            setMessage(event.data.label);
          }
          if (event.type === "result") {
            setResult(event.data);
            setMessage("核查结束，请查看结论与证据。");
            completed = true;
          }
          if (event.type === "error") throw Error(event.data.message);
        }
      }
      if (!completed) throw Error("诊断连接中断，已完成的步骤仅供核查，请重试");
    } catch (error) {
      if (generation.current === token)
        setMessage(
          c.signal.aborted
            ? "诊断已停止，未完成步骤不生成结论。"
            : error instanceof Error
              ? error.message
              : "诊断失败，请重试",
        );
    } finally {
      window.clearTimeout(timeout);
      if (generation.current === token) setBusy(false);
    }
  }
  const memory = (raw: number | null, kind: "capacity" | "allocation") => {
    const value = memoryValue(raw, result?.units[kind], displayUnit);
    return `${value.value} ${value.unit}`;
  };
  return (
    <section className="card diagnosis-panel" aria-label="资源变化诊断">
      <div className="panel-heading">
        <div>
          <h2>
            <SearchCheck size={20} aria-hidden="true" />
            资源变化诊断
          </h2>
        </div>
      </div>

      <div className="diagnosis-range">
        <label>
          诊断时间范围
          <select
            aria-label="诊断时间范围"
            value={diagnosisWindow}
            onChange={(e) =>
              setDiagnosisWindow(e.target.value as WindowSize | "custom")
            }
          >
            {timeRanges.map((r) => (
              <option key={r.value} value={r.value}>
                {r.label}
              </option>
            ))}
            <option value="custom">自定义时间</option>
          </select>
        </label>
        {diagnosisWindow === "custom" && (
          <>
            <label>
              开始时间
              <input
                aria-label="诊断开始时间"
                type="datetime-local"
                required
                value={customStart}
                onChange={(e) => setCustomStart(e.target.value)}
              />
            </label>
            <label>
              结束时间
              <input
                aria-label="诊断结束时间"
                type="datetime-local"
                required
                value={customEnd}
                onChange={(e) => setCustomEnd(e.target.value)}
              />
            </label>
          </>
        )}
      </div>
      {diagnosisWindow === "custom" && (
        <p className="input-help">范围限最近四个自然月。</p>
      )}
      {capability && !capability.available ? (
        <p className="notice">
          诊断需要 PostgreSQL 历史库，请先配置数据库并完成对应范围的历史同步。
        </p>
      ) : (
        <div className="diagnosis-input">
          <label htmlFor="diagnosis-question">
            核查问题
            <input
              id="diagnosis-question"
              maxLength={500}
              value={question}
              disabled={busy}
              onChange={(e) => setQuestion(e.target.value)}
            />
          </label>
          {busy ? (
            <button onClick={() => controller.current?.abort()}>
              <Square size={14} aria-hidden="true" />
              取消诊断
            </button>
          ) : (
            <button
              className="primary"
              disabled={!capability?.available || !question.trim()}
              data-analytics-feature="diagnosis_run"
              onClick={start}
            >
              {steps.length || result ? "重新诊断" : "开始诊断"}
            </button>
          )}
        </div>
      )}
      {capability?.available && (
        <div
          className="diagnosis-examples"
          role="group"
          aria-label="核查问题示例"
        >
          {questionExamples.map((example) => (
            <button
              type="button"
              key={example.label}
              disabled={busy}
              aria-pressed={question === example.question}
              title={example.question}
              onClick={() => {
                setQuestion(example.question);
                document.getElementById("diagnosis-question")?.focus();
              }}
            >
              {example.label}
            </button>
          ))}
        </div>
      )}
      <p role="status" className="diagnosis-progress">
        {busy && <span className="diagnosis-dot" />}
        {message}
      </p>
      {result && (
        <div className="diagnosis-results">
          <details className="diagnosis-result-meta">
            <summary>查询信息</summary>
            {statusName[result.status] || result.status} ·{" "}
            {result.mode === "deepseek"
              ? "DeepSeek 工具编排"
              : result.mode === "degraded"
                ? "模型降级 · 确定性补充核查"
                : "确定性流程 · 未使用模型"}{" "}
            · {result.tool_calls} 次工具 / {result.model_calls} 次模型请求
          </details>
          {result.windows.full && (
            <>
              <p className="trend-note">
                基线：{date(result.windows.full.baseline.start)} —{" "}
                {date(result.windows.full.baseline.end)}
                <br />
                比较：{date(result.windows.full.target.start)} —{" "}
                {date(result.windows.full.target.end)} · 采样粒度{" "}
                {result.windows.full.target.grain} 秒
              </p>
            </>
          )}
          {result.findings.map((f) => (
            <article
              className={`diagnosis-finding ${f.kind === "insufficient" ? "limited" : ""}`}
              key={f.cluster}
            >
              <h3>
                {clusterName(f.cluster)} · {f.title}
              </h3>
              <div className="diagnosis-values">
                <span>
                  已分配变化{" "}
                  <b>{memory(f.allocated_change_raw, "allocation")}</b>
                </span>
                <span>
                  监控容量变化{" "}
                  <b>{memory(f.capacity_change_raw, "capacity")}</b>
                </span>
                <span>
                  平均分配率{" "}
                  <b>
                    {percent(f.baseline_ratio)} → {percent(f.target_ratio)}
                  </b>
                </span>
              </div>
              {f.limitations.some((text) => !generalExplanations.has(text)) && (
                <p className="trend-note">
                  {f.limitations
                    .filter((text) => !generalExplanations.has(text))
                    .join("；")}
                </p>
              )}
              <div className="diagnosis-citations">
                {f.evidence_ids.map((id) => (
                  <a
                    key={id}
                    href={`#${result.id}-${id}`}
                    onClick={(event) => {
                      event.preventDefault();
                      const target = document.getElementById(
                        `${result.id}-${id}`,
                      );
                      const outer = target?.closest(
                        ".diagnosis-evidence",
                      ) as HTMLDetailsElement | null;
                      if (outer) outer.open = true;
                      const inner = target?.querySelector("details");
                      if (inner) inner.open = true;
                      requestAnimationFrame(() =>
                        target?.scrollIntoView({ block: "center" }),
                      );
                    }}
                  >
                    {id.replace("evidence-", "证据 ")}
                  </a>
                ))}
              </div>
            </article>
          ))}
          {result.warnings.map((w) => (
            <p key={w} className="notice">
              {w}
            </p>
          ))}
        </div>
      )}
      <InvestigationLink id={result?.investigation?.id ?? taskId} />
      {steps.length > 0 && (
        <details className="diagnosis-evidence" open={busy}>
          <summary>核查步骤与原始证据（{steps.length}）</summary>
          <ol>
            {steps.map((step) => (
              <li
                key={step.id}
                id={result ? `${result.id}-${step.id}` : undefined}
              >
                <details>
                  <summary>
                    <span>
                      {step.label} · {clusterName(step.cluster)}
                    </span>
                    <small>
                      {statusName[step.status]} · {Math.round(step.elapsed_ms)}{" "}
                      ms
                    </small>
                  </summary>
                  <EvidenceView
                    step={step}
                    result={result}
                    displayUnit={displayUnit}
                  />
                </details>
              </li>
            ))}
          </ol>
        </details>
      )}
    </section>
  );
}
function EvidenceView({
  step,
  result,
  displayUnit,
}: {
  step: Evidence;
  result: Result | null;
  displayUnit: DisplayUnit;
}) {
  const changes = step.data.changes as
    | {
        entity_id: string;
        allocated_change_raw: number | null;
        capacity_change_raw: number | null;
      }[]
    | undefined;
  const format = (n: number | null, kind: "capacity" | "allocation") => {
    const v = memoryValue(n, result?.units[kind], displayUnit);
    return `${v.value} ${v.unit}`;
  };
  return (
    <div>
      {changes && (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>历史实体</th>
                <th>已分配变化</th>
                <th>容量变化</th>
              </tr>
            </thead>
            <tbody>
              {changes.map((c) => (
                <tr key={c.entity_id}>
                  <td>
                    {result?.entities.find((e) => e.id === c.entity_id)
                      ?.label ||
                      c.entity_id ||
                      "集群"}
                  </td>
                  <td>{format(c.allocated_change_raw, "allocation")}</td>
                  <td>{format(c.capacity_change_raw, "capacity")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="trend-note">
        下方为数据库 Service 原始证据；数值字段以 units
        声明的原始单位计量，entity_id 对应本次诊断实体。
      </p>
      <pre tabIndex={0}>{JSON.stringify(step.data, null, 2)}</pre>
    </div>
  );
}
