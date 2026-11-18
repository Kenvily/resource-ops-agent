import { useEffect, useState } from "react";
import type { WindowSize } from "./types/trend";
import { timeRanges } from "./types/windows";

export const modules = {
  workbench: "工作台",
  assets: "资源资产",
  workloads: "业务模型",
  analysis: "分析中心",
  planning: "资源规划",
  operations: "运营中心",
} as const;
export const views = {
  assets: [
    ["servers", "服务器"],
    ["gpus", "GPU"],
  ],
  workloads: [["business_models", "业务模型"]],
  analysis: [
    ["history", "历史分析"],
    ["assistant", "资源助手"],
    ["diagnosis", "资源诊断"],
    ["efficiency", "占用核查"],
    ["records", "调查记录"],
  ],
  operations: [
    ["reports", "运营报告"],
    ["monitoring", "指标告警"],
    ["issues", "问题核查"],
  ],
  settings: [
    ["status", "数据与同步"],
    ["infrastructure", "运行评估"],
    ["usage", "使用统计"],
  ],
} as const;
export type Module = keyof typeof modules | "settings";
export type Route = {
  module: Module;
  view: string;
  range?: WindowSize;
  taskId?: string;
  containers?: boolean;
  gpuMemory?: "zero" | "used" | "unknown";
};
const aliases: Record<string, [Module, string]> = {
  overview: ["workbench", ""],
  resources: ["assets", "servers"],
  statistics: ["assets", "servers"],
  history: ["analysis", "history"],
  diagnosis: ["analysis", "diagnosis"],
  efficiency: ["analysis", "efficiency"],
  assessment: ["planning", ""],
  reports: ["operations", "reports"],
  monitoring: ["operations", "monitoring"],
};
export function parseRoute(hash: string): Route {
  const [path, query = ""] = hash.replace(/^#/, "").split("?", 2);
  const params = new URLSearchParams(query);
  const alias = Object.hasOwn(aliases, path) ? aliases[path] : undefined;
  const legacyCompute =
    path === "workloads" && params.get("view") === "compute";
  const module: Module = legacyCompute
    ? "assets"
    : (alias?.[0] ??
      (Object.hasOwn(modules, path) || path === "settings"
        ? (path as Module)
        : "workbench"));
  const choices = module in views ? views[module as keyof typeof views] : [];
  let requested = legacyCompute
    ? "servers"
    : (alias?.[1] ?? params.get("view"));
  if (module === "assets")
    requested =
      (
        {
          nodes: "servers",
          machines: "servers",
          devices: "gpus",
          gpu_models: "gpus",
        } as Record<string, string>
      )[requested ?? ""] ?? requested;
  const view =
    choices.find(([key]) => key === requested)?.[0] ?? choices[0]?.[0] ?? "";
  const range = params.get("range");
  const task = params.get("task");
  return {
    ...(module === "analysis" &&
    (view === "records" || view === "assistant") &&
    task &&
    /^[0-9a-f-]{36}$/i.test(task)
      ? { taskId: task }
      : {}),
    module,
    view,
    ...(module === "assets" &&
    view === "gpus" &&
    ["zero", "used", "unknown"].includes(params.get("memory") ?? "")
      ? { gpuMemory: params.get("memory") as "zero" | "used" | "unknown" }
      : {}),
    ...(module === "assets" &&
    view === "servers" &&
    (legacyCompute || params.get("detail") === "containers")
      ? { containers: true }
      : {}),
    ...(timeRanges.some((r) => r.value === range)
      ? { range: range as WindowSize }
      : {}),
  };
}
export function routeHref(
  module: Module,
  view = "",
  range?: WindowSize,
  taskId?: string,
  containers?: boolean,
  gpuMemory?: "zero" | "used" | "unknown",
) {
  const params = new URLSearchParams();
  if (view) params.set("view", view);
  if (range) params.set("range", range);
  if (taskId) params.set("task", taskId);
  if (containers && module === "assets" && view === "servers")
    params.set("detail", "containers");
  if (gpuMemory && module === "assets" && view === "gpus")
    params.set("memory", gpuMemory);
  return `#${module}${params.size ? `?${params}` : ""}`;
}
export function useNavigation() {
  const [route, setRoute] = useState(() => parseRoute(window.location.hash));
  useEffect(() => {
    const read = () => {
      const next = parseRoute(window.location.hash);
      const canonical = routeHref(
        next.module,
        next.view,
        next.range,
        next.taskId,
        next.containers,
        next.gpuMemory,
      );
      if (window.location.hash !== canonical)
        window.history.replaceState(null, "", canonical);
      setRoute(next);
    };
    read();
    window.addEventListener("hashchange", read);
    return () => window.removeEventListener("hashchange", read);
  }, []);
  useEffect(() => {
    document.getElementById("page-title")?.focus();
    window.scrollTo(0, 0);
  }, [route.module, route.view]);
  return route;
}
