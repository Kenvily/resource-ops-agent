export type DisplayUnit = "MB" | "GB";
export type MemoryKind = "capacity" | "allocation" | "unallocated";
export type MemoryFormatter = (
  value: number | null | undefined,
  kind?: MemoryKind,
) => string;
const bytesPerUnit: Record<string, number> = {
  bytes: 1,
  MB: 1_000_000,
  MiB: 1_048_576,
  GB: 1_000_000_000,
};
export function memoryValue(
  raw: number | null | undefined,
  source: string | undefined,
  target: DisplayUnit,
) {
  const verified = source != null && source in bytesPerUnit;
  const unit = verified ? target : "原始单位待确认";
  if (raw == null) return { value: "—", unit };
  const value = verified
    ? (raw * bytesPerUnit[source!]) / bytesPerUnit[target]
    : raw;
  return {
    value: value.toLocaleString("zh-CN", {
      maximumFractionDigits: verified && target !== "MB" ? 2 : 1,
    }),
    unit,
  };
}

/** Preserve visible positive values below the selected display precision. */
export function memoryText(
  raw: number | null | undefined,
  source: string | undefined,
  target: DisplayUnit,
) {
  const result = memoryValue(raw, source, target);
  if (raw != null && raw > 0 && result.value === "0" && result.unit === target)
    return `<${target === "GB" ? "0.01" : "0.1"} ${result.unit}`;
  return result.value === "—" ? "—" : `${result.value} ${result.unit}`;
}
