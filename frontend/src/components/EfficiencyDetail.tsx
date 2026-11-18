import type { EfficiencyDevice } from "../types/efficiency";
import type { Gpu } from "../types/resource";
import { DetailDrawer } from "./DetailDrawer";
import {
  EfficiencyEvidence,
  usagePercent,
  type EvidenceFormatter,
} from "./EfficiencyEvidence";
import { dateTime } from "./Metrics";

export function EfficiencyDetail({
  clusterName,
  node,
  devices,
  device,
  start,
  end,
  amount,
  close,
  labels,
  onSelectGpu,
  onBack,
  onCurrentNode,
  onCurrentGpu,
  currentGpu,
  currentUnit,
  currentSampledAt,
}: {
  clusterName: string;
  node: string;
  devices: EfficiencyDevice[];
  device?: EfficiencyDevice;
  start: number;
  end: number;
  amount: EvidenceFormatter;
  close: () => void;
  labels: Record<string, string>;
  onSelectGpu: (device: EfficiencyDevice) => void;
  onBack: () => void;
  onCurrentNode?: () => void;
  onCurrentGpu?: () => void;
  currentGpu?: Gpu;
  currentUnit?: string;
  currentSampledAt?: number | null;
}) {
  return (
    <DetailDrawer
      title={device ? "GPU 占用核查详情" : "机器占用核查详情"}
      label={device ? "GPU 占用核查详情" : "机器占用核查详情"}
      close={close}
    >
      <div className="detail-banner">
        <div>
          <h3>{device?.device_uuid ?? node}</h3>
          <p>
            {clusterName} · 机器：{node}
          </p>
        </div>
      </div>
      <p className="efficiency-detail-period">
        核查区间：{dateTime(start)} — {dateTime(end)}
      </p>
      {device ? (
        <>
          <button className="link drawer-back" type="button" onClick={onBack}>
            ← 返回机器 GPU 列表
          </button>
          <p className="assessment-risk">{labels[device.device_uuid]}</p>
          <EfficiencyEvidence
            device={device}
            amount={amount}
            currentAllocation={currentGpu?.allocated_raw}
            currentUnit={currentUnit}
            detailed
          />
          <p className="trend-note">
            当前额度采样：{dateTime(currentSampledAt)}；时段统计截至{" "}
            {dateTime(end)}。
          </p>
          {onCurrentGpu ? (
            <button
              type="button"
              className="link efficiency-current-link"
              onClick={onCurrentGpu}
            >
              查看当前资源详情
            </button>
          ) : (
            <p className="notice">当前快照不可用或该 GPU 已不在当前资源中。</p>
          )}
        </>
      ) : (
        <>
          <div className="gpu-list">
            {devices.map((d) => (
              <button
                type="button"
                className="gpu-item"
                key={d.device_uuid}
                onClick={() => onSelectGpu(d)}
              >
                <span>{d.device_uuid}</span>
                <b>{labels[d.device_uuid]}</b>
                <small>
                  时段平均 {usagePercent(d.mean_utilization_pct)} · 峰值{" "}
                  {usagePercent(d.max_utilization_pct)}
                </small>
              </button>
            ))}
          </div>
          {onCurrentNode && (
            <button
              type="button"
              className="link efficiency-current-link"
              onClick={onCurrentNode}
            >
              查看当前机器资源
            </button>
          )}
        </>
      )}
    </DetailDrawer>
  );
}
