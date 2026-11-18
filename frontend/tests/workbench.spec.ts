import { test, expect, type Page } from "@playwright/test";

test.beforeEach(async ({ request }) => {
  expect((await (await request.get("/api/health")).json()).mode).toBe("mock");
});
async function signals(page: Page, resources: any, failed = false) {
  const gpus = resources.clusters.flatMap((c: any) => c.gpus);
  const devices = gpus.map((g: any, i: number) => ({
    ...g,
    gpu_model: "MOCK-ACCELERATOR",
    driver_version: "MOCK-DRIVER",
    utilization_pct: 40,
    physical_used_mib: i < 4 ? 0 : 1024,
    physical_free_mib: i < 4 ? 24024 : 23000,
    physical_observed_total_mib: 24024,
  }));
  const metrics = resources.clusters.flatMap((c: any) => [
    {
      cluster: c.id,
      key: "gpu_temp",
      label: "GPU 温度",
      unit: "°C",
      source: "DCGM_FI_DEV_GPU_TEMP",
      status: failed ? "error" : "ready",
      evaluated_at: Date.now() / 1000,
      expected_entities: c.gpus.length,
      values: failed
        ? []
        : c.gpus.map((g: any, i: number) => ({
            node: g.node,
            device_uuid: g.device_uuid,
            value: i ? 45 : 90,
          })),
    },
    {
      cluster: c.id,
      key: "node_ready",
      label: "节点 Ready",
      unit: "状态",
      source: "kube_node_status_condition",
      status: failed ? "missing" : "ready",
      evaluated_at: Date.now() / 1000,
      expected_entities: resources.nodes.filter((n: any) => n.cluster === c.id)
        .length,
      values: failed
        ? []
        : resources.nodes
            .filter((n: any) => n.cluster === c.id)
            .map((n: any, i: number) => ({
              node: n.node,
              device_uuid: null,
              value: i ? 1 : 0,
            })),
    },
  ]);
  let metricCalls = 0;
  await page.route("**/api/v1/resource-statistics", (r) =>
    failed
      ? r.fulfill({ status: 503, json: { detail: "不可用" } })
      : r.fulfill({
          json: {
            mode: "mock",
            database_read_at: Date.now() / 1000,
            complete: true,
            units: resources.units,
            sources: resources.clusters.map((c: any) => ({
              cluster: c.id,
              status: c.status,
            })),
            machines: [],
            gpu_models: [],
            business_models: [],
            limitations: [],
            devices,
            summary: {
              gpu_count: gpus.length,
              machine_count: resources.nodes.length,
              utilization_pct: 40,
              usage_observed_gpus: gpus.length,
              physical_used_mib: devices.reduce(
                (sum: any, d: any) => sum + d.physical_used_mib,
                0,
              ),
              physical_observed_total_mib: 24024 * gpus.length,
              physical_usage_ratio:
                devices.reduce(
                  (sum: number, d: any) => sum + d.physical_used_mib,
                  0,
                ) /
                (24024 * gpus.length),
            },
          },
        }),
  );
  await page.route("**/api/v1/operations/monitoring?*", (r) => {
    metricCalls++;
    return r.fulfill({
      json: {
        hosts: failed
          ? []
          : resources.nodes.map((n: any) => ({
              cluster: n.cluster,
              node: n.node,
              status: "observed",
              evaluated_at: Date.now() / 1000,
              cpu_cores: 64,
              cpu_utilization_pct: 20,
              memory_total_bytes: 32e9,
              memory_used_bytes: 16e9,
              memory_available_bytes: 16e9,
              server_model: "MOCK-SERVER",
              server_vendor: "MOCK-VENDOR",
            })),
        mode: "mock",
        database_read_at: Date.now() / 1000,
        collection_enabled: true,
        health: failed ? "unknown" : "attention",
        alert_count: 0,
        alerts: [],
        metrics,
        truncated: false,
      },
    });
  });
  return () => metricCalls;
}

