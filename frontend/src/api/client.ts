import type { ClusterFilter, Overview } from "../types/resource";
export async function getOverview(
  cluster: ClusterFilter,
  signal: AbortSignal,
  refresh = false,
): Promise<Overview> {
  const response = await fetch(
    `/api/v1/overview?cluster=${cluster}${refresh ? "&refresh=true" : ""}`,
    {
      signal,
      cache: "no-store",
    },
  );
  if (!response.ok)
    throw new Error(
      response.status === 429
        ? "请求过于频繁，请稍后重试"
        : "资源接口暂时不可用，请稍后重试",
    );
  return response.json();
}
