import { DetailDrawer } from "./DetailDrawer";
import { ChevronDown, ChevronRight, Cpu } from "lucide-react";
import { Bar, percent, time } from "./Metrics";
import type { Gpu, Node, Overview, Workload } from "../types/resource";
import { TrendPanel } from "./TrendPanel";
import type { MemoryFormatter } from "./memory";
const names = { yz: "亦庄", zw: "兆维" };
interface Props {
  memory: MemoryFormatter;
  node: Node | null;
  gpu: Gpu | null;
  help: boolean;
  data: Overview | null;
  close: () => void;
  setGpu: (gpu: Gpu | null) => void;
}
export function ResourceDetail({
  node,
  gpu,
  help,
  data,
  memory,
  close,
  setGpu,
}: Props) {
  const workloads = data?.clusters.flatMap((c) => c.workloads) ?? [];
  const selectedRows = gpu
    ? workloads.filter(
        (w) =>
          w.cluster === gpu.cluster &&
          w.node === gpu.node &&
          w.device_uuid === gpu.device_uuid,
      )
    : [];
  const pods = new Map<string, Workload[]>();
  for (const w of selectedRows) {
    const key = `${w.cluster}/${w.namespace}/${w.pod}`;
    pods.set(key, [...(pods.get(key) ?? []), w]);
  }
  return (
    <DetailDrawer
      title={help ? "指标说明" : gpu ? "GPU 详情" : "节点 GPU 明细"}
      label={help ? "指标说明" : gpu ? "GPU 详情" : "节点详情"}
      close={close}
    >
      {data?.clusters
        .filter(
          (c) =>
            c.id === (gpu?.cluster ?? node?.cluster) && c.status !== "healthy",
        )
        .map((c) => (
          <p className="notice" key={c.id}>
            {c.status === "stale"
              ? "刷新失败：以下为上次成功数据，已过期。"
              : c.warnings.join("；") || "当前集群数据不完整。"}
          </p>
        ))}
      {gpu &&
        !data?.clusters
          .flatMap((c) => c.gpus)
          .some(
            (g) =>
              g.cluster === gpu.cluster &&
              g.node === gpu.node &&
              g.device_uuid === gpu.device_uuid,
          ) && (
          <p className="notice">
            该设备未出现在最新结果中，以下为上次查看记录；请关闭后重新核查。
          </p>
        )}
      {gpu && node && (
        <button className="link drawer-back" onClick={() => setGpu(null)}>
          ← 返回节点 GPU 列表
        </button>
      )}
      {help ? (
        <div className="definitions">
          <h3>理解这组数据</h3>
          <p>
            监控覆盖
            GPU：本次容量指标返回的唯一设备数，不等于在线或可调度设备数。
          </p>
          <p>
            已分配额度：HAMi 给业务容器分配的虚拟显存额度，不是实际显存占用。
          </p>
          <p>未分配额度：总容量减去分配额度。负数会保留并提示核查。</p>
          <p>
            显示单位：可切换 MB、GB，1000 MB = 1
            GB。仅对已确认单位的指标换算，后端保留原始值。
          </p>
          <p>
            缺失与零值不同：未确认的分配记录显示
            —。失败或过期时明确标记旧数据，不将其计入当前完整合计。
          </p>
          <p>
            历史趋势来自受限查询；资源解读使用确定性规则，多步骤调查在分析中心单独启动。
          </p>
        </div>
      ) : (
        <>
          <div className="detail-banner">
            <Cpu aria-hidden="true" size={26} />
            <div>
              <h3>{gpu ? gpu.device_uuid : node?.node}</h3>
              <p>
                {gpu
                  ? `${names[gpu.cluster]} · ${gpu.node} · 查询样本于 ${time(gpu.sampled_at)}`
                  : node
                    ? names[node.cluster]
                    : ""}
              </p>
            </div>
          </div>
          {node && !gpu ? (
            <div className="gpu-list">
              {data?.clusters
                .flatMap((c) => c.gpus)
                .filter(
                  (g) => g.cluster === node.cluster && g.node === node.node,
                )
                .map((g) => (
                  <button
                    className="gpu-item"
                    key={g.device_uuid}
                    onClick={() => setGpu(g)}
                  >
                    <span>
                      <Cpu aria-hidden="true" size={17} />
                      GPU · {g.device_uuid.slice(-8)}
                    </span>
                    <b>
                      {percent(g.allocation_ratio)}{" "}
                      <ChevronRight aria-hidden="true" size={16} />
                    </b>
                    <Bar ratio={g.allocation_ratio} />
                    <small>
                      已分配 {memory(g.allocated_raw)} / 容量{" "}
                      {memory(g.capacity_raw, "capacity")}
                    </small>
                  </button>
                ))}
            </div>
          ) : (
            <>
              {gpu && (
                <>
                  <div className="detail-kpis">
                    <div>
                      总容量<b>{memory(gpu.capacity_raw, "capacity")}</b>
                    </div>
                    <div>
                      已分配<b>{memory(gpu.allocated_raw)}</b>
                    </div>
                    <div>
                      未分配
                      <b>{memory(gpu.unallocated_raw, "unallocated")}</b>
                    </div>
                  </div>
                  {gpu.warnings.map((w) => (
                    <p className="notice" key={w}>
                      {w}
                    </p>
                  ))}
                </>
              )}
              {gpu && (
                <TrendPanel cluster={gpu.cluster} gpu={gpu} memory={memory} />
              )}
              <h3 className="pod-heading">Pod / Container 分配明细</h3>
              {[...pods].map(([key, rows]) => (
                <details className="pod" key={key}>
                  <summary>
                    <ChevronDown aria-hidden="true" size={14} />
                    <span title={rows[0].pod}>
                      {rows[0].pod}
                      <small>
                        {rows[0].namespace} · {rows.length} 条容器/GPU 记录
                      </small>
                    </span>
                    <b>
                      {memory(
                        rows.every((r) => r.allocated_raw !== null)
                          ? rows.reduce((s, r) => s + (r.allocated_raw ?? 0), 0)
                          : null,
                      )}
                    </b>
                  </summary>
                  {rows.map((w, i) => (
                    <div className="container-row" key={i}>
                      <span>
                        {w.container} · {w.node}
                        <small title={w.device_uuid}>{w.device_uuid}</small>
                      </span>
                      <b>{memory(w.allocated_raw)}</b>
                      <button
                        className="link"
                        onClick={() => {
                          const match = data?.clusters
                            .flatMap((c) => c.gpus)
                            .find(
                              (g) =>
                                g.cluster === w.cluster &&
                                g.node === w.node &&
                                g.device_uuid === w.device_uuid,
                            );
                          if (match) setGpu(match);
                        }}
                      >
                        查看 GPU
                      </button>
                    </div>
                  ))}
                </details>
              ))}
              {!selectedRows.length && (
                <div className="empty">
                  当前没有关联的业务分配记录，请结合数据提示核查。
                </div>
              )}
            </>
          )}
        </>
      )}
    </DetailDrawer>
  );
}
