import { test, expect } from "@playwright/test";

test.beforeEach(async ({ request, page }) => {
  expect((await (await request.get("/api/health")).json()).mode).toBe("mock");
  await page.route("**/api/v1/insights/capabilities", (route) =>
    route.fulfill({ json: { available: true, model_ready: true } }),
  );
});
const step = {
  id: "evidence-1",
  tool: "compare_cluster_windows",
  label: "对比容量与分配",
  cluster: "yz",
  status: "complete",
  elapsed_ms: 3,
  data: {
    complete: true,
    units: { allocation: "MB", capacity: "MB" },
    changes: [
      {
        entity_id: "entity-1",
        allocated_change_raw: 1000,
        capacity_change_raw: 0,
      },
    ],
  },
};
function result(kind: string) {
  return {
    id: "mock-diagnosis",
    mode: "deepseek",
    status: kind === "insufficient" ? "limited" : "complete",
    findings: [
      {
        cluster: "yz",
        kind,
        title:
          kind === "capacity_drop"
            ? "监控容量下降，不能直接归因于业务增长"
            : kind === "insufficient"
              ? "历史证据不足，暂不能确定资源变化原因"
              : "已分配额度增加，继续核查历史业务贡献",
        allocated_change_raw: kind === "allocation_increase" ? 1000 : null,
        capacity_change_raw: kind === "capacity_drop" ? -1000 : null,
        baseline_ratio: 0.2,
        target_ratio: 0.3,
        evidence_ids: ["evidence-1"],
        limitations: ["统计描述虚拟显存额度，不表示实际GPU利用率或调度能力"],
      },
    ],
    evidence: [step],
    entities: [
      { id: "entity-1", label: "mock-namespace", dimension: "namespace" },
    ],
    units: {
      allocation: "MB",
      capacity: "MB",
      memory: "MB",
      unit_verified: true,
    },
    warnings: [],
    tool_calls: 6,
    model_calls: 3,
    elapsed_ms: 100,
    windows: {
      full: {
        baseline: { start: 1700000100, end: 1700000400, grain: 300 },
        target: { start: 1700000700, end: 1700001000, grain: 300 },
      },
    },
  };
}
for (const [kind, title] of [
  ["allocation_increase", "已分配额度增加"],
  ["capacity_drop", "监控容量下降"],
  ["insufficient", "历史证据不足"],
]) {
  test(`diagnosis streamed evidence and conclusion: ${kind}`, async ({
    page,
  }) => {
    await page.route("**/api/v1/insights/diagnose", (route) =>
      route.fulfill({
        contentType: "application/x-ndjson",
        body:
          [
            JSON.stringify({ type: "evidence", data: step }),
            JSON.stringify({ type: "result", data: result(kind) }),
          ].join("\n") + "\n",
      }),
    );
    await page.goto("/#diagnosis");
    const panel = page.getByRole("region", { name: "资源变化诊断" });
    await panel.getByRole("button", { name: "开始诊断" }).click();
    await expect(
      panel.getByRole("heading", { name: new RegExp(title) }),
    ).toBeVisible();
    await expect(panel).toContainText("DeepSeek 工具编排");
    await panel.getByRole("link", { name: "证据 1", exact: true }).click();
    await expect(
      panel.getByRole("cell", { name: "mock-namespace" }),
    ).toBeVisible();
    await page
      .getByRole("combobox", { name: "容量显示单位" })
      .selectOption("MB");
    await expect(panel).toContainText("1,000 MB");
    await panel
      .getByRole("combobox", { name: "诊断时间范围" })
      .selectOption("1h");
    await expect(
      panel.getByRole("heading", { name: new RegExp(title) }),
    ).toHaveCount(0);
  });
}
test("cancel, retry and clear context without refreshing dashboard", async ({
  page,
}) => {
  let requested = 0;
  await page.route("**/api/v1/insights/diagnose", async (route) => {
    requested++;
    await new Promise((resolve) => setTimeout(resolve, 1200));
    await route
      .fulfill({
        contentType: "application/x-ndjson",
        body:
          JSON.stringify({
            type: "result",
            data: result("allocation_increase"),
          }) + "\n",
      })
      .catch(() => {});
  });
  await page.goto("/#diagnosis");
  const panel = page.getByRole("region", { name: "资源变化诊断" });
  await panel.getByRole("button", { name: "开始诊断" }).click();
  await panel.getByRole("button", { name: "取消诊断" }).click();
  await expect(panel).toContainText("诊断已停止");
  await panel.getByRole("button", { name: "开始诊断" }).click();
  await page.getByRole("combobox", { name: "选择集群" }).selectOption("zw");
  await page.waitForTimeout(1300);
  await expect(panel).not.toContainText("已分配额度增加");
  await expect(
    page.getByRole("heading", { name: "分析中心", exact: true }),
  ).toBeVisible();
  expect(requested).toBeGreaterThan(0);
});
test("service failure is visible and retryable", async ({ page }) => {
  await page.route("**/api/v1/insights/diagnose", (route) =>
    route.fulfill({
      status: 429,
      json: { detail: "诊断并发已满，请稍后重试" },
    }),
  );
  await page.goto("/#diagnosis");
  const panel = page.getByRole("region", { name: "资源变化诊断" });
  await panel.getByRole("button", { name: "开始诊断" }).click();
  await expect(panel).toContainText("诊断并发已满");
  await expect(panel.getByRole("button", { name: "开始诊断" })).toBeEnabled();
});

