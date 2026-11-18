import { test, expect } from "@playwright/test";
const id = "a1111111-1111-4111-8111-111111111111";
const proof = {
  id: "resource-evidence-1",
  tool: "get_resource_snapshot",
  label: "get_resource_snapshot",
  cluster: "yz",
  status: "complete",
  data: { summary: { gpu_count: 4, machine_count: 1 } },
};
const result = {
  status: "complete",
  mode: "deterministic",
  summary: "已完成数据库事实查询",
  findings: [
    {
      title: "当前数据库观测GPU 4张、服务器 1台",
      level: "fact",
      cluster: "yz",
      evidence_ids: [proof.id],
    },
  ],
  evidence: [proof],
  entities: [],
  questions: [],
  warnings: [],
  model_calls: 0,
  tool_calls: 1,
  investigation: { id },
};
const stream = (value: unknown) =>
  [
    { type: "task", data: { id } },
    { type: "progress", data: { label: "证据复核：当前资源快照" } },
    { type: "evidence", data: proof },
    { type: "result", data: value },
  ]
    .map((x) => JSON.stringify(x))
    .join("\n") + "\n";

test("assistant queries facts, cites evidence and continues the same scoped task", async ({
  page,
}) => {
  const bodies: Record<string, unknown>[] = [];
  await page.route("**/api/v1/agents/query", (r) => {
    bodies.push(r.request().postDataJSON());
    return r.fulfill({
      contentType: "application/x-ndjson",
      body: stream(result),
    });
  });
  await page.goto("/#analysis?view=assistant");
  await page
    .getByRole("button", { name: "当前GPU有多少张？", exact: true })
    .click();
  expect(bodies).toHaveLength(0);
  await page.getByRole("button", { name: "开始查询", exact: true }).click();
  await expect(page.getByRole("region", { name: "调查结论" })).toContainText(
    "GPU 4张",
  );
  await page.getByText("回答信息", { exact: true }).click();
  await expect(
    page.getByText("模型 0 轮 · 工具 1 次", { exact: true }),
  ).toBeVisible();
  await page.getByRole("link", { name: proof.id, exact: true }).click();
  await expect(page.locator(`#proof-${proof.id}`)).toHaveAttribute("open", "");
  await page.getByLabel("你想了解什么？").fill("现在再查一次");
  await page.getByRole("button", { name: "继续调查", exact: true }).click();
  await expect.poll(() => bodies.length).toBe(2);
  expect(bodies[1].parent_id).toBe(id);
  expect(bodies[1]).not.toHaveProperty("cluster");
  await page.getByRole("button", { name: "新建调查", exact: true }).click();
  await expect(page.getByLabel("你想了解什么？")).toBeFocused();
  await expect(page.getByRole("region", { name: "调查结论" })).toHaveCount(0);
});

