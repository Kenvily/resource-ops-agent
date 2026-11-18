import { test, expect } from "@playwright/test";
import { mockAssets } from "./fixtures/resourceAssets";

// Never create screenshots or run fixture assertions against private Live data.
test.beforeEach(async ({ request, page }) => {
  await mockAssets(page);
  const health = await (await request.get("/api/health")).json();
  expect(health.mode, "Browser acceptance requires DATA_MODE=mock").toBe(
    "mock",
  );
});
test("cluster filter, search, sorting and metric explanation", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByRole("combobox", { name: "选择集群" }).selectOption("zw");
  await expect(
    page.getByRole("heading", { name: "亦庄集群" }),
  ).not.toBeVisible();
  await expect(page.getByRole("heading", { name: "兆维集群" })).toBeVisible();
  await page.getByRole("combobox", { name: "选择集群" }).selectOption("all");
  await page.getByRole("link", { name: "资源资产", exact: true }).click();
  await page.getByRole("textbox", { name: "搜索服务器" }).fill("mock-machine");
  await expect(page.locator("tbody tr")).toHaveCount(1);
  await page.getByRole("combobox", { name: "服务器排序" }).selectOption("cpu");
  await page.getByRole("button", { name: "指标说明", exact: true }).click();
  await expect(page.getByRole("dialog")).toContainText("不是实际显存占用");
});

for (const [width, height] of [
  [1440, 900],
  [1280, 800],
  [390, 844],
  [375, 812],
  [844, 390],
]) {
  test(`layout ${width}x${height}`, async ({ page }) => {
    await page.setViewportSize({ width, height });
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "兆维集群" })).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  });
}

test("API failure marks previous data stale and allows retry", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.locator(".kpi-0 strong")).toHaveText("20张");
  await page.route("**/api/v1/overview*", (route) =>
    route.fulfill({ status: 503, body: "unavailable" }),
  );
  await page.getByRole("button", { name: "刷新数据", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("已保留上次成功数据");
  await expect(page.getByText("数据已过期", { exact: true })).toHaveCount(2);
  await expect(page.locator(".kpi-0 strong")).toHaveText("—张");
  await page.unroute("**/api/v1/overview*");
  await page.getByRole("button", { name: "重新连接" }).click();
  await expect(page.locator(".kpi-0 strong")).toHaveText("20张");
});

test("partial source is explicit and healthy cluster stays usable", async ({
  page,
  request,
}) => {
  const data = await (await request.get("/api/v1/overview")).json();
  data.complete = false;
  data.overall_status = "degraded";
  for (const key of Object.keys(data.totals)) data.totals[key] = null;
  const failed = data.clusters[1];
  Object.assign(failed, {
    status: "unavailable",
    sampled_at: null,
    gpus: [],
    workloads: [],
    warnings: ["演示：查询超时"],
  });
  for (const key of Object.keys(data.totals)) failed[key] = null;
  data.nodes = data.nodes.filter(
    (n: { cluster: string }) => n.cluster === "yz",
  );
  await page.route("**/api/v1/overview*", (route) =>
    route.fulfill({ json: data }),
  );
  await page.goto("/");
  await expect(page.getByText("连接失败", { exact: true })).toBeVisible();
  await expect(page.locator(".kpi-0 strong")).toHaveText("—张");
  await page.goto("/#assets?view=gpus");
  await page.getByRole("button", { name: "查看显卡历史" }).first().click();
  await expect(page.getByRole("dialog")).toBeVisible();
});

test("keyboard dialog focus and reduced motion", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/#assets?view=gpus");
  const opener = page.getByRole("button", { name: "查看显卡历史" }).first();
  await opener.click();
  await expect(page.getByRole("button", { name: "关闭详情" })).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  expect(
    await page
      .getByRole("dialog")
      .evaluate((e) => e.contains(document.activeElement)),
  ).toBe(true);
  await page.keyboard.press("Escape");
  await expect(opener).toBeFocused();
});

