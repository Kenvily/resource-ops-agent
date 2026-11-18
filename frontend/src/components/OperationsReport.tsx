import { createRequestId } from "../api/requestId";
import { ReportSources } from "./ReportSources";
import {
  DeliveryHistory,
  DeliveryReview,
  useWorkspace,
  workspace,
  type Delivery,
} from "./OperationsWorkspace";
import { useEffect, useRef, useState } from "react";
import { operationsRequest, type OperationReport } from "../api/operations";
import type { ClusterFilter } from "../types/resource";
import { dateTime } from "./Metrics";
import { memoryValue, type DisplayUnit } from "./memory";
const names: Record<string, string> = { yz: "亦庄", zw: "兆维" };
const safe = (text: string) => text.replace(/[\r\n|<>]/g, " ");
export function OperationsReport({
  cluster,
  displayUnit,
}: {
  cluster: ClusterFilter;
  displayUnit: DisplayUnit;
}) {
  const caps = useWorkspace();
  const [record, setRecord] = useState<Delivery<OperationReport> | null>(null);
  const [taskIds, setTaskIds] = useState<string[]>([]),
    [issueIds, setIssueIds] = useState<string[]>([]);
  const [windowSize, setWindow] = useState("1d"),
    [report, setReport] = useState<OperationReport | null>(null);
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [reviewed, setReviewed] = useState(false),
    [notes, setNotes] = useState("");
  const active = useRef<AbortController | null>(null);
  function clear() {
    active.current?.abort();
    setReport(null);
    setRecord(null);
    setBusy(false);
    setError("");
    setReviewed(false);
    setNotes("");
  }
  useEffect(() => {
    clear();
    return () => active.current?.abort();
  }, [cluster, windowSize]);
  useEffect(() => {
    setTaskIds([]);
    setIssueIds([]);
  }, [cluster]);
  useEffect(() => setReviewed(false), [displayUnit]);
  const amount = (raw: number | null | undefined, source: string) => {
    const v = memoryValue(raw, source, displayUnit);
    return `${v.value} ${v.unit}`;
  };
  async function generate() {
    clear();
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    const timeout = setTimeout(() => controller.abort("timeout"), 25000);
    try {
      if (caps.write_enabled) {
        const saved = await workspace<Delivery<OperationReport>>(
          "reports",
          {
            request_id: createRequestId(),
            report: { cluster, window: windowSize },
            investigation_ids: taskIds,
            issue_ids: issueIds,
          },
          controller.signal,
        );
        if (!controller.signal.aborted) {
          setRecord(saved);
          setReport(saved.result);
        }
        return;
      }
      const result = await operationsRequest<OperationReport>(
        "reports",
        controller.signal,
        { cluster, window: windowSize },
      );
      if (!controller.signal.aborted) setReport(result);
    } catch (e) {
      if (controller.signal.reason === "timeout")
        setError("报告生成超时，请稍后重试");
      else if (!controller.signal.aborted)
        setError(e instanceof Error ? e.message : "报告生成失败");
    } finally {
      clearTimeout(timeout);
      if (active.current === controller) setBusy(false);
    }
  }
  function download() {
    if (
      !report ||
      (record ? record.decisions.at(-1)?.decision !== "accepted" : !reviewed)
    )
      return;
    const lines = [
      "# GPU 资源运营报告",
      `生成时间：${dateTime(report.generated_at)}`,
      `目标时段：${dateTime(report.target.start)} — ${dateTime(report.target.end)}`,
      `对比时段：${dateTime(report.baseline.start)} — ${dateTime(report.baseline.end)}`,
      `显示单位：${displayUnit}`,
      record
        ? `报告编号：${record.id}；复核状态：本机人工确认`
        : "复核状态：用户已在浏览器会话确认（不代表审计认证）",
      "",
    ];
    for (const entry of report.clusters) {
      const change = entry.comparison?.changes[0];
      lines.push(
        `## ${names[entry.cluster]}`,
        `历史证据：${entry.comparison?.complete ? "完整" : "不足，不能判断变化"}`,
        `当前GPU数：${entry.current?.gpu_count ?? "未知"}`,
        `当前已分配：${amount(entry.current?.allocated_raw, report.units.allocation)}`,
      );
      if (change)
        lines.push(
          `时段平均已分配：${amount(change.target.allocated_mean_raw, report.units.allocation)}`,
          `相比前一时段：${amount(change.allocated_change_raw, report.units.allocation)}`,
          `覆盖：${change.target.verified_coverage_points}/${change.target.expected_points}`,
        );
      for (const row of entry.namespace_changes?.changes ?? [])
        lines.push(
          `Namespace ${safe(row.entity.join(" / "))}：均值变化 ${amount(row.allocated_change_raw, report.units.allocation)}`,
        );
      lines.push(
        ...(entry.comparison?.warnings ?? []).map(safe),
        ...entry.warnings.map(safe),
        "",
      );
    }
    lines.push(
      "## 当前关注项",
      ...report.alerts.map(
        (a) =>
          `${names[a.cluster]} / ${safe(a.node)}：${safe(a.message)}（${dateTime(a.evaluated_at)}）`,
      ),
    );
    lines.push(
      "## 当前指标覆盖",
      ...report.metrics.map(
        (m) =>
          `${names[m.cluster]} / ${m.label}：${safe(m.status)}，有效实体 ${m.values.length}/${m.expected_entities ?? "未知"}`,
      ),
      "## 人工复核说明",
      notes || "无补充",
      ...report.notes,
    );
    const url = URL.createObjectURL(
      new Blob([lines.join("\n\n")], { type: "text/markdown;charset=utf-8" }),
    );
    const a = document.createElement("a");
    a.href = url;
    a.download = `gpu-operations-${report.window}.md`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  return (
    <section className="card operations-panel" aria-label="运营报告">
      <div className="panel-heading">
        <h2>运营报告</h2>
      </div>
      <form
        data-analytics-feature="report_generate"
        className="operations-form"
        onSubmit={(e) => {
          e.preventDefault();
          void generate();
        }}
      >
        <label>
          报告时段
          <select
            value={windowSize}
            onChange={(e) => setWindow(e.target.value)}
          >
            <option value="1d">近 1 天</option>
            <option value="1w">近 1 周</option>
            <option value="1mo">近 30 天</option>
          </select>
        </label>
        <button type="submit" className="primary" disabled={busy}>
          {busy ? "生成中…" : "生成报告"}
        </button>
        {busy && (
          <button type="button" onClick={clear}>
            取消
          </button>
        )}
      </form>
      {caps.available && (
        <DeliveryHistory<OperationReport>
          kind="report"
          cluster={cluster}
          revision={record?.id ?? ""}
          onLoad={(saved) => {
            active.current?.abort();
            setBusy(false);
            setError("");
            setRecord(saved);
            setReport(saved.result);
            setReviewed(false);
            setNotes("");
          }}
        />
      )}
      {caps.write_enabled && (
        <ReportSources
          cluster={cluster}
          tasks={taskIds}
          issues={issueIds}
          setTasks={setTaskIds}
          setIssues={setIssueIds}
          disabled={busy}
        />
      )}
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {!report && !busy && !error && (
        <p className="empty">选择时段生成报告，核对后可导出。</p>
      )}
      {report && (
        <div className="operations-report">
          <div className="assessment-summary">
            <h3>资源运营报告</h3>
            <span>
              {(
                record
                  ? record.decisions.at(-1)?.decision === "accepted"
                  : reviewed
              )
                ? "已人工核对"
                : "待核对草稿"}
              {report.mode === "mock" ? " · Mock" : ""}
            </span>
          </div>
          <p>
            {dateTime(report.target.start)} — {dateTime(report.target.end)}
          </p>
          <p className="trend-note">
            对比前一等长时段：{dateTime(report.baseline.start)} —{" "}
            {dateTime(report.baseline.end)}
          </p>
          {report.clusters.map((entry) => {
            const change = entry.comparison?.changes[0];
            return (
              <section className="operations-section" key={entry.cluster}>
                <h3>{names[entry.cluster]}</h3>
                <p className={entry.comparison?.complete ? "" : "notice"}>
                  {entry.comparison?.complete
                    ? "历史证据完整"
                    : "历史证据不足，暂不判断时段变化"}
                </p>
                {entry.current &&
                  !["healthy", "degraded"].includes(entry.current.status) && (
                    <p className="notice">当前快照不可用或已过期。</p>
                  )}
                <div className="efficiency-evidence">
                  <div>
                    当前 GPU 数
                    <strong>{entry.current?.gpu_count ?? "—"}</strong>
                  </div>
                  <div>
                    当前已分配
                    <strong>
                      {amount(
                        entry.current?.allocated_raw,
                        report.units.allocation,
                      )}
                    </strong>
                  </div>
                  <div>
                    时段平均已分配
                    <strong>
                      {amount(
                        change?.target.allocated_mean_raw,
                        report.units.allocation,
                      )}
                    </strong>
                  </div>
                  <div>
                    较前一时段变化
                    <strong>
                      {amount(
                        change?.allocated_change_raw,
                        report.units.allocation,
                      )}
                    </strong>
                  </div>
                </div>
                {change && (
                  <p>
                    有效覆盖 {change.target.verified_coverage_points} /{" "}
                    {change.target.expected_points} · 当前快照{" "}
                    {dateTime(entry.current?.sampled_at)}
                  </p>
                )}
                <details>
                  <summary>查看历史证据与 Namespace 变化</summary>
                  <div className="table-scroll">
                    <table>
                      <thead>
                        <tr>
                          <th>Namespace</th>
                          <th>前期平均分配</th>
                          <th>本期平均分配</th>
                          <th>变化</th>
                        </tr>
                      </thead>
                      <tbody>
                        {entry.namespace_changes?.changes.map((row) => (
                          <tr key={row.entity.join("/")}>
                            <td>{row.entity.join(" / ")}</td>
                            <td>
                              {amount(
                                row.baseline.allocated_mean_raw,
                                report.units.allocation,
                              )}
                            </td>
                            <td>
                              {amount(
                                row.target.allocated_mean_raw,
                                report.units.allocation,
                              )}
                            </td>
                            <td>
                              {amount(
                                row.allocated_change_raw,
                                report.units.allocation,
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  {entry.comparison?.warnings.map((w) => (
                    <p key={w}>{w}</p>
                  ))}
                  {entry.namespace_changes?.warnings.map((w) => (
                    <p key={w}>{w}</p>
                  ))}
                  {entry.warnings.map((w) => (
                    <p key={w}>{w}</p>
                  ))}
                </details>
              </section>
            );
          })}
          <section className="operations-section">
            <h3>生成时关注项 · {report.alert_count} 条</h3>
            {report.alerts.map((a, i) => (
              <p key={i}>
                {names[a.cluster]} · {a.node} · {a.message}
              </p>
            ))}
            <p>
              {report.metrics.filter((m) => m.status !== "ready").length}{" "}
              项指标未完整覆盖。
            </p>
          </section>
          {report.linked_investigations?.length ||
          report.linked_issues?.length ? (
            <section className="operations-section">
              <h3>关联证据</h3>
              {(report.trace_warnings ?? []).map((w) => (
                <p className="notice" key={w}>
                  {w}
                </p>
              ))}
              {(report.linked_investigations ?? []).map((t) => (
                <details key={t.id}>
                  <summary>
                    调查 · {t.status} · {t.id}
                  </summary>
                  {t.findings.map((f, i) => (
                    <p key={i}>
                      {f.level === "fact"
                        ? "事实"
                        : f.level === "inference"
                          ? "推断"
                          : "无法判断"}{" "}
                      · {f.title}
                    </p>
                  ))}
                  <pre>{JSON.stringify(t, null, 2)}</pre>
                </details>
              ))}
              {(report.linked_issues ?? []).map((i) => (
                <p key={i.id}>
                  {i.title} · {i.status}
                </p>
              ))}
            </section>
          ) : null}
          {record && (
            <DeliveryReview
              record={record}
              enabled={caps.write_enabled}
              onSaved={() =>
                workspace<Delivery<OperationReport>>(
                  `records/${record.id}?cluster=${cluster}`,
                )
                  .then((saved) =>
                    setRecord((current) =>
                      current?.id === saved.id ? saved : current,
                    ),
                  )
                  .catch((e) => setError(e.message))
              }
            />
          )}
          <div className="operations-review">
            {!record && (
              <>
                <label>
                  复核说明（可选）
                  <textarea
                    maxLength={2000}
                    value={notes}
                    onChange={(e) => {
                      setNotes(e.target.value);
                      setReviewed(false);
                    }}
                  />
                </label>
                <label className="efficiency-filter">
                  <input
                    type="checkbox"
                    checked={reviewed}
                    onChange={(e) => setReviewed(e.target.checked)}
                  />
                  我已核对报告的时间范围、数据缺口与结论
                </label>
              </>
            )}
            <button
              type="button"
              className="primary"
              disabled={
                record
                  ? record.decisions.at(-1)?.decision !== "accepted"
                  : !reviewed
              }
              data-analytics-feature="report_export"
              onClick={download}
            >
              导出 Markdown
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
