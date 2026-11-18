import { lazy, Suspense } from "react";
const Markdown = lazy(() => import("react-markdown"));

export function openProof(id: string) {
  const proof = document.getElementById(`proof-${id}`);
  if (!proof) return;
  let ancestor: HTMLElement | null = proof;
  while (ancestor) {
    if (ancestor instanceof HTMLDetailsElement) ancestor.open = true;
    ancestor = ancestor.parentElement;
  }
  proof.querySelector("summary")?.focus({ preventScroll: true });
  proof.scrollIntoView({
    block: "nearest",
    behavior: matchMedia("(prefers-reduced-motion: reduce)").matches
      ? "instant"
      : "smooth",
  });
}

export function MarkdownAnswer({
  text,
  evidenceIds = [],
}: {
  text: string;
  evidenceIds?: string[];
}) {
  return (
    <Suspense fallback={<p>{text}</p>}>
      <Markdown
        skipHtml
        components={{
          // Answers cannot embed external tracking images, links or executable HTML.
          img: () => null,
          a: ({ href, children }) => {
            const match = href?.match(/^#evidence-(\d+)$/);
            const id = match ? evidenceIds[Number(match[1]) - 1] : undefined;
            return id ? (
              <a
                href={`#proof-${id}`}
                onClick={(e) => {
                  e.preventDefault();
                  openProof(id);
                }}
              >
                {children}
              </a>
            ) : (
              <span>{children}</span>
            );
          },
          h1: ({ children }) => <h3>{children}</h3>,
          h2: ({ children }) => <h3>{children}</h3>,
        }}
      >
        {text}
      </Markdown>
    </Suspense>
  );
}
