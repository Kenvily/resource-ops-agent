import { useEffect, useRef, type ReactNode } from "react";
import { X } from "lucide-react";

/** Shared detail shell: focus trap, Escape, backdrop and focus restoration. */
export function DetailDrawer({
  title,
  label,
  close,
  children,
}: {
  title: string;
  label: string;
  close: () => void;
  children: ReactNode;
}) {
  const button = useRef<HTMLButtonElement>(null);
  const latestClose = useRef(close);
  latestClose.current = close;
  useEffect(() => {
    const previousFocus = document.activeElement as HTMLElement | null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") latestClose.current();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = previousOverflow;
      if (previousFocus?.isConnected) previousFocus.focus();
      else document.getElementById("page-title")?.focus();
    };
  }, []);
  useEffect(() => {
    button.current?.focus();
  }, [label]);
  return (
    <div className="modal-backdrop" onClick={close}>
      <section
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-label={label}
        onClick={(event) => event.stopPropagation()}
        onKeyDown={(event) => {
          if (event.key !== "Tab") return;
          const items = Array.from(
            event.currentTarget.querySelectorAll<HTMLElement>(
              'button, a[href], input, select, textarea, summary, [tabindex="0"]',
            ),
          ).filter(
            (item) =>
              !item.hasAttribute("disabled") &&
              item.getClientRects().length > 0,
          );
          const first = items[0],
            last = items[items.length - 1];
          if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last?.focus();
          } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first?.focus();
          }
        }}
      >
        <div className="drawer-heading">
          <h2>{title}</h2>
          <button
            ref={button}
            type="button"
            className="icon-button"
            aria-label="关闭详情"
            onClick={close}
          >
            <X aria-hidden="true" size={21} />
          </button>
        </div>
        {children}
      </section>
    </div>
  );
}
