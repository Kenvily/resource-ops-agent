import { test, expect } from "@playwright/test";

test("assessment constraints, evidence and form invalidation", async ({
  page,
  request,
}) => {
  expect((await (await request.get("/api/health")).json()).mode).toBe("mock");
  let payload: Record<string, unknown> = {};
  await page.route("**/api/v1/insights/assess", async (route) => {
    payload = route.request().postDataJSON();
    await route.fulfill({
      json: {
        mode: "mock",
        database_read_at: 1700000000,
        candidate_count: 1,
        truncated: false,
        history_window: { start: 1700000000, end: 1700003600, grain: 60 },
        clusters: [
          {
            cluster: "yz",
            status: "healthy",
            eligible_gpu_count: 2,
            warnings: [],
            source_sampled_at: 1700000000,
            snapshot_stored_at: 1700000000,
          },
        ],
        candidates: [
          {
            cluster: "yz",
            nodes: ["mock-node"],
            risk: "unknown",
            gpus: [
              {
                cluster: "yz",
                node: "mock-node",
                device_uuid: "mock-gpu",
                unallocated_bytes: 25e9,
                historical_min_bytes: null,
                coverage: 0.5,
                observed_points: 30,
                expected_points: 60,
              },
            ],
          },
        ],
        limitations: ["仅初筛虚拟显存额度，不能保证部署成功"],
      },
    });
  });
  await page.goto("/#assessment");
  await page.getByLabel("GPU 数量", { exact: true }).fill("2");
  await expect(page.getByLabel("需求单位", { exact: true })).toHaveCount(0);
  await page.getByRole("combobox", { name: "容量显示单位" }).selectOption("MB");
  await expect(page.getByLabel("每卡显存（MB）", { exact: true })).toHaveValue(
    "20000",
  );
  await page.getByRole("button", { name: "开始评估" }).click();
  await expect(page.getByText("找到 1 个额度组合")).toBeVisible();
  await expect(
    page.locator(".assessment-risk").getByText("历史证据不足", { exact: true }),
  ).toBeVisible();
  await page.locator(".planning-device-details summary").click();
  await expect(page.getByText("30 / 60 · 50.0%")).toBeVisible();
  expect(payload.memory_unit).toBe("MB");
  expect(payload.memory_per_gpu).toBe(20000);
  expect(payload.gpu_count).toBe(2);
  await page.getByLabel("每卡显存（MB）", { exact: true }).fill("30");
  await expect(page.getByText("找到 1 个额度组合")).toHaveCount(0);
});

test("assessment mobile layout and database errors", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.route("**/api/v1/insights/assess", (route) =>
    route.fulfill({
      status: 503,
      json: { detail: "部署评估需要已配置的 PostgreSQL 数据库" },
    }),
  );
  await page.goto("/#assessment");
  await page.getByRole("button", { name: "开始评估" }).click();
  await expect(page.getByRole("alert")).toContainText("PostgreSQL");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await expect(page.getByLabel("节点名称（可选）")).not.toBeVisible();
  await page.getByText("高级选项", { exact: true }).click();
  await expect(page.getByLabel("节点名称（可选）")).toBeVisible();
});

for (const [unit, hours] of [
  ["hour", 2],
  ["day", 48],
  ["week", 336],
  ["month", 1440],
] as const) {
  test(`assessment converts ${unit} into hours`, async ({ page }) => {
    await page.route("**/api/v1/insights/assess", (route) =>
      route.fulfill({
        json: {
          mode: "mock",
          database_read_at: 1700000000,
          candidate_count: 0,
          truncated: false,
          history_window: { start: 1700000000, end: 1700003600, grain: 60 },
          clusters: [],
          candidates: [],
          limitations: [],
        },
      }),
    );
    await page.goto("/#assessment");
    await page.getByText("高级选项", { exact: true }).click();
    await page.getByLabel("预计运行时长（记录用）", { exact: true }).fill("2");
    await page
      .getByRole("combobox", { name: "运行时长单位", exact: true })
      .selectOption(unit);
    const sent = page.waitForRequest((request) =>
      request.url().endsWith("/api/v1/insights/assess"),
    );
    await page.getByRole("button", { name: "开始评估", exact: true }).click();
    expect((await sent).postDataJSON().duration_hours).toBe(hours);
    await expect(
      page.getByRole("heading", {
        name: "暂无符合条件的额度组合",
        exact: true,
      }),
    ).toBeVisible();
  });
}

