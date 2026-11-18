import { useCallback, useEffect, useRef, useState } from "react";
import { getOverview } from "../api/client";
import { mergeOverview, markStale } from "../api/merge";
import type { ClusterFilter, Overview } from "../types/resource";

export function useResources(cluster: ClusterFilter, enabled = true) {
  const [data, setData] = useState<Overview | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState<ClusterFilter[]>([]);
  const [error, setError] = useState("");
  const generation = useRef(0);
  const active = useRef(new Map<ClusterFilter, AbortController>());
  const refresh = useCallback(
    async (target: ClusterFilter = cluster, bypass = true) => {
      if (
        active.current.has(target) ||
        active.current.has("all") ||
        (target === "all" && active.current.size)
      )
        return;
      const version = generation.current;
      const controller = new AbortController();
      active.current.set(target, controller);
      setRefreshing((values) => [...values, target]);
      setError("");
      let timedOut = false;
      const timeout = window.setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, 35000);
      try {
        const result = await getOverview(target, controller.signal, bypass);
        if (version === generation.current)
          setData((previous) => mergeOverview(previous, result));
      } catch (reason) {
        if (
          version === generation.current &&
          (!controller.signal.aborted || timedOut)
        ) {
          setError(
            `${timedOut ? "查询超时" : reason instanceof Error ? reason.message : "刷新失败"}。已保留上次成功数据并标记为过期，请重试。`,
          );
          setData((previous) =>
            previous ? markStale(previous, target) : null,
          );
        }
      } finally {
        window.clearTimeout(timeout);
        if (version === generation.current) {
          active.current.delete(target);
          setRefreshing((values) => values.filter((v) => v !== target));
          setLoading(false);
        }
      }
    },
    [cluster],
  );
  useEffect(() => {
    generation.current++;
    setData(null);
    setError("");
    setLoading(enabled);
    setRefreshing([]);
    if (!enabled) return;
    void refresh(cluster, true);
    const timer = window.setInterval(() => {
      void refresh(cluster, false);
    }, 30000);
    return () => {
      generation.current++;
      for (const c of active.current.values()) c.abort();
      active.current.clear();
      window.clearInterval(timer);
    };
  }, [refresh, enabled]);
  return { data, loading, refreshing, error, refresh };
}
