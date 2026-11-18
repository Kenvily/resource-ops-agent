import { useEffect, useState } from "react";
import type { DeviceStatistics, ResourceStatistics } from "../types/statistics";
import { memoryText, type DisplayUnit } from "./memory";
import { usagePercent } from "./EfficiencyEvidence";
import { AssetSummary } from "./AssetSummary";
const names: Record<string, string> = { yz: "亦庄", zw: "兆维" };
export function GpuAssets({
  data,
  displayUnit,
  onInspect,
  memoryFilter,
  onMemoryFilterChange,
}: {
  data: ResourceStatistics;
  displayUnit: DisplayUnit;
  onInspect: (d: DeviceStatistics) => void;
  memoryFilter?: "zero" | "used" | "unknown";
  onMemoryFilterChange?: (value: "all" | "zero" | "used" | "unknown") => void;
}) {
  const [query, setQuery] = useState(""),
    [model, setModel] = useState("all"),
    [state, setState] = useState("all"),
    [sort, setSort] = useState("zero");
  const scope = data.sources.map((s) => s.cluster).join(",");
  useEffect(() => {
    setQuery("");
    setModel("all");
    setState("all");
  }, [scope]);
  useEffect(() => setState(memoryFilter ?? "all"), [memoryFilter]);
  const changeMemoryFilter = (value: "all" | "zero" | "used" | "unknown") => {
    setState(value);
    onMemoryFilterChange?.(value);
  };
  const devices = data.devices;
  const zero = devices.filter((d) => d.physical_used_mib === 0).length;
  const completeSources = data.sources.every((s) => s.status === "healthy");
  const missing = devices.filter((d) => d.physical_used_mib == null).length;
  const amount = (n: number | null | undefined, unit = "MiB") =>
    memoryText(n, unit, displayUnit);
  const models = Array.from(
    new Set(devices.map((d) => d.gpu_model || "型号未知")),
  ).sort();
  const shown = devices
    .filter(
      (d) =>
        (model === "all" || (d.gpu_model || "型号未知") === model) &&
        `${names[d.cluster]} ${d.node} ${d.device_uuid} ${d.gpu_model ?? ""}`
          .toLowerCase()
          .includes(query.toLowerCase()) &&
        (state === "all" ||
          (state === "zero" && d.physical_used_mib === 0) ||
          (state === "used" &&
            d.physical_used_mib != null &&
            d.physical_used_mib > 0) ||
          (state === "unknown" && d.physical_used_mib == null)),
    )
    .sort((a, b) => {
      if (sort === "util")
        return (b.utilization_pct ?? -1) - (a.utilization_pct ?? -1);
      if (sort === "temperature")
        return (b.temperature_c ?? -1) - (a.temperature_c ?? -1);
      if (sort === "zero")
        return (
          Number(b.physical_used_mib === 0) -
            Number(a.physical_used_mib === 0) ||
          (a.physical_used_mib ?? Infinity) -
            (b.physical_used_mib ?? Infinity) ||
          a.node.localeCompare(b.node)
        );
      return (
        a.cluster.localeCompare(b.cluster) ||
        a.node.localeCompare(b.node) ||
        a.device_uuid.localeCompare(b.device_uuid)
      );
    });
  return (
    <>
      <AssetSummary
        items={[
          {
            label: "GPU 总数",
            value: completeSources ? `${devices.length} 张` : "—",
            note: completeSources ? undefined : `已观测 ${devices.length} 张`,
          },
          {
            label: "零显存占用 GPU",
            value: `${zero} 张`,
            note: missing ? `${missing} 张显存缺测` : undefined,
            active: state === "zero",
            onClick: () =>
              changeMemoryFilter(state === "zero" ? "all" : "zero"),
          },
          {
            label: "物理显存已用",
            value: amount(data.summary.physical_used_mib),
          },
          {
            label: "平均 GPU 利用率",
            value: usagePercent(data.summary.utilization_pct),
          },
        ]}
      />
      <div className="asset-filters">
        <label className="search">
          <span className="sr-only">搜索 GPU</span>
          <input
            aria-label="搜索 GPU"
            placeholder="搜索服务器、GPU UUID"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </label>
        <label>
          GPU 型号
          <select
            aria-label="筛选 GPU 型号"
            value={model}
            onChange={(e) => setModel(e.target.value)}
          >
            <option value="all">全部型号</option>
            {models.map((m) => (
              <option key={m} value={m}>
                {m}（
                {
                  devices.filter((d) => (d.gpu_model || "型号未知") === m)
                    .length
                }
                张）
              </option>
            ))}
          </select>
        </label>
        <label>
          显存状态
          <select
            aria-label="筛选显存状态"
            value={state}
            onChange={(e) =>
              changeMemoryFilter(
                e.target.value as "all" | "zero" | "used" | "unknown",
              )
            }
          >
            <option value="all">全部状态</option>
            <option value="zero">显存占用为 0</option>
            <option value="used">显存占用大于 0</option>
            <option value="unknown">显存缺测</option>
          </select>
        </label>
        <label>
          排序
          <select
            aria-label="GPU 排序"
            value={sort}
            onChange={(e) => setSort(e.target.value)}
          >
            <option value="zero">零显存占用优先</option>
            <option value="util">GPU 利用率优先</option>
            <option value="temperature">温度优先</option>
            <option value="name">服务器名称</option>
          </select>
        </label>
        <span className="count">
          {shown.length} / {devices.length} 张
        </span>
      </div>
      <div className="table-scroll">
        <table className="asset-table">
          <thead>
            <tr>
              <th>GPU / 服务器</th>
              <th>型号</th>
              <th>GPU 利用率</th>
              <th>物理显存已用 / 总量</th>
              <th>GPU 温度</th>
              <th>HAMi 已分配额度</th>
              <th>显存状态</th>
              <th>详情</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((d) => (
              <tr
                key={JSON.stringify([d.cluster, d.node, d.device_uuid])}
                className={d.physical_used_mib === 0 ? "zero-memory-row" : ""}
              >
                <td>
                  <strong>
                    {names[d.cluster]} · {d.node}
                  </strong>
                  <small title={d.device_uuid}>{d.device_uuid}</small>
                </td>
                <td>{d.gpu_model ?? "型号未知"}</td>
                <td>{usagePercent(d.utilization_pct)}</td>
                <td>
                  {amount(d.physical_used_mib)} /{" "}
                  {amount(d.physical_observed_total_mib)}
                </td>
                <td>
                  {d.temperature_c == null ? (
                    "—"
                  ) : (
                    <span
                      className={
                        data.temperature_threshold_c != null &&
                        d.temperature_c >= data.temperature_threshold_c
                          ? "temperature-attention"
                          : ""
                      }
                    >
                      {d.temperature_c.toLocaleString("zh-CN", {
                        maximumFractionDigits: 1,
                      })}{" "}
                      °C
                    </span>
                  )}
                </td>
                <td>{amount(d.allocated_raw, data.units.allocation)}</td>
                <td>
                  <span
                    className={`asset-state ${d.physical_used_mib === 0 ? "ready" : d.physical_used_mib == null ? "unknown" : ""}`}
                    title="按物理显存已用值识别，不代表无预留或可调度"
                  >
                    {d.physical_used_mib === 0
                      ? "零显存占用"
                      : d.physical_used_mib == null
                        ? "缺测"
                        : "有显存占用"}
                  </span>
                </td>
                <td>
                  <button
                    type="button"
                    className="link"
                    onClick={() => onInspect(d)}
                  >
                    查看显卡历史
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!shown.length && <p className="empty">没有匹配的 GPU</p>}
    </>
  );
}
