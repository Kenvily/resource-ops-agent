import { useCallback, useEffect, useRef, useState } from "react";
import type { ClusterFilter } from "../types/resource";
import type { ResourceStatistics } from "../types/statistics";
export function useWorkbenchStatistics(
  cluster: ClusterFilter,
  revision: number,
) {
  const [data, setData] = useState<ResourceStatistics | null>(null),
    [error, setError] = useState("");
  const active = useRef<AbortController | null>(null);
  const load = useCallback(async () => {
    active.current?.abort();
    const c = new AbortController();
    active.current = c;
    let timedOut = false;
    const timeout = window.setTimeout(() => {
      timedOut = true;
      c.abort();
    }, 15000);
    try {
      const r = await fetch("/api/v1/resource-statistics", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cluster }),
        signal: c.signal,
      });
      if (!r.ok) throw Error("实际使用统计暂不可用");
      const value = await r.json();
      if (!c.signal.aborted) {
        setData(value);
        setError("");
      }
    } catch {
      if (!c.signal.aborted || timedOut) {
        setData(null);
        setError("实际使用统计暂不可用");
      }
    } finally {
      window.clearTimeout(timeout);
    }
  }, [cluster]);
  useEffect(() => {
    setData(null);
    setError("");
    void load();
    const timer = window.setInterval(() => void load(), 30000);
    return () => {
      active.current?.abort();
      window.clearInterval(timer);
    };
  }, [load, revision]);
  return { data, error };
}
