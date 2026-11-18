import { test, expect } from "@playwright/test";
import { mockAssets } from "./fixtures/resourceAssets";
const base = process.env.EFFICIENCY_TEST_BASE_URL;
test("business CPU/memory reads database, switches units locally and preserves identity drilldown", async ({
  page,
}) => {
  test.skip(!base, "requires isolated Mock database");
  expect(
    (await (await page.request.get(`${base}/api/health`)).json()).mode,
  ).toBe("mock");
  await page.goto(`${base}/#workloads?view=compute`);
  await expect(
    page.getByRole("heading", { name: "容器 CPU 与内存", exact: true }),
  ).toBeVisible();
  const detail = page
    .getByRole("button", { name: "容器详情", exact: true })
    .first();
  await expect(detail).toBeVisible();
  const first = page.locator(".workload-compute tbody tr").first();
  await expect(first).toContainText("2 GB");
  await expect(first).toContainText("0.25");
  await page.getByLabel("容量显示单位").selectOption("MB");
  await expect(first).toContainText("2,000 MB");
  await detail.click();
  const dialog = page.getByRole("dialog", { name: "容器资源详情" });
  await expect(dialog).toContainText("Pod UID");
  await expect(dialog).toContainText("containerd://");
  await page.keyboard.press("Escape");
  await expect(detail).toBeFocused();
  await page.setViewportSize({ width: 375, height: 812 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.evaluate(() => {
    (document.activeElement as HTMLElement)?.blur();
    window.scrollTo(0, 0);
    document.querySelectorAll(".table-scroll").forEach((el) => {
      el.scrollLeft = 0;
    });
  });
  await page.screenshot({
    path: test.info().outputPath("mock-workload-compute-375.png"),
    fullPage: true,
  });
});

test("missing declarations stay unreported and a CPU fault does not erase valid memory", async ({
  page,
}) => {
  await mockAssets(page);
  const row = {
    cluster: "yz",
    node: "mock-node",
    namespace_name: "demo",
    pod_name: "model-one",
    container_name: "worker",
    pod_uid: "UID-MOCK",
    container_id: "containerd://mock",
    model_name: null,
    running: true,
    cpu_usage_cores: null,
    memory_working_set_bytes: 1000,
    cpu_request_cores: null,
    cpu_limit_cores: null,
    memory_request_bytes: null,
    memory_limit_bytes: null,
    metric_statuses: {
      cpu_usage_cores: "error",
      memory_working_set_bytes: "ready",
      cpu_request_cores: "missing",
      cpu_limit_cores: "missing",
      memory_request_bytes: "missing",
      memory_limit_bytes: "missing",
    },
  };
  await page.route("**/api/v1/resource-statistics/workloads", (r) =>
    r.fulfill({
      json: {
        collection_enabled: true,
        database_read_at: 1700000000,
        mode: "mock",
        sources: [
          {
            cluster: "yz",
            status: "partial",
            row_count: 1,
            identity_rejected: 0,
            truncated: false,
            evaluated_at: 1700000000,
            source_statuses: {},
          },
        ],
        containers: [row],
        summary: { cpu_usage_cores: null, memory_working_set_bytes: 1000 },
        coverage: { cpu_usage_cores: 0, memory_working_set_bytes: 1 },
      },
    }),
  );
  await page.goto("/#workloads?view=compute");
  await expect(page.locator(".workload-compute .notice")).toContainText(
    "部分覆盖",
  );
  await expect(page.locator(".workload-compute tbody")).toContainText(
    "未报告 / 未报告",
  );
  await expect(page.locator(".workload-compute tbody")).toContainText(
    "<0.01 GB",
  );
  await expect(
    page.locator(".workload-compute .host-resource-summary").first(),
  ).toContainText("<0.01 GB");
  await page.getByLabel("搜索业务资源").fill("does-not-exist");
  await expect(page.getByText("没有匹配的业务资源")).toBeVisible();
});

test("host sync diagnostics expose real progress and failure classifications", async ({
  page,
}) => {
  await page.route("**/api/v1/operations/host-sync?*", (r) =>
    r.fulfill({
      json: {
        available: true,
        clusters: [
          {
            cluster: "yz",
            state: "attention",
            expected_minutes: 2880,
            complete_minutes: 120,
            pending_minutes: 2760,
            error_minutes: 30,
            partial_minutes: 10,
            empty_minutes: 0,
            unknown_minutes: 5,
            latest_attempt_at: 1700000000,
            latest_complete_at: 1699990000,
          },
        ],
      },
    }),
  );
  await page.goto("/#settings?view=status");
  await expect(
    page.getByRole("heading", { name: "主机分钟覆盖" }),
  ).toBeVisible();
  await expect(page.getByText("120/2880", { exact: true })).toBeVisible();
  await page.getByText("缺口与失败分类", { exact: true }).click();
  await expect(page.getByText(/身份查询失败 30/)).toBeVisible();
  await expect(page.getByText(/旧记录原因未知 5/)).toBeVisible();
});