test("allocation gaps preserve known GPU and capacity totals", async ({
  page,
  request,
}) => {
  const data = await (await request.get("/api/v1/overview")).json();
  data.complete = false;
  data.overall_status = "degraded";
  data.totals.allocated_observed_raw = null;
  data.totals.unallocated_observed_raw = null;
  for (const key of ["allocated_raw", "unallocated_raw", "allocation_ratio"])
    data.totals[key] = null;
  await page.route("**/api/v1/overview*", (route) =>
    route.fulfill({ json: data }),
  );
  await page.goto("/");
  await expect(page.locator(".kpi-0 strong")).toHaveText("20张");
  await expect(page.locator(".cluster-footer").first()).toContainText(
    "容量 288 GB",
  );
  await expect(page.locator(".kpi-1 strong")).toHaveText("12%");
  await expect(
    page
      .locator(".notice[role=status]")
      .filter({ hasText: "已观测值与覆盖范围" }),
  ).toContainText("已观测值与覆盖范围");
});

test("unit switching converts all memory values without another query", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.locator(".cluster-footer").first()).toContainText(
    "容量 288 GB",
  );
  await expect(
    page.getByRole("combobox", { name: "容量显示单位" }).locator("option"),
  ).toHaveText(["MB", "GB"]);
  let queries = 0;
  page.on("request", (r) => {
    if (r.url().includes("/api/v1/overview")) queries++;
  });
  const selector = page.getByRole("combobox", { name: "容量显示单位" });
  await selector.selectOption("MB");
  await expect(page.locator(".cluster-footer").first()).toContainText(
    "容量 288,000 MB",
  );
  await page.getByRole("link", { name: "业务模型", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "未映射业务负载" }),
  ).toContainText("MB");
  await selector.selectOption("GB");
  await page.goto("/#assets?view=gpus");
  await expect(page.locator(".asset-table tbody")).toContainText("GB");
  expect(queries).toBe(0);
});

test("cluster refresh updates only that cluster and preserves content", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.locator(".kpi-0 strong")).toHaveText("20张");
  const zwBefore = await page.locator(".cluster-card").nth(1).innerText();
  const urls: string[] = [];
  page.on("request", (r) => {
    if (r.url().includes("/api/v1/overview")) urls.push(r.url());
  });
  let release: () => void = () => {};
  const hold = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/v1/overview?cluster=yz*", async (route) => {
    await hold;
    await route.continue();
  });

  await page.getByRole("button", { name: "刷新亦庄数据源" }).click();
  await expect(page.locator(".kpi-0 strong")).toHaveText("20张");
  await expect(page.locator(".skeleton")).toHaveCount(0);
  expect(await page.locator(".cluster-card").nth(1).innerText()).toBe(zwBefore);
  release();
  await expect(
    page.getByRole("button", { name: "刷新亦庄数据源" }),
  ).toBeEnabled();
  expect(urls).toHaveLength(1);
  expect(urls[0]).toContain("cluster=yz");
});

test("background polling preserves open GPU asset details", async ({
  page,
}) => {
  await page.clock.install();
  await page.goto("/#assets?view=gpus");
  await page.getByRole("button", { name: "查看显卡历史" }).first().click();
  await expect(
    page.getByRole("img", { name: "DCGM 整卡利用率历史，缺测处断开" }),
  ).toBeVisible();
  await page.clock.fastForward(30000);
  await expect(
    page.getByRole("dialog", { name: "GPU 显存与使用历史" }),
  ).toBeVisible();
  await expect(
    page.getByRole("img", { name: "DCGM 整卡利用率历史，缺测处断开" }),
  ).toBeVisible();
});

test("upstream source failure retains explicitly stale cluster snapshot", async ({
  page,
  request,
}) => {
  await page.goto("/");
  await expect(page.locator(".kpi-0 strong")).toHaveText("20张");
  const data = await (await request.get("/api/v1/overview?cluster=yz")).json();
  const failed = data.clusters[0];
  Object.assign(failed, {
    status: "unavailable",
    sampled_at: null,
    gpus: [],
    workloads: [],
    warnings: ["演示：数据源超时"],
  });
  for (const key of Object.keys(data.totals)) {
    data.totals[key] = null;
    failed[key] = null;
  }
  data.nodes = [];
  data.complete = false;
  await page.route("**/api/v1/overview?cluster=yz*", (route) =>
    route.fulfill({ json: data }),
  );
  await page.getByRole("button", { name: "刷新亦庄数据源" }).click();
  await expect(page.locator(".cluster-card").first()).toContainText(
    "数据已过期",
  );
  await expect(page.locator(".cluster-card").first()).toContainText("12 张");
  await page.getByRole("link", { name: "资源资产", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "服务器资源", exact: true }),
  ).toBeVisible();
  await page.getByRole("link", { name: "工作台", exact: true }).click();
  await expect(page.locator(".kpi-0 strong")).toHaveText("20张");
});

