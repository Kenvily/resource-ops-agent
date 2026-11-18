import { test, expect } from "@playwright/test";

test.beforeEach(async ({ request }) => {
  expect((await (await request.get("/api/health")).json()).mode).toBe("mock");
});
const device = {
  cluster: "yz",
  node: "mock-node",
  device_uuid: "GPU-MOCK-1",
  classification: "memory_resident",
  screening_status: "low_utilization",
  coverage: 1,
  usage_coverage: 1,
  valid_points: 61,
  usage_valid_points: 61,
  expected_points: 61,
  sustained_minutes: 0,
  current_allocated_raw: 18000,
  allocation_unit: "MB",
  mean_utilization_pct: 0.04,
  max_utilization_pct: 2,
  mean_physical_memory_bytes: 12e9,
  limitations: [],
  workloads: [],
  workloads_truncated: false,
};
const report = {
  mode: "mock",
  database_read_at: 1793237400,
  start: 1793233800,
  end: 1793237400,
  result_count: 2,
  truncated: false,
  limitations: [],
  clusters: [
    {
      cluster: "yz",
      device_count: 2,
      candidate_count: 0,
      collected_points: 61,
      expected_points: 61,
      latest_collection_at: 1793237400,
      warnings: [],
      steps: [],
    },
  ],
  results: [
    device,
    {
      ...device,
      device_uuid: "GPU-MOCK-2",
      screening_status: "insufficient",
      usage_coverage: 0.5,
      usage_valid_points: 30,
      mean_utilization_pct: null,
      max_utilization_pct: null,
    },
  ],
};
async function mock(page: import("@playwright/test").Page, value = report) {
  await page.route("**/api/v1/efficiency/capabilities", (route) =>
    route.fulfill({ json: { available: true } }),
  );
  await page.route("**/api/v1/efficiency/investigate", (route) =>
    route.fulfill({ json: value }),
  );
}

test("mean threshold screening is simple, precise and independent of lifecycle classifications", async ({
  page,
}) => {
  await mock(page);
  await page.goto("/#efficiency");
  await expect(page.getByText(/时段平均整卡利用率 ≤ 10%/)).toBeVisible();
  await page.getByRole("button", { name: "开始核查", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "低利用率 GPU 1 张", exact: true }),
  ).toBeVisible();
  const card = page.locator(".assessment-candidate");
  await expect(card).toHaveCount(1);
  await expect(card).toContainText("0.04%");
  await expect(card).not.toContainText("0.0%");
  await expect(card).toContainText("时段峰值利用率");
  await expect(
    page.getByText("关联业务与生命周期", { exact: true }),
  ).toHaveCount(0);
  await expect(page.getByText("人工复核", { exact: true })).toHaveCount(0);
  await page.getByLabel("显示全部 GPU").check();
  await expect(card).toHaveCount(2);
  await expect(card.filter({ hasText: "GPU-MOCK-2" })).toContainText(
    "采样或分配数据不足",
  );
  await page.getByLabel("整卡利用率上限（%）", { exact: true }).fill("5");
  await expect(card).toHaveCount(0);
});

test("zero and tiny nonzero utilization remain distinguishable", async ({
  page,
}) => {
  await mock(page, {
    ...report,
    results: [
      { ...device, mean_utilization_pct: 0.001, max_utilization_pct: 0.061 },
      {
        ...device,
        device_uuid: "GPU-MOCK-2",
        mean_utilization_pct: 0,
        max_utilization_pct: 0,
      },
    ],
  });
  await page.goto("/#efficiency");
  await page.getByRole("button", { name: "开始核查", exact: true }).click();
  await expect(
    page.locator(".assessment-candidate").filter({ hasText: "GPU-MOCK-1" }),
  ).toContainText("<0.01%");
  await expect(
    page
      .locator(".assessment-candidate")
      .filter({ hasText: "GPU-MOCK-2" })
      .getByText("0%", { exact: true }),
  ).toHaveCount(2);
});

test("machine and GPU details stay compact and usable on mobile", async ({
  page,
}) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await mock(page);
  await page.goto("/#efficiency");
  await page.getByRole("button", { name: "开始核查", exact: true }).click();
  const machine = page.getByRole("region", {
    name: "亦庄 · 机器 mock-node",
    exact: true,
  });
  const trigger = machine.getByRole("button", {
    name: "查看机器详情",
    exact: true,
  });
  await trigger.click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: /GPU-MOCK-1/ })
    .click();
  const dialog = page.getByRole("dialog", {
    name: "GPU 占用核查详情",
    exact: true,
  });
  await expect(dialog).toContainText("观察末点已分配");
  await expect(dialog).toContainText("时段峰值利用率");
  await expect(dialog).toContainText("利用率有效采样");
  await expect(dialog).toContainText("当前额度采样：暂无记录");
  await expect(dialog).not.toContainText("任务阶段");
  expect(await dialog.evaluate((e) => e.scrollWidth <= e.clientWidth)).toBe(
    true,
  );
  await page.screenshot({
    path: test.info().outputPath("mock-efficiency-detail-375.png"),
  });
  await page.keyboard.press("Escape");
  await expect(trigger).toBeFocused();
  await page.evaluate(() => {
    if (document.activeElement instanceof HTMLElement)
      document.activeElement.blur();
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: test.info().outputPath("mock-efficiency-375.png"),
    fullPage: true,
  });
});

