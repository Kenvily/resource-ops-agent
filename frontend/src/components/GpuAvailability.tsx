import { ArrowUpRight } from "lucide-react";
import { DataDetails } from "./DataDetails";
import type { gpuAvailability } from "./workbenchMetrics";
export function GpuAvailability({
  state,
}: {
  state: ReturnType<typeof gpuAvailability>;
}) {
  const { total, idle, occupied, unknown } = state;
  const pending = unknown != null && unknown > 0;
  return (
    <section className="card gpu-availability" aria-label="GPU 空闲概况">
      <div className="availability-heading">
        <h2>GPU 空闲</h2>
        {pending && <span className="availability-partial">部分覆盖</span>}
      </div>
      <div className="availability-ratio">
        <strong>
          {idle ?? "—"}
          <span> / {total ?? "—"}</span>
        </strong>
        <span className="availability-unit">张</span>
      </div>
      <div
        className="availability-bar"
        role="img"
        aria-label={`GPU 空闲 ${idle ?? "未知"} 张，有显存占用 ${occupied ?? "未知"} 张，显存缺测 ${unknown ?? "未知"} 张`}
      >
        {total != null && total > 0 ? (
          <>
            {(idle ?? 0) > 0 && (
              <span
                className="idle"
                style={{ width: `${(idle! / total) * 100}%` }}
              />
            )}
            {(occupied ?? 0) > 0 && (
              <span
                className="occupied"
                style={{ width: `${(occupied! / total) * 100}%` }}
              />
            )}
            {(unknown ?? 0) > 0 && (
              <span
                className="unknown"
                style={{ width: `${(unknown! / total) * 100}%` }}
              />
            )}
          </>
        ) : (
          <span className="unknown" style={{ width: "100%" }} />
        )}
      </div>
      <div className="availability-breakdown">
        <a href="#assets?view=gpus&memory=used">
          <i className="occupied" />
          有显存占用<strong>{occupied ?? "—"}</strong>
        </a>
        <a href="#assets?view=gpus&memory=unknown">
          <i className="unknown" />
          显存缺测<strong>{unknown ?? "—"}</strong>
        </a>
      </div>
      <div className="availability-actions">
        <a className="link" href="#assets?view=gpus&memory=zero">
          查看空闲 GPU
          <ArrowUpRight aria-hidden="true" size={16} />
        </a>
        <DataDetails label="统计口径">
          <p>
            按当前设备清单中已核验的物理显存已用值精确为0计数，与HAMi分配额度不同，不代表可调度。缺测不补零；部分覆盖时仅展示已确认空闲数量。
          </p>
        </DataDetails>
      </div>
    </section>
  );
}