test("planning progressively reveals options and compares explicit selections without extra queries", async ({
  page,
  request,
}) => {
  expect((await (await request.get("/api/health")).json()).mode).toBe("mock");
  let calls = 0;
  await page.route("**/api/v1/insights/assess", async (r) => {
    calls++;
    const requirement = r.request().postDataJSON();
    const candidates = Array.from({ length: 4 }, (_, i) => ({
      cluster: i === 1 ? "zw" : "yz",
      nodes: [`mock-server-${i + 1}`],
      risk:
        i === 1
          ? "unknown"
          : i === 2
            ? "historical_shortfall"
            : "within_observed",
      minimum_headroom_bytes: (15 + i) * 1e9,
      gpus: [1, 2].map((n) => ({
        cluster: i === 1 ? "zw" : "yz",
        node: `mock-server-${i + 1}`,
        device_uuid: `GPU-MOCK-${i}-${n}`,
        capacity_bytes: 80e9,
        allocated_bytes: 45e9 - i * 1e9,
        unallocated_bytes: (35 + i) * 1e9,
        historical_min_bytes: i === 1 ? null : i === 2 ? 10e9 : 30e9,
        coverage: i === 1 ? 0.5 : 1,
        expected_points: 288,
        observed_points: i === 1 ? 144 : 288,
      })),
    }));
    await r.fulfill({
      json: {
        request: requirement,
        required_bytes_per_gpu: 20e9,
        mode: "mock",
        database_read_at: 1700000000,
        candidate_count: 4,
        truncated: false,
        history_window: { start: 1699913600, end: 1700000000, grain: 300 },
        clusters: [
          {
            cluster: "yz",
            status: "healthy",
            eligible_gpu_count: 8,
            warnings: [],
          },
          {
            cluster: "zw",
            status: "healthy",
            eligible_gpu_count: 2,
            warnings: [],
          },
        ],
        candidates,
        limitations: ["仅初筛 HAMi 虚拟显存额度，不保证部署成功。"],
      },
    });
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/#planning");
  await expect(
    page.locator(".planning-request-form input:visible"),
  ).toHaveCount(2);
  await expect(page.getByLabel("历史观察窗口")).not.toBeVisible();
  await page.getByRole("button", { name: "开始评估", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "找到 4 个额度组合" }),
  ).toBeVisible();
  await expect(page.locator(".planning-device-details[open]")).toHaveCount(0);
  await expect(page.getByRole("region", { name: "候选方案对比" })).toHaveCount(
    0,
  );
  await expect(
    page
      .getByRole("article", { name: "候选 2", exact: true })
      .locator(".planning-card-metrics"),
  ).toContainText("—");
  await page.screenshot({
    path: test.info().outputPath("mock-planning-cards-1440.png"),
    fullPage: true,
  });
  await page.getByLabel("选择候选 1", { exact: true }).check();
  await expect(
    page.getByRole("button", { name: "对比方案", exact: true }),
  ).toBeDisabled();
  await page.getByLabel("选择候选 2", { exact: true }).check();
  await page.getByRole("button", { name: "对比方案", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "候选方案对比" }).locator("tbody tr"),
  ).toHaveCount(2);
  await page.getByLabel("选择候选 3", { exact: true }).check();
  await expect(page.getByLabel("选择候选 4", { exact: true })).toBeDisabled();
  await page.getByLabel("选择候选 2", { exact: true }).uncheck();
  await expect(page.getByLabel("选择候选 4", { exact: true })).toBeEnabled();
  await page.getByRole("combobox", { name: "容量显示单位" }).selectOption("MB");
  await expect(page.locator(".planning-candidate").first()).toContainText(
    "35,000 MB",
  );
  expect(calls).toBe(1);
  for (const width of [768, 375]) {
    await page.setViewportSize({ width, height: 900 });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await page.screenshot({
      path: test.info().outputPath(`mock-planning-cards-${width}.png`),
      fullPage: true,
    });
  }
  await page.getByText("高级选项", { exact: true }).click();
  await page.getByLabel("历史观察窗口").selectOption("168");
  await expect(
    page.getByRole("heading", { name: "找到 4 个额度组合" }),
  ).toHaveCount(0);
});

test("unavailable planning sources never display zero as available resources", async ({
  page,
  request,
}) => {
  expect((await (await request.get("/api/health")).json()).mode).toBe("mock");
  await page.route("**/api/v1/insights/assess", (r) =>
    r.fulfill({
      json: {
        mode: "mock",
        database_read_at: 1700000000,
        candidate_count: 0,
        truncated: false,
        history_window: { start: 1699913600, end: 1700000000, grain: 300 },
        clusters: [
          {
            cluster: "yz",
            status: "stale",
            eligible_gpu_count: 0,
            warnings: ["快照已过期，请检查采集状态。"],
          },
        ],
        candidates: [],
        limitations: [],
      },
    }),
  );
  await page.goto("/#planning");
  await page.getByRole("button", { name: "开始评估", exact: true }).click();
  await expect(
    page.getByText("数据暂不可用于评估", { exact: true }),
  ).toBeVisible();
  await expect(page.locator(".planning-source")).not.toContainText(
    "符合单卡要求 0 张",
  );
  await expect(
    page.getByRole("heading", { name: "等待有效数据后重新评估" }),
  ).toBeVisible();
});

test("cancelling a planning query cannot resurrect a late result", async ({
  page,
  request,
}) => {
  expect((await (await request.get("/api/health")).json()).mode).toBe("mock");
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/v1/insights/assess", async (r) => {
    await gate;
    await r
      .fulfill({
        json: {
          mode: "mock",
          database_read_at: 1700000000,
          candidate_count: 0,
          truncated: false,
          history_window: { start: 1699913600, end: 1700000000, grain: 300 },
          clusters: [],
          candidates: [],
          limitations: [],
        },
      })
      .catch(() => {});
  });
  await page.goto("/#planning");
  const sent = page.waitForRequest((r) =>
    r.url().endsWith("/api/v1/insights/assess"),
  );
  await page.getByRole("button", { name: "开始评估", exact: true }).click();
  await sent;
  await page.getByRole("button", { name: "取消", exact: true }).click();
  release();
  await expect(
    page.getByRole("heading", { name: "查看适合需求的 GPU 额度组合" }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "暂无符合条件的额度组合" }),
  ).toHaveCount(0);
  await expect(page.getByRole("alert")).toHaveCount(0);
});
