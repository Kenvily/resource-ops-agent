import { test, expect } from "@playwright/test";
import { mockAssets } from "./fixtures/resourceAssets";

test.beforeEach(async ({ request, page }) => {
  await mockAssets(page);
  expect((await (await request.get("/api/health")).json()).mode).toBe("mock");
});

test("six business modules and all legacy deep links remain reachable", async ({
  page,
}) => {
  const aliases = [
    ["overview", "workbench", "工作台"],
    ["resources", "assets?view=servers", "资源资产"],
    ["statistics", "assets?view=servers", "资源资产"],
    ["history", "analysis?view=history", "分析中心"],
    ["diagnosis", "analysis?view=diagnosis", "分析中心"],
    ["efficiency", "analysis?view=efficiency", "分析中心"],
    ["assessment", "planning", "资源规划"],
    ["reports", "operations?view=reports", "运营中心"],
    ["monitoring", "operations?view=monitoring", "运营中心"],
  ];
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  for (const [old, next, title] of aliases) {
    await page.goto(`/#${old}`);
    await expect(page.locator("#page-title")).toHaveText(title);
    await expect.poll(() => new URL(page.url()).hash).toBe(`#${next}`);
    await expect(
      page
        .getByRole("navigation", { name: "主导航", exact: true })
        .getByRole("link"),
    ).toHaveText([
      "工作台",
      "资源资产",
      "业务模型",
      "分析中心",
      "资源规划",
      "运营中心",
    ]);
  }
  await page.goto("/#constructor?view=anything");
  await expect(page.locator("#page-title")).toHaveText("工作台");
  expect(errors).toEqual([]);
});

test("history context reaches diagnosis without overview polling and survives back", async ({
  page,
}) => {
  let snapshots = 0,
    models = 0;
  page.on("request", (r) => {
    if (r.url().includes("/api/v1/overview")) snapshots++;
    if (r.url().endsWith("/diagnose")) models++;
  });
  await page.goto("/#analysis?view=history&range=1h");
  const range = page.getByLabel("历史时间范围");
  await expect(range).toHaveValue("1h");
  await page.getByLabel("选择集群").selectOption("yz");
  await page.getByLabel("容量显示单位").selectOption("MB");
  await range.selectOption("1w");
  await expect
    .poll(() => new URL(page.url()).hash)
    .toBe("#analysis?view=history&range=1w");
  await page.getByRole("link", { name: "分析这段变化", exact: true }).click();
  await expect(page).toHaveURL(/#analysis\?view=diagnosis&range=1w$/);
  await expect(page.getByLabel("诊断时间范围")).toHaveValue("1w");
  await expect(page.getByLabel("选择集群")).toHaveValue("yz");
  await expect(page.getByLabel("容量显示单位")).toHaveValue("MB");
  await page.goBack();
  await expect(page.getByLabel("历史时间范围")).toHaveValue("1w");
  expect(snapshots).toBe(0);
  expect(models).toBe(0);
});

test("leaving an investigation cancels it and ignores a late model result", async ({
  page,
}) => {
  let started = 0;
  await page.route("**/api/v1/insights/capabilities", (r) =>
    r.fulfill({ json: { available: true, model_ready: true } }),
  );
  await page.route("**/api/v1/insights/diagnose", async (r) => {
    started++;
    await new Promise((resolve) => setTimeout(resolve, 400));
    await r
      .fulfill({
        contentType: "application/x-ndjson",
        body:
          JSON.stringify({
            type: "error",
            data: { message: "过期的调查结果" },
          }) + "\n",
      })
      .catch(() => {});
  });
  await page.goto("/#diagnosis");
  await page.getByRole("button", { name: "开始诊断", exact: true }).click();
  await expect.poll(() => started).toBe(1);
  await page.getByRole("link", { name: "资源规划", exact: true }).click();
  await page.waitForTimeout(500);
  await expect(page.getByText("过期的调查结果")).toHaveCount(0);
  await page.goBack();
  await expect(
    page.getByRole("button", { name: "开始诊断", exact: true }),
  ).toBeEnabled();
  await expect(page.getByText("过期的调查结果")).toHaveCount(0);
});

test("workbench attention summary reads once and operations has no background resource polling", async ({
  page,
}) => {
  let monitoring = 0,
    overview = 0;
  await page.route("**/api/v1/operations/monitoring?*", (r) => {
    monitoring++;
    return r.fulfill({
      json: {
        mode: "mock",
        database_read_at: 1793235700,
        collection_enabled: true,
        health: "unknown",
        alert_count: 0,
        alerts: [],
        metrics: [],
        truncated: false,
      },
    });
  });
  page.on("request", (r) => {
    if (r.url().includes("/api/v1/overview")) overview++;
  });
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "待关注问题", exact: true }),
  ).toBeVisible();
  await expect(page.getByText("部分指标未知", { exact: false })).toBeVisible();
  await expect.poll(() => monitoring).toBe(1);
  await expect(page.locator(".kpi-0 strong")).toHaveText("20张");
  expect(overview).toBe(1);
  await page
    .getByRole("link", { name: "查看全部指标与关注项", exact: true })
    .click();
  await expect.poll(() => monitoring).toBe(2);
  await expect(
    page.getByRole("heading", { name: "运行评估", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("link", { name: "查看运行评估", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "获取运行评估", exact: true }),
  ).toBeVisible();
  expect(overview).toBe(1);
});