test("assistant renders clarification and restores a persisted task", async ({
  page,
}) => {
  await page.route(`**/api/v1/investigations/${id}?*`, (r) =>
    r.fulfill({
      json: {
        id,
        kind: "resource_investigation",
        status: "waiting_input",
        clusters: ["yz"],
        request: { window: "1mo" },
        result: {
          ...result,
          status: "waiting_input",
          summary: "请明确需要核查的资源指标",
          findings: [],
          evidence: [],
          questions: ["请明确需要核查的资源指标"],
        },
        evidence: [],
        events: [],
      },
    }),
  );
  await page.goto(`/#analysis?view=assistant&task=${id}`);
  await expect(
    page.getByRole("heading", { name: "需要补充", exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("助手观察范围")).toHaveValue("1mo");
  await expect(
    page.getByText("请明确需要核查的资源指标", { exact: true }),
  ).toHaveCount(1);
  await page.getByLabel("你想了解什么？").fill("CPU历史哪里有缺口？");
  await expect(
    page.getByRole("button", { name: "继续调查", exact: true }),
  ).toBeEnabled();
});

test("assistant network retry reuses idempotency key", async ({ page }) => {
  const keys: (string | undefined)[] = [];
  await page.route("**/api/v1/agents/query", (r) => {
    keys.push(r.request().headers()["idempotency-key"]);
    return keys.length === 1
      ? r.fulfill({ status: 503, json: { detail: "数据库暂不可用" } })
      : r.fulfill({
          contentType: "application/x-ndjson",
          body: stream(result),
        });
  });
  await page.goto("/#analysis?view=assistant");
  await page.getByLabel("你想了解什么？").fill("当前GPU多少？");
  await page.getByRole("button", { name: "开始查询", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("数据库暂不可用");
  await page.getByRole("button", { name: "开始查询", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "已完成", exact: true }),
  ).toBeVisible();
  expect(keys[0]).toBeTruthy();
  expect(keys[1]).toBe(keys[0]);
});

test("assistant remains readable on mobile and exposes evidence by keyboard", async ({
  page,
}) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.route("**/api/v1/agents/query", (r) =>
    r.fulfill({ contentType: "application/x-ndjson", body: stream(result) }),
  );
  await page.goto("/#analysis?view=assistant");
  await page.getByLabel("你想了解什么？").fill("当前GPU有多少张？");
  await page.getByRole("button", { name: "开始查询", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "已完成", exact: true }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.getByText("查看依据与过程", { exact: true }).click();
  await page.locator(`#proof-${proof.id} summary`).focus();
  await page.keyboard.press("Enter");
  await expect(page.locator(`#proof-${proof.id}`)).toHaveAttribute("open", "");
  await page.screenshot({
    path: test.info().outputPath("mock-agent-workspace-375.png"),
    fullPage: true,
  });
});

test("assistant can continue an old occupancy record without crashing on legacy result shape", async ({
  page,
}) => {
  await page.route(`**/api/v1/investigations/${id}?*`, (r) =>
    r.fulfill({
      json: {
        id,
        kind: "gpu_usage",
        status: "limited",
        clusters: ["yz"],
        request: { hours: 1 },
        result: {
          agent: {
            mode: "degraded",
            model_calls: 1,
            tool_calls: 2,
            findings: [
              { title: "整卡低计算活动尚不能证明浪费", evidence_ids: [] },
            ],
          },
          results: [],
        },
        evidence: [],
        events: [],
      },
    }),
  );
  await page.goto(`/#analysis?view=assistant&task=${id}`);
  await expect(
    page.getByText("整卡低计算活动尚不能证明浪费", { exact: true }),
  ).toBeVisible();
  await page.getByLabel("你想了解什么？").fill("重新核查GPU利用率的历史覆盖");
  await expect(
    page.getByRole("button", { name: "继续调查", exact: true }),
  ).toBeEnabled();
});

test("idempotent replay is visibly saved rather than presented as a new query", async ({
  page,
}) => {
  await page.route("**/api/v1/agents/query", (r) =>
    r.fulfill({
      contentType: "application/x-ndjson",
      body: stream({
        ...result,
        investigation: { id, reused: true },
        scope: { window: "1d" },
        answer_type: "facts",
      }),
    }),
  );
  await page.goto("/#analysis?view=assistant");
  await page.getByLabel("你想了解什么？").fill("当前GPU多少？");
  await page.getByRole("button", { name: "开始查询", exact: true }).click();
  await expect(page.getByText("已保存", { exact: true })).toBeVisible();
  await expect(page.getByText("原快照", { exact: true })).toBeVisible();
});

test("assistant reads as a report and exports the same verified content as Markdown", async ({
  page,
}) => {
  await page.route("**/api/v1/agents/query", (r) =>
    r.fulfill({
      contentType: "application/x-ndjson",
      body: stream({
        ...result,
        status: "limited",
        summary: "额度上升，但仍不能认定发生了资源浪费。",
        findings: [
          ...result.findings,
          {
            title: "业务分配增长",
            level: "supported_explanation",
            evidence_ids: [proof.id],
          },
          { title: "容量下降", state: "refuted", evidence_ids: [proof.id] },
          {
            title: "<script>alert('unsafe')</script>",
            level: "unknown",
            evidence_ids: [],
            limitations: ["缺少任务阶段证据"],
          },
        ],
        questions: ["需要观察哪个业务？"],
      }),
    }),
  );
  await page.goto("/#analysis?view=assistant");
  await page.getByLabel("你想了解什么？").fill("分配为何增长？");
  await page.getByRole("button", { name: "开始查询", exact: true }).click();
  const report = page.getByRole("article", { name: "分析报告" });
  await expect(
    report.getByRole("heading", { name: "查到了什么" }),
  ).toBeVisible();
  await expect(report.getByRole("heading", { name: "如何理解" })).toBeVisible();
  await expect(
    report.getByRole("heading", { name: "已排除的解释" }),
  ).toBeVisible();
  await expect(report.getByRole("heading", { name: "仍需确认" })).toBeVisible();
  await expect(report.locator("script")).toHaveCount(0);
  await expect(report.getByText("缺少任务阶段证据")).toBeVisible();
  const downloading = page.waitForEvent("download");
  await page.getByRole("button", { name: "导出 Markdown" }).click();
  const download = await downloading;
  expect(download.suggestedFilename()).toBe("resource-assistant.md");
  const source = await download.createReadStream();
  let markdown = "";
  for await (const chunk of source!) markdown += chunk.toString();
  expect(markdown).toContain("## 如何理解");
  expect(markdown).toContain("业务分配增长[^1]");
  expect(markdown).toContain(`[^1]: resource-evidence-1`);
  expect(markdown).toContain("缺少任务阶段证据");
  expect(markdown).not.toContain("<script>");
});

test("streamed Markdown appears before completion and auxiliary details stay collapsed", async ({
  page,
}) => {
  await page.addInitScript(
    (payload) => {
      const original = window.fetch.bind(window);
      window.fetch = (input, init) => {
        if (input !== "/api/v1/agents/query") return original(input, init);
        return Promise.resolve(
          new Response(
            new ReadableStream({
              start(controller) {
                const emit = (type: string, data: unknown) =>
                  controller.enqueue(
                    new TextEncoder().encode(
                      JSON.stringify({ type, data }) + "\n",
                    ),
                  );
                emit("task", { id: payload.id });
                emit("evidence", payload.proof);
                emit("answer_start", {});
                emit("answer_delta", {
                  text: "**配置概况**\n\n两地配置已查询。",
                });
                setTimeout(() => {
                  emit("result", {
                    ...payload.result,
                    answer_markdown: "**配置概况**\n\n两地配置已查询。",
                    answer_status: "complete",
                  });
                  controller.close();
                }, 1800);
              },
            }),
            { headers: { "Content-Type": "application/x-ndjson" } },
          ),
        );
      };
    },
    { id, proof, result },
  );
  await page.goto("/#analysis?view=assistant");
  await page.getByLabel("你想了解什么？").fill("两个机房总体服务器配置如何？");
  await page.getByRole("button", { name: "开始查询", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "正在生成的回答" }),
  ).toContainText("两地配置已查询");
  await expect(page.getByRole("status")).toContainText("正在撰写回答");
  await expect(page.getByRole("region", { name: "调查结论" })).toBeVisible();
  await expect(
    page.getByRole("region", { name: "正在生成的回答" }),
  ).toHaveCount(0);
  await expect(
    page
      .getByRole("article", { name: "分析报告" })
      .locator("strong")
      .filter({ hasText: "配置概况" }),
  ).toContainText("配置概况");
  await expect(page.locator(".agent-evidence")).not.toHaveAttribute("open", "");
  await expect(page.locator(".agent-result-footer")).not.toHaveAttribute(
    "open",
    "",
  );
});

test("failed generation removes the partial draft and shows verified fallback", async ({
  page,
}) => {
  await page.route("**/api/v1/agents/query", (r) =>
    r.fulfill({
      contentType: "application/x-ndjson",
      body:
        [
          { type: "answer_start", data: {} },
          { type: "answer_delta", data: { text: "中断的草稿不要保留" } },
          { type: "answer_reset", data: {} },
          { type: "result", data: { ...result, answer_status: "fallback" } },
        ]
          .map((e) => JSON.stringify(e))
          .join("\n") + "\n",
    }),
  );
  await page.goto("/#analysis?view=assistant");
  await page.getByLabel("你想了解什么？").fill("配置怎么样？");
  await page.getByRole("button", { name: "开始查询", exact: true }).click();
  await expect(page.getByRole("region", { name: "调查结论" })).toContainText(
    "GPU 4张",
  );
  await expect(page.getByText("中断的草稿不要保留")).toHaveCount(0);
});
