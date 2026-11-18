import type { ReactNode } from "react";
export function AssetSummary({
  items,
}: {
  items: {
    label: string;
    value: ReactNode;
    note?: string;
    active?: boolean;
    onClick?: () => void;
  }[];
}) {
  return (
    <div className="asset-summary">
      {items.map((item) => {
        const content = (
          <>
            <span>{item.label}</span>
            <strong>{item.value}</strong>
            {item.note && <small>{item.note}</small>}
          </>
        );
        return item.onClick ? (
          <button
            type="button"
            key={item.label}
            className={item.active ? "active" : ""}
            aria-pressed={item.active}
            onClick={item.onClick}
          >
            {content}
          </button>
        ) : (
          <div key={item.label}>{content}</div>
        );
      })}
    </div>
  );
}
