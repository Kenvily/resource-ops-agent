import { Download } from "lucide-react";
import { MarkdownAnswer, openProof } from "./MarkdownAnswer";

type Finding = {
  title: string;
  level?: string;
  state?: string;
  cluster?: string;
  evidence_ids: string[];
  limitations?: string[];
};
type Evidence = { id: string; label: string; tool: string };

const clusterName = (cluster?: string) =>
  cluster === "yz" ? "亦庄" : cluster === "zw" ? "兆维" : cluster;
// Export plain data as literal Markdown text, never executable HTML or links.
const literal = (value: string) =>
  value
    .replace(/\\/g, "\\\\")
    .replace(/([`*_{}\[\]()<>#!|])/g, "\\$1")
    .replace(/\r?\n/g, " ");

export function AgentReport<T extends Finding>({
  summary,
  markdown,
  findings,
  evidence,
  questions = [],
  formatFinding,
  context,
}: {
  summary?: string;
  markdown?: string;
  findings: T[];
  evidence: Evidence[];
  questions?: string[];
  formatFinding: (finding: T) => string;
  context: string;
}) {
  const showSummary =
    summary &&
    !/^(已完成数据库事实查询|已形成证据核查结果；未获支持的解释保留为未知)$/.test(
      summary,
    );
  const number = new Map(evidence.map((proof, index) => [proof.id, index + 1]));
  const groups = [
    { title: "查到了什么", matches: (f: T) => f.level === "fact" },
    {
      title: "如何理解",
      matches: (f: T) =>
        f.level === "supported_explanation" && f.state !== "refuted",
    },
    { title: "已排除的解释", matches: (f: T) => f.state === "refuted" },
    {
      title: "仍需确认",
      matches: (f: T) =>
        f.level !== "fact" &&
        f.level !== "supported_explanation" &&
        f.state !== "refuted",
    },
  ]
    .map((group, index) => ({
      ...group,
      // Refuted facts are unusual legacy results; each finding still appears once.
      items: findings.filter(
        (f) => group.matches(f) && (index !== 2 || f.level !== "fact"),
      ),
    }))
    .filter((group) => group.items.length > 0);
  const pending = [...new Set(questions)].filter((q) => q !== summary);
  const exportMarkdown = () => {
    const lines = [
      "# 资源助手分析",
      literal(context),
      summary ? literal(summary) : "",
    ];
    if (markdown)
      lines.push(markdown.replace(/\[证据(\d+)\]\(#evidence-\d+\)/g, "[^$1]"));
    for (const group of markdown ? [] : groups) {
      // For a short factual response, the prose itself is sufficient.
      if (groups.length > 1 || group.title !== "查到了什么")
        lines.push(`## ${group.title}`);
      for (const finding of group.items) {
        const refs = finding.evidence_ids
          .filter((id) => number.has(id))
          .map((id) => `[^${number.get(id)}]`)
          .join("");
        const cluster = clusterName(finding.cluster);
        lines.push(
          `${cluster ? `**${literal(cluster)}**：` : ""}${literal(formatFinding(finding))}${refs}`,
        );
        if (finding.limitations?.length)
          lines.push(
            `> ${[...new Set(finding.limitations)].map(literal).join("；")}`,
          );
      }
    }
    if (pending.length)
      lines.push("## 需要你补充", ...pending.map((q) => `- ${literal(q)}`));
    if (evidence.length)
      lines.push(
        "## 证据来源",
        ...evidence.map(
          (p) =>
            `[^${number.get(p.id)}]: ${literal(p.id)} · ${literal(p.label || p.tool)}`,
        ),
      );
    const url = URL.createObjectURL(
      new Blob([lines.filter(Boolean).join("\n\n") + "\n"], {
        type: "text/markdown;charset=utf-8",
      }),
    );
    const link = document.createElement("a");
    link.href = url;
    link.download = "resource-assistant.md";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  const FactsContainer = markdown ? "details" : "div";
  return (
    <div className="agent-report">
      <div className="agent-report-toolbar">
        <button className="button-secondary" onClick={exportMarkdown}>
          <Download size={16} aria-hidden="true" />
          导出 Markdown
        </button>
      </div>
      <article className="agent-prose" aria-label="分析报告">
        {markdown && (
          <MarkdownAnswer
            text={markdown}
            evidenceIds={evidence.map((p) => p.id)}
          />
        )}
        {!markdown && showSummary && (
          <p className="agent-report-lead">{summary}</p>
        )}
        <FactsContainer className="agent-verified-facts">
          {markdown && <summary>核对事实</summary>}
          {groups.map((group) => (
            <section key={group.title}>
              {(groups.length > 1 || group.title !== "查到了什么") && (
                <h3>{group.title}</h3>
              )}
              {group.items.map((finding, index) => (
                <div className="agent-report-paragraph" key={index}>
                  <p>
                    {clusterName(finding.cluster) && (
                      <strong>{clusterName(finding.cluster)}： </strong>
                    )}
                    <span>{formatFinding(finding)}</span>
                    {finding.evidence_ids
                      .filter((id) => number.has(id))
                      .map((id) => (
                        <sup key={id}>
                          <a
                            href={`#proof-${id}`}
                            aria-label={id}
                            title={`查看证据 ${number.get(id)}`}
                            onClick={(event) => {
                              event.preventDefault();
                              openProof(id);
                            }}
                          >
                            [{number.get(id)}]
                          </a>
                        </sup>
                      ))}
                  </p>
                  {!!finding.limitations?.length && (
                    <blockquote>
                      {[...new Set(finding.limitations)].join("；")}
                    </blockquote>
                  )}
                </div>
              ))}
            </section>
          ))}
        </FactsContainer>
        {pending.length > 0 && (
          <section>
            <h3>需要你补充</h3>
            <ul>
              {pending.map((q) => (
                <li key={q}>{q}</li>
              ))}
            </ul>
          </section>
        )}
      </article>
    </div>
  );
}