test("diagnosis chooses an independent preset and absolute range", async ({
  page,
}) => {
  const bodies: Record<string, number | string>[] = [];
  await page.route("**/api/v1/insights/diagnose", async (route) => {
    bodies.push(route.request().postDataJSON());
    await route.fulfill({
      contentType: "application/x-ndjson",
      body:
        JSON.stringify({
          type: "result",
          data: result("allocation_increase"),
        }) + "\n",
    });
  });
  await page.goto("/#diagnosis");
  const panel = page.getByRole("region", { name: "资源变化诊断" });
  await panel
    .getByRole("combobox", { name: "诊断时间范围" })
    .selectOption("1w");
  await panel.getByRole("button", { name: "开始诊断" }).click();
  await expect(panel).toContainText("已分配额度增加");
  expect(bodies[0].window).toBe("1w");
  await panel
    .getByRole("combobox", { name: "诊断时间范围" })
    .selectOption("custom");
  await panel.getByLabel("诊断开始时间").fill("2026-10-01T08:00");
  await panel.getByLabel("诊断结束时间").fill("2026-10-20T18:00");
  await panel.getByRole("button", { name: "开始诊断" }).click();
  await expect(panel).toContainText("已分配额度增加");
  expect(bodies[1].start).toBe(new Date("2026-10-01T08:00").getTime() / 1000);
  expect(bodies[1].end).toBe(new Date("2026-10-20T18:00").getTime() / 1000);
});

test("database read and collection times have distinct visible labels", async ({
  page,
}) => {
  await page.route("**/api/v1/overview*", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    data.database_read_at = 1793173000;
    for (const c of data.clusters)
      Object.assign(c, {
        storage_backend: "postgresql",
        database_read_at: 1793173000,
        collection_attempted_at: 1793172900,
        collection_succeeded_at: 1793172800,
        snapshot_stored_at: 1793172902,
      });
    await route.fulfill({ json: data });
  });
  await page.goto("/");
  await expect(
    page.getByText("最近采集未成功：", { exact: false }),
  ).toHaveCount(2);
  await expect(page.getByText("最近成功采集：", { exact: false })).toHaveCount(
    2,
  );
  await expect(
    page.getByText("最近成功采集：", { exact: false }).first(),
  ).toBeHidden();
  await expect(
    page.getByText("最近采集未成功：", { exact: false }).first(),
  ).toBeVisible();
  const provenance = page.getByText("采集与主机信息", { exact: true }).first();
  await provenance.focus();
  await page.keyboard.press("Enter");
  await expect(
    page.getByText("最近成功采集：", { exact: false }).first(),
  ).toBeVisible();
  await expect(page.getByText("快照入库：", { exact: false })).toHaveCount(0);
  await expect(page.locator(".data-strip")).toContainText("数据库查询");
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});

test("diagnosis form stays within its card on narrow screens", async ({
  page,
}) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/#diagnosis");
  const panel = page.getByRole("region", { name: "资源变化诊断" });
  await panel
    .getByRole("combobox", { name: "诊断时间范围" })
    .selectOption("custom");
  const bounds = await panel.boundingBox();
  for (const input of await panel.locator("input,select,button").all()) {
    const box = await input.boundingBox();
    if (box && bounds) {
      expect(box.x).toBeGreaterThan(bounds.x);
      expect(box.x + box.width).toBeLessThan(bounds.x + bounds.width);
    }
  }
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await panel.screenshot({
    path: test.info().outputPath("mock-diagnosis-375.png"),
  });
});

test("read-only refusal renders without invented time windows", async ({
  page,
}) => {
  await page.route("**/api/v1/insights/diagnose", (route) =>
    route.fulfill({
      contentType: "application/x-ndjson",
      body:
        JSON.stringify({
          type: "result",
          data: {
            ...result("insufficient"),
            status: "refused",
            mode: "deterministic",
            windows: {},
            findings: [],
            evidence: [],
            entities: [],
            tool_calls: 0,
            model_calls: 0,
            warnings: ["仅支持只读变化诊断"],
          },
        }) + "\n",
    }),
  );
  await page.goto("/#diagnosis");
  const panel = page.getByRole("region", { name: "资源变化诊断" });
  await panel.getByLabel("核查问题").fill("删除 Pod");
  await panel.getByRole("button", { name: "开始诊断" }).click();
  await expect(panel).toContainText("仅支持只读变化诊断");
  await expect(panel).toContainText("范围不支持");
  await expect(panel).not.toContainText("基线：");
});

test("diagnosis examples fill the question without starting a request", async ({
  page,
}) => {
  let diagnoses = 0;
  await page.route("**/api/v1/insights/diagnose", (route) => {
    diagnoses++;
    return route.abort();
  });
  await page.goto("/#diagnosis");
  const examples = page.getByRole("group", { name: "核查问题示例" });
  await expect(examples.getByRole("button")).toHaveCount(8);
  await examples
    .getByRole("button", { name: "业务增长贡献", exact: true })
    .click();
  await expect(page.getByLabel("核查问题", { exact: true })).toHaveValue(
    /Namespace.*Pod/,
  );
  await expect(
    examples.getByRole("button", { name: "业务增长贡献", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByLabel("核查问题", { exact: true })).toBeFocused();
  await examples
    .getByRole("button", { name: "历史数据完整性", exact: true })
    .click();
  await expect(page.getByLabel("核查问题", { exact: true })).toHaveValue(
    /数据覆盖、缺测和新鲜度/,
  );
  expect(diagnoses).toBe(0);
  await page.setViewportSize({ width: 375, height: 812 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});