test("current allocation matches resource details while historical allocation stays labeled", async ({
  page,
  request,
}) => {
  const overview = await (await request.get("/api/v1/overview")).json();
  const gpu = overview.clusters[0].gpus[0];
  await mock(page, {
    ...report,
    results: [
      {
        ...device,
        cluster: gpu.cluster,
        node: gpu.node,
        device_uuid: gpu.device_uuid,
      },
    ],
  });
  await page.goto("/#efficiency");
  await page.getByRole("button", { name: "开始核查", exact: true }).click();
  const value = await page
    .locator(".assessment-candidate .efficiency-evidence > div")
    .filter({ hasText: "当前已分配" })
    .locator("strong")
    .innerText();
  await page
    .getByRole("button", { name: "查看 GPU 详情", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("当前额度采样");
  await expect(
    dialog
      .locator(".efficiency-evidence > div")
      .filter({ hasText: "当前已分配" }),
  ).toContainText(value);
  await dialog
    .getByRole("button", { name: "查看当前资源详情", exact: true })
    .click();
  const resource = page.getByRole("dialog", { name: "GPU 详情", exact: true });
  await expect(resource).toContainText(gpu.device_uuid);
  // Both places use the same Overview GPU, raw allocation and unit formatter.
  await expect(resource).toContainText(value);
});

test("streamed model result shows a concise status and never requests review metadata", async ({
  page,
}) => {
  const metadata: string[] = [];
  page.on("request", (r) => {
    if (/efficiency\/(reviews|evaluation|reports)/.test(r.url()))
      metadata.push(r.url());
  });
  await page.route("**/api/v1/efficiency/capabilities", (r) =>
    r.fulfill({ json: { available: true, supports_agent: true } }),
  );
  await page.route("**/api/v1/efficiency/diagnose", (r) =>
    r.fulfill({
      contentType: "application/x-ndjson",
      body:
        JSON.stringify({
          type: "result",
          data: {
            ...report,
            run_id: "mock-run",
            agent: {
              mode: "deepseek",
              model_calls: 3,
              tool_calls: 8,
              investigated_devices: 2,
              warnings: [],
              evidence: [],
            },
          },
        }) + "\n",
    }),
  );
  await page.goto("/#efficiency");
  await page.getByRole("button", { name: "开始核查", exact: true }).click();
  await page.getByText("查询信息", { exact: true }).click();
  await expect(
    page.getByText("DeepSeek 已重点核查 2 张 GPU", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "查看 GPU 详情", exact: true })
    .click();
  await expect(
    page.getByText("容器使用与任务阶段核查", { exact: true }),
  ).toHaveCount(0);
  expect(metadata).toEqual([]);
});

test("disabled source and interrupted streams do not fabricate results", async ({
  page,
}) => {
  await page.route("**/api/v1/efficiency/capabilities", (r) =>
    r.fulfill({ json: { available: false } }),
  );
  await page.goto("/#efficiency");
  await expect(
    page.getByRole("button", { name: "开始核查", exact: true }),
  ).toBeDisabled();
  await page.route("**/api/v1/efficiency/capabilities", (r) =>
    r.fulfill({ json: { available: true, supports_agent: true } }),
  );
  await page.route("**/api/v1/efficiency/diagnose", (r) =>
    r.fulfill({
      contentType: "application/x-ndjson",
      body:
        JSON.stringify({ type: "progress", data: { message: "读取证据" } }) +
        "\n",
    }),
  );
  await page.reload();
  await page.getByRole("button", { name: "开始核查", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("未收到完整结果");
  await expect(page.locator(".assessment-candidate")).toHaveCount(0);
});

test("cancelled requests cannot publish a late result", async ({ page }) => {
  await mock(page);
  await page.route("**/api/v1/efficiency/investigate", async (r) => {
    await new Promise((resolve) => setTimeout(resolve, 300));
    await r.fulfill({ json: report }).catch(() => {});
  });
  await page.goto("/#efficiency");
  await page.getByRole("button", { name: "开始核查", exact: true }).click();
  await page.getByRole("button", { name: "取消", exact: true }).click();
  await page.waitForTimeout(400);
  await expect(page.locator(".assessment-candidate")).toHaveCount(0);
});

test("usage facts are queried from isolated PostgreSQL", async ({ page }) => {
  const base = process.env.EFFICIENCY_TEST_BASE_URL;
  test.skip(!base, "requires isolated Mock PostgreSQL");
  await page.goto(`${base}/#efficiency`);
  await page.getByRole("button", { name: "开始核查", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: /低利用率 GPU [1-9]\d* 张/ }),
  ).toBeVisible();
  await page
    .locator(".assessment-candidate")
    .first()
    .getByRole("button", { name: "查看 GPU 详情", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toContainText("利用率有效采样");
});
