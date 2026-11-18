import { UsageAnalytics } from "../components/UsageAnalytics";
import { useProductAnalytics } from "../hooks/useProductAnalytics";
import { AgentWorkspace } from "../components/AgentWorkspace";
import { OperationsIssues } from "../components/OperationsIssues";
import { InvestigationRecords } from "../components/InvestigationRecords";
import { useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { AppShell } from "../components/AppShell";
import { Workbench } from "./Workbench";
import { DataStatus } from "./DataStatus";
import { ResourceStatisticsPanel } from "../components/ResourceStatisticsPanel";
import { OperationsReport } from "../components/OperationsReport";
import { MonitoringPanel } from "../components/MonitoringPanel";
import { InfrastructurePanel } from "../components/InfrastructurePanel";
import { EfficiencyPanel } from "../components/EfficiencyPanel";
import { AssessmentPanel } from "../components/AssessmentPanel";
import { DiagnosisPanel } from "../components/DiagnosisPanel";
import { TrendPanel } from "../components/TrendPanel";
import { StatisticsPanel } from "../components/StatisticsPanel";
import { ResourceDetail } from "../components/ResourceDetail";
import { dateTime, time } from "../components/Metrics";
import {
  memoryValue,
  type DisplayUnit,
  type MemoryKind,
} from "../components/memory";
import { useResources } from "../hooks/useResources";
import { routeHref, useNavigation } from "../navigation";
import type { ClusterFilter, Gpu, Node } from "../types/resource";
import type { WindowSize } from "../types/trend";

/** Page coordinator: existing services and domain components retain their contracts. */
export function OverviewPage() {
  const route = useNavigation();
  useProductAnalytics(route);
  const [cluster, setCluster] = useState<ClusterFilter>("all");
  const [historyWindow, setHistoryWindow] = useState<WindowSize>(
    route.range ?? "1d",
  );
  useEffect(() => {
    if (route.range) setHistoryWindow(route.range);
  }, [route.range]);
  const [historyActual, setHistoryActual] = useState(false);
  const [displayUnit, setDisplayUnit] = useState<DisplayUnit>(() => {
    try {
      return localStorage.getItem("gpu-memory-unit") === "MB" ? "MB" : "GB";
    } catch {
      return "GB";
    }
  });
  const quotaPage = route.module === "workbench";
  const needsOverview =
    quotaPage || (route.module === "analysis" && route.view === "efficiency");
  const { data, loading, refreshing, error, refresh } = useResources(
    cluster,
    needsOverview,
  );
  const [serverFilters, setServerFilters] = useState({
    query: "",
    status: "all",
    sort: "name",
  });
  useEffect(
    () => setServerFilters({ query: "", status: "all", sort: "name" }),
    [cluster],
  );
  const [node, setNode] = useState<Node | null>(null);
  const [gpu, setGpu] = useState<Gpu | null>(null);
  const [help, setHelp] = useState(false);
  function close() {
    setNode(null);
    setGpu(null);
    setHelp(false);
  }
  useEffect(close, [cluster, route.module, route.view]);
  const memory = (
    raw: number | null | undefined,
    kind: MemoryKind = "allocation",
  ) => {
    const source =
      kind === "unallocated"
        ? data?.units.capacity === data?.units.allocation
          ? data?.units.capacity
          : "unverified"
        : data?.units[kind];
    const result = memoryValue(raw, source, displayUnit);
    return result.value === "—" ? "—" : `${result.value} ${result.unit}`;
  };
  const [signalRevision, setSignalRevision] = useState(0);
  async function refreshWorkbench(target?: ClusterFilter) {
    setSignalRevision((v) => v + 1);
    await refresh(target);
  }
  const busy = refreshing.length > 0;
  const actions = (
    <>
      <label>
        <span className="sr-only">容量显示单位</span>
        <select
          aria-label="容量显示单位"
          value={displayUnit}
          onChange={(e) => {
            const unit = e.target.value as DisplayUnit;
            setDisplayUnit(unit);
            try {
              localStorage.setItem("gpu-memory-unit", unit);
            } catch {
              /* Unit changes remain usable when storage is blocked. */
            }
          }}
        >
          <option value="MB">MB</option>
          <option value="GB">GB</option>
        </select>
      </label>
      <label className="select-wrap">
        <span className="sr-only">选择集群</span>
        <select
          value={cluster}
          onChange={(e) => setCluster(e.target.value as ClusterFilter)}
        >
          <option value="all">全部集群</option>
          <option value="yz">亦庄 YZ</option>
          <option value="zw">兆维 ZW</option>
        </select>
      </label>
      {quotaPage && (
        <button
          className="primary"
          disabled={busy}
          data-analytics-feature="manual_refresh"
          onClick={() => void refreshWorkbench()}
        >
          <RefreshCw
            aria-hidden="true"
            size={15}
            className={busy ? "spin" : ""}
          />
          刷新数据
        </button>
      )}
    </>
  );
  const details = (node || gpu || help) && (
    <ResourceDetail
      node={
        node
          ? (data?.nodes.find(
              (n) => n.cluster === node.cluster && n.node === node.node,
            ) ?? node)
          : null
      }
      gpu={
        gpu
          ? (data?.clusters
              .flatMap((c) => c.gpus)
              .find(
                (g) =>
                  g.cluster === gpu.cluster &&
                  g.node === gpu.node &&
                  g.device_uuid === gpu.device_uuid,
              ) ?? gpu)
          : null
      }
      help={help}
      data={data}
      memory={memory}
      close={close}
      setGpu={setGpu}
    />
  );
  return (
    <AppShell
      route={{ ...route, range: historyWindow }}
      actions={actions}
      details={details}
      onHelp={() => setHelp(true)}
    >
      {quotaPage && (
        <>
          <div className="data-strip">
            {data?.mode === "mock" && (
              <span className="demo-label">演示数据</span>
            )}
            <span>
              {busy
                ? "正在后台更新…"
                : data?.database_read_at
                  ? `数据库查询 ${dateTime(data.database_read_at)}`
                  : `获取于 ${time(data?.fetched_at)}`}
            </span>
          </div>
          {error && (
            <div className="error" role="alert">
              {error}
              <button onClick={() => void refreshWorkbench()}>重新连接</button>
            </div>
          )}
          {data && !data.complete && (
            <div className="notice" role="status">
              部分数据缺测、过期或不可用，仅展示已观测值与覆盖范围。
            </div>
          )}
        </>
      )}
      {route.module === "workbench" && (
        <>
          <Workbench
            data={data}
            loading={loading}
            refreshing={refreshing}
            memory={memory}
            displayUnit={displayUnit}
            refresh={refreshWorkbench}
            onAssets={() => {
              window.location.hash = routeHref("assets", "servers");
            }}
            onCluster={setCluster}
            cluster={cluster}
            signalRevision={signalRevision}
          />
          <div className="workbench-links">
            <a href="#analysis?view=history">查看历史趋势</a>
            <a href="#analysis?view=diagnosis">调查资源变化</a>
            <a href="#planning">评估资源需求</a>
          </div>
        </>
      )}
      {route.module === "assets" && (
        <ResourceStatisticsPanel
          cluster={cluster}
          displayUnit={displayUnit}
          view={route.view as "servers" | "gpus"}
          containers={route.containers}
          gpuMemory={route.gpuMemory}
          onGpuMemoryChange={(value) => {
            window.location.hash = routeHref(
              "assets",
              "gpus",
              undefined,
              undefined,
              undefined,
              value === "all" ? undefined : value,
            );
          }}
          serverFilters={serverFilters}
          onServerFiltersChange={setServerFilters}
        />
      )}
      {route.module === "workloads" && (
        <ResourceStatisticsPanel
          cluster={cluster}
          displayUnit={displayUnit}
          view="business_models"
        />
      )}
      {route.module === "analysis" && (
        <>
          {route.view === "assistant" && (
            <AgentWorkspace
              cluster={cluster}
              initialWindow={route.range ?? "auto"}
              taskId={route.taskId}
              displayUnit={displayUnit}
            />
          )}
          {route.view === "history" && (
            <>
              <TrendPanel
                cluster={cluster}
                memory={memory}
                displayUnit={displayUnit}
                initialWindow={historyWindow}
                onMetricSourceChange={setHistoryActual}
                onWindowChange={(value) => {
                  setHistoryWindow(value);
                  window.history.replaceState(
                    null,
                    "",
                    routeHref("analysis", "history", value),
                  );
                }}
              />
              {!historyActual && (
                <div className="context-links">
                  <a href={routeHref("analysis", "diagnosis", historyWindow)}>
                    分析这段变化
                  </a>
                </div>
              )}
              {!historyActual && (
                <StatisticsPanel
                  cluster={cluster}
                  windowSize={historyWindow}
                  displayUnit={displayUnit}
                />
              )}
            </>
          )}
          {route.view === "records" && (
            <InvestigationRecords cluster={cluster} taskId={route.taskId} />
          )}
          {route.view === "diagnosis" && (
            <DiagnosisPanel
              cluster={cluster}
              windowSize={historyWindow}
              displayUnit={displayUnit}
            />
          )}
          {route.view === "efficiency" && (
            <EfficiencyPanel
              cluster={cluster}
              displayUnit={displayUnit}
              resources={data}
              onInspectNode={(value) => {
                setNode(value);
                setGpu(null);
              }}
              onInspectGpu={(value) => {
                setNode(
                  data?.nodes.find(
                    (n) => n.cluster === value.cluster && n.node === value.node,
                  ) ?? null,
                );
                setGpu(value);
              }}
            />
          )}
        </>
      )}
      {route.module === "planning" && (
        <AssessmentPanel cluster={cluster} displayUnit={displayUnit} />
      )}
      {route.module === "operations" &&
        (route.view === "reports" ? (
          <OperationsReport cluster={cluster} displayUnit={displayUnit} />
        ) : route.view === "issues" ? (
          <OperationsIssues cluster={cluster} />
        ) : (
          <>
            <MonitoringPanel cluster={cluster} displayUnit={displayUnit} />
            <div className="context-links">
              <a href="#settings?view=infrastructure">查看运行评估</a>
            </div>
          </>
        ))}
      {route.module === "settings" &&
        (route.view === "usage" ? (
          <UsageAnalytics />
        ) : route.view === "infrastructure" ? (
          <InfrastructurePanel displayUnit={displayUnit} />
        ) : (
          <DataStatus cluster={cluster} />
        ))}
    </AppShell>
  );
}
