import { test, expect } from "@playwright/test";

test("archived statistics remain readable after raw sample pruning", async ({
  page,
  request,
}) => {
  const base = process.env.ARCHIVE_TEST_BASE_URL;
  test.skip(!base, "requires the disposable Mock archive database server");
  const health = await (await request.get(`${base}/api/health`)).json();
  expect(health.mode).toBe("mock");
  const response = await request.get(
    `${base}/api/v1/history/statistics?cluster=yz&window=1w&grain=60`,
  );
  expect(response.status()).toBe(200);
  const report = await response.json();
  expect(report.buckets.some((b: { archived: boolean }) => b.archived)).toBe(
    true,
  );
  expect(report.summary.allocated.valid_count).toBeGreaterThan(0);
  await page.goto(`${base}/#history`);
  await page.getByRole("combobox", { name: "历史时间范围" }).selectOption("1w");
  const panel = page.getByRole("region", { name: "时段统计" });
  await expect(
    panel.getByRole("cell", {
      name: String(report.summary.allocated.valid_count),
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    panel.getByRole("cell", { name: "亦庄", exact: true }),
  ).toBeVisible();
});
