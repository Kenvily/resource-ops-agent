import { test, expect } from "@playwright/test";
const base = process.env.EFFICIENCY_TEST_BASE_URL;
test("machine detail reads PostgreSQL host facts, keeps units and supports scoped history", async ({
  page,
}) => {
  test.skip(!base, "requires isolated Mock PostgreSQL");
  expect(
    (await (await page.request.get(`${base}/api/health`)).json()).mode,
  ).toBe("mock");
  await page.goto(`${base}/#assets?view=machines`);
  const open = page
    .getByRole("button", { name: "服务器详情", exact: true })
    .first();
  await expect(open).toBeVisible();
  await open.click();
  const dialog = page.getByRole("dialog", { name: "机器资源详情" });
  await expect(
    dialog.getByRole("heading", { name: "文件系统", exact: true }),
  ).toBeVisible();
  await expect(dialog).toContainText("1,000 GB");
  await expect(dialog).toContainText("2 MB/s");
  await expect(dialog).not.toContainText("0 GB/s");
  await expect(
    dialog.getByRole("img", { name: "主机资源历史；缺测处断开" }),
  ).toBeVisible();
  await dialog.getByLabel("主机历史指标").selectOption("memory_used_bytes");
  await expect(dialog.getByRole("img")).toBeVisible();
  await dialog.getByLabel("主机历史时间范围").focus();
  await dialog.getByLabel("主机历史时间范围").selectOption("1d");
  await expect(dialog.getByLabel("主机历史时间范围")).toBeFocused();
  await expect(dialog.getByRole("img")).toBeVisible();
  await dialog.getByText("历史采样与覆盖", { exact: true }).click();
  await expect(
    dialog.getByRole("columnheader", { name: "有效采样" }),
  ).toBeVisible();
  await expect(
    dialog.getByText("0/5 · 0.0%", { exact: true }).first(),
  ).toBeVisible();
  await dialog.getByText("历史采样与覆盖", { exact: true }).click();
  await dialog.evaluate((el) => {
    el.scrollTop = 0;
  });
  await page.setViewportSize({ width: 375, height: 812 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  const before = page.url();
  const brand = await page.locator(".brand").boundingBox();
  if (brand)
    await page.mouse.click(
      brand.x + brand.width / 2,
      brand.y + brand.height / 2,
    );
  expect(page.url()).toBe(before);
  await page.evaluate(() => {
    (document.activeElement as HTMLElement)?.blur();
    window.scrollTo(0, 0);
  });
  await dialog.evaluate((el) => {
    el.scrollTop = 0;
  });
  await page.screenshot({
    path: test.info().outputPath("mock-host-resources-375.png"),
    fullPage: false,
  });
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(open).toBeFocused();
});

test("host API failure stays visible and leaves GPU details accessible", async ({
  page,
}) => {
  const aggregate = {
    gpu_count: 1,
    machine_count: 1,
    hami_capacity_raw: 48000,
    allocated_raw: 12000,
    unallocated_raw: 36000,
    physical_used_mib: null,
    physical_free_mib: null,
    physical_observed_total_mib: null,
    physical_usage_ratio: null,
    utilization_pct: null,
    usage_observed_gpus: 0,
  };
  await page.route("**/api/v1/resource-statistics", (r) =>
    r.fulfill({
      json: {
        mode: "mock",
        database_read_at: 1700000000,
        complete: false,
        units: { capacity: "MB", allocation: "MB" },
        sources: [],
        summary: aggregate,
        devices: [],
        machines: [{ ...aggregate, cluster: "yz", name: "mock-host" }],
        gpu_models: [],
        business_models: [],
        limitations: [],
      },
    }),
  );
  await page.route("**/api/v1/resource-statistics/host", (r) =>
    r.fulfill({ status: 503, json: { detail: "主机资源数据库暂不可用" } }),
  );
  await page.goto("/#assets?view=machines");
  await page.getByRole("button", { name: "服务器详情", exact: true }).click();
  await expect(page.getByRole("dialog").getByRole("alert")).toContainText(
    "暂不可用",
  );
  await page.keyboard.press("Escape");
  await expect(
    page.getByRole("button", { name: "容器资源", exact: true }),
  ).toBeVisible();
});

test("hourly host samples show point values without fabricated means or extrema", async ({
  page,
}) => {
  test.skip(!base, "requires isolated Mock PostgreSQL");
  await page.route("**/api/v1/resource-statistics/host", async (route) => {
    const query = route.request().postDataJSON();
    expect(query.history_mode).toBe("auto");
    await route.fulfill({
      json: {
        current: null,
        database_read_at: 1793870000,
        collection_enabled: true,
        history: {
          start: 1793808000,
          end: 1793811600,
          grain: 3600,
          source_grain: 3600,
          sampling: "hourly_point",
          series: [
            {
              metric: "cpu_utilization_pct",
              label: "CPU 利用率",
              unit: "%",
              points: [
                {
                  timestamp: 1793808000,
                  sampled_value: 37,
                  mean: null,
                  min: null,
                  max: null,
                  valid_points: 1,
                  expected_points: 1,
                  coverage: 1,
                },
              ],
            },
          ],
        },
        definitions: {},
      },
    });
  });
  await page.goto(`${base}/#assets?view=servers`);
  await page
    .getByRole("button", { name: "服务器详情", exact: true })
    .first()
    .click();
  const dialog = page.getByRole("dialog", { name: "机器资源详情" });
  await expect(dialog).toContainText("时点采样");
  await expect(
    dialog.getByRole("img", { name: "主机资源历史；缺测处断开" }),
  ).toBeVisible();
  await dialog.getByText("历史采样与覆盖", { exact: true }).click();
  await expect(
    dialog.getByRole("columnheader", { name: "采样值", exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByRole("cell", { name: "37%", exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByRole("cell", { name: "— / —", exact: true }),
  ).toBeVisible();
});
