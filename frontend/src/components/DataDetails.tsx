import type { ReactNode } from "react";
/** Secondary evidence stays reachable without competing with values and exceptions. */
export function DataDetails({
  children,
  label = "数据详情",
}: {
  children: ReactNode;
  label?: string;
}) {
  return (
    <details className="data-details">
      <summary>{label}</summary>
      <div className="data-details-content">{children}</div>
    </details>
  );
}
