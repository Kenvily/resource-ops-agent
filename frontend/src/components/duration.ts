/** Human-readable sampling intervals without rounding away precision. */
export function durationLabel(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "—";
  let remaining = seconds;
  const parts: string[] = [];
  for (const [size, unit] of [
    [86400, "天"],
    [3600, "小时"],
    [60, "分钟"],
  ] as const) {
    const count = Math.floor(remaining / size);
    if (count) parts.push(`${count} ${unit}`);
    remaining %= size;
  }
  if (remaining) parts.push(`${Number(remaining.toFixed(6))} 秒`);
  return parts.join(" ");
}
