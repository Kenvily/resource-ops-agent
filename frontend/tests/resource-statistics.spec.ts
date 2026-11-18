import { test, expect } from "@playwright/test";
import {
  mockAssets,
  statistics,
  device,
  history,
  aggregate,
} from "./fixtures/resourceAssets";
test.beforeEach(async ({ request, page }) => {
  expect((await (await request.get("/api/health")).json()).mode).toBe("mock");
  await mockAssets(page);
});

test("assets have exactly server and GPU views, and old links remain compatible", async ({
  page,
}) => {
  for (const [old, view] of [
    ["nodes", "servers"],
    ["machines", "servers"],
    ["devices", "gpus"],
    ["gpu_models", "gpus"],
  ]) {
    await page.goto(`/#assets?view=${old}`);
    await expect(page).toHaveURL(new RegExp(`#assets\\?view=${view}$`));
  }
  const nav = page.getByRole("navigation", { name: "资源资产视图" });
  await expect(nav.getByRole("link")).toHaveCount(2);
  await nav.getByRole("link", { name: "服务器", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "服务器资源", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("columnheader", { name: "GPU 温度" }),
  ).toHaveCount(0);
  await nav.getByRole("link", { name: "GPU", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "GPU 资源", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("columnheader", { name: "CPU 配置 / 利用率" }),
  ).toHaveCount(0);
  await page.getByRole("link", { name: "业务模型", exact: true }).click();
  await expect(
    page.getByRole("navigation", { name: "业务模型视图" }),
  ).toHaveCount(0);
});

test("zero memory uses physical raw values, highlights known zero and keeps allocated quota distinct", async ({
  page,
}) => {
  await page.route("**/api/v1/resource-statistics", (r) =>
    r.fulfill({
      json: {
        ...statistics,
        devices: [
          { ...device, physical_used_mib: 0, temperature_c: 42 },
          {
            ...device,
            device_uuid: "tiny",
            physical_used_mib: 0.000001,
            temperature_c: null,
          },
          {
            ...device,
            device_uuid: "missing",
            physical_used_mib: null,
            temperature_c: null,
          },
        ],
        summary: { ...aggregate, physical_used_mib: null },
      },
    }),
  );
  await page.goto("/#assets?view=gpus");
  const zero = page.getByRole("button", { name: /零显存占用 GPU/ });
  await expect(zero).toContainText("1 张");
  await expect(zero).toContainText("1 张显存缺测");
  await expect(page.locator(".zero-memory-row")).toHaveCount(1);
  await expect(page.locator(".zero-memory-row")).toContainText("10 GB");
  await expect(page.locator(".zero-memory-row")).toContainText("42 °C");
  await expect(page.getByRole("row").filter({ hasText: "tiny" })).toContainText(
    "有显存占用",
  );
  await expect(page.getByRole("row").filter({ hasText: "tiny" })).toContainText(
    "<0.01 GB",
  );
  await page
    .getByRole("row")
    .filter({ hasText: "tiny" })
    .getByRole("button", { name: "查看显卡历史" })
    .click();
  await expect(
    page
      .getByRole("dialog")
      .locator(".efficiency-evidence > div")
      .filter({ hasText: "当前物理已用" }),
  ).toContainText("<0.01 GB");
  await page.keyboard.press("Escape");
  await zero.click();
  await expect(page.getByRole("button", { name: "查看显卡历史" })).toHaveCount(
    1,
  );
  await page.getByLabel("筛选显存状态").selectOption("unknown");
  await expect(page.getByRole("button", { name: "查看显卡历史" })).toHaveCount(
    1,
  );
  await expect(page.locator(".asset-table tbody")).toContainText("missing");
});

test("GPU model and server filters are local; units convert without refreshing data", async ({
  page,
}) => {
  let calls = 0;
  await page.route("**/api/v1/resource-statistics", (r) => {
    calls++;
    return r.fulfill({
      json: {
        ...statistics,
        devices: [device, { ...device, cluster: "zw", gpu_model: "OTHER" }],
      },
    });
  });
  await page.goto("/#assets?view=gpus");
  await expect(page.getByRole("button", { name: "查看显卡历史" })).toHaveCount(
    2,
  );
  const initial = calls;
  await page.getByLabel("筛选 GPU 型号").selectOption("OTHER");
  await expect(page.locator(".asset-table tbody")).toContainText("兆维");
  await expect(page.getByRole("button", { name: "查看显卡历史" })).toHaveCount(
    1,
  );
  await page.getByLabel("容量显示单位").selectOption("MB");
  await expect(page.locator(".asset-table tbody")).toContainText("10,000 MB");
  expect(calls).toBe(initial);
});

test("server page shows weighted host metrics and keeps per-server containers on demand", async ({
  page,
}) => {
  const host = {
    status: "observed",
    cpu_cores: 64,
    cpu_utilization_pct: 10,
    memory_total_bytes: 16e9,
    memory_used_bytes: 8e9,
    load1: 1.5,
    server_model: "MOCK-SERVER",
  };
  await page.route("**/api/v1/resource-statistics", (r) =>
    r.fulfill({
      json: {
        ...statistics,
        servers: [
          { cluster: "yz", node: "same-node", host, node_ready: true },
          {
            cluster: "zw",
            node: "same-node",
            host: {
              ...host,
              cpu_cores: 32,
              cpu_utilization_pct: 50,
              memory_total_bytes: 8e9,
              memory_used_bytes: 4e9,
            },
            node_ready: false,
          },
        ],
      },
    }),
  );
  let reads = 0;
  await page.route("**/api/v1/resource-statistics/workloads", (r) => {
    reads++;
    expect(r.request().postDataJSON()).toEqual({ cluster: "zw" });
    return r.fulfill({
      json: {
        collection_enabled: true,
        sources: [{ cluster: "zw", status: "ready" }],
        containers: [
          {
            cluster: "zw",
            node: "same-node",
            namespace_name: "demo",
            pod_name: "selected",
            container_name: "worker",
            pod_uid: "uid",
            model_name: null,
            cpu_usage_cores: 0.2,
            memory_working_set_bytes: 1e9,
            metric_statuses: {},
          },
          {
            cluster: "zw",
            node: "another-node",
            namespace_name: "demo",
            pod_name: "excluded",
            container_name: "worker",
            metric_statuses: {},
          },
        ],
        summary: {},
        coverage: {},
      },
    });
  });
  await page.goto("/#assets?view=servers");
  await expect(page.locator(".asset-summary")).toContainText("96 核");
  await expect(page.locator(".asset-summary")).toContainText("24 GB");
  await expect(page.locator(".asset-summary")).toContainText("23.33%");
  expect(reads).toBe(0);
  const row = page.getByRole("row").filter({ hasText: "兆维" });
  await row.getByRole("button", { name: "容器资源" }).click();
  await expect(
    page.getByRole("heading", { name: "容器 CPU 与内存" }),
  ).toBeVisible();
  await expect(page.locator(".workload-compute tbody")).toContainText(
    "selected",
  );
  await expect(page.locator(".workload-compute tbody")).not.toContainText(
    "excluded",
  );
  await expect(
    page.locator(".workload-compute .host-resource-summary"),
  ).toHaveCount(0);
});

test("server details use full cluster/node identity and errors do not redirect", async ({
  page,
}) => {
  let selected: unknown;
  await page.route("**/api/v1/resource-statistics/host", (r) => {
    selected = r.request().postDataJSON();
    return r.fulfill({
      status: 503,
      json: { detail: "Mock 主机指标暂不可用" },
    });
  });
  await page.goto("/#assets?view=servers");
  const trigger = page.getByRole("button", { name: "服务器详情" });
  await trigger.click();
  await expect(page.getByRole("dialog").getByRole("alert")).toContainText(
    "Mock 主机指标",
  );
  expect(selected).toMatchObject({ cluster: "yz", node: device.node });
  await expect(page).toHaveURL(/view=servers$/);
  await page.keyboard.press("Escape");
  await expect(trigger).toBeFocused();
});

test("business models and unmapped identities retain searchable GPU drilldown", async ({
  page,
}) => {
  await page.goto("/#workloads?view=business_models");
  await expect(
    page.getByRole("cell", { name: "demo-pod-1", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("cell", { name: "demo-pod-2", exact: true }),
  ).toBeVisible();
  await page.getByLabel("搜索业务模型").fill("demo-pod-2");
  const trigger = page.getByRole("button", {
    name: "查看关联 GPU",
    exact: true,
  });
  await trigger.click();
  const dialog = page.getByRole("dialog", { name: "关联 GPU", exact: true });
  await expect(
    dialog.getByRole("button", { name: "查看显卡历史" }),
  ).toHaveCount(1);
  await dialog.getByRole("button", { name: "查看显卡历史" }).click();
  await expect(dialog.getByRole("img")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(trigger).toBeFocused();
  await expect(page.getByLabel("搜索业务模型")).toHaveValue("demo-pod-2");
  await expect(page).toHaveURL(/#workloads\?view=business_models$/);
});

test("GPU history has scoped requests, visible gaps, keyboard focus and closes on navigation", async ({
  page,
}) => {
  let query: unknown;
  await page.route("**/api/v1/resource-statistics/history", (r) => {
    query = r.request().postDataJSON();
    return r.fulfill({ json: history });
  });
  await page.goto("/#assets?view=gpus");
  const trigger = page.getByRole("button", { name: "查看显卡历史" }).first();
  await trigger.click();
  const dialog = page.getByRole("dialog", { name: "GPU 显存与使用历史" });
  await expect(dialog.getByRole("img")).toBeVisible();
  expect(query).toEqual({
    cluster: "yz",
    node: device.node,
    device_uuid: device.device_uuid,
    window: "1h",
  });
  await dialog.getByText("查看采样数据", { exact: false }).click();
  await expect(
    dialog.getByRole("cell", { name: "0/1", exact: true }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(trigger).toBeFocused();
  await trigger.click();
  await page.goto("/#assets?view=servers");
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("partial GPU measurements never synthesize utilization or physical totals", async ({
  page,
}) => {
  await page.route("**/api/v1/resource-statistics", (r) =>
    r.fulfill({
      json: {
        ...statistics,
        complete: false,
        sources: [{ cluster: "yz", status: "stale" }],
        summary: {
          ...aggregate,
          physical_used_mib: null,
          utilization_pct: null,
        },
        devices: [
          {
            ...device,
            physical_used_mib: null,
            physical_observed_total_mib: null,
            utilization_pct: null,
          },
        ],
      },
    }),
  );
  await page.goto("/#assets?view=gpus");
  await expect(page.getByText(/数据已过期/)).toBeVisible();
  await expect(page.locator(".zero-memory-row")).toHaveCount(0);
  await expect(page.locator(".asset-summary")).toContainText("—");
});

test("asset screens fit narrow and desktop layouts with two navigation choices", async ({
  page,
}) => {
  for (const width of [375, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    for (const view of ["servers", "gpus"]) {
      await page.goto(`/#assets?view=${view}`);
      await expect(
        page.getByRole("heading", {
          name: view === "servers" ? "服务器资源" : "GPU 资源",
          exact: true,
        }),
      ).toBeVisible();
      await expect(
        page
          .getByRole("navigation", { name: "资源资产视图" })
          .getByRole("link", {
            name: view === "servers" ? "服务器" : "GPU",
            exact: true,
          }),
      ).toHaveAttribute("aria-current", "page");
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      await page.screenshot({
        path: test.info().outputPath(`assets-${view}-${width}.png`),
        fullPage: true,
        animations: "disabled",
      });
    }
  }
});

test("assets read real isolated Mock PostgreSQL snapshots", async ({
  page,
}) => {
  const base = process.env.EFFICIENCY_TEST_BASE_URL;
  test.skip(!base, "requires isolated Mock PostgreSQL");
  expect(
    (await (await page.request.get(`${base}/api/health`)).json()).mode,
  ).toBe("mock");
  await page.goto(`${base}/#assets?view=gpus`);
  await expect(
    page.getByRole("button", { name: "查看显卡历史" }).first(),
  ).toBeVisible();
  await page.getByRole("link", { name: "服务器", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "服务器详情" }).first(),
  ).toBeVisible();
});

test("business categories search versions and Pod details without a separate allocation page", async ({
  page,
}) => {
  const { statistics } = await import("./fixtures/resourceAssets");
  const mapped = (key: string, category: string) => ({
    model_key: key,
    name: `演示模型 ${key}`,
    category,
    description: "示例说明",
    model_names: [`version-${key}`],
    allocated_raw: 10000,
    gpu_count: 1,
    machine_count: 1,
    shared_gpu_count: 0,
    container_count: 1,
    attribution: "explicit_name_mapping",
    associated_devices: statistics.unmapped_workloads[0].associated_devices,
    workloads: [
      { cluster: "yz", namespace: "demo-team", pod: `demo-pod-${key}` },
    ],
  });
  await page.route("**/api/v1/resource-statistics", (r) =>
    r.fulfill({
      json: {
        ...statistics,
        business_models: [mapped("one", "生产"), mapped("two", "测试")],
      },
    }),
  );
  await page.goto("/#workloads?view=business_models");
  await expect(page).toHaveURL(/#workloads\?view=business_models$/);
  await expect(
    page.getByRole("link", { name: "业务资源分配", exact: true }),
  ).toHaveCount(0);
  const section = page.getByRole("region", { name: "已映射业务模型" });
  await expect(section.locator("tbody tr")).toHaveCount(2);
  await page.getByLabel("归属类别").selectOption("生产");
  await expect(section.locator("tbody tr")).toHaveCount(1);
  await expect(
    page.getByRole("region", { name: "未映射业务负载" }),
  ).toHaveCount(0);
  await section.getByText("查看模型与 Pod").click();
  await expect(section).toContainText("demo-pod-one");
  await page.getByLabel("搜索业务模型").fill("version-two");
  await expect(section).toContainText("没有匹配的业务模型");
  await page.getByLabel("归属类别").selectOption("all");
  await expect(section.locator("tbody tr")).toHaveCount(1);
  await expect(section).toContainText("演示模型 two");
  await page.getByRole("button", { name: "清除筛选" }).click();
  await expect(section.locator("tbody tr")).toHaveCount(2);
  await page.setViewportSize({ width: 375, height: 812 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: test.info().outputPath("mock-business-models-375.png"),
    fullPage: true,
  });
});
