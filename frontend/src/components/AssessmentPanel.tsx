import { createRequestId } from "../api/requestId";
import { AssessmentResults } from "./AssessmentResults";
import type { AssessmentReport as Report } from "../types/assessment";
import {
  ArrowRight,
  Layers3,
  LoaderCircle,
  SlidersHorizontal,
} from "lucide-react";
import {
  DeliveryHistory,
  DeliveryReview,
  useWorkspace,
  workspace,
  type Delivery,
} from "./OperationsWorkspace";
import { useEffect, useRef, useState } from "react";
import type { ClusterFilter } from "../types/resource";
import type { DisplayUnit } from "./memory";

export function AssessmentPanel({
  cluster,
  displayUnit,
}: {
  cluster: ClusterFilter;
  displayUnit: DisplayUnit;
}) {
  const capabilities = useWorkspace();
  const [record, setRecord] = useState<Delivery<Report> | null>(null);
  const [selected, setSelected] = useState<number[]>([]);
  const [count, setCount] = useState(2);
  const [memoryBytes, setMemoryBytes] = useState(20e9);
  const memoryFactor = displayUnit === "GB" ? 1e9 : 1e6;
  const memory = memoryBytes / memoryFactor;
  const [duration, setDuration] = useState(8);
  const [durationUnit, setDurationUnit] = useState<
    "hour" | "day" | "week" | "month"
  >("hour");
  const durationFactors = { hour: 1, day: 24, week: 168, month: 720 };
  const [history, setHistory] = useState(24);
  const [node, setNode] = useState("");
  const [sameNode, setSameNode] = useState(true);
  const [result, setResult] = useState<Report | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const controller = useRef<AbortController | null>(null);
  const clear = () => {
    controller.current?.abort();
    setBusy(false);
    setResult(null);
    setRecord(null);
    setSelected([]);
    setError("");
  };
  useEffect(() => {
    clear();
    setNode("");
    return () => controller.current?.abort();
  }, [cluster]);
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    controller.current?.abort();
    const active = new AbortController();
    controller.current = active;
    setBusy(true);
    setResult(null);
    setRecord(null);
    setSelected([]);
    setError("");
    let timedOut = false;
    const timeout = window.setTimeout(() => {
      timedOut = true;
      active.abort();
    }, 35000);
    const requirement = {
      cluster,
      gpu_count: count,
      memory_per_gpu: memory,
      memory_unit: displayUnit,
      duration_hours: duration * durationFactors[durationUnit],
      history_hours: history,
      node: node.trim() || null,
      same_node: sameNode,
    };
    try {
      if (capabilities.write_enabled) {
        const saved = await workspace<Delivery<Report>>(
          "assessments",
          { request_id: createRequestId(), requirement },
          active.signal,
        );
        if (!active.signal.aborted) {
          setRecord(saved);
          setResult(saved.result);
        }
      } else {
        const response = await fetch("/api/v1/insights/assess", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          signal: active.signal,
          body: JSON.stringify(requirement),
        });
        if (!response.ok) {
          const body = await response.json().catch(() => ({}));
          throw new Error(
            typeof body.detail === "string"
              ? body.detail
              : "评估暂不可用，请稍后重试",
          );
        }
        const report: Report = await response.json();
        if (!active.signal.aborted) setResult(report);
      }
    } catch (reason) {
      if (timedOut && controller.current === active)
        setError("评估查询超时，请稍后重试");
      else if (!active.signal.aborted)
        setError(reason instanceof Error ? reason.message : "评估失败");
    } finally {
      window.clearTimeout(timeout);
      if (controller.current === active) setBusy(false);
    }
  }
  return (
    <section className="planning-workspace" aria-label="资源规划评估">
      <section className="card planning-request">
        <div className="planning-request-heading">
          <div className="planning-section-icon">
            <Layers3 size={21} aria-hidden="true" />
          </div>
          <h2>部署需求</h2>
        </div>
        <form
          data-analytics-feature="planning_assess"
          onSubmit={submit}
          onChange={clear}
          className="assessment-form planning-request-form"
          onInvalidCapture={(e) => {
            const details = (e.target as HTMLElement).closest("details");
            if (details) details.open = true;
          }}
        >
          <div className="planning-primary-fields">
            <label>
              GPU 数量
              <input
                type="number"
                min="1"
                max="64"
                step="1"
                required
                value={count}
                onChange={(e) => setCount(Number(e.target.value))}
              />
            </label>
            <label>
              每卡显存（{displayUnit}）
              <input
                type="number"
                min="0.001"
                max="1000000"
                step="any"
                required
                value={memory}
                onChange={(e) =>
                  setMemoryBytes(Number(e.target.value) * memoryFactor)
                }
              />
            </label>
            <div className="assessment-actions planning-submit">
              <button className="primary" disabled={busy} type="submit">
                {busy ? (
                  <LoaderCircle size={18} className="spin" aria-hidden="true" />
                ) : (
                  <ArrowRight size={18} aria-hidden="true" />
                )}
                {busy ? "正在评估…" : "开始评估"}
              </button>
              {busy && (
                <button type="button" onClick={clear}>
                  取消
                </button>
              )}
            </div>
          </div>
          <details className="planning-advanced">
            <summary>
              <SlidersHorizontal size={16} aria-hidden="true" />
              <strong>高级选项</strong>
              <span>
                {sameNode ? "同一服务器" : "允许跨服务器"} · 历史
                {history === 1 ? "近1小时" : history === 24 ? "近1天" : "近1周"}
                {node.trim() ? " · 已限定服务器" : ""}
              </span>
            </summary>
            <div className="planning-advanced-grid">
              <label>
                多卡位置
                <select
                  value={String(sameNode)}
                  onChange={(e) => setSameNode(e.target.value === "true")}
                >
                  <option value="true">要求同一服务器</option>
                  <option value="false">允许同集群跨服务器</option>
                </select>
              </label>
              <label>
                历史观察窗口
                <select
                  value={history}
                  onChange={(e) => setHistory(Number(e.target.value))}
                >
                  <option value={1}>近 1 小时</option>
                  <option value={24}>近 1 天</option>
                  <option value={168}>近 1 周</option>
                </select>
              </label>
              <label>
                节点名称（可选）
                <input
                  maxLength={256}
                  value={node}
                  onChange={(e) => setNode(e.target.value)}
                  placeholder="精确匹配服务器节点名称"
                />
              </label>
              <div className="assessment-duration">
                <label htmlFor="assessment-duration">
                  预计运行时长（记录用）
                </label>
                <div className="duration-controls">
                  <input
                    id="assessment-duration"
                    type="number"
                    min="1"
                    max={Math.floor(8760 / durationFactors[durationUnit])}
                    step="1"
                    required
                    value={duration}
                    onChange={(e) => setDuration(Number(e.target.value))}
                  />
                  <select
                    aria-label="运行时长单位"
                    value={durationUnit}
                    onChange={(e) =>
                      setDurationUnit(e.target.value as typeof durationUnit)
                    }
                  >
                    <option value="hour">小时</option>
                    <option value="day">天</option>
                    <option value="week">周</option>
                    <option value="month">月（30 天）</option>
                  </select>
                </div>
              </div>
            </div>
          </details>
        </form>
      </section>
      {error && (
        <p role="alert" className="error-banner">
          {error}
        </p>
      )}
      {busy && (
        <div className="card planning-loading" role="status">
          <LoaderCircle size={22} className="spin" aria-hidden="true" />
          <strong>正在核查显存额度与历史记录</strong>
        </div>
      )}
      {!result && !busy && !error && (
        <div className="card planning-empty">
          <Layers3 size={32} aria-hidden="true" />
          <h3>查看适合需求的 GPU 额度组合</h3>
          <p>填写卡数和每卡显存，查看候选服务器与历史余量。</p>
        </div>
      )}
      {result && (
        <AssessmentResults
          key={record?.id ?? result.database_read_at}
          report={result}
          saved={record !== null}
          displayUnit={displayUnit}
          selected={selected}
          onSelected={setSelected}
        />
      )}
      {record && (
        <details className="card planning-saved-review">
          <summary>保存记录与复核</summary>
          <DeliveryReview
            record={record}
            selected={selected}
            enabled={capabilities.write_enabled}
            onSaved={() => {
              workspace<Delivery<Report>>(
                `records/${record.id}?cluster=${cluster}`,
              )
                .then((saved) =>
                  setRecord((current) =>
                    current?.id === saved.id ? saved : current,
                  ),
                )
                .catch((e) => setError(e.message));
            }}
          />
        </details>
      )}
      {capabilities.available && (
        <div className="card planning-history">
          <DeliveryHistory<Report>
            kind="assessment"
            cluster={cluster}
            revision={record?.id ?? ""}
            onLoad={(saved) => {
              controller.current?.abort();
              setBusy(false);
              setError("");
              setRecord(saved);
              setResult(saved.result);
              setSelected(saved.decisions.at(-1)?.candidates ?? []);
              const request = saved.result.request;
              if (request) {
                setCount(request.gpu_count);
                const factor = (
                  { MB: 1e6, GB: 1e9, GiB: 2 ** 30 } as Record<string, number>
                )[request.memory_unit];
                if (factor) setMemoryBytes(request.memory_per_gpu * factor);
                setSameNode(request.same_node);
                setNode(request.node ?? "");
                setHistory(request.history_hours);
                setDuration(request.duration_hours);
                setDurationUnit("hour");
              }
            }}
          />
        </div>
      )}
    </section>
  );
}
