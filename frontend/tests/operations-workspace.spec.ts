import { test, expect } from "@playwright/test";
const base = process.env.EFFICIENCY_TEST_BASE_URL;
const fakeId = "11111111-1111-4111-8111-111111111111";

test("saved planning compares candidates, appends review and survives navigation", async ({
  page,
}) => {
  test.skip(!base, "requires isolated Mock PostgreSQL");
  expect(
    (await (await page.request.get(`${base}/api/health`)).json()).mode,
  ).toBe("mock");
  await page.goto(`${base}/#planning`);
  await expect(page.getByText("已保存的评估（最近 50 条）")).toBeVisible();
  await page.getByLabel("GPU 数量", { exact: true }).fill("1");
  const created = page.waitForResponse(
    (r) =>
      r.url().endsWith("/workspace/assessments") &&
      r.request().method() === "POST",
  );
  await page.getByRole("button", { name: "开始评估", exact: true }).click();
  const response = await created;
  expect(response.status()).toBe(200);
  const delivery = await response.json();
  await expect(page.getByText("已保存评估", { exact: true })).toBeVisible();
  await page.getByLabel("选择候选 1", { exact: true }).check();
  await page.getByLabel("选择候选 2", { exact: true }).check();
  await page.getByRole("button", { name: "对比方案", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "候选方案对比" }),
  ).toBeVisible();
  await page.getByText("保存记录与复核", { exact: true }).click();
  await page.getByLabel("补充说明", { exact: true }).fill("Mock 方案核对");
  await page.getByRole("button", { name: "保存复核" }).click();
  await expect(page.getByText(/候选.*Mock 方案核对/)).toBeVisible();
  await page.goto(`${base}/#workbench`);
  await page.goto(`${base}/#planning`);
  await page.getByText("已保存的评估（最近 50 条）").click();
  await page
    .getByRole("button", { name: "查看记录", exact: true })
    .first()
    .click();
  await page.getByText("保存记录与复核", { exact: true }).click();
  await expect(
    page.locator("p").filter({ hasText: /Mock 方案核对/ }),
  ).toBeVisible();
  await page.setViewportSize({ width: 375, height: 812 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.evaluate(() => (document.activeElement as HTMLElement)?.blur());
  await page.screenshot({
    path: test.info().outputPath("mock-planning-r3-375.png"),
    fullPage: true,
  });
  expect(
    (
      await page.request.get(
        `${base}/api/v1/operations/workspace/records/${delivery.id}?cluster=all`,
      )
    ).status(),
  ).toBe(200);
});

test("saved report freezes issue evidence, requires persisted review and exports trace", async ({
  page,
}) => {
  test.skip(!base, "requires isolated Mock PostgreSQL");
  await page.goto(`${base}/#operations?view=reports`);
  await expect(page.getByText("已保存的报告（最近 50 条）")).toBeVisible();
  const created = page.waitForResponse(
    (r) =>
      r.url().endsWith("/workspace/reports") && r.request().method() === "POST",
  );
  await page.getByRole("button", { name: "生成报告", exact: true }).click();
  expect((await created).status()).toBe(200);
  const download = page.getByRole("button", { name: "导出 Markdown" });
  await expect(download).toBeDisabled();
  await page.getByLabel("补充说明", { exact: true }).fill("Mock 报告核对");
  await page.getByRole("button", { name: "保存复核" }).click();
  await expect(download).toBeEnabled();
  const event = page.waitForEvent("download");
  await download.click();
  expect((await event).suggestedFilename()).toBe("gpu-operations-1d.md");
});

test("issue workflow shows frozen capture and reports optimistic conflict", async ({
  page,
}) => {
  await page.route("**/workspace/capabilities", (r) =>
    r.fulfill({ json: { available: true, write_enabled: true } }),
  );
  const issue = {
    id: fakeId,
    cluster: "yz",
    title: "GPU 温度需要核查",
    status: "open",
    revision: 1,
    evidence: {
      node: "mock-node",
      device_uuid: "mock-gpu",
      evaluated_at: 1700000000,
    },
    events: [],
  };
  await page.route("**/workspace/issues?*", (r) =>
    r.fulfill({ json: { issues: [issue] } }),
  );
  await page.route(`**/workspace/issues/${fakeId}?*`, (r) =>
    r.fulfill({ json: issue }),
  );
  await page.route(`**/workspace/issues/${fakeId}/review`, (r) =>
    r.fulfill({
      status: 409,
      json: { detail: "问题已被更新，请重新读取后提交" },
    }),
  );
  await page.goto("/#operations?view=issues");
  await page.getByRole("button", { name: /GPU 温度需要核查/ }).click();
  await expect(page.getByText("mock-node / mock-gpu")).toBeVisible();
  await page.getByLabel("核查状态").selectOption("resolved");
  await page.getByLabel("核查结论").fill("核查结束");
  await page.getByRole("button", { name: "保存核查" }).click();
  await expect(page.getByRole("alert")).toContainText("问题已被更新");
  await expect(page.getByLabel("核查结论")).toHaveValue("核查结束");
  await page.setViewportSize({ width: 375, height: 812 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.evaluate(() => (document.activeElement as HTMLElement)?.blur());
  await page.screenshot({
    path: test.info().outputPath("mock-operations-issue-375.png"),
    fullPage: true,
  });
});

test("report selection carries investigation IDs and displays frozen limitations", async ({
  page,
}) => {
  await page.route("**/workspace/capabilities", (r) =>
    r.fulfill({ json: { available: true, write_enabled: true } }),
  );
  await page.route("**/workspace/records?*", (r) =>
    r.fulfill({ json: { records: [] } }),
  );
  await page.route("**/workspace/issues?*", (r) =>
    r.fulfill({ json: { issues: [] } }),
  );
  await page.route("**/api/v1/investigations?*", (r) =>
    r.fulfill({
      json: {
        tasks: [
          {
            id: fakeId,
            kind: "allocation_change",
            status: "limited",
            clusters: ["yz"],
            created_at: 1700000000,
          },
        ],
      },
    }),
  );
  await page.route("**/workspace/reports", (r) => {
    expect(r.request().postDataJSON().investigation_ids).toEqual([fakeId]);
    return r.fulfill({
      json: {
        id: fakeId,
        kind: "report",
        clusters: ["yz"],
        created_at: 1700000000,
        decisions: [],
        result: {
          mode: "mock",
          generated_at: 1700000000,
          window: "1d",
          baseline: { start: 1699900000, end: 1699950000 },
          target: { start: 1699950001, end: 1700000000 },
          units: { allocation: "MB", capacity: "MB" },
          clusters: [],
          metrics: [],
          alerts: [],
          alert_count: 0,
          notes: [],
          linked_investigations: [
            {
              id: fakeId,
              status: "limited",
              evidence: [],
              findings: [
                {
                  title: "覆盖不足，不能判断原因",
                  level: "unknown",
                  evidence_ids: [],
                },
              ],
            },
          ],
          linked_issues: [],
          trace_warnings: ["关联调查仅作背景"],
        },
      },
    });
  });
  await page.goto("/#operations?view=reports");
  await page.getByText("关联调查与问题（可选）").click();
  await page.getByLabel(/资源变化 · limited/).check();
  await page.getByRole("button", { name: "生成报告", exact: true }).click();
  await expect(page.getByText("关联调查仅作背景")).toBeVisible();
  await page.getByText(`调查 · limited · ${fakeId}`).click();
  await expect(
    page.getByText("无法判断 · 覆盖不足，不能判断原因", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "导出 Markdown" }),
  ).toBeDisabled();
});
