import type { Status } from "../types/resource";
export const number = (value: number | null | undefined) =>
  value == null
    ? "—"
    : value.toLocaleString("zh-CN", { maximumFractionDigits: 1 });
export const percent = (value: number | null | undefined) =>
  value == null ? "—" : `${(value * 100).toFixed(1)}%`;
export const time = (value: number | null | undefined) =>
  value
    ? new Date(value * 1000).toLocaleTimeString("zh-CN", { hour12: false })
    : "暂无采样";
export const dateTime = (value: number | null | undefined) =>
  value
    ? new Date(value * 1000).toLocaleString("zh-CN", { hour12: false })
    : "暂无记录";
const labels: Record<Status, string> = {
  healthy: "数据完整",
  degraded: "数据不完整",
  unavailable: "连接失败",
  empty: "暂无指标",
  stale: "数据已过期",
};
export function StatusBadge({ status }: { status: Status }) {
  return (
    <span className={`badge ${status}`}>
      <i />
      {labels[status]}
    </span>
  );
}
export function Bar({ ratio }: { ratio: number | null }) {
  return (
    <div className="bar" aria-hidden="true">
      <span
        className={ratio != null && ratio > 1 ? "over" : ""}
        style={{ width: `${Math.min(100, Math.max(0, (ratio ?? 0) * 100))}%` }}
      />
    </div>
  );
}
