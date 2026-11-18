import { test, expect, type Page } from "@playwright/test";

const origin = "http://roc-http.invalid";
const id = "00000000-0000-4000-8000-000000000001";
const uuidV4 =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

async function serveHttp(page: Page, baseURL: string) {
  const requests: string[] = [];
  const feedback: string[] = [];
  const task = {
    id,
    kind: "allocation_change",
    status: "limited",
    created_at: 1700000000,
    clusters: ["yz"],
  };
  await page.route(`${origin}/**`, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path === "/api/v1/analytics/events") {
      const body = request.postDataJSON();
      expect(body.visitor_id).toMatch(uuidV4);
      for (const event of body.events) expect(event.id).toMatch(uuidV4);
      return route.fulfill({
        json: { enabled: true, accepted: body.events.length },
      });
    }
    if (path === "/api/v1/insights/capabilities")
      return route.fulfill({ json: { available: true, model_ready: true } });
    if (path === "/api/v1/efficiency/capabilities")
      return route.fulfill({ json: { available: true, supports_agent: true } });
    if (path === "/api/v1/investigations")
      return route.fulfill({ json: { tasks: [task] } });
    if (path === `/api/v1/investigations/${id}`)
      return route.fulfill({
        json: {
          ...task,
          request: { cluster: "yz", window: "1h" },
          goals: [],
          findings: [],
          evidence: [],
          events: [],
          feedback: [],
          feedback_enabled: true,
        },
      });
    if (path === `/api/v1/investigations/${id}/feedback`) {
      feedback.push(request.postDataJSON().request_id);
      return route.fulfill({
        status: feedback.length === 1 ? 503 : 200,
        json: {},
      });
    }
    if (
      path === "/api/v1/insights/diagnose" ||
      path === "/api/v1/efficiency/diagnose"
    ) {
      const key = request.headers()["idempotency-key"];
      expect(key).toMatch(uuidV4);
      requests.push(key);
      const result = path.includes("insights")
        ? {
            id: "mock-http-diagnosis",
            mode: "deepseek",
            status: "complete",
            findings: [],
            evidence: [],
            entities: [],
            warnings: [],
            tool_calls: 1,
            model_calls: 1,
            elapsed_ms: 1,
            windows: {},
            units: {
              allocation: "MB",
              capacity: "MB",
              memory: "MB",
              unit_verified: true,
            },
          }
        : {
            mode: "mock",
            database_read_at: 1700000000,
            start: 1699996400,
            end: 1700000000,
            result_count: 0,
            truncated: false,
            limitations: [],
            clusters: [],
            results: [],
            agent: { mode: "deepseek", warnings: [], investigated_devices: 0 },
          };
      return route.fulfill({
        contentType: "application/x-ndjson",
        body: JSON.stringify({ type: "result", data: result }) + "\n",
      });
    }
    // Fetch only application assets and read-only Mock data from the isolated backend.
    expect(request.method()).toBe("GET");
    const response = await route.fetch({
      url: `${baseURL}${url.pathname}${url.search}`,
      headers: { ...request.headers(), host: new URL(baseURL).host },
    });
    return route.fulfill({ response });
  });
  return { requests, feedback };
}

test("plain HTTP opens records and submits both agent requests with valid secure UUIDs", async ({
  page,
  baseURL,
  request,
}) => {
  expect((await (await request.get("/api/health")).json()).mode).toBe("mock");
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const state = await serveHttp(page, baseURL!);
  await page.goto(`${origin}/#analysis?view=records`);
  expect(
    await page.evaluate(() => ({
      secure: isSecureContext,
      uuid: typeof crypto.randomUUID,
      random: typeof crypto.getRandomValues,
    })),
  ).toEqual({ secure: false, uuid: "undefined", random: "function" });
  await page.getByRole("link", { name: "查看证据", exact: true }).click();
  await expect(page.getByRole("heading", { name: "人工反馈" })).toBeVisible();
  await page.getByRole("button", { name: "保存反馈", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("反馈未保存");
  await page.getByRole("button", { name: "保存反馈", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("反馈已保存");
  expect(state.feedback).toHaveLength(2);
  expect(state.feedback[0]).toMatch(uuidV4);
  expect(state.feedback[0]).toBe(state.feedback[1]);
  await page.goto(`${origin}/#analysis?view=diagnosis`);
  await page.getByRole("button", { name: "开始诊断", exact: true }).click();
  await page.getByText("查询信息", { exact: true }).click();
  await expect(page.getByText(/DeepSeek 工具编排/)).toBeVisible();
  await page.goto(`${origin}/#analysis?view=efficiency`);
  await page.getByRole("button", { name: "开始核查", exact: true }).click();
  await page.getByText("查询信息", { exact: true }).click();
  await expect(page.getByText(/DeepSeek 已重点核查/)).toBeVisible();
  expect(state.requests).toHaveLength(2);
  expect(new Set([...state.requests, state.feedback[0]]).size).toBe(3);
  expect(errors).toEqual([]);
});

test("records remain readable without any crypto API and save reports a controlled error", async ({
  page,
  baseURL,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const state = await serveHttp(page, baseURL!);
  await page.addInitScript(() =>
    Object.defineProperty(window, "crypto", {
      value: undefined,
      configurable: true,
    }),
  );
  await page.goto(`${origin}/#analysis?view=records`);
  await page.getByRole("link", { name: "查看证据", exact: true }).click();
  await expect(page.getByRole("heading", { name: "人工反馈" })).toBeVisible();
  await page.getByRole("button", { name: "保存反馈", exact: true }).click();
  await expect(page.getByRole("status")).toContainText(
    "浏览器不支持安全请求标识",
  );
  expect(state.feedback).toEqual([]);
  expect(errors).toEqual([]);
});
