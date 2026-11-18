import { useState } from "react";
import {
  ArrowLeftRight,
  ChevronRight,
  Layers3,
  Server,
  SearchX,
} from "lucide-react";
import type { AssessmentReport } from "../types/assessment";
import { DataDetails } from "./DataDetails";
import { dateTime } from "./Metrics";
import { memoryText, type DisplayUnit } from "./memory";
const names: Record<string, string> = { yz: "亦庄", zw: "兆维" };
const risks = {
  within_observed: "观察期内额度满足",
  historical_shortfall: "历史曾低于需求",
  unknown: "历史证据不足",
};
const minimum = (values: (number | null)[]) =>
  values.length && values.every((v) => v != null)
    ? Math.min(...(values as number[]))
    : null;

export function AssessmentResults({
  report,
  displayUnit,
  selected,
  onSelected,
  saved = false,
}: {
  report: AssessmentReport;
  displayUnit: DisplayUnit;
  selected: number[];
  onSelected: (selected: number[]) => void;
  saved?: boolean;
}) {
  const [compare, setCompare] = useState(false);
  const amount = (v: number | null | undefined) =>
    memoryText(v, "bytes", displayUnit);
  const requirement = report.request;
  const factor = requirement
    ? ({ MB: 1e6, GB: 1e9, GiB: 2 ** 30 } as Record<string, number>)[
        requirement.memory_unit
      ]
    : null;
  const demand =
    report.required_bytes_per_gpu ??
    (factor && requirement ? requirement.memory_per_gpu * factor : null);
  return (
    <div className="planning-report assessment-results">
      <header className="assessment-summary planning-result-heading">
        <div>
          <span className="planning-eyebrow">评估结果</span>
          <h2>
            {report.candidate_count
              ? `找到 ${report.candidate_count} 个额度组合`
              : "暂无符合条件的额度组合"}
          </h2>
        </div>
        <div className="planning-result-meta">
          {report.mode === "mock" && (
            <span className="planning-mode">演示数据</span>
          )}
          {saved && <span className="planning-saved-badge">已保存评估</span>}
          <span>评估于 {dateTime(report.database_read_at)}</span>
        </div>
      </header>
      {requirement && (
        <div className="planning-requirement" aria-label="本次评估需求">
          <Layers3 size={17} aria-hidden="true" />
          <strong>{requirement.gpu_count} 张 GPU</strong>
          <span>
            每卡{" "}
            {factor
              ? amount(demand)
              : `${requirement.memory_per_gpu} ${requirement.memory_unit}`}
          </span>
          <span>
            {requirement.same_node ? "同一服务器" : "同集群可跨服务器"}
          </span>
        </div>
      )}
      <div className="planning-sources" aria-label="集群评估范围">
        {report.clusters.map((c) => (
          <div
            className={`planning-source ${c.status === "healthy" ? "ready" : "unavailable"}`}
            key={c.cluster}
          >
            <Server size={18} aria-hidden="true" />
            <div>
              <strong>{names[c.cluster] ?? c.cluster}</strong>
              <span>
                {c.status === "healthy"
                  ? c.eligible_gpu_count > 0
                    ? `符合单卡要求 ${c.eligible_gpu_count} 张`
                    : "未生成满足条件的候选"
                  : "数据暂不可用于评估"}
              </span>
              {c.warnings.map((w, i) => (
                <p className="assessment-warning" key={i}>
                  {w}
                </p>
              ))}
            </div>
          </div>
        ))}
      </div>
      {!report.candidates.length && (
        <div className="card planning-no-match">
          <SearchX size={30} aria-hidden="true" />
          <h3>
            {report.clusters.some((c) => c.status === "healthy")
              ? "检查条件后重新评估"
              : "等待有效数据后重新评估"}
          </h3>
          <p>
            {report.clusters.some((c) => c.status === "healthy")
              ? "先核对集群提示，再调整卡数、每卡显存或高级选项中的同机要求。"
              : "请先查看集群状态，数据不可用不代表资源不足。"}
          </p>
        </div>
      )}
      <div className="planning-candidates">
        {report.candidates.map((c, i) => {
          const spare = minimum(c.gpus.map((g) => g.unallocated_bytes));
          const historical = minimum(c.gpus.map((g) => g.historical_min_bytes));
          const headroom =
            c.minimum_headroom_bytes ??
            (spare != null && demand != null ? spare - demand : null);
          const checked = selected.includes(i);
          return (
            <article
              className={`card assessment-candidate planning-candidate ${checked ? "is-selected" : ""}`}
              key={`${c.cluster}-${i}`}
              aria-label={`候选 ${i + 1}`}
            >
              <header className="planning-candidate-heading">
                <div className="planning-candidate-title">
                  <span className="planning-candidate-number">
                    {String(i + 1).padStart(2, "0")}
                  </span>
                  <h3>{names[c.cluster] ?? c.cluster}</h3>
                </div>
                <span className={`assessment-risk ${c.risk}`}>
                  {risks[c.risk]}
                </span>
              </header>
              <div className="planning-location">
                <Server size={18} aria-hidden="true" />
                <strong>{c.nodes.join("、")}</strong>
              </div>
              <div className="planning-card-count">
                {c.gpus.length} 张 GPU · {c.nodes.length} 台服务器
              </div>
              <dl className="planning-card-metrics">
                <div>
                  <dt>每卡最低未分配</dt>
                  <dd>{amount(spare)}</dd>
                </div>
                <div>
                  <dt>每卡历史最低</dt>
                  <dd>{amount(historical)}</dd>
                </div>
                {headroom != null && (
                  <div>
                    <dt>满足需求后至少剩余</dt>
                    <dd>{amount(headroom)}</dd>
                  </div>
                )}
              </dl>
              <div className="planning-candidate-footer">
                <label className="planning-selection">
                  <input
                    type="checkbox"
                    aria-label={`选择候选 ${i + 1}`}
                    checked={checked}
                    disabled={!checked && selected.length >= 3}
                    onChange={(e) =>
                      onSelected(
                        e.target.checked
                          ? [...selected, i]
                          : selected.filter((x) => x !== i),
                      )
                    }
                  />
                  {checked ? "已选择" : "选择方案"}
                </label>
                <span>
                  历史覆盖{" "}
                  {minimum(c.gpus.map((g) => g.coverage)) == null
                    ? "—"
                    : `${(minimum(c.gpus.map((g) => g.coverage))! * 100).toFixed(1)}%`}
                </span>
              </div>
              <details className="planning-device-details">
                <summary>
                  查看 {c.gpus.length} 张 GPU 的明细
                  <ChevronRight size={15} aria-hidden="true" />
                </summary>
                <div
                  className="table-scroll"
                  tabIndex={0}
                  role="region"
                  aria-label="单卡额度明细"
                >
                  <table>
                    <thead>
                      <tr>
                        <th>GPU / 服务器</th>
                        <th>当前未分配额度</th>
                        <th>历史最低额度</th>
                        <th>有效采样</th>
                      </tr>
                    </thead>
                    <tbody>
                      {c.gpus.map((g) => (
                        <tr
                          key={JSON.stringify([
                            g.cluster,
                            g.node,
                            g.device_uuid,
                          ])}
                        >
                          <td>
                            <strong>{g.device_uuid}</strong>
                            <small>{g.node}</small>
                          </td>
                          <td>{amount(g.unallocated_bytes)}</td>
                          <td>{amount(g.historical_min_bytes)}</td>
                          <td>
                            {g.observed_points} / {g.expected_points} ·{" "}
                            {(g.coverage * 100).toFixed(1)}%
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </details>
            </article>
          );
        })}
      </div>
      {report.truncated && (
        <p className="notice">
          仅展示前 {report.candidates.length} 个代表组合。
        </p>
      )}
      {report.candidates.length > 1 && (
        <div className="planning-compare-bar">
          <span>
            已选择 <strong>{selected.length}</strong> / 3 个方案
          </span>
          <button
            type="button"
            disabled={selected.length < 2}
            onClick={() => setCompare((v) => !v)}
          >
            <ArrowLeftRight size={16} aria-hidden="true" />
            {compare && selected.length >= 2 ? "收起对比" : "对比方案"}
          </button>
        </div>
      )}
      {compare && selected.length >= 2 && (
        <section className="card planning-comparison" aria-label="候选方案对比">
          <h3>候选方案对比</h3>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>方案</th>
                  <th>集群 / 服务器</th>
                  <th>GPU 数量</th>
                  <th>每卡最低未分配</th>
                  <th>每卡历史最低</th>
                  <th>最低有效覆盖</th>
                  <th>历史风险</th>
                </tr>
              </thead>
              <tbody>
                {selected.map((i) => {
                  const c = report.candidates[i];
                  return c ? (
                    <tr key={i}>
                      <td>候选 {i + 1}</td>
                      <td>
                        {names[c.cluster]} / {c.nodes.join("、")}
                      </td>
                      <td>{c.gpus.length}</td>
                      <td>
                        {amount(
                          minimum(c.gpus.map((g) => g.unallocated_bytes)),
                        )}
                      </td>
                      <td>
                        {amount(
                          minimum(c.gpus.map((g) => g.historical_min_bytes)),
                        )}
                      </td>
                      <td>
                        {minimum(c.gpus.map((g) => g.coverage)) == null
                          ? "—"
                          : `${(minimum(c.gpus.map((g) => g.coverage))! * 100).toFixed(1)}%`}
                      </td>
                      <td>{risks[c.risk]}</td>
                    </tr>
                  ) : null;
                })}
              </tbody>
            </table>
          </div>
        </section>
      )}
      <div className="planning-report-details">
        <DataDetails label="评估口径与数据时间">
          <p>
            历史观察：{dateTime(report.history_window.start)} —{" "}
            {dateTime(report.history_window.end)}
          </p>
          {report.clusters.map((c) => (
            <p key={c.cluster}>
              {names[c.cluster]}源采样：{dateTime(c.source_sampled_at)} · 入库：
              {dateTime(c.snapshot_stored_at)}
            </p>
          ))}
          <p>当前数值为 HAMi 虚拟显存额度，不表示物理显存空闲或可调度能力。</p>
          {report.limitations.map((v, i) => (
            <p key={i}>{v}</p>
          ))}
        </DataDetails>
      </div>
    </div>
  );
}
