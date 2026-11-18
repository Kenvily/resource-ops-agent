import { test, expect } from "@playwright/test";
const id = "00000000-0000-4000-8000-000000000001";
const task = {
  id,
  kind: "allocation_change",
  status: "limited",
  created_at: 1793235600,
  finished_at: 1793235601,
  clusters: ["yz"],
  legacy: false,
};
const report = {
  ...task,
  request: { cluster: "yz", window: "1h" },
  goals: ["检查覆盖", "对比窗口", "核查替代解释"],
  failure_reason: null,
  metadata_version: 1,
  feedback_enabled: true,
  feedback: [] as { decision: string; note: string; created_at: number }[],
  events: [{ sequence: 1, type: "evidence", data: { label: "覆盖核验" } }],
  evidence: [
    {
      id: "evidence-1",
      tool: "check_data_quality",
      label: "覆盖核验",
      arguments: { cluster: "yz" },
      data: {
        expected_points: 60,
        observed_points: 30,
        coverage: 0.5,
        end: 1793235600,
      },
    },
  ],
  findings: [
    {
      title: "数据不足，不能判断业务原因",
      level: "unknown",
      evidence_ids: ["evidence-1"],
      limitations: ["历史覆盖不足"],
    },
  ],
};
test.beforeEach(async ({ request }) => {
  expect((await (await request.get("/api/health")).json()).mode).toBe("mock");
});

test("records expose scope evidence classification and append controlled feedback", async ({
  page,
}) => {
  let current = structuredClone(report),
    saved = 0;
  await page.route("**/api/v1/investigations?*", (r) =>
    r.fulfill({ json: { tasks: [task] } }),
  );
  await page.route(`**/api/v1/investigations/${id}?*`, (r) =>
    r.fulfill({ json: current }),
  );
  await page.route(`**/api/v1/investigations/${id}/feedback`, (r) => {
    saved++;
    const b = r.request().postDataJSON();
    expect(b.request_id).toMatch(/^[a-f0-9-]{36}$/);
    current = {
      ...current,
      feedback: [
        { decision: b.decision, note: b.note, created_at: 1793235602 },
      ],
    };
    return r.fulfill({ json: { saved: true } });
  });
  await page.goto("/#analysis?view=records");
  await page.getByRole("link", { name: "查看证据", exact: true }).click();
  await expect(page.getByText("结论受限", { exact: true })).toBeVisible();
  await expect(page.getByText("无法判断", { exact: true })).toBeVisible();
  await page
    .getByRole("link", { name: "证据 evidence-1", exact: true })
    .click();
  await expect(page.locator(".investigation-evidence")).toHaveAttribute(
    "open",
    "",
  );
  await expect(page.locator(".investigation-evidence")).toContainText(
    "expected_points",
  );
  await page.getByLabel("反馈结果").selectOption("insufficient");
  await page.getByLabel("补充说明").fill("请补齐时间段");
  await page.getByRole("button", { name: "保存反馈", exact: true }).click();
  await expect(page.getByRole("status")).toHaveText("反馈已保存");
  await expect(page.getByRole("region", { name: "调查反馈" })).toContainText(
    "请补齐时间段",
  );
  expect(saved).toBe(1);
  await page.setViewportSize({ width: 375, height: 812 });
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
    path: test.info().outputPath("mock-investigation-record-375.png"),
    fullPage: true,
  });
});

test("cancelled legacy and unavailable tasks do not invent complete findings", async ({
  page,
}) => {
  await page.route(`**/api/v1/investigations/${id}?*`, (r) =>
    r.fulfill({
      json: {
        ...report,
        status: "cancelled",
        legacy: true,
        feedback_enabled: false,
        findings: [],
        failure_reason: "连接断开，未完成调查",
      },
    }),
  );
  await page.goto(`/#analysis?view=records&task=${id}`);
  await expect(page.getByText("已取消", { exact: true })).toBeVisible();
  await expect(
    page.getByText("尚无足够证据形成结论", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "保存反馈", exact: true }),
  ).toHaveCount(0);
  await page.getByLabel("选择集群").selectOption("zw");
  await page.route(`**/api/v1/investigations/${id}?*`, (r) =>
    r.fulfill({ status: 404, json: {} }),
  );
  await page.getByRole("button", { name: "刷新记录", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("不在当前范围");
});

test("database investigation links to persisted evidence and accepts local feedback", async ({
  page,
}) => {
  const base = process.env.EFFICIENCY_TEST_BASE_URL;
  test.skip(!base, "requires isolated Mock PostgreSQL");
  await page.goto(`${base}/#efficiency`);
  await page.getByRole("button", { name: "开始核查", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: /低利用率 GPU [1-9]\d* 张/ }),
  ).toBeVisible();
  await page
    .getByRole("link", { name: "查看调查记录与反馈", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "占用深入调查", exact: true }),
  ).toBeVisible();
  await page.getByLabel("反馈结果").selectOption("insufficient");
  await page.getByLabel("补充说明").fill("Mock 数据闭环核验");
  await page.getByRole("button", { name: "保存反馈", exact: true }).click();
  await expect(page.getByRole("status")).toHaveText("反馈已保存");
  await expect(page.getByRole("region", { name: "调查反馈" })).toContainText(
    "Mock 数据闭环核验",
  );
});
