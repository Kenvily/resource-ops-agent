import { useEffect, useState } from "react";
import type { DeviceStatistics, ResourceHistory } from "../types/statistics";
import { readStatistics as read } from "../api/statistics";
import { memoryText, type DisplayUnit } from "./memory";
import { dateTime } from "./Metrics";
import { usagePercent } from "./EfficiencyEvidence";
import { DetailDrawer } from "./DetailDrawer";
const names: Record<string, string> = { yz: "亦庄", zw: "兆维" };
export function DeviceTable({
  devices,
  allocation,
  physical,
  onSelect,
}: {
  devices: DeviceStatistics[];
  allocation: (value: number | null) => string;
  physical: (value: number | null) => string;
  onSelect: (device: DeviceStatistics) => void;
}) {
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>机器 / GPU</th>
            <th>GPU 型号</th>
            <th>已分配额度</th>
            <th>物理已用 / 剩余</th>
            <th>整卡利用率</th>
            <th>详情</th>
          </tr>
        </thead>
        <tbody>
          {devices.map((d) => (
            <tr key={JSON.stringify([d.cluster, d.node, d.device_uuid])}>
              <td>
                {names[d.cluster]} · {d.node}
                <br />
                {d.device_uuid}
              </td>
              <td>{d.gpu_model ?? "型号未知"}</td>
              <td>{allocation(d.allocated_raw)}</td>
              <td>
                {physical(d.physical_used_mib)} /{" "}
                {physical(d.physical_free_mib)}
              </td>
              <td>{usagePercent(d.utilization_pct)}</td>
              <td>
                <button
                  type="button"
                  className="link"
                  onClick={() => onSelect(d)}
                >
                  查看显卡历史
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
export function DeviceHistory({
  device,
  displayUnit,
  allocation,
  close,
  embedded = false,
}: {
  device: DeviceStatistics;
  displayUnit: DisplayUnit;
  allocation: (v: number | null) => string;
  close: () => void;
  embedded?: boolean;
}) {
  const [windowSize, setWindow] = useState("1h"),
    [result, setResult] = useState<ResourceHistory | null>(null),
    [error, setError] = useState("");
  useEffect(() => {
    const c = new AbortController();
    setResult(null);
    setError("");
    const timer = setTimeout(() => c.abort("timeout"), 20000);
    read<ResourceHistory>(
      "/history",
      {
        cluster: device.cluster,
        node: device.node,
        device_uuid: device.device_uuid,
        window: windowSize,
      },
      c.signal,
    )
      .then((d) => {
        if (!c.signal.aborted) setResult(d);
      })
      .catch((e) => {
        if (!c.signal.aborted)
          setError(e instanceof Error ? e.message : "历史查询失败");
        else if (c.signal.reason === "timeout") setError("历史查询超时");
      })
      .finally(() => clearTimeout(timer));
    return () => c.abort();
  }, [device.cluster, device.node, device.device_uuid, windowSize]);
  const series = (metric: string) =>
    result?.series.find((s) => s.metric === metric)?.points ?? [];
  const points = series("gpu_util");
  const physical = (v: number | null | undefined) =>
    memoryText(v, "MiB", displayUnit);
  const segments: string[][] = [];
  let current: string[] = [];
  points.forEach((p, i) => {
    if (p.mean == null) {
      if (current.length) segments.push(current);
      current = [];
    } else
      current.push(
        `${20 + (i / Math.max(1, points.length - 1)) * 520},${140 - (p.mean / 100) * 120}`,
      );
  });
  if (current.length) segments.push(current);
  const content = (
    <>
      <div className="detail-banner">
        <div>
          <h3>{device.device_uuid}</h3>
          <p>
            {names[device.cluster]} · {device.node} ·{" "}
            {device.gpu_model ?? "型号未知"}
          </p>
          <p>驱动：{device.driver_version ?? "未知"}</p>
        </div>
      </div>
      <div className="efficiency-evidence">
        <div>
          当前已分配额度<strong>{allocation(device.allocated_raw)}</strong>
        </div>
        <div>
          当前物理已用<strong>{physical(device.physical_used_mib)}</strong>
        </div>
        <div>
          当前物理剩余<strong>{physical(device.physical_free_mib)}</strong>
        </div>
      </div>
      <p className="trend-note">
        额度采样：{dateTime(device.quota_sampled_at)} · DCGM 评估：
        {dateTime(device.usage_sampled_at)}
      </p>
      <label className="resource-history-range">
        历史范围
        <select value={windowSize} onChange={(e) => setWindow(e.target.value)}>
          <option value="1h">近 1 小时</option>
          <option value="1d">近 1 天</option>
          <option value="1w">近 1 周</option>
          <option value="1mo">近 1 个月</option>
          <option value="3mo">近 3 个月</option>
        </select>
      </label>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {result && (
        <>
          <p>
            {dateTime(result.start)} — {dateTime(result.end)} ·{" "}
            {result.grain < 3600
              ? `${result.grain / 60} 分钟`
              : `${result.grain / 3600} 小时`}
            粒度
          </p>
          <p>
            整卡利用率有效采样 {points.reduce((n, p) => n + p.valid_points, 0)}{" "}
            / {points.reduce((n, p) => n + p.expected_points, 0)}
          </p>
          {segments.length ? (
            <svg
              viewBox="0 0 560 160"
              role="img"
              aria-label="DCGM 整卡利用率历史，缺测处断开"
              className="resource-history-chart"
            >
              <text x="0" y="14">
                100%
              </text>
              <text x="0" y="156">
                0%
              </text>
              <line x1="20" y1="140" x2="540" y2="140" stroke="#dce4ef" />
              {segments.map((s, i) => (
                <polyline
                  key={i}
                  points={s.join(" ")}
                  fill="none"
                  stroke="#7c3aed"
                  strokeWidth="2"
                />
              ))}
            </svg>
          ) : (
            <p className="notice">该窗口尚无完整 DCGM 历史采样，缺测不补零。</p>
          )}
          <details>
            <summary>查看采样数据（最近 60 个点）</summary>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>时间</th>
                    <th>均值 / 观测峰值利用率</th>
                    <th>物理已用均值</th>
                    <th>物理剩余均值</th>
                    <th>利用率覆盖</th>
                  </tr>
                </thead>
                <tbody>
                  {points.slice(-60).map((p, i) => {
                    const index =
                      points.length - Math.min(60, points.length) + i;
                    return (
                      <tr key={p.timestamp}>
                        <td>{dateTime(p.timestamp)}</td>
                        <td>
                          {usagePercent(p.mean)} / {usagePercent(p.max)}
                        </td>
                        <td>{physical(series("gpu_memory")[index]?.mean)}</td>
                        <td>
                          {physical(series("gpu_memory_free")[index]?.mean)}
                        </td>
                        <td>
                          {p.valid_points}/{p.expected_points}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </details>
        </>
      )}
    </>
  );
  return embedded ? (
    content
  ) : (
    <DetailDrawer
      title="GPU 显存与使用历史"
      label="GPU 显存与使用历史"
      close={close}
    >
      {content}
    </DetailDrawer>
  );
}