test("aggregate history windows, metrics and accessible samples", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByRole("link", { name: "分析中心", exact: true }).click();
  const panel = page.getByRole("region", { name: "集群历史趋势" });
  await expect(panel.getByRole("img")).toBeVisible();
  await panel
    .getByRole("combobox", { name: "历史时间范围" })
    .selectOption("1h");
  await expect(panel.locator(".panel-foot")).toContainText("步长 1 分钟");
  await panel.getByRole("slider", { name: "历史采样点" }).fill("0");
  await expect(panel.locator("output")).toContainText("亦庄");
  await expect(panel.locator("output")).toContainText("兆维");
  await panel
    .getByRole("combobox", { name: "历史趋势指标" })
    .selectOption("allocation_ratio");
  await expect(panel.locator("output")).toContainText("%");
  await panel.locator(".trend-table summary").click();
  await expect(panel.locator("tbody tr")).toHaveCount(40);
  const weekResponse = page.waitForResponse(
    (response) =>
      response.url().includes("/api/v1/trends?") &&
      response.url().includes("window=1w") &&
      response.ok(),
  );
  await panel
    .getByRole("combobox", { name: "历史时间范围" })
    .selectOption("1w");
  const week = await (await weekResponse).json();
  expect(week.step).toBeGreaterThanOrEqual(60);
  expect(week.series[0].points.length).toBeLessThanOrEqual(721);
  await expect(panel.locator(".panel-foot")).toContainText(
    `步长 ${week.step / 60} 分钟`,
  );
  const fourMonths = page.waitForResponse(
    (response) =>
      response.url().includes("/api/v1/trends?") &&
      response.url().includes("window=3mo") &&
      response.ok(),
  );
  await panel
    .getByRole("combobox", { name: "历史时间范围" })
    .selectOption("3mo");
  const history = await (await fourMonths).json();
  expect(history.window).toBe("3mo");
  expect(
    history.series.every(
      (series: { points: unknown[] }) => series.points.length <= 721,
    ),
  ).toBe(true);
  await expect(
    panel.getByRole("combobox", { name: "历史时间范围" }),
  ).toHaveValue("3mo");

  await page.getByRole("link", { name: "业务模型", exact: true }).click();
  await page.getByLabel("搜索业务模型").fill("demo-pod-1");
  await page.getByRole("button", { name: "查看关联 GPU", exact: true }).click();
  const single = page.getByRole("dialog", { name: "关联 GPU", exact: true });
  await single.getByRole("button", { name: "查看显卡历史" }).click();
  await expect(single.getByRole("img")).toBeVisible();
});

test("history partial failure preserves healthy line and reports gaps", async ({
  page,
  request,
}) => {
  const data = await (await request.get("/api/v1/trends?window=1h")).json();
  data.complete = false;
  data.series[0].status = "degraded";
  data.series[0].points[3].allocated_raw = null;
  data.series[0].warnings = ["历史采样存在缺口；未补零或跨缺口连线"];
  data.series[1].status = "unavailable";
  data.series[1].points = data.series[1].points.map(
    (p: { timestamp: number }) => ({
      timestamp: p.timestamp,
      capacity_raw: null,
      allocated_raw: null,
      unallocated_raw: null,
      allocation_ratio: null,
    }),
  );
  data.series[1].warnings = ["演示：历史查询超时"];
  await page.route("**/api/v1/trends?**", (route) =>
    route.fulfill({ json: data }),
  );
  await page.goto("/");
  await page.getByRole("link", { name: "分析中心", exact: true }).click();
  const panel = page.getByRole("region", { name: "集群历史趋势" });
  await expect(panel.getByText("连接失败", { exact: true })).toBeVisible();
  await expect(panel.locator(".chart-series-yz polyline")).toHaveCount(2);
  await expect(panel.locator(".chart-series-zw polyline")).toHaveCount(0);
  await expect(panel.getByText(/未补零或跨缺口连线/)).toBeVisible();
});

