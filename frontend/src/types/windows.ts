import type { WindowSize } from "./trend";
export const timeRanges: { value: WindowSize; label: string }[] = [
  { value: "1h", label: "近 1 小时" },
  { value: "1d", label: "近 1 天" },
  { value: "1w", label: "近 1 周" },
  { value: "1mo", label: "近 1 个月" },
  { value: "3mo", label: "近 3 个月" },
  { value: "all", label: "全部" },
];

export const rangeLabel = (value: WindowSize) =>
  timeRanges.find((r) => r.value === value)?.label ?? value;
