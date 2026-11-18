import type { EfficiencyDevice } from "../types/efficiency";
export type EvidenceFormatter = (
  raw: number | null | undefined,
  unit: string,
) => string;

export function usagePercent(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  if (value > 0 && value < 0.01) return "<0.01%";
  return `${value.toLocaleString("zh-CN", { maximumFractionDigits: 2 })}%`;
}

export function screeningStatus(
  device: EfficiencyDevice,
  threshold: number,
): string {
  if (device.screening_status) return device.screening_status;
  if (
    (device.usage_coverage ?? device.coverage) < 0.9 ||
    device.mean_utilization_pct == null ||
    device.current_allocated_raw == null
  )
    return "insufficient";
  if (device.current_allocated_raw <= 0) return "unallocated";
  return device.mean_utilization_pct <= threshold
    ? "low_utilization"
    : "above_threshold";
}
export const screeningLabels: Record<string, string> = {
  low_utilization: "平均利用率较低",
  above_threshold: "高于利用率上限",
  unallocated: "观察末点无分配",
  insufficient: "采样或分配数据不足",
};

export function EfficiencyEvidence({
  device,
  amount,
  currentAllocation,
  currentUnit,
  detailed = false,
}: {
  device: EfficiencyDevice;
  amount: EvidenceFormatter;
  currentAllocation?: number | null;
  currentUnit?: string;
  detailed?: boolean;
}) {
  return (
    <div className="efficiency-evidence">
      <div>
        当前已分配
        <strong>
          {amount(currentAllocation, currentUnit ?? device.allocation_unit)}
        </strong>
      </div>
      <div>
        时段平均利用率
        <strong>{usagePercent(device.mean_utilization_pct)}</strong>
      </div>
      <div>
        时段峰值利用率
        <strong>{usagePercent(device.max_utilization_pct)}</strong>
      </div>
      {detailed && (
        <>
          <div>
            观察末点已分配
            <strong>
              {amount(device.current_allocated_raw, device.allocation_unit)}
            </strong>
          </div>
          <div>
            时段平均物理显存
            <strong>
              {amount(device.mean_physical_memory_bytes, "bytes")}
            </strong>
          </div>
          <div>
            利用率有效采样
            <strong>
              {device.usage_valid_points ?? device.valid_points} /{" "}
              {device.expected_points}
            </strong>
          </div>
        </>
      )}
    </div>
  );
}
