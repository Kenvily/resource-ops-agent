export async function readStatistics<T>(
  path: string,
  body: unknown,
  signal: AbortSignal,
): Promise<T> {
  const r = await fetch("/api/v1/resource-statistics" + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!r.ok) {
    const data = await r.json().catch(() => ({}));
    throw Error(
      typeof data.detail === "string" ? data.detail : "资源统计暂不可用",
    );
  }
  return r.json();
}
