import { useEffect, useRef } from "react";
import { createRequestId } from "../api/requestId";
import type { Route } from "../navigation";

type Event = {
  id: string;
  page: string;
  kind: "page_view" | "feature_use";
  feature?: string;
};
const key = "roc.analytics.visitor.v1";
const features = new Set([
  "assistant_query",
  "diagnosis_run",
  "efficiency_run",
  "planning_assess",
  "report_generate",
  "report_export",
  "history_change",
  "manual_refresh",
]);
let visitor: string | undefined;
let pending: Event[] = [];
let timer: ReturnType<typeof setTimeout> | undefined;
let disabled = false;
let sending = false;

function visitorId() {
  if (visitor) return visitor;
  try {
    const saved = localStorage.getItem(key);
    if (saved && /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(saved))
      visitor = saved;
    else {
      visitor = createRequestId();
      localStorage.setItem(key, visitor);
    }
  } catch {
    visitor = createRequestId();
  }
  return visitor;
}
async function flush() {
  timer = undefined;
  if (sending || !pending.length || disabled) return;
  sending = true;
  const batch = pending.splice(0, 10);
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 4000);
  try {
    const response = await fetch("/api/v1/analytics/events", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ visitor_id: visitorId(), events: batch }),
      signal: controller.signal,
      keepalive: true,
    });
    if (!response.ok) throw Error();
    const result = await response.json();
    if (result.enabled === false) {
      disabled = true;
      pending = [];
    }
    if (pending.length) timer = setTimeout(() => void flush(), 200);
  } catch {
    // Retry the same event IDs on the next interaction; never block or retry business requests.
    pending = [...batch, ...pending].slice(-100);
  } finally {
    clearTimeout(timeout);
    sending = false;
  }
}
function enqueue(event: Omit<Event, "id">) {
  if (disabled) return;
  try {
    visitorId();
    pending = [...pending, { ...event, id: createRequestId() }].slice(-100);
    if (!timer) timer = setTimeout(() => void flush(), 200);
  } catch {
    /* Analytics cannot prevent a product interaction. */
  }
}
function flushOnHide() {
  if (!visitor || disabled || !navigator.sendBeacon) return;
  while (pending.length) {
    const batch = pending.slice(0, 10);
    const sent = navigator.sendBeacon(
      "/api/v1/analytics/events",
      new Blob([JSON.stringify({ visitor_id: visitor, events: batch })], {
        type: "application/json",
      }),
    );
    if (!sent) break;
    pending.splice(0, batch.length);
  }
}
export function useProductAnalytics(route: Route) {
  const page = route.view ? `${route.module}.${route.view}` : route.module;
  const current = useRef(page);
  current.current = page;
  const previous = useRef<string | null>(null);
  useEffect(() => {
    // StrictMode replays effects; canonical route filters and task IDs are deliberately excluded.
    if (previous.current !== page) {
      previous.current = page;
      enqueue({ page, kind: "page_view" });
    }
  }, [page]);
  useEffect(() => {
    const record = (event: globalThis.Event) => {
      if (!(event.target instanceof Element)) return;
      const target =
        event.type === "submit"
          ? event.target.closest("form[data-analytics-feature]")
          : event.type === "change"
            ? event.target.closest("select[data-analytics-feature]")
            : event.target.closest(
                "button[data-analytics-feature],a[data-analytics-feature]",
              );
      const feature = target?.getAttribute("data-analytics-feature");
      if (feature && features.has(feature))
        enqueue({ page: current.current, kind: "feature_use", feature });
    };
    const hidden = () => {
      if (document.visibilityState === "hidden") flushOnHide();
    };
    document.addEventListener("submit", record, true);
    document.addEventListener("click", record, true);
    document.addEventListener("change", record, true);
    document.addEventListener("visibilitychange", hidden);
    window.addEventListener("pagehide", flushOnHide);
    return () => {
      document.removeEventListener("submit", record, true);
      document.removeEventListener("click", record, true);
      document.removeEventListener("change", record, true);
      document.removeEventListener("visibilitychange", hidden);
      window.removeEventListener("pagehide", flushOnHide);
    };
  }, []);
}
