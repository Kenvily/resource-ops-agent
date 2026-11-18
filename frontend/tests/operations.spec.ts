import { test, expect } from "@playwright/test";

test.beforeEach(async ({ request }) => {
  expect((await (await request.get("/api/health")).json()).mode).toBe("mock");
});
const period = { start: 1793232000, end: 1793235600, grain: 300 };
const metrics = {
  complete: true,
  expected_points: 12,
  verified_coverage_points: 12,
  allocated_mean_raw: 12000,
  capacity_mean_raw: 48000,
  allocated_max_raw: 18000,
  ratio_mean: 0.25,
};
const change = {
  entity: [],
  baseline: metrics,
  target: { ...metrics, allocated_mean_raw: 18000 },
  allocated_change_raw: 6000,
  capacity_change_raw: 0,
};
const report = {
  mode: "mock",
  generated_at: 1793235700,
  window: "1d",
  baseline: { ...period, start: 1793145600, end: 1793149200 },
  target: period,
  units: { allocation: "MB", capacity: "MB" },
  clusters: [
    {
      cluster: "yz",
      current: {
        status: "healthy",
        sampled_at: 1793235650,
        gpu_count: 2,
        allocated_raw: 18000,
        capacity_raw: 48000,
      },
      comparison: {
        complete: true,
        changes: [change],
        warnings: [],
        omitted_entities: 0,
      },
      namespace_changes: {
        complete: true,
        changes: [{ ...change, entity: ["mock-team"] }],
        warnings: [],
        omitted_entities: 0,
      },
      warnings: [],
    },
  ],
  metrics: [],
  alerts: [],
  alert_count: 0,
  review_status: "draft",
  notes: ["缺测不生成增长结论。"],
};
const monitoring = {
  mode: "mock",
  database_read_at: 1793235700,
  collection_enabled: true,
  health: "unknown",
  alert_count: 1,
  alerts: [
    {
      cluster: "yz",
      metric: "gpu_temp",
      node: "mock-node",
      device_uuid: "GPU-MOCK",
      message: "GPU 温度达到 90 °C（阈值 85 °C）",
      evaluated_at: 1793235600,
    },
  ],
  truncated: false,
  temperature_threshold_c: 85,
  metrics: [
    {
      cluster: "yz",
      key: "gpu_temp",
      source: "DCGM_FI_DEV_GPU_TEMP",
      label: "GPU 温度",
      unit: "°C",
      status: "ready",
      evaluated_at: 1793235600,
      expected_entities: 1,
      values: [{ node: "mock-node", device_uuid: "GPU-MOCK", value: 90 }],
    },
    {
      cluster: "yz",
      key: "gpu_ecc",
      source: "DCGM_FI_DEV_ECC_DBE_VOL_TOTAL",
      label: "累计双位 ECC 错误",
      unit: "次",
      status: "missing",
      evaluated_at: 1793235600,
      expected_entities: 1,
      values: [],
    },
  ],
};

test("report is generated manually, reviewed and exported without persistence", async ({
  page,
}) => {
  let calls = 0;
  let overviewCalls = 0;
  page.on("request", (r) => {
    if (r.url().includes("/api/v1/overview")) overviewCalls++;
  });
  await page.route("**/api/v1/operations/reports", (r) => {
    calls++;
    expect(r.request().postDataJSON().window).toBe("1d");
    return r.fulfill({ json: report });
  });
  await page.goto("/#reports");
  expect(calls).toBe(0);
  await page.getByRole("button", { name: "生成报告", exact: true }).click();
  await expect(page.getByText("待核对草稿", { exact: false })).toBeVisible();
  const download = page.getByRole("button", {
    name: "导出 Markdown",
    exact: true,
  });
  await expect(download).toBeDisabled();
  await page.getByLabel("我已核对报告的时间范围、数据缺口与结论").check();
  await expect(download).toBeEnabled();
  const downloaded = page.waitForEvent("download");
  await download.click();
  expect((await downloaded).suggestedFilename()).toBe("gpu-operations-1d.md");
  await page.getByLabel("复核说明（可选）").fill("需确认后续业务变化");
  await expect(download).toBeDisabled();
  await page.getByLabel("报告时段").selectOption("1w");
  await expect(download).toHaveCount(0);
  expect(calls).toBe(1);
  expect(overviewCalls).toBe(0);
});