test("data status keeps partial failures unknown and never exposes raw diagnostics", async ({
  page,
}) => {
  await page.route("**/api/v1/diagnostics", (r) =>
    r.fulfill({
      json: {
        snapshots: { sync_status: "healthy" },
        history: { sync_status: "database_unavailable" },
        secret_field: "DO-NOT-DISPLAY",
      },
    }),
  );
  await page.route("**/api/v1/insights/capabilities", (r) =>
    r.fulfill({ status: 503, json: {} }),
  );
  await page.route("**/api/v1/efficiency/capabilities", (r) =>
    r.fulfill({ json: { available: false, model_ready: false } }),
  );
  await page.goto("/#settings");
  await expect(page.getByRole("alert")).toHaveText("资源诊断暂不可用");
  await expect(page.getByText("数据库不可用", { exact: true })).toBeVisible();
  await expect(page.getByText("采集未启用", { exact: true })).toBeVisible();
  await expect(page.getByText("DO-NOT-DISPLAY")).toHaveCount(0);
});

for (const width of [375, 768, 1024, 844]) {
  test(`module navigation and secondary views stay usable at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 812 });
    await page.goto("/#resources");
    await page.getByLabel("搜索服务器").fill("mock-machine");
    await page.getByRole("link", { name: "业务模型", exact: true }).click();
    await expect(
      page.getByRole("heading", { name: "业务模型", exact: true }),
    ).toBeVisible();
    await page.goBack();
    await expect(page.getByLabel("搜索服务器")).toHaveValue("mock-machine");
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await page.getByRole("link", { name: "数据与运行", exact: true }).focus();
    await page.keyboard.press("Enter");
    await expect(page.locator("#page-title")).toHaveText("数据与运行");
    await page.getByRole("link", { name: "运行评估", exact: true }).click();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  });
}

test("changing a deep-linked history range updates the mounted view", async ({
  page,
}) => {
  await page.goto("/#analysis?view=history&range=1h");
  await expect(page.getByLabel("历史时间范围")).toHaveValue("1h");
  await page.evaluate(() => {
    window.location.hash = "analysis?view=history&range=1mo";
  });
  await expect(page.getByLabel("历史时间范围")).toHaveValue("1mo");
  await page.getByRole("link", { name: "资源诊断", exact: true }).click();
  await expect(page.getByLabel("诊断时间范围")).toHaveValue("1mo");
});

test("keyboard skip navigation reaches main content after shell redesign", async ({
  page,
}) => {
  await page.goto("/#planning");
  const skip = page.getByRole("link", { name: "跳转到资源内容", exact: true });
  await skip.focus();
  await expect(skip).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("#main-content")).toBeFocused();
  await expect(page.locator("#page-title")).toHaveText("资源规划");
});
