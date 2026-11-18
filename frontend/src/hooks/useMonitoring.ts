import { useCallback, useEffect, useRef, useState } from "react";
import { operationsRequest, type Monitoring } from "../api/operations";
import type { ClusterFilter } from "../types/resource";
export type MonitoringState = {
  data: Monitoring | null;
  error: string;
  busy: boolean;
  refresh: () => Promise<void>;
};
export function useMonitoring(
  cluster: ClusterFilter,
  enabled = true,
  pollMs = 0,
  revision = 0,
): MonitoringState {
  const [data, setData] = useState<Monitoring | null>(null),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const active = useRef<AbortController | null>(null);
  const refresh = useCallback(async () => {
    active.current?.abort();
    const c = new AbortController();
    active.current = c;
    setBusy(true);
    setError("");
    let timedOut = false;
    const timeout = window.setTimeout(() => {
      timedOut = true;
      c.abort();
    }, 15000);
    try {
      const value = await operationsRequest<Monitoring>(
        `monitoring?cluster=${cluster}`,
        c.signal,
      );
      if (!c.signal.aborted) setData(value);
    } catch (e) {
      if (!c.signal.aborted || timedOut) {
        setData(null);
        setError(
          timedOut
            ? "指标查询超时"
            : e instanceof Error
              ? e.message
              : "指标查询失败",
        );
      }
    } finally {
      window.clearTimeout(timeout);
      if (active.current === c) setBusy(false);
    }
  }, [cluster]);
  useEffect(() => {
    setData(null);
    setError("");
    setBusy(false);
    if (!enabled) return;
    void refresh();
    const timer = pollMs
      ? window.setInterval(() => void refresh(), pollMs)
      : undefined;
    return () => {
      active.current?.abort();
      window.clearInterval(timer);
    };
  }, [refresh, enabled, pollMs, revision]);
  return { data, error, busy, refresh };
}
