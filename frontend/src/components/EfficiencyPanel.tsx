import { createRequestId } from "../api/requestId";
import { InvestigationLink } from "./InvestigationRecords";
import { readUsageInvestigation } from "../api/efficiency";
import { EfficiencyDetail } from "./EfficiencyDetail";
import {
  EfficiencyEvidence,
  screeningLabels,
  screeningStatus,
} from "./EfficiencyEvidence";
import type {
  EfficiencyDevice as Device,
  EfficiencyReport as Report,
} from "../types/efficiency";
import { useEffect, useRef, useState } from "react";
import type { ClusterFilter, Overview, Node, Gpu } from "../types/resource";
import { dateTime } from "./Metrics";
import { memoryValue, type DisplayUnit } from "./memory";

const names: Record<string, string> = { yz: "亦庄", zw: "兆维" };
const identity = (d: { cluster: string; node: string; device_uuid: string }) =>
  JSON.stringify([d.cluster, d.node, d.device_uuid]);
export function EfficiencyPanel({
  cluster,
  displayUnit,
  resources,
  onInspectNode,
  onInspectGpu,
}: {
  cluster: ClusterFilter;
  displayUnit: DisplayUnit;
  resources?: Overview | null;
  onInspectNode?: (node: Node) => void;
  onInspectGpu?: (gpu: Gpu) => void;
}) {
  const [supportsAgent, setSupportsAgent] = useState(false),
    [available, setAvailable] = useState<boolean | null>(null);
  const [hours, setHours] = useState(1),
    [utilization, setUtilization] = useState(10);
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [progress, setProgress] = useState("");
  const [taskId, setTaskId] = useState<string>();
  const [result, setResult] = useState<Report | null>(null),
    [showAll, setShowAll] = useState(false);
  const [selected, setSelected] = useState<{
    cluster: string;
    node: string;
    uuid?: string;
  } | null>(null);
  const controller = useRef<AbortController | null>(null);
  useEffect(() => {
    const active = new AbortController();
    fetch("/api/v1/efficiency/capabilities", { signal: active.signal })
      .then(async (r) => {
        if (!r.ok) throw Error();
        const data = await r.json();
        setAvailable(data.available);
        setSupportsAgent(Boolean(data.supports_agent));
      })
      .catch(() => {
        if (!active.signal.aborted) setError("核查服务暂不可用");
      });
    return () => active.abort();
  }, []);
  const clear = () => {
    controller.current?.abort();
    setBusy(false);
    setResult(null);
    setTaskId(undefined);
    setSelected(null);
    setError("");
    setProgress("");
  };
  useEffect(() => {
    clear();
    return () => controller.current?.abort();
  }, [cluster]);
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    clear();
    const active = new AbortController();
    controller.current = active;
    setBusy(true);
    const timeout = window.setTimeout(() => active.abort("timeout"), 125000);
    try {
      const response = await fetch(
        supportsAgent
          ? "/api/v1/efficiency/diagnose"
          : "/api/v1/efficiency/investigate",
        {
          method: "POST",
          signal: active.signal,
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": createRequestId(),
          },
          body: JSON.stringify({
            cluster,
            hours,
            utilization_threshold: utilization,
          }),
        },
      );
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw Error(
          typeof data.detail === "string" ? data.detail : "核查暂不可用",
        );
      }
      const data: Report = supportsAgent
        ? await readUsageInvestigation(
            response,
            (message) => {
              if (!active.signal.aborted) setProgress(message);
            },
            (id) => {
              if (!active.signal.aborted) setTaskId(id);
            },
          )
        : await response.json();
      if (!active.signal.aborted) setResult(data);
    } catch (reason) {
      if (active.signal.reason === "timeout") setError("核查超时，请稍后重试");
      else if (!active.signal.aborted)
        setError(reason instanceof Error ? reason.message : "核查失败");
    } finally {
      window.clearTimeout(timeout);
      if (controller.current === active) setBusy(false);
    }
  }
  const amount = (raw: number | null | undefined, unit: string) => {
    const v = memoryValue(raw, unit, displayUnit);
    return `${v.value} ${v.unit}`;
  };
  const current = new Map(
    resources?.clusters
      .filter((c) => c.status === "healthy" || c.status === "degraded")
      .flatMap((c) => c.gpus.map((g) => [identity(g), g] as const)) ?? [],
  );
  const candidates =
    result?.results.filter(
      (d) => screeningStatus(d, utilization) === "low_utilization",
    ) ?? [];
  const devices = showAll ? (result?.results ?? []) : candidates;
  const machines = new Map<
    string,
    { cluster: string; node: string; devices: Device[] }
  >();
  for (const d of devices) {
    const key = JSON.stringify([d.cluster, d.node]);
    const m = machines.get(key) ?? {
      cluster: d.cluster,
      node: d.node,
      devices: [],
    };
    m.devices.push(d);
    machines.set(key, m);
  }
  const selectedDevices =
    result?.results.filter(
      (d) => d.cluster === selected?.cluster && d.node === selected?.node,
    ) ?? [];
  const selectedGpu = selectedDevices.find(
    (d) => d.device_uuid === selected?.uuid,
  );
  const currentGpu = selectedGpu
    ? current.get(identity(selectedGpu))
    : undefined;
  const currentCluster = resources?.clusters.find(
    (c) => c.id === selected?.cluster,
  );
  const currentNode = resources?.nodes.find(
    (n) => n.cluster === selected?.cluster && n.node === selected?.node,
  );
  return (
    <section
      className="card assessment-panel efficiency-panel"
      aria-label="低效占用核查"
    >
      <div className="panel-heading">
        <h2>占用核查</h2>
      </div>
      {available === false && (
        <p className="notice">
          使用数据采集尚未启用，请联系管理员配置监控接入。
        </p>
      )}
      <form
        className="assessment-form efficiency-form"
        data-analytics-feature="efficiency_run"
        onSubmit={submit}
        onChange={clear}
      >
        <label>
          观察窗口
          <select
            value={hours}
            onChange={(e) => setHours(Number(e.target.value))}
          >
            <option value={1}>近 1 小时</option>
            <option value={6}>近 6 小时</option>
            <option value={24}>近 1 天</option>
          </select>
        </label>
        <label>
          整卡利用率上限（%）
          <input
            type="number"
            required
            min={0}
            max={30}
            step="any"
            value={utilization}
            aria-describedby="efficiency-rule"
            onChange={(e) => setUtilization(Number(e.target.value))}
          />
        </label>
        <div className="assessment-actions">
          <button
            type="submit"
            className="primary"
            disabled={busy || !available}
          >
            {busy ? "核查中…" : "开始核查"}
          </button>
          {busy && (
            <button type="button" onClick={clear}>
              取消
            </button>
          )}
        </div>
      </form>
      <p id="efficiency-rule" className="efficiency-rule">
        时段平均整卡利用率 ≤ {utilization}% · 末点有分配 · 有效采样 ≥ 90%
      </p>
      <div aria-live="polite">
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        {busy && <p>{progress || "正在读取使用历史…"}</p>}
      </div>
      <InvestigationLink id={result?.investigation?.id ?? taskId} />
      {result && (
        <div className="assessment-results">
          <div className="assessment-summary">
            <h3>低利用率 GPU {candidates.length} 张</h3>
            <label className="efficiency-filter">
              <input
                type="checkbox"
                checked={showAll}
                onChange={(e) => setShowAll(e.target.checked)}
              />
              显示全部 GPU
            </label>
          </div>
          <p className="efficiency-detail-period">
            {dateTime(result.start)} — {dateTime(result.end)}
          </p>
          <details className="efficiency-result-meta">
            <summary>查询信息</summary>
            <span>已查询 {result.result_count} 张 GPU</span>
            <span>数据库查询：{dateTime(result.database_read_at)}</span>
            {result.agent && (
              <span>
                {result.agent.mode === "deepseek"
                  ? `DeepSeek 已重点核查 ${result.agent.investigated_devices ?? 0} 张 GPU`
                  : result.agent.mode === "degraded"
                    ? "模型降级，已完成规则筛选"
                    : "规则筛选（未调用模型）"}
              </span>
            )}
          </details>
          {result.clusters.flatMap((c) =>
            c.warnings.map((w) => (
              <p className="notice" key={`${c.cluster}-${w}`}>
                {names[c.cluster]}：{w}
              </p>
            )),
          )}
          {result.agent?.warnings.map((w) => (
            <p className="notice" key={w}>
              {w}
            </p>
          ))}
          {[...machines.entries()].map(([key, m]) => (
            <section
              className="efficiency-node"
              key={key}
              aria-label={`${names[m.cluster]} · 机器 ${m.node}`}
            >
              <div className="efficiency-node-heading">
                <div>
                  <h3>
                    {names[m.cluster]} · 机器 {m.node}
                  </h3>
                  <p>本次展示 {m.devices.length} 张 GPU</p>
                </div>
                <button
                  type="button"
                  className="link"
                  onClick={() =>
                    setSelected({ cluster: m.cluster, node: m.node })
                  }
                >
                  查看机器详情
                </button>
              </div>
              {m.devices.map((d) => {
                const gpu = current.get(identity(d));
                return (
                  <article className="assessment-candidate" key={d.device_uuid}>
                    <h4>
                      <span>GPU · {d.device_uuid}</span>
                      <span
                        className={`assessment-risk ${screeningStatus(d, utilization) === "low_utilization" ? "historical_shortfall" : ""}`}
                      >
                        {screeningLabels[screeningStatus(d, utilization)]}
                      </span>
                    </h4>
                    <EfficiencyEvidence
                      device={d}
                      amount={amount}
                      currentAllocation={gpu?.allocated_raw}
                      currentUnit={resources?.units.allocation}
                    />
                    <div className="efficiency-card-actions">
                      <button
                        type="button"
                        className="link"
                        onClick={() =>
                          setSelected({
                            cluster: d.cluster,
                            node: d.node,
                            uuid: d.device_uuid,
                          })
                        }
                      >
                        查看 GPU 详情
                      </button>
                    </div>
                  </article>
                );
              })}
            </section>
          ))}
          {!devices.length && (
            <p className="empty">
              {result.result_count
                ? showAll
                  ? "没有可展示的 GPU。"
                  : "没有符合条件的 GPU，可勾选“显示全部 GPU”查看其他状态。"
                : "尚无可核查的使用历史。"}
            </p>
          )}
          {result.truncated && (
            <p className="notice">
              仅展示前 100 张 GPU，低利用率数量仅统计已展示部分，请收窄集群。
            </p>
          )}
        </div>
      )}
      {selected && result && (
        <EfficiencyDetail
          clusterName={names[selected.cluster]}
          node={selected.node}
          devices={selectedDevices}
          device={selectedGpu}
          start={result.start}
          end={result.end}
          amount={amount}
          close={() => setSelected(null)}
          labels={Object.fromEntries(
            selectedDevices.map((d) => [
              d.device_uuid,
              screeningLabels[screeningStatus(d, utilization)],
            ]),
          )}
          onSelectGpu={(d) =>
            setSelected({
              cluster: d.cluster,
              node: d.node,
              uuid: d.device_uuid,
            })
          }
          onBack={() =>
            setSelected({ cluster: selected.cluster, node: selected.node })
          }
          currentGpu={currentGpu}
          currentUnit={resources?.units.allocation}
          currentSampledAt={
            currentGpu
              ? (currentGpu.sampled_at ?? currentCluster?.sampled_at)
              : null
          }
          onCurrentGpu={
            currentGpu && onInspectGpu
              ? () => {
                  setSelected(null);
                  onInspectGpu(currentGpu);
                }
              : undefined
          }
          onCurrentNode={
            currentNode &&
            currentCluster &&
            ["healthy", "degraded"].includes(currentCluster.status) &&
            onInspectNode
              ? () => {
                  setSelected(null);
                  onInspectNode(currentNode);
                }
              : undefined
          }
        />
      )}
    </section>
  );
}