test("missing report history never fabricates zero growth", async ({
  page,
}) => {
  const missing = {
    ...report,
    clusters: [
      {
        ...report.clusters[0],
        comparison: {
          complete: false,
          changes: [
            {
              ...change,
              allocated_change_raw: null,
              target: { ...metrics, complete: false, allocated_mean_raw: null },
            },
          ],
          warnings: ["历史尚未同步"],
          omitted_entities: 0,
        },
        namespace_changes: null,
      },
    ],
  };
  await page.route("**/api/v1/operations/reports", (r) =>
    r.fulfill({ json: missing }),
  );
  await page.goto("/#reports");
  await page.getByRole("button", { name: "生成报告", exact: true }).click();
  await expect(
    page.getByText("历史证据不足，暂不判断时段变化", { exact: true }),
  ).toBeVisible();
  await expect(
    page
      .locator(".efficiency-evidence > div")
      .filter({ hasText: "较前一时段变化" }),
  ).toContainText("—");
});

test("report cancellation cannot publish late data", async ({ page }) => {
  await page.route("**/api/v1/operations/reports", async (r) => {
    await new Promise((resolve) => setTimeout(resolve, 300));
    await r.fulfill({ json: report }).catch(() => {});
  });
  await page.goto("/#reports");
  await page.getByRole("button", { name: "生成报告", exact: true }).click();
  await page.getByRole("button", { name: "取消", exact: true }).click();
  await page.waitForTimeout(400);
  await expect(page.locator(".operations-report")).toHaveCount(0);
});

test("monitoring distinguishes known alerts from missing metrics and refreshes database only", async ({
  page,
}) => {
  let calls = 0;
  await page.route("**/api/v1/operations/monitoring?*", (r) => {
    calls++;
    return r.fulfill({ json: monitoring });
  });
  await page.goto("/#monitoring");
  await expect(page.getByText("部分指标未知", { exact: false })).toBeVisible();
  await page.getByText("指标接入与覆盖", { exact: true }).click();
  await expect(page.getByText("无新鲜序列", { exact: true })).toBeVisible();
  await expect(
    page.getByText("GPU 温度达到 90", { exact: false }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "刷新数据库数据", exact: true })
    .click();
  await expect.poll(() => calls).toBe(2);
});

test("infrastructure evaluation is manual and shows measured latency rather than installing components", async ({
  page,
}) => {
  let calls = 0;
  await page.route("**/api/v1/operations/monitoring?*", (r) =>
    r.fulfill({ json: monitoring }),
  );
  await page.route("**/api/v1/operations/infrastructure", (r) => {
    calls++;
    return r.fulfill({
      json: {
        observed_at: 1793235600,
        telemetry: {
          uptime_seconds: 120,
          upstream_response_bytes: 1024,
          concurrency: { upstream_active: { current: 0, peak: 4 } },
          metrics: [
            {
              domain: "http",
              label: "application",
              operation: "/api/v1/overview",
              count: 40,
              recent_samples: 40,
              errors: 0,
              p95_ms: 20,
              p50_ms: 10,
              error_rate: 0,
            },
          ],
        },
        database: {
          database_bytes: 100e6,
          connections: 4,
          lock_waiters: 0,
          active_queries: 1,
          tables: [],
        },
        recommendations: [
          {
            component: "共享缓存",
            status: "insufficient",
            reason: "缺少跨实例热点证据",
          },
        ],
        limitations: ["进程内统计，不能代表生产负载"],
      },
    });
  });
  await page.goto("/#monitoring");
  await page.getByRole("link", { name: "查看运行评估", exact: true }).click();
  expect(calls).toBe(0);
  await page.getByRole("button", { name: "获取运行评估", exact: true }).click();
  await expect(
    page.getByText("共享缓存 · 样本不足", { exact: true }),
  ).toBeVisible();
  await page.getByText("延迟、上游压力与数据库明细", { exact: true }).click();
  await expect(
    page.getByRole("cell", { name: "20 ms", exact: true }),
  ).toBeVisible();
  expect(calls).toBe(1);
});

test("operations pages remain usable on narrow screens", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.route("**/api/v1/operations/reports", (r) =>
    r.fulfill({ json: report }),
  );
  await page.goto("/#reports");
  await page.getByRole("button", { name: "生成报告", exact: true }).click();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.evaluate(() => {
    if (document.activeElement instanceof HTMLElement)
      document.activeElement.blur();
  });
  await expect(
    page.getByRole("heading", { name: "资源运营报告", exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: test.info().outputPath("mock-report-375.png"),
    fullPage: true,
  });
  await page.route("**/api/v1/operations/monitoring?*", (r) =>
    r.fulfill({ json: monitoring }),
  );
  await page.getByRole("link", { name: "指标告警", exact: true }).click();
  await expect(page.getByText("部分指标未知", { exact: false })).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.evaluate(() => {
    if (document.activeElement instanceof HTMLElement)
      document.activeElement.blur();
  });
  await page.screenshot({
    path: test.info().outputPath("mock-monitoring-375.png"),
    fullPage: true,
  });
});
