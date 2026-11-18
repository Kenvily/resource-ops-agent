import { useEffect, useRef, useState } from "react";
import type { ClusterFilter } from "../types/resource";
import type {
  DeviceStatistics,
  ResourceStatistics,
  ServerStatistics,
} from "../types/statistics";
import { readStatistics } from "../api/statistics";
import { memoryText, type DisplayUnit } from "./memory";
import { dateTime } from "./Metrics";
import { DetailDrawer } from "./DetailDrawer";
import { DataDetails } from "./DataDetails";
import { BusinessOwnership } from "./BusinessOwnership";
import { DeviceHistory, DeviceTable } from "./GpuDetails";
import { HostResourceDetail } from "./HostResourceDetail";
import { type ServerFilters, ServerAssets } from "./ServerAssets";
import { GpuAssets } from "./GpuAssets";
const names: Record<string, string> = { yz: "亦庄", zw: "兆维" };
const deviceKey = (
  d: Pick<DeviceStatistics, "cluster" | "node" | "device_uuid">,
) => JSON.stringify([d.cluster, d.node, d.device_uuid]);
export function ResourceStatisticsPanel({
  cluster,
  displayUnit,
  view = "servers",
  containers = false,
  gpuMemory,
  onGpuMemoryChange,
  serverFilters,
  onServerFiltersChange,
}: {
  cluster: ClusterFilter;
  displayUnit: DisplayUnit;
  view?: "servers" | "gpus" | "business_models";
  containers?: boolean;
  gpuMemory?: "zero" | "used" | "unknown";
  onGpuMemoryChange?: (value: "all" | "zero" | "used" | "unknown") => void;
  serverFilters?: ServerFilters;
  onServerFiltersChange?: (filters: ServerFilters) => void;
}) {
  const [data, setData] = useState<ResourceStatistics | null>(null),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const [host, setHost] = useState<ServerStatistics | null>(null),
    [selected, setSelected] = useState<DeviceStatistics | null>(null);
  const [related, setRelated] = useState<{
      title: string;
      keys: string[];
    } | null>(null),
    [relatedSelected, setRelatedSelected] = useState<DeviceStatistics | null>(
      null,
    );
  const active = useRef<AbortController | null>(null),
    body = useRef<HTMLDivElement>(null);
  useEffect(() => {
    body.current?.querySelector<HTMLButtonElement>("button")?.focus();
  }, [related, relatedSelected]);
  async function load(quiet = false) {
    active.current?.abort();
    const c = new AbortController();
    active.current = c;
    if (!quiet) setBusy(true);
    setError("");
    try {
      const result = await readStatistics<ResourceStatistics>(
        "",
        { cluster },
        c.signal,
      );
      if (!c.signal.aborted) {
        setData(result);
        setSelected((prev) =>
          prev
            ? (result.devices.find((d) => deviceKey(d) === deviceKey(prev)) ??
              null)
            : null,
        );
      }
    } catch (e) {
      if (!c.signal.aborted) {
        setData(null);
        setHost(null);
        setSelected(null);
        setRelated(null);
        setError(e instanceof Error ? e.message : "资源读取失败");
      }
    } finally {
      if (active.current === c) setBusy(false);
    }
  }
  useEffect(() => {
    setData(null);
    void load();
    const timer = setInterval(() => void load(true), 30000);
    return () => {
      clearInterval(timer);
      active.current?.abort();
    };
  }, [cluster]);
  useEffect(() => {
    setHost(null);
    setSelected(null);
    setRelated(null);
    setRelatedSelected(null);
  }, [cluster, view, gpuMemory]);
  const amount = (value: number | null | undefined, unit: string) =>
    memoryText(value, unit, displayUnit);
  const allocation = (v: number | null | undefined) =>
    amount(v, data?.units.allocation ?? "unverified");
  const currentDevice =
    selected && data?.devices.find((d) => deviceKey(d) === deviceKey(selected));
  return (
    <>
      <section
        className="card operations-panel asset-panel"
        aria-label="资源对象统计"
      >
        <div className="panel-heading">
          <h2>
            {view === "servers"
              ? "服务器资源"
              : view === "gpus"
                ? "GPU 资源"
                : "模型与负载归属"}
          </h2>
          <button type="button" disabled={busy} onClick={() => void load()}>
            {busy ? "读取中…" : "刷新统计"}
          </button>
        </div>
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        {busy && !data && <p role="status">正在读取资源…</p>}
        {data && (
          <>
            <div className="asset-data-status">
              {data.mode === "mock" && (
                <span className="demo-label">演示数据</span>
              )}
              <span>数据库查询：{dateTime(data.database_read_at)}</span>
            </div>
            {data.sources
              .filter((s) => s.status !== "healthy")
              .map((s) => (
                <p className="notice" key={s.cluster}>
                  {names[s.cluster]} ·{" "}
                  {{
                    degraded: "部分缺测",
                    stale: "数据已过期",
                    empty: "暂无数据",
                    unavailable: "不可用",
                  }[s.status] ?? "数据待核验"}
                </p>
              ))}
            {view === "servers" ? (
              <ServerAssets
                data={data}
                cluster={cluster}
                displayUnit={displayUnit}
                onInspect={setHost}
                containers={containers}
                filters={serverFilters}
                onFiltersChange={onServerFiltersChange}
              />
            ) : view === "gpus" ? (
              <GpuAssets
                data={data}
                displayUnit={displayUnit}
                onInspect={setSelected}
                memoryFilter={gpuMemory}
                onMemoryFilterChange={onGpuMemoryChange}
              />
            ) : (
              <BusinessOwnership
                data={data}
                allocation={allocation}
                onDevices={(refs, title) => {
                  setRelated({ title, keys: refs.map(deviceKey) });
                  setRelatedSelected(null);
                }}
              />
            )}
            <DataDetails>
              <p>
                范围：已接入的 GPU 节点及设备。缺测不补零，服务器与 GPU
                使用各自来源及采样时间。
              </p>
              <ul>
                {data.sources.map((s) => (
                  <li key={s.cluster}>
                    {names[s.cluster]} · 额度快照：
                    {dateTime(s.quota_sampled_at)}
                  </li>
                ))}
              </ul>
              {data.limitations.map((l, i) => (
                <p key={i}>{l}</p>
              ))}
            </DataDetails>
          </>
        )}
      </section>
      {host && (
        <HostResourceDetail
          cluster={host.cluster}
          node={host.node}
          displayUnit={displayUnit}
          close={() => setHost(null)}
        />
      )}
      {currentDevice && (
        <DeviceHistory
          device={currentDevice}
          displayUnit={displayUnit}
          allocation={allocation}
          close={() => setSelected(null)}
        />
      )}
      {related && (
        <DetailDrawer
          title={`${related.title} · 关联 GPU`}
          label="关联 GPU"
          close={() => {
            setRelated(null);
            setRelatedSelected(null);
          }}
        >
          <div ref={body}>
            {relatedSelected ? (
              <>
                <button
                  type="button"
                  className="link"
                  onClick={() => setRelatedSelected(null)}
                >
                  返回关联 GPU
                </button>
                <DeviceHistory
                  device={relatedSelected}
                  displayUnit={displayUnit}
                  allocation={allocation}
                  close={() => setRelatedSelected(null)}
                  embedded
                />
              </>
            ) : (
              <>
                <p>关联 GPU 的整卡数据，共享卡不按业务分摊。</p>
                <DeviceTable
                  devices={
                    data?.devices.filter((d) =>
                      related.keys.includes(deviceKey(d)),
                    ) ?? []
                  }
                  allocation={allocation}
                  physical={(v) => amount(v, "MiB")}
                  onSelect={setRelatedSelected}
                />
                {!data?.devices.some((d) =>
                  related.keys.includes(deviceKey(d)),
                ) && (
                  <p className="empty">
                    当前范围暂无可用的关联 GPU，请核查采集状态。
                  </p>
                )}
              </>
            )}
          </div>
        </DetailDrawer>
      )}
    </>
  );
}
