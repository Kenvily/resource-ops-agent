import { expect, test } from "@playwright/test";

type Batch = {
  visitor_id: string;
  events: { id: string; page: string; kind: string; feature?: string }[];
};
const summary = {
  enabled: true,
  tracking_started_at: 1793923200,
  pv: 12,
  uv: 3,
  dau: 2,
  feature_uses: 5,
  daily: [
    { date: "2026-11-04", pv: null, dau: null, feature_uses: null },
    { date: "2026-11-05", pv: 4, dau: 2, feature_uses: 1 },
    { date: "2026-11-06", pv: 8, dau: 2, feature_uses: 4 },
  ],
  pages: [
    { page: "analysis.assistant", pv: 8, feature_uses: 4 },
    { page: "workbench", pv: 4, feature_uses: 1 },
  ],
  features: [
    { feature: "assistant_query", count: 4 },
    { feature: "manual_refresh", count: 1 },
  ],
  agents: {
    tasks: 4,
    terminal_tasks: 3,
    model_tasks: 1,
    unknown_model_tasks: 1,
    success_rate: 1 / 3,
    avg_duration_ms: 20000,
    duration_samples: 3,
    statuses: { complete: 1, limited: 1, cancelled: 1, running: 1 },
    kinds: { resource_investigation: 4 },
  },
  definitions: [
    "UV按匿名浏览器去重，不代表实名人数。",
    "完整完成率不包含受限和取消。",
  ],
};
async function recorder(page: import("@playwright/test").Page) {
  const batches: Batch[] = [];
  await page.route("**/api/v1/analytics/events", (route) => {
    const batch = route.request().postDataJSON();
    batches.push(batch);
    return route.fulfill({
      json: { enabled: true, accepted: batch.events.length },
    });
  });
  return batches;
}

test("canonical page visits are counted once under StrictMode; range and cluster changes are not PV", async ({
  page,
}) => {
  const batches = await recorder(page);
  const views = () =>
    batches.flatMap((b) => b.events).filter((e) => e.kind === "page_view");
  await page.goto("/#workbench");
  await expect.poll(() => views().length).toBe(1);
  await page.getByLabel("选择集群").selectOption("yz");
  await page.evaluate(() => {
    location.hash = "#workbench?range=3mo";
  });
  await page.waitForTimeout(300);
  expect(views()).toHaveLength(1);
  await page.getByRole("link", { name: "分析中心", exact: true }).click();
  await expect.poll(() => views().length).toBe(2);
  await page.getByLabel("历史时间范围").selectOption("1w");
  await expect
    .poll(
      () =>
        batches
          .flatMap((b) => b.events)
          .filter((e) => e.feature === "history_change").length,
    )
    .toBe(1);
  expect(views()).toHaveLength(2);
  const visitor = batches[0].visitor_id;
  const ids = views().map((e) => e.id);
  await page.reload();
  await expect.poll(() => views().length).toBe(3);
  expect(batches.at(-1)?.visitor_id).toBe(visitor);
  expect(ids).not.toContain(views().at(-1)?.id);
});

test("feature events exclude the question, URL filters and private labels", async ({
  page,
}) => {
  const batches = await recorder(page);
  await page.route("**/api/v1/agents/query", (route) =>
    route.fulfill({
      contentType: "application/x-ndjson",
      body:
        JSON.stringify({
          type: "result",
          data: {
            status: "waiting_input",
            summary: "请补充",
            findings: [],
            evidence: [],
          },
        }) + "\n",
    }),
  );
  await page.goto("/#analysis?view=assistant&range=all");
  await page
    .getByLabel("你想了解什么？")
    .fill("private-business-pod 的GPU为何异常？");
  await page.getByRole("button", { name: "开始查询", exact: true }).click();
  await expect
    .poll(
      () =>
        batches
          .flatMap((b) => b.events)
          .filter((e) => e.feature === "assistant_query").length,
    )
    .toBe(1);
  const text = JSON.stringify(batches);
  expect(text).not.toContain("private-business-pod");
  expect(text).not.toContain("range=");
  expect(text).not.toContain("question");
  expect(
    batches
      .flatMap((b) => b.events)
      .every((e) => e.page === "analysis.assistant"),
  ).toBe(true);
});

test("unavailable analytics and blocked storage do not prevent normal navigation", async ({
  page,
}) => {
  await page.addInitScript(() => {
    Object.defineProperty(window, "localStorage", {
      get() {
        throw Error("blocked");
      },
    });
  });
  let calls = 0;
  await page.route("**/api/v1/analytics/events", (route) => {
    calls++;
    return route.fulfill({ status: 503, json: { detail: "暂不可用" } });
  });
  await page.goto("/#workbench");
  await expect.poll(() => calls).toBeGreaterThan(0);
  await page.getByRole("link", { name: "资源资产", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "资源资产", exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("alert")).not.toContainText("使用统计");
});

test("usage dashboard shows separate visit, feature and task statistics with mobile layout", async ({
  page,
}) => {
  await page.setViewportSize({ width: 375, height: 900 });
  await recorder(page);
  await page.route("**/api/v1/analytics/summary?*", (route) =>
    route.fulfill({ json: summary }),
  );
  await page.goto("/#settings?view=usage");
  await expect(
    page.getByRole("heading", { name: "使用统计", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("img", { name: "页面访问与活跃浏览器趋势" }),
  ).toBeVisible();
  await expect(page.getByText("33.3%", { exact: true })).toBeVisible();
  await expect(page.getByText("20.00 秒", { exact: true })).toBeVisible();
  await page.getByText("每日访问明细", { exact: true }).click();
  await expect(
    page.getByRole("row", { name: "2026-11-04 — — —", exact: true }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: test.info().outputPath("mock-usage-375.png"),
    fullPage: true,
  });
});

test("no database or disabled analytics never appears as zero usage", async ({
  page,
}) => {
  await recorder(page);
  await page.route("**/api/v1/analytics/summary?*", (route) =>
    route.fulfill({ status: 503, json: { detail: "使用统计需要PostgreSQL" } }),
  );
  await page.goto("/#settings?view=usage");
  await expect(page.getByRole("alert")).toContainText("使用统计需要PostgreSQL");
  await expect(page.locator(".usage-kpis")).toHaveCount(0);
  await page.route("**/api/v1/analytics/summary?*", (route) =>
    route.fulfill({ json: { enabled: false } }),
  );
  await page.getByRole("button", { name: "刷新统计" }).click();
  await expect(page.getByText("使用统计已关闭", { exact: true })).toBeVisible();
  await expect(page.locator(".usage-kpis")).toHaveCount(0);
});