test("history error keeps old chart explicitly marked", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("link", { name: "分析中心", exact: true }).click();
  const panel = page.getByRole("region", { name: "集群历史趋势" });
  await expect(panel.getByRole("img")).toBeVisible();
  await page.route("**/api/v1/trends?**", (route) =>
    route.fulfill({ status: 503, body: "unavailable" }),
  );
  await panel.getByRole("button", { name: "刷新集群历史" }).click();
  await expect(panel.getByRole("alert")).toContainText("上次结果");
  await expect(panel.getByRole("img")).toBeVisible();
  await expect(panel.locator(".old-history")).toHaveCount(1);
});

test("page reload requests a new persisted snapshot", async ({ page }) => {
  const snapshotResponse = () =>
    page.waitForResponse(
      (response) =>
        response.url().includes("/api/v1/overview?") &&
        response.url().includes("refresh=true") &&
        response.ok(),
    );
  const initial = snapshotResponse();
  await page.goto("/");
  const before = await (await initial).json();
  await expect(page.locator(".kpi-0 strong")).toHaveText("20张");
  const refreshed = snapshotResponse();
  await page.reload();
  const after = await (await refreshed).json();
  expect(after.clusters[0].fetched_at).toBeGreaterThan(
    before.clusters[0].fetched_at,
  );
  if (after.clusters[0].storage_backend === "postgresql") {
    await expect(page.locator(".cluster-footer").first()).toContainText(
      "数据库快照",
    );
  }
  await expect(page.locator(".kpi-0 strong")).toHaveText("20张");
});

test("functional pages keep filters, deep links and browser back", async ({
  page,
}) => {
  await page.goto("/#resources");
  await expect(
    page.getByRole("heading", { name: "资源资产", exact: true }),
  ).toBeVisible();
  await page.getByRole("textbox", { name: "搜索服务器" }).fill("mock-machine");
  await page.getByRole("combobox", { name: "容量显示单位" }).selectOption("MB");
  await page.getByRole("link", { name: "分析中心", exact: true }).click();
  const select = page.getByRole("combobox", { name: "历史时间范围" });
  await expect(select.locator("option")).toHaveText([
    "近 1 小时",
    "近 1 天",
    "近 1 周",
    "近 1 个月",
    "近 3 个月",
    "全部",
  ]);
  const allResponse = page.waitForResponse(
    (r) => r.url().includes("window=all") && r.url().includes("/api/v1/trends"),
  );
  await select.selectOption("all");
  expect((await allResponse).status()).toBe(200);
  await expect(
    page.getByRole("region", { name: "集群历史趋势" }).getByRole("img"),
  ).toBeVisible();
  await page.goBack();
  await expect(page.getByRole("textbox", { name: "搜索服务器" })).toHaveValue(
    "mock-machine",
  );
  await expect(
    page.getByRole("combobox", { name: "容量显示单位" }),
  ).toHaveValue("MB");
});

test("statistics retain healthy cluster when the other query fails", async ({
  page,
}) => {
  await page.route("**/api/v1/history/statistics*", (route) => {
    if (new URL(route.request().url()).searchParams.get("cluster") === "zw")
      return route.fulfill({ status: 503, json: { detail: "统计暂不可用" } });
    return route.fulfill({
      json: {
        cluster: "yz",
        source_grain: 60,
        expected_buckets: 1,
        truncated: false,
        units: {
          allocation: "MB",
          capacity: "MB",
          memory: "MB",
          unit_verified: true,
        },
        summary: {
          allocated: {
            sum: 60000,
            mean: 1000,
            min: 500,
            max: 1500,
            valid_count: 60,
            coverage: 1,
          },
        },
        buckets: [],
      },
    });
  });
  await page.goto("/#history");
  const panel = page.getByRole("region", { name: "时段统计" });
  await expect(
    panel.getByRole("cell", { name: "1 GB", exact: true }),
  ).toBeVisible();
  await expect(panel.getByRole("alert")).toContainText("兆维");
});

