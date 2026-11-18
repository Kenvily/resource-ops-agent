import type { ReactNode } from "react";
import {
  Activity,
  Boxes,
  ChartNoAxesCombined,
  CircleHelp,
  ClipboardList,
  LayoutDashboard,
  Server,
  Settings,
} from "lucide-react";
import { modules, routeHref, views, type Route } from "../navigation";

const icons = {
  workbench: LayoutDashboard,
  assets: Server,
  workloads: Boxes,
  analysis: ChartNoAxesCombined,
  planning: ClipboardList,
  operations: Activity,
};
export function AppShell({
  route,
  actions,
  children,
  details,
  onHelp,
}: {
  route: Route;
  actions: ReactNode;
  children: ReactNode;
  details: ReactNode;
  onHelp: () => void;
}) {
  const title =
    route.module === "settings" ? "数据与运行" : modules[route.module];
  const tabs =
    route.module in views ? views[route.module as keyof typeof views] : [];
  return (
    <div className="app">
      <a
        className="skip-link"
        href="#main-content"
        onClick={(e) => {
          e.preventDefault();
          document.getElementById("main-content")?.focus();
        }}
      >
        跳转到资源内容
      </a>
      <aside className="sidebar">
        <a className="brand" href="#workbench">
          <span className="brand-icon">
            <img
              src="/app-icon.svg"
              width={36}
              height={36}
              alt=""
              aria-hidden="true"
            />
          </span>
          <span>资源运营中心</span>
        </a>
        <button
          className="plain mobile-help"
          onClick={onHelp}
          aria-label="指标说明"
        >
          <CircleHelp aria-hidden="true" size={18} />
        </button>
        <nav aria-label="主导航">
          {Object.entries(modules).map(([key, label]) => {
            const module = key as keyof typeof modules,
              Icon = icons[module];
            return (
              <a
                key={key}
                href={routeHref(module)}
                className={route.module === key ? "active" : ""}
                aria-current={route.module === key ? "page" : undefined}
              >
                <Icon aria-hidden="true" size={18} />
                {label}
              </a>
            );
          })}
        </nav>
        <button className="desktop-help" onClick={onHelp}>
          <CircleHelp aria-hidden="true" size={18} />
          指标说明
        </button>
        <a
          className={`settings-link ${route.module === "settings" ? "active" : ""}`}
          href="#settings"
          aria-current={route.module === "settings" ? "page" : undefined}
        >
          <Settings aria-hidden="true" size={18} />
          数据与运行
        </a>
      </aside>
      <div className="workspace">
        <main id="main-content" tabIndex={-1}>
          <div className="page-heading">
            <h1 id="page-title" tabIndex={-1}>
              {title}
            </h1>
            <div className="heading-actions">{actions}</div>
          </div>
          {tabs.length > 1 && (
            <nav className="module-views" aria-label={`${title}视图`}>
              {tabs.map(([view, label]) => (
                <a
                  key={view}
                  href={routeHref(
                    route.module,
                    view,
                    route.module === "analysis" ? route.range : undefined,
                  )}
                  aria-current={route.view === view ? "page" : undefined}
                >
                  {label}
                </a>
              ))}
            </nav>
          )}
          <div className={`page-content page-${route.module}`}>{children}</div>
        </main>
      </div>
      {details}
    </div>
  );
}