test("workbench prioritizes actual use hardware composition temperature and readiness", async ({
  page,
  request,
}) => {
  const resources = await (await request.get("/api/v1/overview")).json();
  const calls = await signals(page, resources);
  await page.goto("/");
  await expect(page.locator(".kpi-1 strong")).toHaveText("40%");
  await expect(page.locator(".cluster-health-grid").first()).toContainText(
    "MOCK-ACCELERATOR",
  );
  await expect(page.locator(".cluster-health-grid").first()).toContainText(
    "90 °C",
  );
  await expect(page.locator(".cluster-health-grid").first()).toContainText(
    "1 台未就绪",
  );
  await expect(page.locator(".kpis .kpi")).toHaveCount(4);
  await expect(
    page.getByRole("heading", { name: "资源解读", exact: false }),
  ).toHaveCount(0);
  await expect(page.locator(".cluster-health-grid").first()).toContainText(
    "20%",
  );
  await page.getByText("采集与主机信息", { exact: true }).first().click();
  await expect(page.locator(".host-model-summary").first()).toContainText(
    "MOCK-SERVER",
  );
  await page.getByText("采集与主机信息", { exact: true }).first().click();
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.evaluate(() => {
    (document.activeElement as HTMLElement)?.blur();
    window.scrollTo(0, 0);
  });
  await page.screenshot({
    path: test.info().outputPath("mock-console-workbench-1440.png"),
    fullPage: true,
  });
  expect(calls()).toBe(1);
  await expect(
    page
      .getByRole("region", { name: "GPU 空闲概况" })
      .locator(".availability-ratio strong"),
  ).toHaveText(`4 / ${resources.totals.gpu_count_observed}`);
  await page.getByLabel("容量显示单位").selectOption("MB");
  await expect(page.locator(".kpi-2 p")).toContainText("MB");
  expect(calls()).toBe(1);
  await page.setViewportSize({ width: 375, height: 812 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.evaluate(() => {
    (document.activeElement as HTMLElement)?.blur();
    window.scrollTo(0, 0);
  });
  await page.screenshot({
    path: test.info().outputPath("mock-workbench-signals-375.png"),
    fullPage: true,
  });
});

test("unavailable measurements stay unknown while quota and inventory remain usable", async ({
  page,
  request,
}) => {
  const resources = await (await request.get("/api/v1/overview")).json();
  await signals(page, resources, true);
  await page.goto("/");
  await expect(page.locator(".availability-ratio strong")).toHaveText(
    `— / ${resources.totals.gpu_count_observed}`,
  );
  await expect(page.locator(".kpi-1 strong")).toHaveText("—");
  await expect(page.locator(".kpi-2 strong")).toHaveText("—");
  await expect(page.locator(".cluster-health-grid").first()).toContainText(
    "台未知",
  );
  await expect(page.locator(".cluster-footer").first()).toContainText("容量");
  await expect(page.locator(".cluster-health-grid").first()).not.toContainText(
    "0 °C",
  );
  await expect(page.locator(".cluster-health-grid").first()).not.toContainText(
    "已观测节点就绪",
  );
  await page.getByText("采集与主机信息", { exact: true }).first().click();
  await expect(page.locator(".host-model-summary").first()).toContainText(
    "型号未知",
  );
});

test("idle summary opens the matching GPU memory filter", async ({
  page,
  request,
}) => {
  const resources = await (await request.get("/api/v1/overview")).json();
  await signals(page, resources);
  await page.goto("/");
  const panel = page.getByRole("region", { name: "GPU 空闲概况" });
  await expect(panel.locator(".availability-ratio strong")).toHaveText(
    `4 / ${resources.totals.gpu_count_observed}`,
  );
  await panel.getByRole("link", { name: "查看空闲 GPU" }).click();
  await expect(page).toHaveURL(/#assets\?view=gpus&memory=zero$/);
  await expect(page.getByLabel("筛选显存状态")).toHaveValue("zero");
  await expect(page.locator(".asset-table tbody tr")).toHaveCount(4);
});