test("statistics follow the shared time range without a grain selector", async ({
  page,
}) => {
  const queries: URL[] = [];
  await page.route("**/api/v1/history/statistics*", (route) => {
    const url = new URL(route.request().url());
    queries.push(url);
    const short = url.searchParams.get("window") === "1h";
    return route.fulfill({
      json: {
        cluster: url.searchParams.get("cluster"),
        source_grain: short ? 60 : 3600,
        expected_points: short ? 61 : 721,
        expected_buckets: 1,
        truncated: false,
        units: {
          allocation: "MB",
          capacity: "MB",
          memory: "MB",
          unit_verified: true,
        },
        summary: {
          allocated: {
            sum: short ? 1000 : 2000,
            mean: short ? 1000 : 2000,
            min: 500,
            max: 2500,
            valid_count: short ? 61 : 721,
            coverage: 1,
          },
        },
        buckets: [],
      },
    });
  });
  await page.goto("/#history");
  const panel = page.getByRole("region", { name: "时段统计" });
  await expect(panel.getByRole("combobox")).toHaveCount(0);
  await page.getByRole("combobox", { name: "历史时间范围" }).selectOption("1h");
  await expect(
    panel.getByRole("cell", { name: "1 GB", exact: true }),
  ).toHaveCount(2);
  await page
    .getByRole("combobox", { name: "历史时间范围" })
    .selectOption("1mo");
  await expect(
    panel.getByRole("cell", { name: "2 GB", exact: true }),
  ).toHaveCount(2);
  expect(queries.some((q) => q.searchParams.get("window") === "1h")).toBe(true);
  expect(queries.some((q) => q.searchParams.get("window") === "1mo")).toBe(
    true,
  );
  expect(queries.every((q) => !q.searchParams.has("grain"))).toBe(true);
});

test("history selects real GPU CPU memory and load metrics through database history", async ({
  page,
}) => {
  const calls: string[] = [];
  await page.route("**/api/v1/resource-statistics/trends", async (r) => {
    const q = r.request().postDataJSON();
    calls.push(q.metric);
    const value =
      q.metric === "gpu_util"
        ? 42
        : q.metric === "cpu_utilization_pct"
          ? 25
          : 1024;
    await r.fulfill({
      json: {
        mode: "mock",
        units: { capacity: "MB", allocation: "MB" },
        window: q.window,
        start: 1793232000,
        end: 1793235600,
        step: 60,
        fetched_at: 1793235600,
        complete: false,
        cache_status: "miss",
        cache_age_seconds: 0,
        series: [
          {
            id: "yz",
            label: "亦庄",
            status: "degraded",
            warnings: [],
            points: [
              {
                timestamp: 1793232000,
                value: null,
                allocated_raw: null,
                unallocated_raw: null,
                capacity_raw: null,
                allocation_ratio: null,
              },
              {
                timestamp: 1793232060,
                value,
                allocated_raw: null,
                unallocated_raw: null,
                capacity_raw: null,
                allocation_ratio: null,
              },
            ],
          },
        ],
      },
    });
  });
  await page.goto("/#analysis?view=history");
  const selector = page.getByLabel("历史趋势指标");
  await selector.selectOption("gpu_util");
  const trend = page.getByRole("region", { name: "集群历史趋势" });
  await expect(trend.getByRole("heading")).toHaveText("GPU 利用率趋势");
  await expect(trend.locator("output")).toContainText("42.0%");
  await selector.selectOption("cpu_utilization_pct");
  await expect(trend.locator("output")).toContainText("25.0%");
  await selector.selectOption("memory_used_bytes");
  await expect(trend.locator("output")).toContainText("GB");
  await selector.selectOption("load1");
  await expect(trend.locator("output")).toContainText("1024.00");
  expect(calls).toEqual([
    "gpu_util",
    "cpu_utilization_pct",
    "memory_used_bytes",
    "load1",
  ]);
});
